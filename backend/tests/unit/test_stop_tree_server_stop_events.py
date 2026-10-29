"""服务端停机：停止请求与结果都落进 plan 的事件流。

背景
----
服务端的停机以前只留了**请求**的一半证据：``kill_service`` 会往 plan 目录写
一条 ``service_kill_requested``（谁要求停），但**结果**——这个执行器到底停了
没有、是不是被硬杀的、等了多久——一个字都没有。进程重启之后，没人答得出
「谁停在哪一步」。

R4 要求每一层都落盘，服务端这一层落两条：

* ``execution_stop_requested`` —— 请求，**写在信号发出之前**（先写后发：信号
  可能把对方的进程带走，写在后面就可能什么都没留下）；
* ``execution_stop_result`` —— 结果，字段齐全（``pid`` / ``hard_killed`` /
  ``elapsed_s``）。

落盘失败**不能**挡住停机：plan 不在了、日志目录不可写、日志器炸了——停机照
做，只是那一条证据没了。

测试不碰真实进程：执行器换成假进程。plan 事件流用**真的**
``ExecutionLogger``（写到临时目录），外面套一层记录器，把「事件写在哪一步」
也记下来——顺序本身就是契约的一部分。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import execution_logger
import server


#: 真的 ``ExecutionLogger``（在 fixture 换掉 ``server.ExecutionLogger`` 之前
#: 抓住它，否则记录器会包住自己）。
_REAL_LOGGER = server.ExecutionLogger


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
        order: List[Tuple],
        exit_after: Optional[float] = None,
    ) -> None:
        self.pid = pid
        self.exit_after = exit_after
        self.wait_timeouts: List[Optional[float]] = []
        self.kill_calls = 0
        self.exited = False
        self._order = order
        self._exit_at: Optional[float] = None

    # -- the test drives this one; it stands in for the real SIGTERM --
    def deliver_signal(self) -> None:
        self._order.append(("signal", self.pid))
        if self.exit_after is not None:
            self._exit_at = time.monotonic() + self.exit_after

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
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
        self._order.append(("kill", self.pid))
        return 0


class _Sink:
    """假执行器 + 顺序记录 + plan 事件写入记录。"""

    def __init__(self, plans_dir: Path) -> None:
        self.plans_dir = plans_dir
        #: 混在一起的一条时间线：``("event", 事件名, plan_id)`` /
        #: ``("signal", pid)`` / ``("kill", pid)``。
        self.order: List[Tuple] = []
        #: 每一次 plan 事件写入。
        self.writes: List[Dict] = []
        self.procs: Dict[str, FakeProc] = {}
        self.by_pid: Dict[int, FakeProc] = {}

    def add(
        self, plan_id: str, *, exit_after: Optional[float] = None
    ) -> FakeProc:
        """登记一个在飞的执行器（连同它的 plan 目录）。"""
        proc = FakeProc(
            pid=1000 + len(self.procs),
            order=self.order,
            exit_after=exit_after,
        )
        self.procs[plan_id] = proc
        self.by_pid[proc.pid] = proc
        (self.plans_dir / plan_id).mkdir(parents=True, exist_ok=True)
        server._execution_state[plan_id] = {
            "status": "running",
            "process": proc,
        }
        return proc

    def events(self, name: str) -> List[Dict]:
        return [w for w in self.writes if w["event"] == name]

    def index_of(self, kind: str, name: Optional[str] = None) -> int:
        for i, entry in enumerate(self.order):
            if entry[0] != kind:
                continue
            if name is None or entry[1] == name:
                return i
        raise AssertionError(f"no {kind}/{name} in {self.order}")


class _RecordingLogger:
    """真的 ``ExecutionLogger``，外面套一层记录器。

    写入照样落到 plan 自己的 ``execution.log``（所以「事件真的落在 plan 的
    事件流里」也被钉住），同时把每一次写入和它发生的顺序记下来。
    """

    def __init__(self, plan_id: str, sink: _Sink) -> None:
        self._inner = _REAL_LOGGER(plan_id)
        self._sink = sink

    def info(self, event, message, **kwargs):
        self._sink.writes.append(
            {
                "plan_id": self._inner.plan_id,
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
            }
        )
        self._sink.order.append(("event", event, self._inner.plan_id))
        self._inner.info(event, message, **kwargs)


@pytest.fixture
def sink(monkeypatch, tmp_path):
    """把服务端接到假进程 + 临时 plan 目录上。

    * 信号路径：进程组解析得出来，停止请求走一个假的 ``_kill_service``
      —— 它才是真正把信号交到假进程手上的地方；
    * plan 目录：临时目录，事件落在那里；
    * 收尾的孤儿清扫：空操作。
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    s = _Sink(plans_dir)
    monkeypatch.setattr(server, "_execution_state", {})
    monkeypatch.setattr(server, "_verification_state", {})
    monkeypatch.setattr(server.os, "getpgid", lambda pid: 10_000 + pid)
    monkeypatch.setattr(server, "_signallable_pgid", lambda pgid: pgid)
    monkeypatch.setattr(server, "_reap_all_plans", lambda *a, **k: {})
    # 停机窗口调小：断言的是**记录**，不是秒数。
    monkeypatch.setattr(
        server, "_execution_stop_grace_seconds", lambda: 0.2
    )
    monkeypatch.setattr(
        server,
        "ExecutionLogger",
        lambda plan_id, *a, **k: _RecordingLogger(plan_id, s),
    )

    def _kill_service(runtime, caller, reason, round_token="", **kwargs):
        s.order.append(("signal", runtime.pid))
        proc = s.by_pid.get(runtime.pid)
        if proc is not None:
            proc.deliver_signal()
        return {}

    monkeypatch.setattr(server, "_kill_service", _kill_service)
    return s


