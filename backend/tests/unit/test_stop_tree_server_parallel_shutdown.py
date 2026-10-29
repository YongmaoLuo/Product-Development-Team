"""服务端批量停机：并行发信号 + 统一 join（arch 决策点 1 / test 决策点 2）。

背景
----
``_shutdown_all_executions`` 逐个执行器调 ``_kill_process_tree``，而后者内部
``proc.wait(timeout=grace)`` —— **等待发生在循环里**。F1 的日志里 8 个在飞
执行器只有前 3 个被处理：第 4 个要等前 3 个各自走完一整个 grace 才轮得到它
的信号，「后 5 个从未收到信号」。

停机必须是两段，顺序就是要点：

1. **先把信号全部发出去** —— 每个在飞执行器同一批收到 SIGTERM；
2. **再用一个统一截止 join** —— 整批共享一个窗口，不是每个执行器各等各的
   （串行等就是 N 个窗口）。

测试不碰真实进程。假 proc 记录收到的信号与 wait 的时刻，并用**真实的时间
流逝**模拟「收到信号后多久退出」——墙钟断言落在真实经过的时间上，而不是
函数内部的计数器上。

``_kill_process_tree`` 的既有契约由本文件末尾的
``test_kill_process_tree_contract_unchanged`` 钉住：它改造前就为真，改造后
必须还是为真。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import server


#: 停机扫描用的 grace 窗口。真实路径上的窗口有 4s 下限（任务 5），拿它做
#: 墙钟断言会让这个测试跑十几秒。这里把窗口调小：断言的是**结构**（整批
#: 一个窗口，不是每个执行器一个窗口），不是秒数。
#:
#: 比例是钉死的（执行器在 0.6×grace 后退出、整批墙钟 < 1.3×grace、串行是
#: 1.8×grace）；秒数只决定余量。1s 留出的绝对余量（≈0.55s）足够吸收
#: 机器调度抖动 —— 实测 ``time.sleep`` 在这台机器上每次会多睡 0.1~0.15s。
_TEST_GRACE = 1.0


class FakeProc:
    """``subprocess.Popen`` 的最小替身。

    * :meth:`deliver_signal` —— 测试的 ``_kill_service`` 替身调用它：记录
      信号，并安排「收到信号后 ``exit_after`` 秒退出」；
    * :meth:`wait` —— 睡到该退出的时刻；睡不到就抛 ``TimeoutExpired``；
    * :meth:`kill` —— 硬杀。
    """

    def __init__(
        self,
        pid: int,
        log: List[Tuple[str, int]],
        exit_after: Optional[float] = None,
    ) -> None:
        self.pid = pid
        self.exit_after = exit_after
        self.wait_timeouts: List[Optional[float]] = []
        self.kill_calls = 0
        self.exited = False
        self._log = log
        self._exit_at: Optional[float] = None

    # -- the test drives this one; it stands in for the real SIGTERM --
    def deliver_signal(self) -> None:
        self._log.append(("signal", self.pid))
        if self.exit_after is not None:
            self._exit_at = time.monotonic() + self.exit_after

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        self._log.append(("wait", self.pid))
        if self.exited:
            return 0
        if self._exit_at is None:
            # Never exits on its own: burn the whole window.
            if timeout:
                time.sleep(timeout)
            raise subprocess.TimeoutExpired(cmd="fake-proc", timeout=timeout)
        remaining = self._exit_at - time.monotonic()
        if timeout is not None and remaining > timeout:
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(cmd="fake-proc", timeout=timeout)
        time.sleep(max(0.0, remaining))
        self.exited = True
        return 0

    def kill(self):
        self.kill_calls += 1
        self.exited = True
        self._log.append(("kill", self.pid))
        return 0


class _Batch:
    """一批在飞的执行器：共享的事件日志 + 假进程表。"""

    def __init__(self) -> None:
        self.log: List[Tuple[str, int]] = []
        self.procs: Dict[str, FakeProc] = {}
        self.by_pid: Dict[int, FakeProc] = {}

    def add(
        self, plan_id: str, *, exit_after: Optional[float] = None
    ) -> FakeProc:
        """登记一个在飞的执行器。"""
        proc = FakeProc(
            pid=1000 + len(self.procs),
            log=self.log,
            exit_after=exit_after,
        )
        self.procs[plan_id] = proc
        self.by_pid[proc.pid] = proc
        server._execution_state[plan_id] = {
            "status": "running",
            "process": proc,
        }
        return proc

    def signals(self) -> List[int]:
        return [pid for kind, pid in self.log if kind == "signal"]


@pytest.fixture
def batch(monkeypatch):
    """把服务端接到假进程上。

    * 信号路径：进程组解析得出来，停止请求走一个假的 ``_kill_service``
      —— 它才是真正把信号交到假进程手上的地方；
    * plan 目录：一律 ``None``，测试不碰磁盘；
    * 收尾的孤儿清扫：空操作。
    """
    state = _Batch()
    monkeypatch.setattr(server, "_execution_state", {})
    monkeypatch.setattr(server, "_verification_state", {})
    monkeypatch.setattr(server.os, "getpgid", lambda pid: 10_000 + pid)
    monkeypatch.setattr(server, "_signallable_pgid", lambda pgid: pgid)
    monkeypatch.setattr(server, "_execution_plan_dir", lambda proc: None)
    monkeypatch.setattr(server, "_reap_all_plans", lambda *a, **k: {})
    monkeypatch.setattr(
        server, "_execution_stop_grace_seconds", lambda: _TEST_GRACE
    )

    def _kill_service(runtime, caller, reason, round_token="", **kwargs):
        proc = state.by_pid.get(runtime.pid)
        if proc is not None:
            proc.deliver_signal()
        return {}

    monkeypatch.setattr(server, "_kill_service", _kill_service)
    return state


def test_all_signals_sent_before_any_wait(batch):
    """三个在飞执行器同一批收到 SIGTERM —— 第一次 wait 之前信号已经发完。

    改造前是「信号 → 等 → 信号 → 等」：第二个执行器的信号要等第一个走完
    整个 grace 才发得出去（F1：后 5 个从未收到信号）。
    """
    for plan_id in ("plan-a", "plan-b", "plan-c"):
        batch.add(plan_id, exit_after=0.01)

    server._shutdown_all_executions()

    kinds = [kind for kind, _ in batch.log]
    signal_at = [i for i, kind in enumerate(kinds) if kind == "signal"]
    wait_at = [i for i, kind in enumerate(kinds) if kind == "wait"]
    assert len(signal_at) == 3, batch.log
    assert wait_at, batch.log
    assert max(signal_at) < min(wait_at), batch.log
    assert sorted(batch.signals()) == sorted(
        proc.pid for proc in batch.procs.values()
    ), batch.log


def test_one_shared_deadline_not_per_process(batch):
    """三个执行器各要 0.6×grace 才退出 —— 整批墙钟是一个 grace，不是三个。

    串行等是 3 × 0.6 × grace = 1.8×grace；统一截止是 0.6×grace。
    """
    for plan_id in ("plan-a", "plan-b", "plan-c"):
        batch.add(plan_id, exit_after=0.6 * _TEST_GRACE)

    started = time.monotonic()
    server._shutdown_all_executions()
    elapsed = time.monotonic() - started

    assert elapsed < 1.3 * _TEST_GRACE, (
        f"the batch took {elapsed:.3f}s — one shared window is "
        f"{_TEST_GRACE:.3f}s, one window per execution would be "
        f"{3 * 0.6 * _TEST_GRACE:.3f}s"
    )


def test_leftovers_killed_after_deadline(batch):
    """过了截止还在的收到 kill；窗口内自己退出的那个不收到。"""
    stubborn = batch.add("plan-stubborn")
    polite = batch.add("plan-polite", exit_after=0.01)

    server._shutdown_all_executions()

    assert stubborn.kill_calls == 1, batch.log
    assert polite.kill_calls == 0, batch.log
    # The polite execution was told to stop *before* the stubborn one was
    # killed: the sweep does not work through them one at a time.
    assert batch.log.index(("signal", polite.pid)) < batch.log.index(
        ("kill", stubborn.pid)
    ), batch.log


def test_hard_kill_reported_per_plan(batch):
    """报告里只有被硬杀的那个 plan 是 True。"""
    batch.add("plan-stubborn")
    batch.add("plan-polite", exit_after=0.01)

    report = server._shutdown_all_executions()

    assert isinstance(report, dict), f"the sweep returned {report!r}"
    assert report["hard_killed"] == {
        "plan-stubborn": True,
        "plan-polite": False,
    }, report


def test_no_in_flight_executions_sends_nothing(batch):
    """没有在飞执行器：不发信号、不等待，直接进入 verification 清理。"""
    cleaned = []

    class _Orchestrator:
        def cleanup(self):
            cleaned.append("cleaned")

    server._verification_state["plan-verify"] = {"orchestrator": _Orchestrator()}

    report = server._shutdown_all_executions()

    assert isinstance(report, dict), f"the sweep returned {report!r}"
    assert report["signalled"] == [], report
    assert report["hard_killed"] == {}, report
    assert cleaned == ["cleaned"]
    assert [kind for kind, _ in batch.log if kind in ("signal", "wait")] == []


def test_already_gone_is_skipped_and_recorded(batch):
    """发信号前就退出的执行器：跳过，并记成 already_gone。"""
    gone = batch.add("plan-gone")
    gone.exited = True
    live = batch.add("plan-live", exit_after=0.01)

    report = server._shutdown_all_executions()

    assert isinstance(report, dict), f"the sweep returned {report!r}"
    assert report["already_gone"] == ["plan-gone"], report
    assert batch.signals() == [live.pid], batch.log
    assert "plan-gone" not in report["hard_killed"], report


def test_kill_service_failure_does_not_stop_the_sweep(batch, monkeypatch):
    """一个执行器的停止请求写盘失败 → 记日志后继续，其余执行器照常收到信号。"""
    first = batch.add("plan-a", exit_after=0.01)
    second = batch.add("plan-b", exit_after=0.01)

    def _kill_service(runtime, caller, reason, round_token="", **kwargs):
        if runtime.pid == first.pid:
            raise RuntimeError("the ledger is gone")
        batch.by_pid[runtime.pid].deliver_signal()
        return {}

    monkeypatch.setattr(server, "_kill_service", _kill_service)

    report = server._shutdown_all_executions()

    assert isinstance(report, dict), f"the sweep returned {report!r}"
    # The failure did not stop the sweep: the second execution still got
    # its signal, and the report covers both plans.
    assert batch.signals() == [second.pid], batch.log
    assert set(report["hard_killed"]) == {"plan-a", "plan-b"}, report


def test_kill_process_tree_contract_unchanged(batch):
    """``_kill_process_tree`` 的契约改造前后一致。

    这条是**契约钉**：改造前也应为真，改造后必须还是为真。

    * 显式 grace：等的就是那个 grace，没到期就不硬杀 → False；
    * 已经退出：什么都不做 → False；
    * grace 耗尽：升级 SIGKILL，并把「硬杀过」报告给调用方 → True。
    """
    # 1. explicit grace — the wait gets exactly that grace.
    polite = batch.add("plan-polite", exit_after=0.0)
    hard = server._kill_process_tree(polite, grace_seconds=2.0)
    assert hard is False
    assert polite.wait_timeouts == [2.0]
    assert polite.kill_calls == 0

    # 2. already exited — nothing is signalled, nothing is waited for.
    gone = batch.add("plan-gone")
    gone.exited = True
    hard = server._kill_process_tree(gone, grace_seconds=2.0)
    assert hard is False
    assert gone.wait_timeouts == []
    assert gone.kill_calls == 0

    # 3. grace exhausted — SIGKILL, then a fixed 5s reap.
    stubborn = batch.add("plan-stubborn")
    hard = server._kill_process_tree(stubborn, grace_seconds=0.05)
    assert hard is True
    assert stubborn.kill_calls == 1
    assert stubborn.wait_timeouts == [0.05, 5]
