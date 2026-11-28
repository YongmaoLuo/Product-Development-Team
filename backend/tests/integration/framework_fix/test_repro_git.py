"""TDD spec —— ReproCoordinator：在干净 origin/main 上把失败命令跑两遍并分类。

这个 spec 要证明什么
------------------
PRD 要求「在**干净 main** 上复现失败，才允许把失败判成框架问题」。复现协议
（架构决策点 3）因此有四条硬性要求，每一条都对应一个具体的现场：

1. **命令跑在目标仓库的临时 detached worktree 上，跑的是 origin/main。** 在原
   工作区跑等于在 plan 分支上跑 —— 那不是「干净 main」，判定的前提就没了。复
   制树必须由 ``git worktree add --detach`` **现场建立**，绝不复用 plan 自己的
   worktree。
2. **同一条命令跑两遍，两次 attempt 一起分类。** 一次观察撑不起 2/2 的判据。
3. **跑完必须删掉 worktree。** 留下一个装着失败现场的临时目录，是把磁盘当成
   了垃圾桶；异常路径同样要清理。
4. **基准读不出来时结论是 unclassified，证据留着，请求不丢。** 悄悄换个基准
   跑，等于在别的地方复现；把请求丢在 ``failed`` 里，等于这条失败从此没人再
   看 —— PRD 那条「连续 3 轮无法归类 -> 升级 HITL」也就永远走不到。

TDD contract —— 12 cases
-----------------------
1. ``test_two_runs_classified`` —— 恒定失败命令 -> ``framework_deterministic``，
   两次 attempt 都在。
2. ``test_clean_worktree`` —— 运行期 HEAD 是 origin/main、处于 detached 状态、
   工作目录是临时 worktree（不是原仓库），跑完 worktree 被删掉。
3. ``test_timeout_killed`` —— 超时命令 -> ``framework_timeout``，``timed_out``
   为真，且**子进程树**没有残留。
4. ``test_unknown_request_returns_none`` —— 未知 request -> ``get`` 返回 None。
5. ``test_command_is_argv_not_a_shell_string`` —— 命令以 argv 列表传递，
   ``shell=True`` 不出现，带 shell 元字符的参数不会被 shell 执行。
6. ``test_worktree_removed_when_the_runner_raises`` —— 异常路径同样清理 worktree。
7. ``test_request_survives_a_restart`` —— 请求落库后，**另一个**协调器实例能从
   库里把它读回来（队列是持久的，不只是进程内的）。
8. ``test_worktree_is_built_by_worktree_add`` —— 复现树是**链接 worktree**
   （``.git`` 是文件）而不是克隆（``.git`` 是目录）；它挂在目标仓库上，跑完从
   目标仓库的 worktree 名单里消失。
9. ``test_worktree_add_argv_keeps_paths_behind_the_separator`` —— 复现树的
   git 命令是 ``git worktree add --detach``，它在失败所属的仓库里跑，路径与
   基准 ref 都躲在 ``--`` 后面。
10. ``test_base_ref_is_never_silently_substituted`` —— 基准永远是请求声明的那
    一个：``origin/HEAD`` 不会被悄悄顶上来；``origin/main`` 读不出来时结论是
    ``unclassified``，不是「换个 ref 跑」。
11. ``test_unreadable_base_ref_is_recorded_unclassified`` —— 目标仓库里读不出
   ``origin/main`` -> 结论 ``unclassified``，证据留在结论里，这一行**不是**
   ``failed``。
12. ``test_unclassified_request_is_requeued_then_escalates`` ——
    连续三轮读不出基准 -> 前两轮把请求放回排队，第三轮不再重排队，并往审计链
    追加一条升级事件。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from framework_fix import repro as repro_module
from framework_fix.repro import (
    FRAMEWORK_DETERMINISTIC,
    FRAMEWORK_TIMEOUT,
    UNCLASSIFIED,
    ReproAttempt,
    ReproCoordinator,
    ReproRequest,
    default_runner,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _git_output(cwd: Path, *args: str) -> str:
    """在 ``cwd`` 里跑一条 git 命令，返回它的 stdout（去掉尾部换行）。"""
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _clone_of(origin: Path, destination: Path) -> Path:
    """把裸仓克隆成一个**工作区** —— 它带着 ``origin`` 远端。

    复现协议在**失败所属的仓库**里解析基准（``origin/main``）并建 worktree。
    裸仓没有远端跟踪分支，所以失败所属的仓库必须是一个有 ``origin`` 远端的工作
    区；夹具给的 ``origin``（裸仓）扮演的是远端，``clone`` 才是失败所属的仓。
    """
    completed = subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git clone failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return destination


def _drop_remote_main(clone: Path) -> None:
    """删掉 ``origin/main`` 这个远端跟踪分支 —— 仓库里读不出基准。

    这是「基准读不出来」最常见的样子：远端换了默认分支、或者那个分支已经被
    删了。它必须落成 ``unclassified``，而不是被悄悄换成另一个 ref。
    """
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "update-ref",
            "-d",
            "refs/remotes/origin/main",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _point_origin_head_at_another_commit(clone: Path) -> str:
    """让 ``origin/HEAD`` 指向**另一个**提交，返回那个提交。

    做法：在仓库里另开一个分支、提交一笔改动，推到远端成为 ``master``，再把
    ``origin/HEAD`` 指到 ``origin/master``。这样 ``origin/main``（A）与
    ``origin/HEAD``（B）指向**不同的提交** —— 「到底用了哪个基准」因此是可
    分辨的，而不是靠两个 ref 恰好一样蒙混过去。
    """
    _git_output(clone, "checkout", "-b", "other-branch")
    (clone / "OTHER.md").write_text("other\n", encoding="utf-8")
    _git_output(clone, "add", "-A")
    _git_output(clone, "commit", "-m", "fixture: another commit")
    _git_output(clone, "push", "origin", "other-branch:master")
    _git_output(clone, "fetch", "origin", "master:refs/remotes/origin/master")
    _git_output(
        clone,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/master",
    )
    return _git_output(clone, "rev-parse", "origin/master")


def _row(db_path: Path, repro_id: str) -> sqlite3.Row:
    """队列里的一行。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, result_json FROM framework_fix_repro "
            "WHERE repro_id = ?",
            (repro_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no queue row for {repro_id!r}"
    return row


def _events(db_path: Path) -> List[sqlite3.Row]:
    """审计链上的事件。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT event_type, payload_json, fingerprint, fix_plan_id "
            "FROM framework_fix_event ORDER BY created_at, event_id"
        ).fetchall()
    finally:
        conn.close()


def _process_alive(pid: int) -> bool:
    """这个 pid 还活着吗？``os.kill(pid, 0)`` 是「存在性探测」，不发信号。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 别人的进程
        return True
    return True


def _wait_until_gone(pid: int, timeout_s: float = 20.0) -> bool:
    """等一个 pid 消失。

    轮询而不是睡死一次：被 SIGKILL 的进程要等它的父进程回收，pid 才会从
    进程表里消失。**等不到就是没死** —— 这一条是「超时后没有残留进程」那个
    断言的全部重量所在。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return not _process_alive(pid)


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


#: 一个会留下**孙进程**的脚本：它 spawn 一个睡很久的子进程，把两个 pid 记到
#: 文件里，然后自己睡死。超时时只杀直接子进程的话，孙进程会活下来 —— 所以
#: 这一条是「kill 子进程树」那个断言的载荷。
_SPAWNER_SOURCE = '''\
import os
import subprocess
import sys
import time

pid_file = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(pid_file, "a", encoding="utf-8") as handle:
    handle.write("%d %d\\n" % (os.getpid(), child.pid))
time.sleep(120)
'''


# ---------------------------------------------------------------------------
# 1. 两次跑完 -> framework_deterministic
# ---------------------------------------------------------------------------
def test_two_runs_classified(fixture_repo, failure_fixtures, state_db, tmp_path):
    """恒定失败命令 -> framework_deterministic，两次 attempt 都在。

    这是最该被升级成修复 plan 的一类：干净 main 上稳定复现同一个错误。
    """
    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=default_runner(),
    )
    source = failure_fixtures["deterministic"]
    request = _request(
        "r1",
        fixture_repo["clone"],
        source["command"],
        first_error_line=source["first_error_line"],
    )

    assert coordinator.submit(request) == "r1"
    assert coordinator.run_pending() == 1

    result = coordinator.get("r1")
    assert result == {
        "request_id": "r1",
        "classification": FRAMEWORK_DETERMINISTIC,
        "attempts": 2,
    }


# ---------------------------------------------------------------------------
# 2. worktree 是干净的 origin/main，跑完被删掉
# ---------------------------------------------------------------------------
def test_clean_worktree(fixture_repo, failure_fixtures, tmp_path):
    """运行期 HEAD 是 origin/main；工作目录是临时 worktree；跑完 worktree 消失。

    三件事一起断言，因为它们是同一句话的三个面：**在干净 main 上跑，跑完不留
    东西**。少断言任何一条，这条 spec 都可能在一个只做对了三分之一的实现上
    变绿。
    """
    observed: Dict[str, Any] = {}
    base_runner = default_runner()

    def observing_runner(command, cwd, timeout_s):
        observed["cwd"] = Path(cwd)
        observed["head"] = _git_output(Path(cwd), "rev-parse", "HEAD")
        observed["branch"] = _git_output(
            Path(cwd), "rev-parse", "--abbrev-ref", "HEAD"
        )
        return base_runner(command, cwd, timeout_s)

    worktree_root = tmp_path / "worktrees"
    coordinator = ReproCoordinator(
        worktree_root=worktree_root,
        runner=observing_runner,
    )
    source = failure_fixtures["deterministic"]
    coordinator.submit(
        _request("r1", fixture_repo["clone"], source["command"])
    )
    assert coordinator.run_pending() == 1

    expected_head = _git_output(fixture_repo["clone"], "rev-parse", "origin/main")

    # (a) HEAD 就在 origin/main 上，且是 detached 的。
    assert observed["head"] == expected_head
    assert observed["branch"] == "HEAD"

    # (b) 命令跑在临时 worktree 里，不是在原仓库里。
    assert observed["cwd"] != fixture_repo["clone"]
    assert observed["cwd"] != fixture_repo["origin"]
    assert worktree_root in observed["cwd"].parents

    # (c) 跑完 worktree 被删掉了。
    assert not observed["cwd"].exists()
    assert list(worktree_root.iterdir()) == []

    # 原仓库一个字节都没被动过。
    assert _git_output(fixture_repo["clone"], "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# 3. 超时 -> framework_timeout，且没有残留进程
# ---------------------------------------------------------------------------
def test_timeout_killed(fixture_repo, tmp_path):
    """超时命令 -> framework_timeout；``timed_out`` 为真；子进程树没有残留。"""
    script = tmp_path / "spawner.py"
    script.write_text(_SPAWNER_SOURCE, encoding="utf-8")
    pid_file = tmp_path / "pids.txt"

    seen: List[ReproAttempt] = []
    base_runner = default_runner()

    def recording_runner(command, cwd, timeout_s):
        attempt = base_runner(command, cwd, timeout_s)
        seen.append(attempt)
        return attempt

    coordinator = ReproCoordinator(
        worktree_root=tmp_path / "worktrees",
        runner=recording_runner,
    )
    coordinator.submit(
        _request(
            "r1",
            fixture_repo["clone"],
            [sys.executable, str(script), str(pid_file)],
            error_class="TimeoutError",
            first_error_line="",
            timeout_s=1.0,
        )
    )
    assert coordinator.run_pending() == 1

    result = coordinator.get("r1")
    assert result is not None
    assert result["classification"] == FRAMEWORK_TIMEOUT
    assert result["attempts"] == 2
    assert [attempt.timed_out for attempt in seen] == [True, True]

    # 两次跑各留下一个「直接子进程 + 孙进程」，四个 pid 都得消失。
    recorded = [
        (int(parent), int(child))
        for parent, child in (
            line.split() for line in pid_file.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert len(recorded) == 2
    for parent_pid, child_pid in recorded:
        assert _wait_until_gone(parent_pid), f"the command survived: {parent_pid}"
        assert _wait_until_gone(child_pid), f"a grandchild survived: {child_pid}"


# ---------------------------------------------------------------------------
# 4. 未知 request -> None
# ---------------------------------------------------------------------------
def test_unknown_request_returns_none(state_db):
    """没提交过的 request_id -> ``get`` 返回 None（不是空字典，不是 KeyError）。"""
    coordinator = ReproCoordinator(db_path=state_db)

    assert coordinator.get("never-submitted") is None


# ---------------------------------------------------------------------------
# 5. 命令是 argv 列表，不是 shell 字符串
# ---------------------------------------------------------------------------
def test_command_is_argv_not_a_shell_string(fixture_repo, tmp_path):
    """参数里的 shell 元字符不会被 shell 执行，也不会被 shell 拆散。

    两道断言，各管一头：

    * **运行期** —— 命令把收到的参数原样写回文件。参数里有 ``;``，如果走了
      shell，它要么被当成第二条命令执行（``touch`` 真的跑起来），要么被 shell
      拆成好几个词（参数原样到不了进程）。两条路都被这两句断言堵住。
    * **源码级** —— 模块里不出现 ``shell=True``。防止有人日后把 ``shell=True``
      加回来，而测试恰好没踩到那条路径。
    """
    marker = tmp_path / "shell-ran"
    received = tmp_path / "received-argv.txt"
    command = [
        sys.executable,
        "-c",
        "import pathlib, sys; pathlib.Path(sys.argv[2]).write_text(sys.argv[1], encoding='utf-8')",
        f"; touch {marker}",
        str(received),
    ]

    coordinator = ReproCoordinator(worktree_root=tmp_path / "worktrees")
    coordinator.submit(_request("r1", fixture_repo["clone"], command))
    assert coordinator.run_pending() == 1

    assert received.exists(), "the command never received its argument"
    assert received.read_text(encoding="utf-8") == f"; touch {marker}"
    assert not marker.exists(), "the argument reached a shell"

    source = Path(repro_module.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source


# ---------------------------------------------------------------------------
# 6. 异常路径同样清理 worktree
# ---------------------------------------------------------------------------
def test_worktree_removed_when_the_runner_raises(fixture_repo, tmp_path):
    """runner 抛异常时，worktree 照样被删掉。"""
    worktree_root = tmp_path / "worktrees"

    def exploding_runner(command, cwd, timeout_s):
        raise RuntimeError("runner exploded")

    coordinator = ReproCoordinator(
        worktree_root=worktree_root,
        runner=exploding_runner,
    )
    coordinator.submit(_request("r1", fixture_repo["clone"], ["true"]))

    with pytest.raises(RuntimeError):
        coordinator.run_pending()

    assert list(worktree_root.iterdir()) == []


# ---------------------------------------------------------------------------
# 7. 队列是持久的：另一个协调器实例能把结果读回来
# ---------------------------------------------------------------------------
def test_request_survives_a_restart(fixture_repo, failure_fixtures, state_db, tmp_path):
    """结果落库后，**另一个**协调器实例能从库里把它读回来。"""
    worktree_root = tmp_path / "worktrees"
    source = failure_fixtures["deterministic"]
    request = _request("r1", fixture_repo["clone"], source["command"])

    first = ReproCoordinator(
        db_path=state_db,
        worktree_root=worktree_root,
        runner=default_runner(),
    )
    first.submit(request)
    assert first.run_pending() == 1

    # 一个全新的实例：内存里什么都没有，只能从库里读。
    second = ReproCoordinator(
        db_path=state_db,
        worktree_root=worktree_root,
        runner=default_runner(),
    )
    assert second.get("r1") == {
        "request_id": "r1",
        "classification": FRAMEWORK_DETERMINISTIC,
        "attempts": 2,
    }


# ---------------------------------------------------------------------------
# 8. 复现树是链接 worktree，不是克隆
# ---------------------------------------------------------------------------
def test_worktree_is_built_by_worktree_add(
    fixture_repo, failure_fixtures, tmp_path
):
    """复现树由 ``git worktree add --detach`` 现场建立，跑完从仓库上摘掉。

    协议原话是「复现树用 ``git worktree add --detach <tmp> origin/main`` 现场
    建立、结束即清理，绝不复用 plan 自己的 worktree」。这一条把那句话拆成
    六个可判定的断言，各管一头：

    * **``.git`` 是文件** —— 链接 worktree 的标记；克隆出来的树里 ``.git`` 是
      **目录**。这一条不依赖实现细节，只看 git 自己留下的形状。
    * **common dir 指向目标仓库** —— 这棵树挂在失败所属的仓库上，不是另一棵
      与它无关的树。
    * **运行期它在目标仓库的 worktree 名单里** —— 这棵树确实登记在目标仓库
      名下。
    * **HEAD 是 origin/main 的 commit，且是 detached 的** —— 跑的是干净 main，
      不是 plan 分支。
    * **跑完目标仓库的 worktree 名单里没有它、目录也不在了** —— 清理是真的
      做了（``git worktree remove`` + ``prune``），不是只把目录删了。
    * **目标仓库 ``git status --porcelain`` 为空** —— 复现没有在失败所属的
      仓库上留下任何改动。
    """
    observed: Dict[str, Any] = {}
    base_runner = default_runner()
    target = fixture_repo["clone"]

    def observing_runner(command, cwd, timeout_s):
        tree = Path(cwd)
        observed["tree"] = tree
        observed["git_marker"] = (tree / ".git").is_file()
        observed["git_pointer"] = (tree / ".git").read_text(encoding="utf-8")
        observed["common_dir"] = _git_output(tree, "rev-parse", "--git-common-dir")
        observed["head"] = _git_output(tree, "rev-parse", "HEAD")
        observed["branch"] = _git_output(tree, "rev-parse", "--abbrev-ref", "HEAD")
        observed["worktree_list"] = _git_output(
            target, "worktree", "list", "--porcelain"
        )
        return base_runner(command, cwd, timeout_s)

    coordinator = ReproCoordinator(
        worktree_root=tmp_path / "worktrees",
        runner=observing_runner,
    )
    source = failure_fixtures["deterministic"]
    coordinator.submit(_request("r1", target, source["command"]))
    assert coordinator.run_pending() == 1

    # (a) 链接 worktree，不是克隆。
    assert observed["git_marker"] is True, (
        "the reproduction tree must be a linked worktree (.git is a file), "
        "not a clone (.git is a directory)"
    )

    # (b) 它挂在失败所属的仓库上：``.git`` 是指向该仓库 worktrees 目录的指针。
    assert Path(observed["common_dir"]).resolve() == (target / ".git").resolve()
    assert str((target / ".git" / "worktrees")) in observed["git_pointer"], (
        "the linked worktree must point into the target repository's "
        f"worktrees directory: {observed['git_pointer']!r}"
    )

    # (c) 运行期它在目标仓库的 worktree 名单里；HEAD 是 origin/main 的 commit，
    #     且处于 detached 状态。
    assert str(observed["tree"]) in observed["worktree_list"]
    assert observed["head"] == _git_output(target, "rev-parse", "origin/main")
    assert observed["branch"] == "HEAD"

    # (d) 跑完：名单里没有它，目录也不在了。
    remaining = _git_output(target, "worktree", "list", "--porcelain")
    assert str(observed["tree"]) not in remaining, (
        "the reproduction tree is still registered on the target repository"
    )
    assert remaining.count("worktree ") == 1, (
        f"the target repository kept a stale worktree registration: {remaining!r}"
    )
    assert not observed["tree"].exists()
    assert not observed["tree"].parent.exists()

    # (e) 目标仓库一个字节都没被动过。
    assert _git_output(target, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# 9. 复现树的 git 命令：路径与基准 ref 都在 ``--`` 之后
# ---------------------------------------------------------------------------
def test_worktree_add_argv_keeps_paths_behind_the_separator(
    fixture_repo, failure_fixtures, tmp_path, monkeypatch
):
    """复现树的 git 命令形状被钉住：``git worktree add --detach``，路径在 ``--`` 之后。

    git 把以 ``-`` 开头的参数读成**选项**：一个叫 ``-x`` 的文件因此变成一条
    选项，一个叫 ``--upload-pack=...`` 的地址因此变成一条要让远端执行的命令。
    ``--`` 是 git 自己给的「选项到此为止」的记号，路径必须待在它后面。

    同时钉住两件事，它们是同一句话的两面：**命令在失败所属的仓库里跑**
    （仓库地址是 ``-C`` 的值，不是一条裸参数），**基准与路径都躲在分隔符
    后面**。
    """
    recorded: List[List[str]] = []

    def fake_run_bounded(command, **kwargs):
        argv = [str(part) for part in command]
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(repro_module, "run_bounded", fake_run_bounded)

    worktree_root = tmp_path / "worktrees"
    coordinator = ReproCoordinator(worktree_root=worktree_root)
    source = failure_fixtures["deterministic"]
    coordinator.submit(_request("r1", fixture_repo["clone"], source["command"]))
    assert coordinator.run_pending() == 1

    adds = [
        argv for argv in recorded if "worktree" in argv and "add" in argv
    ]
    assert adds, f"no `git worktree add` call was made: {recorded!r}"
    argv = adds[0]

    # (a) 命令在失败所属的仓库里跑。
    assert "-C" in argv
    assert argv[argv.index("-C") + 1] == str(fixture_repo["clone"])

    # (b) detached，且路径与基准 ref 都在分隔符后面。
    assert "--detach" in argv[: argv.index("--")]
    separator = argv.index("--")
    tail = argv[separator + 1:]
    assert len(tail) == 2, f"the worktree path and the base ref follow: {argv!r}"
    assert tail[1] == "origin/main"
    assert Path(tail[0]).parent.parent == worktree_root


# ---------------------------------------------------------------------------
# 10. 基准永远是请求声明的那一个
# ---------------------------------------------------------------------------
def test_base_ref_is_never_silently_substituted(
    fixture_repo, failure_fixtures, state_db, tmp_path
):
    """``origin/HEAD`` 顶不上 ``origin/main`` —— 基准不会被悄悄换掉。

    远端默认分支不叫 main 时（master、trunk），``origin/HEAD`` 指向的也是
    同一棵树的另一个名字 —— 但那是**另一处**代码。协议把基准钉死在
    ``origin/main``：悄悄换个基准跑，同一份失败在别的树上就被判成了框架缺陷。

    探针把两个 ref 指向**不同的提交**，这样「到底用了哪个基准」是可分辨的：

    * (a) ``origin/main`` 在 A、``origin/HEAD`` 在 B -> 结论里的
      ``clean_commit`` 必须是 A；
    * (b) ``origin/main`` 没了、``origin/HEAD`` 还在 B -> 结论是
      ``unclassified``，不是「在 B 上跑」。
    """
    target = fixture_repo["clone"]
    other_commit = _point_origin_head_at_another_commit(target)

    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=default_runner(),
    )
    source = failure_fixtures["deterministic"]
    coordinator.submit(_request("r1", target, source["command"]))
    assert coordinator.run_pending() == 1

    # (a) 基准是 origin/main，不是 origin/HEAD。
    payload = json.loads(_row(state_db, "r1")["result_json"] or "{}")
    main_commit = _git_output(target, "rev-parse", "origin/main")
    assert payload["clean_commit"] == main_commit
    assert payload["clean_commit"] != other_commit, (
        "the reproduction ran against origin/HEAD instead of the declared "
        "base ref"
    )

    # (b) origin/main 读不出来时，origin/HEAD 顶不上：结论是 unclassified。
    _drop_remote_main(target)
    coordinator.submit(_request("r2", target, source["command"]))
    assert coordinator.run_pending() == 1

    result = coordinator.get("r2")
    assert result is not None
    assert result["classification"] == UNCLASSIFIED
    payload = json.loads(_row(state_db, "r2")["result_json"] or "{}")
    assert payload["clean_commit"] == "", (
        "the request was reproduced against a ref nobody asked for"
    )


# ---------------------------------------------------------------------------
# 11. 基准读不出来 -> unclassified，证据留着，这一行不是 failed
# ---------------------------------------------------------------------------
def test_unreadable_base_ref_is_recorded_unclassified(
    fixture_repo, failure_fixtures, state_db, tmp_path
):
    """目标仓库里读不出 ``origin/main`` -> 结论 unclassified，证据留着。

    四件事一起断言，因为它们是同一句话的四个面：

    * **结论是 ``unclassified``** —— 不是「换个 ref 跑」（那是冒充干净 main），
      也不是「跑挂了」（那不是结论）。
    * **读得到** —— 结论进了 ``result_json``，``get`` 读得回来。
    * **这一行不是 ``failed``** —— ``failed`` 只带一个 ``error`` 键，谁都读不
      出结论；请求不该被丢在那里。
    * **证据在** —— 请求的 base_ref、仓库路径、git 的原始输出都在。
    """
    target = fixture_repo["clone"]
    _drop_remote_main(target)

    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=default_runner(),
    )
    source = failure_fixtures["deterministic"]
    coordinator.submit(_request("r1", target, source["command"]))
    assert coordinator.run_pending() == 1

    result = coordinator.get("r1")
    assert result is not None
    assert result["classification"] == UNCLASSIFIED

    row = _row(state_db, "r1")
    assert row["status"] != "failed"
    payload = json.loads(row["result_json"] or "{}")
    assert payload["classification"] == UNCLASSIFIED

    evidence = payload["evidence"]
    assert evidence["base_ref"] == "origin/main"
    assert evidence["repo"] == str(target)
    assert evidence["reason"] == "base_ref_unreadable"
    # git 的原始输出（或报错）留在证据里 —— 「读不出来」是一个事实，不是沉默。
    assert evidence["git_exit_code"] != 0
    assert evidence["git_stdout"] or evidence["git_stderr"]


# ---------------------------------------------------------------------------
# 12. 连续三轮读不出基准 -> 放回排队，第三轮升级
# ---------------------------------------------------------------------------
def test_unclassified_request_is_requeued_then_escalates(
    fixture_repo, failure_fixtures, state_db, tmp_path
):
    """读不出基准的请求：前两轮放回排队，第三轮不再重排队并留下升级事件。

    PRD 那条「连续 3 轮无法归类 -> 升级 HITL」在这条路径上必须**可达**：
    每一轮都记下这是第几轮，证据不丢；第三轮之后这一行停在 ``done``（不再
    无限重排队），审计链上多一条升级事件。
    """
    target = fixture_repo["clone"]
    _drop_remote_main(target)

    coordinator = ReproCoordinator(
        db_path=state_db,
        worktree_root=tmp_path / "worktrees",
        runner=default_runner(),
    )
    source = failure_fixtures["deterministic"]
    coordinator.submit(_request("r1", target, source["command"]))

    for round_no in (1, 2):
        assert coordinator.run_pending() == 1
        row = _row(state_db, "r1")
        assert row["status"] == "queued", (
            f"round {round_no} must leave the request queued for the next round"
        )
        payload = json.loads(row["result_json"] or "{}")
        assert payload["classification"] == UNCLASSIFIED
        assert payload["evidence"]["unclassified_round"] == round_no

    # 第三轮：不再重排队。
    assert coordinator.run_pending() == 1
    row = _row(state_db, "r1")
    assert row["status"] == "done"
    payload = json.loads(row["result_json"] or "{}")
    assert payload["evidence"]["unclassified_round"] == 3

    # 升级留痕：审计链上多一条事件。
    escalations = [
        event
        for event in _events(state_db)
        if event["event_type"] == "repro_unclassified_escalated"
    ]
    assert len(escalations) == 1
    event_payload = json.loads(escalations[0]["payload_json"])
    assert event_payload["repro_id"] == "r1"
    assert event_payload["rounds"] == 3

    # 队列不再有待办：再跑一轮什么也不会发生。
    assert coordinator.run_pending() == 0
