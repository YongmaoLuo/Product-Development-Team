"""性能门禁 —— 复现队列的吞吐与按仓隔离。

这个 spec 要证明什么
------------------
复现队列有两个互相拉扯的性质：

1. **同仓严格串行。** 同一个仓库同一时刻只允许一条复现在跑 —— 两条复现同时
   跑在一个仓上，各自看到的「干净 main」可能已经被对方搅过，判出来的「框架
   缺陷」是自己造出来的。
2. **异仓互不排队。** 名额是**按仓库**分的：一个仓的名额被占住，不该把别的
   仓也堵上。否则一个仓的长跑会把整条队列饿死。

这两条都是关于**时间**的断言，不是关于最终状态的断言：最后每条请求都是
``done``，这句话对任何实现都成立；有意义的是「**任何时刻**」的形状。所以观
察点必须落在命令执行**期间**，而不是执行之后。

观察点在哪里 —— 队列表
--------------------
协调器的并行度是**准入层**的并行度：``submit`` 登记请求时就替它占住这个仓
的名额（库里那一行是 ``running``），执行由一个驱动循环按仓推进。所以「同时
在飞」这件事在**队列表**上是看得见的：一个仓一行 ``running``，五个仓就是五
行 ``running``。

这一层正是按仓隔离真正被强制的地方 —— 部分唯一索引
``idx_framework_fix_repro_running`` 让数据库成为最后一道防线（数据库自己就
不允许同一个仓同时有两行 ``running``），而「异仓并行」就是「每个仓各有一行
``running``」。任务 16 的集成 spec 用的是同一个观察点（``_QueueProbe``），
两个文件量的是同一件事：一个说「同仓串行、异仓可并行」，一个说「异仓并行度
等于仓数、吞吐不因单仓长跑而饿死」。

TDD contract —— 4 cases
-----------------------
1. ``test_throughput`` —— 5 仓 × 4 条 = 20 条请求全部跑完，并给出
   ``{'completed': 20, 'max_parallel': 5, 'serial_per_repo': True}``。
2. ``test_parallel_isolation`` —— 异仓并行（同时在飞的仓数 = 仓数）、同仓
   串行（任一时刻同一个仓最多一行 ``running``）。
3. ``test_no_residue`` —— 队列排空后没有残留的 ``running``，临时 worktree 也
   没留下，再排一次排不出东西来。
4. ``test_single_request_double_run_within_double_timeout`` —— 一条请求跑两
   遍，两遍加起来不超过「单次时限 × 2」。

两个标记里只带一个
------------------
本文件跑的是**真**队列：真的 ``git clone`` 真的裸仓、真的子进程、真的
SQLite。上游测试设计（决策点 9）对这种情况的要求是「若用例起真实子进程
（如 clone 真实仓库）而非内存队列替身，叠加 ``needs_real_process``」—— 所以
``needs_real_process`` 是模块级的，一个用例都不少。

``perf`` **不**带，理由是仓库自己的规矩而不是偏好：
``backend/tests/conftest.py`` 里那个共享的负载闸门对带 ``perf`` /
``time_sensitive`` 的用例，在机器忙的时候**跳过**它们（默认阈值：负载 20）。
闸门自己的注释写明了它挑的是哪一类用例 —— 「不带这两个标记的用例不是在量
时钟，机器忙不关它的事，跳过它只会把一个忙的下午变成一份空报告」。

本文件量的不是机器的速度：**队列的形状**（同时在飞几行、每个仓的峰值、跑完
之后剩什么）与**时限有没有被执行**（一条请求跑两遍，两遍加起来不超过单次
时限 × 2）。被测命令是 ``time.sleep(30)`` —— 它不吃 CPU，时限由操作系统定
时器掐住，父进程只是等着管道；机器忙时被推迟的是「父进程醒过来检查超时」
这一刻，推迟量是毫秒级，不是 sleep 的长度。所以这些用例在忙的机器上照样
成立 —— 给它们标上 ``perf`` 等于用「机器忙」换掉一份本来拿得到的证据。

CI 里的位置：``needs_real_process`` 把本文件挡在并行分片之外（那 8 个分片
每个用例只有 60 秒，而本文件一个用例要起 60 个真子进程），它落在每日
nightly lane（标记表达式 ``not e2e and not time_sensitive and not perf``，
收整个 ``tests/``）与本地门禁的第一阶段。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pytest

from framework_fix.repro import (
    FRAMEWORK_DETERMINISTIC,
    FRAMEWORK_TIMEOUT,
    REQUIRED_ATTEMPTS,
    STATUS_DONE,
    STATUS_RUNNING,
    ReproCoordinator,
    ReproRequest,
    default_runner,
)

# ---------------------------------------------------------------------------
# 标记
# ---------------------------------------------------------------------------
#
# ``needs_real_process`` 登记在仓库根的 ``pytest.ini`` 里，没有登记在
# ``backend/pytest.ini`` 里 —— 后者的标记清单被
# ``backend/tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py``
# 冻结，不能长。因此本模块在**用它之前**把它登记到活的 pytest 配置上，这
# 是 ``backend/tests/unit/test_orphan_reaper_procs.py`` 为
# ``observational`` / ``needs_real_process`` 建立的写法。
_NEEDS_REAL_PROCESS_DESCRIPTION = (
    "needs_real_process: cases that drive a real server / real subprocess / "
    "real repository rather than a fixture double, so they need a quiet "
    "machine and a port to themselves."
)


def _register_needs_real_process_marker() -> None:
    """把 ``needs_real_process`` 登记到活的 pytest 配置上。"""
    config = getattr(pytest.mark, "_config", None)
    if config is None:  # pragma: no cover - 收集阶段一定有配置
        return
    known = {
        line.split(":")[0].split("(")[0].strip()
        for line in config.getini("markers")
    }
    if "needs_real_process" not in known:
        config.addinivalue_line("markers", _NEEDS_REAL_PROCESS_DESCRIPTION)


_register_needs_real_process_marker()

#: 模块级标记：真仓库、真子进程（上游测试设计决策点 9 的要求）。
#:
#: ``perf`` **不**在这里 —— 理由见模块文档的「两个标记里只带一个」。
pytestmark = [pytest.mark.needs_real_process]


# ---------------------------------------------------------------------------
# 规模
# ---------------------------------------------------------------------------
#: 仓库数。异仓并行度就等于它 —— 名额是按仓库分的。
REPO_COUNT = 5

#: 每个仓库排几条请求。
PER_REPO = 4

#: 请求总数。
TOTAL = REPO_COUNT * PER_REPO

#: 单次时限之外允许的余量（秒）：掐掉进程组 + 收干管道 + 调度抖动。
#:
#: 2 秒对着 1 秒的时限，宽到机器再忙也翻不了；而「时限没被执行」的那种
#: 失败是 30 秒 —— 差 15 倍，余量再宽也盖不住。
ATTEMPT_SLACK_S = 2.0

#: 克隆裸仓的余量（秒）。克隆与「跑两遍」是两笔账：协议给 git 自己的时限
#: 是另一笔预算（``_GIT_TIMEOUT_SECONDS``）。这里的余量只是为了让「一条
#: 请求整体有界」这句话有个上界，而不是把克隆算进「2 × 单次时限」。
GIT_ALLOWANCE_S = 10.0


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _request(
    request_id: str,
    repo: Path,
    command: Sequence[str],
    **overrides: Any,
) -> ReproRequest:
    """一条最小可用的复现请求。"""
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "repo": str(repo),
        "command": list(command),
        "framework_files": ["src/module.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: deterministic fixture failure",
    }
    payload.update(overrides)
    return ReproRequest(**payload)


def _rows(db_path: Path, sql: str) -> List[sqlite3.Row]:
    """读状态库。``sqlite3.Row`` 让断言能按列名写。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _queue_rows(db_path: Path) -> List[sqlite3.Row]:
    """复现队列里的每一行。"""
    return _rows(
        db_path,
        "SELECT repro_id, repo, status FROM framework_fix_repro "
        "ORDER BY repro_id",
    )


