"""TDD spec —— 复现队列的按仓串行与中断恢复。

这个 spec 要证明什么
------------------
复现协议在**同一个仓库**上把失败命令跑两遍，它的前提是「同一时刻这个仓库
只有一条复现在跑」：两条复现同时跑在一个仓上，各自看到的干净 main 可能已被
对方搅过，判出来的「框架缺陷」是自己造出来的。为此队列要满足两条约束：

1. **按仓串行。** 一个仓库同一时刻最多一条复现在跑；同仓的第二个请求停在
   排队状态，**不进入 running**。不同仓库互不排队 —— 名额是按仓库分的。
2. **重启可恢复。** 进程被杀掉时，跑了一半的请求会在库里留下一行
   ``running``；重启之后这一行必须被放回排队，否则那个仓库的名额永远占着，
   它后面的复现再也排不上队。

TDD contract —— 4 cases
-----------------------
1. ``test_same_repo_serial`` —— 同仓两个请求：任一时刻 running 行数 ≤ 1，
   第二个停在排队状态；两个最终都跑完。
2. ``test_different_repo_parallel`` —— 不同仓的两个请求同时拿到名额，两行
   running 同时存在。
3. ``test_recover_running`` —— 残留的 running 行被放回排队，并留下事件。
4. ``test_unique_index_conflict_degrades_to_pending`` —— 部分唯一索引冲突
   退化成排队，不抛错。

词汇表：queued 与 pending 是同一个状态
-------------------------------------
上游架构（决策点 3）给复现队列定的状态词是 ``queued | running | finished``，
测试设计里也是「协调器启动后将其重排为 ``queued``」。任务简报里说的
「pending」指的就是这个**还没拿到名额、还在排队**的状态 —— 本模块里它是
``STATUS_QUEUED``，``STATUS_PENDING`` 是它的别名。断言落库时用的是上游词表
里的 ``queued``。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from framework_fix.repro import (
    STATUS_PENDING,
    STATUS_QUEUED,
    STATUS_RUNNING,
    ReproCoordinator,
    ReproRequest,
    default_runner,
)

pytestmark = pytest.mark.integration


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


def _rows(db_path: Path, sql: str, parameters: Tuple[Any, ...] = ()) -> List[Any]:
    """读状态库。``sqlite3.Row`` 让断言能按列名写。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, parameters).fetchall()
    finally:
        conn.close()


def _statuses(db_path: Path) -> Dict[str, str]:
    """复现队列里每一行的状态。"""
    rows = _rows(
        db_path,
        "SELECT repro_id, status FROM framework_fix_repro ORDER BY repro_id",
    )
    return {row["repro_id"]: row["status"] for row in rows}


def _events(db_path: Path) -> List[sqlite3.Row]:
    """审计链上的事件。"""
    return _rows(
        db_path,
        "SELECT event_type, payload_json, fingerprint, fix_plan_id "
        "FROM framework_fix_event ORDER BY created_at, event_id",
    )


def _insert_running_row(
    db_path: Path,
    repro_id: str,
    repo: Path,
    request: ReproRequest,
) -> None:
    """直接写一行 ``running`` —— 模拟「上一个进程跑了一半就被杀掉」。

    这一行**不是**本协调器写的：它是另一个进程留下的。正因为它来自别的
    进程，重启恢复才必须把它认出来。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO framework_fix_repro ("
            "  repro_id, repo, plan_id, failure_ref, base_ref, status,"
            "  request_json, result_json, queued_at, started_at, finished_at"
            ") VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, NULL)",
            (
                repro_id,
                str(repo),
                request.plan_id,
                request.base_ref,
                STATUS_RUNNING,
                json.dumps(request.to_dict(), sort_keys=True),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:05+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _clone_of(origin: Path, destination: Path) -> Path:
    """再克隆出一个**工作区** —— 用来造「第二个仓库」。

    复现协议在**失败所属的仓库**里解析基准（``origin/main``）并用
    ``git worktree add --detach`` 建复现树，所以那个仓库必须是一个有
    ``origin`` 远端跟踪分支的工作区；裸仓没有 ``origin/main``，在它上面
    基准读不出来。
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
            str(origin),
            str(destination),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git clone failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return destination


