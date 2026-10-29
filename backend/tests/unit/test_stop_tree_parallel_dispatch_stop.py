"""并行停机：一次停机里所有子进程被同时收掉，预算不按子进程累加。

背景
----
``stop_all_live_dispatches`` 以前是「一个接一个」地停：每个子进程各拿
满整份预算（stdout 0.35 → SIGTERM 0.25 → SIGKILL 0.10）。两个 Claude
Code 同时在跑时，总耗时是 2×预算；而 ``cli.py`` 的信号处理器只等
0.9×预算就 ``_exit_now`` —— 第二个子进程永远等不到 stop。

这里钉住的是**并行语义**：

* 三段（stdout → SIGTERM → SIGKILL），每段对**所有**子进程同时生效 ——
  一个子进程还在等自己那份额度的时候，另一个早就该被叫停了；
* 每段只 join 一次 —— 一段里的等待是**一个共享截止**（0.35 / 0.25 /
  0.10 × 预算），不按子进程累加；
* 阶段二已经退出的子进程，阶段三不再给它 SIGKILL。

测试不碰真实进程：假 process 把 ``stdout.close`` 与收到的信号记进一条
**共享**的调用序日志，断言落在那条日志的顺序上，而不是函数内部的计数器
上。假 ``wait`` **真的把 timeout 睡掉**再抛 ``TimeoutExpired`` —— 预算
是这条路径上唯一的硬约束，不真的等就等于没有预算。
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool

#: One entry of the shared call-order log: ``(pid, what)``.
Event = Tuple[int, str]


def _signal_name(sig: int) -> str:
    return signal.Signals(sig).name


class FakeStdout:
    """``process.stdout`` 的替身：关掉时把调用序记进共享日志。"""

    def __init__(self, owner: "FakeProcess") -> None:
        self._owner = owner
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._owner._on_stdout_closed()


class FakeProcess:
    """``subprocess.Popen`` 的最小替身。

    ``exits_on`` 决定它在什么之后退出：

    * ``"stdout"`` —— stdout 被关掉之后自己退出（模拟「看到管道断了就
      自己走」）；
    * ``"SIGTERM"`` —— 收到 SIGTERM 之后退出；
    * ``None`` —— 永远不退出（不理会 SIGTERM 的子进程）。

    ``wait`` 收到的 timeout 全部记进 ``wait_timeouts``，并且**真的把
    timeout 睡掉**再抛 ``TimeoutExpired``：一个不花时间的假 wait 会让
    「预算不按子进程累加」这条断言变成空话。
    """

    def __init__(
        self,
        pid: int,
        events: List[Event],
        exits_on: Optional[str] = None,
    ) -> None:
        self.pid = pid
        self.events = events
        self.exits_on = exits_on
        self.stdout = FakeStdout(self)
        self.wait_timeouts: List[Optional[float]] = []
        self._dead = False

    # -- the stop path only ever uses these three -------------------------

    def poll(self):
        return 0 if self._dead else None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self._dead:
            return 0
        if timeout:
            time.sleep(timeout)
        raise subprocess.TimeoutExpired(cmd="fake-claude", timeout=timeout)

    # -- internals ---------------------------------------------------------

    def _on_stdout_closed(self) -> None:
        self.events.append((self.pid, "stdout.close"))
        if self.exits_on == "stdout":
            self._dead = True


class FakeTool:
    """``ClaudeCodingTool`` 的替身：只有停止路径真正用到的两个字段。"""

    def __init__(self, process) -> None:
        self._current_process = process
        self._process_lock = threading.Lock()


class ExplodingTool:
    """取 ``_current_process`` 就炸的 tool —— 一个坏 tool 不能拖垮其余的。"""

    def __init__(self) -> None:
        self._process_lock = threading.Lock()

    @property
    def _current_process(self):
        raise RuntimeError("this tool is broken")


@pytest.fixture
def events(monkeypatch) -> List[Event]:
    """所有子进程共用的调用序日志 + 记录信号的 ``kill_process_group`` 替身。"""
    log: List[Event] = []

    def _fake_kill_process_group(proc, *, sig=signal.SIGKILL, wait_timeout=5.0):
        log.append((getattr(proc, "pid", None), _signal_name(sig)))
        if sig == signal.SIGTERM and getattr(proc, "exits_on", None) == "SIGTERM":
            proc._dead = True
        return None

    monkeypatch.setattr(coding_tool, "kill_process_group", _fake_kill_process_group)
    return log


@pytest.fixture(autouse=True)
def _clean_registry():
    """登记表是模块级的全局量 —— 每个用例前后都要清干净，不能互相污染。"""
    coding_tool._LIVE_DISPATCH_TOOLS.clear()
    yield
    coding_tool._LIVE_DISPATCH_TOOLS.clear()


def test_all_children_closed_before_any_sigterm(events):
    """所有子进程的 stdout 都要在**第一个** SIGTERM 之前被关掉。

    一个子进程还在等自己那份额度的时候，另一个不能还没被通知 ——
    否则第二个子进程永远等不到 stop。
    """
    a = FakeProcess(11, events, exits_on="SIGTERM")
    b = FakeProcess(12, events, exits_on="SIGTERM")
    coding_tool.register_live_dispatch(FakeTool(a))
    coding_tool.register_live_dispatch(FakeTool(b))

    records: List[dict] = []
    stopped = coding_tool.stop_all_live_dispatches(2.0, records=records)

    assert stopped == 2
    closes = [i for i, (_, what) in enumerate(events) if what == "stdout.close"]
    terms = [i for i, (_, what) in enumerate(events) if what == "SIGTERM"]
    assert len(closes) == 2, "both children must have their stdout closed"
    assert terms, "the SIGTERM stage must have run"
    assert max(closes) < min(terms)


def test_all_sigterms_before_any_sigkill(events):
    """所有 SIGTERM 都要在**第一个** SIGKILL 之前发出。

    一个子进程被硬杀的时候，另一个还没收到 SIGTERM —— 那另一个就是
    被跳过了 polite 阶段。
    """
    a = FakeProcess(11, events, exits_on=None)
    b = FakeProcess(12, events, exits_on="SIGTERM")
    coding_tool.register_live_dispatch(FakeTool(a))
    coding_tool.register_live_dispatch(FakeTool(b))

    records: List[dict] = []
    coding_tool.stop_all_live_dispatches(2.0, records=records)

    terms = [i for i, (_, what) in enumerate(events) if what == "SIGTERM"]
    kills = [i for i, (_, what) in enumerate(events) if what == "SIGKILL"]
    assert len(terms) == 2, "every survivor must be SIGTERMed first"
    assert len(kills) == 1
    assert max(terms) < min(kills)


def test_survivor_only_gets_sigkill(events):
    """阶段二退出的子进程，阶段三不再给它 SIGKILL。"""
    a = FakeProcess(11, events, exits_on=None)
    b = FakeProcess(12, events, exits_on="SIGTERM")
    coding_tool.register_live_dispatch(FakeTool(a))
    coding_tool.register_live_dispatch(FakeTool(b))

    records: List[dict] = []
    coding_tool.stop_all_live_dispatches(2.0, records=records)

    killed = [pid for pid, what in events if what == "SIGKILL"]
    assert killed == [11], "only the child that ignored SIGTERM may be killed"
    by_pid = {record["pid"]: record for record in records}
    assert by_pid[12]["stage"] == "SIGTERM"
    assert by_pid[12]["forced"] is False
    assert by_pid[11]["stage"] == "SIGKILL"
    assert by_pid[11]["forced"] is True


def test_records_carry_every_child(events):
    """每个子进程都留下一条记录：pid / stage / forced / elapsed_s。"""
    a = FakeProcess(11, events, exits_on="stdout")
    b = FakeProcess(12, events, exits_on=None)
    coding_tool.register_live_dispatch(FakeTool(a))
    coding_tool.register_live_dispatch(FakeTool(b))

    records: List[dict] = []
    stopped = coding_tool.stop_all_live_dispatches(2.0, records=records)

    assert stopped == 2
    assert len(records) == 2
    for record in records:
        assert set(record) == {"pid", "stage", "forced", "elapsed_s"}
        assert isinstance(record["forced"], bool)
        assert record["elapsed_s"] >= 0
    by_pid = {record["pid"]: record for record in records}
    assert set(by_pid) == {11, 12}
    assert by_pid[11]["stage"] == "stdout"
    assert by_pid[11]["forced"] is False
    assert by_pid[12]["stage"] == "SIGKILL"
    assert by_pid[12]["forced"] is True


def test_total_stays_inside_stage_fractions(events):
    """两个都不自己退出的子进程 → 总耗时仍然是一份预算，不是两份。

    串行停机在这里要花 2 × 0.7 × budget；并行的上限是 0.7 × budget。
    断言留了 0.8 的余量：停机路径必须**永远**比调用方的等待窗口短。
    """
    budget = 2.0
    a = FakeProcess(11, events, exits_on=None)
    b = FakeProcess(12, events, exits_on=None)
    coding_tool.register_live_dispatch(FakeTool(a))
    coding_tool.register_live_dispatch(FakeTool(b))

    started = time.monotonic()
    coding_tool.stop_all_live_dispatches(budget)
    elapsed = time.monotonic() - started

    assert elapsed < 0.8 * budget, (
        f"the parallel stop took {elapsed:.3f}s for a {budget}s budget — "
        "the budget is being spent per child instead of shared"
    )


def test_returns_count(events):
    """返回值 = 参与停机的子进程数。"""
    a = FakeProcess(11, events, exits_on="stdout")
    b = FakeProcess(12, events, exits_on="stdout")
    coding_tool.register_live_dispatch(FakeTool(a))
    coding_tool.register_live_dispatch(FakeTool(b))

    records: List[dict] = []
    assert coding_tool.stop_all_live_dispatches(2.0, records=records) == 2
    assert len(records) == 2


def test_empty_registry_returns_zero_and_keeps_records(events):
    """登记表是空的 → 返回 0，records 一个字都不动，也不发任何信号。"""
    records: List[dict] = []

    assert coding_tool.stop_all_live_dispatches(2.0, records=records) == 0
    assert records == []
    assert events == []


def test_broken_tool_is_skipped_but_the_rest_still_stop(events):
    """一个 tool 取 ``_current_process`` 就炸 → 跳过它，其余照常停。"""
    good = FakeProcess(12, events, exits_on="stdout")
    coding_tool.register_live_dispatch(ExplodingTool())
    coding_tool.register_live_dispatch(FakeTool(good))

    records: List[dict] = []
    stopped = coding_tool.stop_all_live_dispatches(2.0, records=records)

    assert stopped == 1
    assert [record["pid"] for record in records] == [12]
    assert good.stdout.closed is True