def _plan_entries(sink: _Sink, plan_id: str) -> List[Dict]:
    """读 plan 自己的 ``execution.log``（真的落盘的那一份）。"""
    log_file = sink.plans_dir / plan_id / "execution.log"
    if not log_file.exists():
        return []
    return [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_request_recorded_before_signal(sink):
    """请求事件必须写在信号发出之前。

    顺序反了，请求就可能永远留不下来：信号会把对方的进程带走，写在后面的
    记录连同写记录的那段代码一起没了。
    """
    proc = sink.add("plan-a", exit_after=0.01)

    server._shutdown_all_executions()

    request_at = sink.index_of("event", "execution_stop_requested")
    signal_at = sink.index_of("signal")
    assert request_at < signal_at, sink.order

    requests = sink.events("execution_stop_requested")
    assert len(requests) == 1, sink.writes
    assert requests[0]["plan_id"] == "plan-a"
    assert requests[0]["data"]["pid"] == proc.pid, requests[0]


def test_result_records_hard_kill_flag(sink):
    """硬杀 → 结果里的 ``hard_killed`` 是 True。"""
    sink.add("plan-stubborn")

    server._shutdown_all_executions()

    results = sink.events("execution_stop_result")
    assert len(results) == 1, sink.writes
    assert results[0]["data"]["hard_killed"] is True, results[0]
    assert results[0]["data"]["pid"] == 1000, results[0]


def test_result_records_elapsed_seconds(sink):
    """结果里的 ``elapsed_s`` 是一个 ≥0 的 float。"""
    sink.add("plan-polite", exit_after=0.01)

    server._shutdown_all_executions()

    results = sink.events("execution_stop_result")
    assert len(results) == 1, sink.writes
    elapsed = results[0]["data"]["elapsed_s"]
    assert isinstance(elapsed, float), results[0]
    assert elapsed >= 0, results[0]


def test_every_executor_gets_a_result(sink):
    """三个在飞执行器 → 三条请求、三条结果，一条对一个执行器。"""
    for plan_id in ("plan-a", "plan-b", "plan-c"):
        sink.add(plan_id, exit_after=0.01)

    server._shutdown_all_executions()

    requests = sink.events("execution_stop_requested")
    results = sink.events("execution_stop_result")
    assert len(requests) == 3, sink.writes
    assert len(results) == 3, sink.writes
    assert sorted(r["plan_id"] for r in results) == [
        "plan-a",
        "plan-b",
        "plan-c",
    ], sink.writes
    assert sorted(r["data"]["pid"] for r in results) == [1000, 1001, 1002]


def test_logger_failure_does_not_block_the_stop(sink, monkeypatch):
    """plan 日志器炸了 → 信号照发，停机照做。

    停机比证据重要：一条记录写不进去，不能把停机一起拖下水。
    """

    attempts = []

    class ExplodingLogger:
        def __init__(self, plan_id, *args, **kwargs):
            attempts.append(plan_id)
            raise RuntimeError("the plan log is broken")

    monkeypatch.setattr(server, "ExecutionLogger", ExplodingLogger)
    proc = sink.add("plan-a", exit_after=0.01)

    server._shutdown_all_executions()  # 不冒泡

    assert attempts, "the stop must have tried to write its record"
    assert ("signal", proc.pid) in sink.order, sink.order


def test_logger_write_failure_does_not_block_the_stop(sink, monkeypatch):
    """日志器构造成功、写入炸了 → 同样不挡停机。"""

    attempts = []

    class ExplodingLogger:
        def __init__(self, plan_id, *args, **kwargs):
            self.plan_id = plan_id

        def info(self, *args, **kwargs):
            attempts.append(kwargs.get("data"))
            raise RuntimeError("the plan log is broken")

    monkeypatch.setattr(server, "ExecutionLogger", ExplodingLogger)
    proc = sink.add("plan-a", exit_after=0.01)

    server._shutdown_all_executions()  # 不冒泡

    assert attempts, "the stop must have tried to write its record"
    assert ("signal", proc.pid) in sink.order, sink.order


def test_event_types_registered():
    """两个停止事件都注册进日志的事件类型表（渲染器要靠它拿前缀）。"""
    assert "execution_stop_result" in execution_logger.LOG_EVENT_TYPES
    assert "execution_stop_requested" in execution_logger.LOG_EVENT_TYPES


def test_the_result_lands_in_the_plan_event_stream(sink):
    """结果真的落在 plan 自己的 ``execution.log`` 里，进程退出后仍读得到。"""
    sink.add("plan-a", exit_after=0.01)

    server._shutdown_all_executions()

    entries = _plan_entries(sink, "plan-a")
    stops = [e for e in entries if e.get("event") == "execution_stop_result"]
    assert len(stops) == 1, entries
    assert stops[0]["level"] == "INFO", stops[0]
    assert stops[0]["message"] == "execution stop result", stops[0]
    assert stops[0]["data"] == {
        "pid": 1000,
        "hard_killed": False,
        "elapsed_s": stops[0]["data"]["elapsed_s"],
    }, stops[0]


def test_unknown_plan_is_not_recorded(sink):
    """plan 不存在 → 记 warning、返回 None，不停机，也不凭空造一个目录。"""
    ts = server._record_execution_stop(
        "plan-unknown", "result", pid=1, hard_killed=False, elapsed_s=0.1
    )

    assert ts is None
    assert not (sink.plans_dir / "plan-unknown").exists()


def test_recorded_stop_returns_the_written_timestamp(sink):
    """返回值就是**写进日志的那一条**的 ts（不是另一个近似值）。"""
    sink.add("plan-a")

    ts = server._record_execution_stop(
        "plan-a", "result", pid=1000, hard_killed=False, elapsed_s=1.5
    )

    assert ts is not None
    entries = _plan_entries(sink, "plan-a")
    stops = [e for e in entries if e.get("event") == "execution_stop_result"]
    assert len(stops) == 1, entries
    assert ts == stops[0]["ts"], (ts, stops[0])


def test_result_is_recorded_before_the_sweep_returns(sink):
    """结果必须写完，停机扫描才返回——返回后读得到，才算数。"""
    sink.add("plan-a", exit_after=0.01)

    report = server._shutdown_all_executions()

    assert isinstance(report, dict), f"the sweep returned {report!r}"
    entries = _plan_entries(sink, "plan-a")
    results = [e for e in entries if e.get("event") == "execution_stop_result"]
    assert len(results) == 1, entries