class _QueueProbe:
    """在命令执行期间观察复现队列。

    「同仓串行」是一条关于**时间**的断言：不是「最后状态对」，而是「任何
    时刻都只有一个 running」。所以观察点必须落在命令执行期间，而不是执行
    之后。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.max_running = 0
        self.snapshots: List[Dict[str, str]] = []

    def _statuses(self) -> Dict[str, str]:
        return _statuses(self.db_path)

    def runner(self):
        """一个会记录队列状态的 runner。"""
        base = default_runner()

        def _run(command, cwd, timeout_s):
            snapshot = self._statuses()
            self.snapshots.append(snapshot)
            running = sum(
                1 for status in snapshot.values() if status == STATUS_RUNNING
            )
            self.max_running = max(self.max_running, running)
            return base(command, cwd, timeout_s)

        return _run


# ---------------------------------------------------------------------------
# 1. 同仓串行
# ---------------------------------------------------------------------------
def test_same_repo_serial(state_db, fixture_repo, failure_fixtures, tmp_path):
    """同仓两个请求：任一时刻 running 行数 ≤ 1，第二个停在排队状态。

    三件事一起断言，因为它们是同一句话的三个面：**名额是按仓库分的，而且
    名额只有一个**。少断言任何一条，这条 spec 都可能在一个只做对了三分之一的
    实现上变绿。
    """
    probe = _QueueProbe(state_db)
    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=probe.runner(),
    )
    command = failure_fixtures["deterministic"]["command"]

    coordinator.submit(_request("r1", fixture_repo["clone"], command))
    coordinator.submit(_request("r2", fixture_repo["clone"], command))

    # (a) 第一个占住了这个仓库的名额。
    statuses = _statuses(state_db)
    assert statuses["r1"] == STATUS_RUNNING

    # (b) 同仓的第二个请求**不进入 running** —— 它在排队。
    assert statuses["r2"] == STATUS_QUEUED
    assert statuses["r2"] == STATUS_PENDING
    assert sum(1 for s in statuses.values() if s == STATUS_RUNNING) == 1

    # (c) 跑完之后两个都跑完了 —— 排队不是丢弃。
    assert coordinator.run_pending() == 2
    assert set(_statuses(state_db).values()) == {"done"}

    # (d) 执行期间任一时刻 running 行数都 ≤ 1。
    assert probe.snapshots, "the runner never ran"
    assert probe.max_running == 1


# ---------------------------------------------------------------------------
# 2. 不同仓可并行
# ---------------------------------------------------------------------------
def test_different_repo_parallel(state_db, fixture_repo, failure_fixtures, tmp_path):
    """不同仓的两个请求同时拿到名额 —— 两行 running 同时存在。

    这是「按仓串行」的反面：串行是**按仓库**分的，不是全局排队的。两个仓库
    各自的名额互相不干涉，所以它们可以同时在飞。
    """
    repo_b = _clone_of(fixture_repo["origin"], tmp_path / "clone-b")
    probe = _QueueProbe(state_db)
    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=probe.runner(),
    )
    command = failure_fixtures["deterministic"]["command"]

    coordinator.submit(_request("r1", fixture_repo["clone"], command))
    coordinator.submit(_request("r2", repo_b, command))

    statuses = _statuses(state_db)
    assert statuses["r1"] == STATUS_RUNNING
    assert statuses["r2"] == STATUS_RUNNING

    assert coordinator.run_pending() == 2
    assert set(_statuses(state_db).values()) == {"done"}

    # 跑第一个的时候，第二个也在飞 —— 跨仓没有排队。
    assert probe.snapshots, "the runner never ran"
    first_snapshot = probe.snapshots[0]
    assert first_snapshot["r1"] == STATUS_RUNNING
    assert first_snapshot["r2"] == STATUS_RUNNING
    assert probe.max_running == 2


# ---------------------------------------------------------------------------
# 3. 重启恢复
# ---------------------------------------------------------------------------
def test_recover_running(state_db, fixture_repo, failure_fixtures, tmp_path):
    """残留的 running 行被放回排队，并留下事件。

    两行残留分别来自两个地方，缺一不可：

    * 库里的一行 —— 上一个进程跑了一半就被杀掉留下的（别的进程写的）；
    * 本进程占住的名额 —— 提交即占坑，这一行同样是 ``running``。

    恢复之前，那个仓库的名额被残留行占着，所以同仓的新请求只能排队；恢复
    之后名额空出来，它们才排得上。这正是「恢复」要解决的东西。
    """
    command = failure_fixtures["deterministic"]["command"]
    repo_b = _clone_of(fixture_repo["origin"], tmp_path / "clone-b")

    # 上一个进程留下的半截请求。
    stale = _request("stale", fixture_repo["clone"], command, plan_id="plan-old")
    _insert_running_row(state_db, "stale", fixture_repo["clone"], stale)

    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=default_runner(),
    )
    # 同仓的新请求：名额被残留行占着，只能排队。
    coordinator.submit(_request("r1", fixture_repo["clone"], command))
    # 另一个仓：名额是按仓库分的，它拿得到自己的名额。
    coordinator.submit(_request("r2", repo_b, command))

    statuses = _statuses(state_db)
    assert statuses["stale"] == STATUS_RUNNING
    assert statuses["r1"] == STATUS_QUEUED
    assert statuses["r2"] == STATUS_RUNNING

    recovered = coordinator.recover_interrupted()

    assert recovered == 2
    statuses = _statuses(state_db)
    assert statuses["stale"] == STATUS_QUEUED
    assert statuses["r1"] == STATUS_QUEUED
    assert statuses["r2"] == STATUS_QUEUED
    assert not any(status == STATUS_RUNNING for status in statuses.values())

    # 恢复要留痕：每一条恢复都写进审计链。
    events = _events(state_db)
    recovery_events = [
        event for event in events if event["event_type"] == "repro_recovered"
    ]
    assert len(recovery_events) == 2
    payloads = {
        json.loads(event["payload_json"])["repro_id"] for event in recovery_events
    }
    assert payloads == {"stale", "r2"}

    # 放回队列的请求还能再被跑起来 —— 恢复不是丢弃。
    assert coordinator.run_pending() == 3
    assert set(_statuses(state_db).values()) == {"done"}


# ---------------------------------------------------------------------------
# 4. 部分唯一索引冲突退化成排队
# ---------------------------------------------------------------------------
def test_unique_index_conflict_degrades_to_pending(
    state_db, fixture_repo, failure_fixtures, tmp_path, monkeypatch
):
    """名额被别人抢走时退化成排队，不抛错。

    数据库里的部分唯一索引 ``idx_framework_fix_repro_running`` 是最后一道
    防线：两个进程同时抢同一个仓库的名额时，只有一个能改成 ``running``，
    另一个撞上唯一索引冲突。撞上冲突的请求必须**退化成排队**（继续等这个
    仓库的名额），而不是把错误抛给调用方 —— 复现队列的一次抢锁失败不是
    事故。
    """
    command = failure_fixtures["deterministic"]["command"]

    # 别的进程占着这个仓库的名额。
    holder = _request("holder", fixture_repo["clone"], command)
    _insert_running_row(state_db, "holder", fixture_repo["clone"], holder)

    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=default_runner(),
    )

    # (a) 协调器自己的预检查认出了占用 —— 排队，不抛错。
    coordinator.submit(_request("r1", fixture_repo["clone"], command))
    assert _statuses(state_db)["r1"] == STATUS_QUEUED

    # (b) 预检查被绕过（两个进程同时抢）—— 唯一索引兜底，依然不抛错。
    monkeypatch.setattr(
        ReproCoordinator, "_db_repo_is_running", lambda self, repo: False
    )
    coordinator.submit(_request("r2", fixture_repo["clone"], command))
    statuses = _statuses(state_db)
    assert statuses["r2"] == STATUS_QUEUED
    assert statuses["r2"] == STATUS_PENDING