def _statuses(db_path: Path) -> Dict[str, str]:
    """复现队列里每一行的状态。"""
    return {row["repro_id"]: row["status"] for row in _queue_rows(db_path)}


def _bare_clone(origin: Path, destination: Path) -> Path:
    """从一个裸仓再克隆出一个裸仓 —— 用来造第二个、第三个……仓库。

    克隆出来的仓有同样的 ``main`` 分支，所以它同样能被复现协议克隆成临时
    worktree。
    """
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "clone",
            "--quiet",
            "--bare",
            str(origin),
            str(destination),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git clone --bare failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return destination


def _repos(fixture_repo: Dict[str, Any], tmp_path: Path, count: int) -> List[Path]:
    """``count`` 个裸仓。第一个是夹具给的，其余是它的克隆。"""
    repos: List[Path] = [fixture_repo["origin"]]
    for index in range(1, count):
        repos.append(
            _bare_clone(fixture_repo["origin"], tmp_path / f"origin-{index}")
        )
    return repos


class _QueueProbe:
    """在命令执行期间观察复现队列。

    「同仓串行」「异仓并行」都是关于**时间**的断言：不是「最后状态对」，而是
    「任何时刻的形状对」。所以观察点必须落在命令执行期间，而不是执行之后。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        #: 任一时刻同时 ``running`` 的行数上限。
        self.max_running = 0
        #: 任一时刻同时 ``running`` 的**不同仓库**数上限。
        self.max_running_repos = 0
        #: 每个仓库同时 ``running`` 的行数上限。
        self.peak_running_per_repo: Dict[str, int] = {}
        #: 第一次观察时在飞的仓库 —— 异仓不排队的直接证据。
        self.first_running_repos: Optional[Set[str]] = None
        #: runner 一共被叫了几次。
        self.attempt_count = 0
        #: 每次 attempt 的耗时。
        self.attempt_durations: List[float] = []
        #: 每个临时 worktree（= 一条请求的一次执行）跑了几遍。
        self.attempts_by_worktree: Dict[str, int] = {}
        #: 每个临时 worktree 累计耗时。
        self.duration_by_worktree: Dict[str, float] = {}

    def _observe(self) -> None:
        """读一眼队列，记下此刻的形状。"""
        running = [
            (row["repro_id"], row["repo"])
            for row in _queue_rows(self.db_path)
            if row["status"] == STATUS_RUNNING
        ]
        self.max_running = max(self.max_running, len(running))
        per_repo: Dict[str, int] = {}
        for _repro_id, repo in running:
            per_repo[repo] = per_repo.get(repo, 0) + 1
        self.max_running_repos = max(self.max_running_repos, len(per_repo))
        for repo, count in per_repo.items():
            self.peak_running_per_repo[repo] = max(
                self.peak_running_per_repo.get(repo, 0), count
            )
        if self.first_running_repos is None:
            self.first_running_repos = {repo for _repro_id, repo in running}

    def runner(self):
        """一个会记录队列形状的 runner。"""
        base = default_runner()

        def _run(command, cwd, timeout_s):
            self._observe()
            started = time.monotonic()
            try:
                return base(command, cwd, timeout_s)
            finally:
                elapsed = time.monotonic() - started
                key = str(cwd)
                self.attempt_count += 1
                self.attempt_durations.append(elapsed)
                self.attempts_by_worktree[key] = (
                    self.attempts_by_worktree.get(key, 0) + 1
                )
                self.duration_by_worktree[key] = (
                    self.duration_by_worktree.get(key, 0.0) + elapsed
                )

        return _run


def _report(
    probe: _QueueProbe,
    completed: int,
    repos: Sequence[Path],
) -> Dict[str, Any]:
    """本文件对外报告的那份形状。"""
    expected_repos = {str(repo) for repo in repos}
    serial_per_repo = (
        set(probe.peak_running_per_repo) == expected_repos
        and all(count == 1 for count in probe.peak_running_per_repo.values())
    )
    return {
        "completed": completed,
        "max_parallel": probe.max_running,
        "serial_per_repo": serial_per_repo,
    }


def _submit_all(
    coordinator: ReproCoordinator,
    repos: Sequence[Path],
    command: Sequence[str],
) -> List[str]:
    """每个仓排 ``PER_REPO`` 条请求，返回请求号。"""
    submitted: List[str] = []
    for repo_index, repo in enumerate(repos):
        for slot in range(PER_REPO):
            request_id = f"r{repo_index}-{slot}"
            coordinator.submit(_request(request_id, repo, command))
            submitted.append(request_id)
    return submitted


# ---------------------------------------------------------------------------
# 1. 吞吐
# ---------------------------------------------------------------------------
def test_throughput(state_db, fixture_repo, failure_fixtures, tmp_path):
    """5 仓 × 4 条 = 20 条请求全部跑完。

    断言四件事，因为它们是同一句话的四个面：

    * **20 条全部完成** —— 排队不是丢弃。
    * **每条请求跑了两遍** —— 协议要的是 2/2 的观察，一次撑不起判据。
    * **每条请求都有结论** —— 跑完了却没结论，等于没跑完。
    * **队列的形状** —— ``max_parallel`` 等于仓数（异仓不排队），
      ``serial_per_repo`` 为真（同仓不并行）。这两条由
      ``test_parallel_isolation`` 逐条展开，这里只钉住汇总值。
    """
    repos = _repos(fixture_repo, tmp_path, REPO_COUNT)
    probe = _QueueProbe(state_db)
    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=probe.runner(),
    )
    command = failure_fixtures["deterministic"]["command"]

    submitted = _submit_all(coordinator, repos, command)
    assert len(submitted) == TOTAL

    completed = coordinator.run_pending()

    # (a) 20 条全部完成。
    assert completed == TOTAL
    statuses = _statuses(state_db)
    assert set(statuses) == set(submitted)
    assert set(statuses.values()) == {STATUS_DONE}

    # (b) 每条请求跑了两遍：20 个临时 worktree，每个两遍。
    assert probe.attempt_count == TOTAL * REQUIRED_ATTEMPTS
    assert len(probe.attempts_by_worktree) == TOTAL
    assert set(probe.attempts_by_worktree.values()) == {REQUIRED_ATTEMPTS}

    # (c) 每条请求都有结论。
    for request_id in submitted:
        result = coordinator.get(request_id)
        assert result is not None, f"{request_id} finished without a result"
        assert result["classification"] == FRAMEWORK_DETERMINISTIC
        assert result["attempts"] == REQUIRED_ATTEMPTS

    # (d) 队列的形状。
    report = _report(probe, completed, repos)
    assert report == {
        "completed": 20,
        "max_parallel": 5,
        "serial_per_repo": True,
    }


# ---------------------------------------------------------------------------
# 2. 异仓并行 / 同仓串行
# ---------------------------------------------------------------------------
def test_parallel_isolation(state_db, fixture_repo, failure_fixtures, tmp_path):
    """异仓并行、同仓串行。

    这两条是一枚硬币的两面，少断言任何一条，这个 spec 都可能在一个只做对了
    一半的实现上变绿：

    * **异仓并行** —— 五个仓同时各有一行 ``running``。特别是**第一次**观察
      就是五仓齐飞：一个仓的名额被占住时，别的仓照样拿到自己的名额 —— 这才
      是「不因单仓长跑而饿死其它仓」。如果实现退化成全局单名额队列，第一
      次观察就只有一个仓。
    * **同仓串行** —— 任一时刻同一个仓最多一行 ``running``。同一个仓的两条
      复现同时跑，各自看到的「干净 main」可能已被对方搅过。
    """
    repos = _repos(fixture_repo, tmp_path, REPO_COUNT)
    probe = _QueueProbe(state_db)
    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=probe.runner(),
    )
    command = failure_fixtures["deterministic"]["command"]

    _submit_all(coordinator, repos, command)
    assert coordinator.run_pending() == TOTAL

    expected_repos = {str(repo) for repo in repos}

    # (a) 异仓并行：同时在飞的不同仓库数 = 仓数。
    assert probe.max_running_repos == REPO_COUNT
    assert probe.max_running == REPO_COUNT

    # (b) 第一次观察就是五仓齐飞 —— 别的仓没有排在第一个仓后面。
    assert probe.first_running_repos == expected_repos

    # (c) 同仓串行：每个仓的峰值都是 1。
    assert set(probe.peak_running_per_repo) == expected_repos
    assert set(probe.peak_running_per_repo.values()) == {1}


# ---------------------------------------------------------------------------
# 3. 队列排空后没有残留
# ---------------------------------------------------------------------------
def test_no_residue(state_db, fixture_repo, failure_fixtures, tmp_path):
    """排空之后没有残留的 ``running``，也没有残留的临时 worktree。

    残留的 ``running`` 行是这个队列最贵的一种垃圾：它占着那个仓的名额，
    而名额是**按仓库**分的 —— 别的仓照跑不误，但这个仓后面的复现永远排不上
    队。重启恢复（任务 16 的 ``recover_interrupted``）是兜底，正常路径下不
    该留下这种行。

    四条一起断言：库里没有 ``running``、全部是 ``done``、临时 worktree 目录
    是空的、再排一次排不出东西来。最后一条最直接：它证明队列真的空了，而
    不是「看起来空了」。
    """
    repos = _repos(fixture_repo, tmp_path, REPO_COUNT)
    worktree_root = tmp_path / "worktrees"
    probe = _QueueProbe(state_db)
    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=worktree_root,
        runner=probe.runner(),
    )
    command = failure_fixtures["deterministic"]["command"]

    _submit_all(coordinator, repos, command)
    assert coordinator.run_pending() == TOTAL

    rows = _queue_rows(state_db)

    # (a) 库里没有残留的 running。
    residue = [row["repro_id"] for row in rows if row["status"] == STATUS_RUNNING]
    assert residue == []

    # (b) 全部 done。
    assert len(rows) == TOTAL
    assert {row["status"] for row in rows} == {STATUS_DONE}

    # (c) 临时 worktree 没有留下 —— 每个都在 finally 里删掉了。
    assert list(worktree_root.iterdir()) == []

    # (d) 再排一次排不出东西来：队列真的空了。
    assert coordinator.run_pending() == 0
    assert not [
        row["repro_id"]
        for row in _queue_rows(state_db)
        if row["status"] == STATUS_RUNNING
    ]


# ---------------------------------------------------------------------------
# 4. 一条请求跑两遍，不超过 2 × 单次时限
# ---------------------------------------------------------------------------
def test_single_request_double_run_within_double_timeout(
    state_db, fixture_repo, failure_fixtures, tmp_path
):
    """一条请求跑两遍，两遍加起来不超过「单次时限 × 2」。

    这条断言的是**时限被执行了**，而且是**每次执行各有时限**：协议跑两遍，
    所以最坏情况是 2 × 单次时限，而不是「一直等到命令自己结束」。

    被测命令睡 30 秒而时限是 1 秒。守住了上限就是 2 秒量级；守不住就是 60
    秒量级 —— 差 30 倍，任何余量都盖不住。

    三个面一起断言：

    * **跑了两遍** —— 一遍是证据不足（2/2 的判据要两次观察）。
    * **两遍加起来 ≤ 2 × 单次时限** —— 时限是每次执行的。
    * **两遍加起来 ≥ 单次时限** —— 时限真的用掉了：命令确实跑到了 deadline
      才被掐掉，而不是压根没跑（那也「没超时」）。
    """
    probe = _QueueProbe(state_db)
    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=probe.runner(),
    )
    fixture = failure_fixtures["timeout"]
    deadline = float(fixture["timeout_s"])

    coordinator.submit(
        _request(
            "slow-1",
            fixture_repo["origin"],
            fixture["command"],
            timeout_s=deadline,
        )
    )

    started = time.monotonic()
    assert coordinator.run_pending() == 1
    elapsed = time.monotonic() - started

    # (a) 跑了两遍。
    assert probe.attempt_count == REQUIRED_ATTEMPTS
    assert list(probe.attempts_by_worktree.values()) == [REQUIRED_ATTEMPTS]

    # (b) 两遍加起来不超过「单次时限 × 2」（余量是掐进程组与调度抖动）。
    runs_total = sum(probe.attempt_durations)
    assert runs_total <= REQUIRED_ATTEMPTS * (deadline + ATTEMPT_SLACK_S), (
        f"the two runs cost {runs_total:.2f}s, above "
        f"{REQUIRED_ATTEMPTS} x ({deadline}s + {ATTEMPT_SLACK_S}s): the "
        f"per-run deadline was not enforced"
    )

    # (c) 时限真的用掉了：不是没跑，是跑到 deadline 才被掐掉。
    assert runs_total >= deadline, (
        f"the two runs cost only {runs_total:.2f}s, below the {deadline}s "
        f"deadline: the command did not actually run to its deadline"
    )

    # 整条请求也有界：两遍之外还有一次克隆，它有自己那笔预算。
    assert elapsed <= (
        REQUIRED_ATTEMPTS * (deadline + ATTEMPT_SLACK_S) + GIT_ALLOWANCE_S
    ), f"the whole request cost {elapsed:.2f}s"

    # 结论是超时：两遍都撞上了时限。
    result = coordinator.get("slow-1")
    assert result is not None
    assert result["classification"] == FRAMEWORK_TIMEOUT
    assert result["attempts"] == REQUIRED_ATTEMPTS
