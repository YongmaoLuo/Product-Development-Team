"""协作式停止序列：stop 时 executor 先自行收尾它的 claude 子进程。

背景
----
``claude`` 子进程以 ``start_new_session=True`` 启动（``coding_tool.py`` 的
``_run_claude_interactive``），自成进程组：**executor 收到的 SIGTERM 打不到
它**。于是 executor 必须在 server 把它的进程组升级到 SIGKILL 之前，**自己**
把子进程收掉 —— 否则 server 侧的 ``_kill_process_tree`` 会走完 grace 窗口
后硬杀，``hard_killed`` 翻 True，停机策略从 resume 变成 full_rerun。

这里钉住的是**收尾顺序本身**：

    stdout.close() → wait(0.35×total) → SIGTERM 组 → wait(0.25×total)
    → SIGKILL 组 → wait(0.10×total)

三段等待合计 0.7×total —— 必须**严格小于** server 的 grace 窗口
（``PDT_STOP_GRACE_SECONDS``，默认 10s）。这是对上游「等 grace → SIGTERM
→ 再等 5s → SIGKILL」字面表述的有意偏离：按字面实现必然超窗。

测试不碰真实进程：假 process 记录收到的 ``wait(timeout=...)``，假
``kill_process_group`` 记录信号。断言落在**实际传给 subprocess 的参数**
上，而不是函数内部的计数器上。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool
from coding_tool import ClaudeCodingTool


class FakeStdout:
    """``process.stdout`` 的替身：记录自己被关掉了没有。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """``subprocess.Popen`` 的最小替身。

    ``exit_when`` 决定 ``wait`` 的行为：

    * ``"stdout_closed"`` —— 只在 stdout 被关掉之后才返回（模拟「子进程
      看到管道断了就自己退出」）；
    * ``"never"`` —— 永远 ``TimeoutExpired``（模拟不理会 SIGTERM 的子进程）。

    ``wait`` 收到的 timeout 全部记在 ``wait_timeouts`` 里 —— 预算是这条
    路径上唯一的硬约束，不能靠「反正只等一小会儿」蒙混过去。
    """

    def __init__(self, *, exit_when: str = "stdout_closed") -> None:
        self.pid = 424242
        self.stdout = FakeStdout()
        self.wait_timeouts: List[Optional[float]] = []
        self._exit_when = exit_when

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self._exit_when == "never":
            raise subprocess.TimeoutExpired(cmd="fake-claude", timeout=timeout)
        if not self.stdout.closed:
            raise subprocess.TimeoutExpired(cmd="fake-claude", timeout=timeout)
        return 0


class FakeTool:
    """``ClaudeCodingTool`` 的替身：只有停止路径真正用到的两个字段。"""

    def __init__(self, process: FakeProcess) -> None:
        self._current_process = process
        self._process_lock = threading.Lock()


@pytest.fixture
def sent_signals(monkeypatch) -> List[int]:
    """把 ``kill_process_group`` 换成记录器，返回收到的信号列表。"""
    sent: List[int] = []

    def _fake_kill_process_group(proc, *, sig=signal.SIGKILL, wait_timeout=5.0):
        sent.append(sig)
        return None

    monkeypatch.setattr(coding_tool, "kill_process_group", _fake_kill_process_group)
    return sent


@pytest.fixture(autouse=True)
def _clean_registry():
    """登记表是模块级的全局量 —— 每个用例前后都要清干净，不能互相污染。"""
    coding_tool._LIVE_DISPATCH_TOOLS.clear()
    yield
    coding_tool._LIVE_DISPATCH_TOOLS.clear()


def test_stdout_closed_before_signal(sent_signals):
    """子进程在 stdout 被关掉后自己退出 → 一个信号都不发（协作式停机）。"""
    proc = FakeProcess(exit_when="stdout_closed")

    hard = coding_tool._cooperative_stop_process(proc, 10.0)

    assert hard is False
    assert proc.stdout.closed is True
    assert sent_signals == []


def test_stubborn_child_gets_sigterm_then_sigkill(sent_signals):
    """子进程不理会 SIGTERM → 升级到 SIGKILL，且顺序是先 TERM 后 KILL。"""
    proc = FakeProcess(exit_when="never")

    hard = coding_tool._cooperative_stop_process(proc, 10.0)

    assert hard is True
    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]


def test_registry_visits_every_live_tool(sent_signals):
    """登记表里的每个 tool 都要被处理到 —— 不能只停第一个。"""
    proc_a = FakeProcess(exit_when="stdout_closed")
    proc_b = FakeProcess(exit_when="stdout_closed")
    tool_a = FakeTool(proc_a)
    tool_b = FakeTool(proc_b)
    coding_tool.register_live_dispatch(tool_a)
    coding_tool.register_live_dispatch(tool_b)

    stopped = coding_tool.stop_all_live_dispatches(10.0)

    assert stopped == 2
    assert proc_a.stdout.closed is True
    assert proc_b.stdout.closed is True


def test_unregistered_tool_is_skipped(sent_signals):
    """注销过的 tool 不再被处理 —— 收尾过的 dispatch 不该再被停一次。"""
    proc = FakeProcess(exit_when="stdout_closed")
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool)
    coding_tool.unregister_live_dispatch(tool)

    stopped = coding_tool.stop_all_live_dispatches(10.0)

    assert stopped == 0
    assert proc.stdout.closed is False
    assert proc.wait_timeouts == []


def test_sequence_stays_inside_budget(sent_signals):
    """三段等待合计不超过 0.7×total —— 必须严格小于 server 的 grace 窗口。"""
    proc = FakeProcess(exit_when="never")

    coding_tool._cooperative_stop_process(proc, 10.0)

    waited = sum(t for t in proc.wait_timeouts if t)
    assert waited <= 0.7 * 10.0
    # 三段的比例是钉死的 —— 少等或多等都会改变收尾语义。
    assert waited == pytest.approx(3.5 + 2.5 + 1.0)


def test_no_live_dispatch_returns_zero():
    """没有活跃 dispatch → 返回 0，不抛。"""
    assert coding_tool.stop_all_live_dispatches(10.0) == 0


@pytest.mark.parametrize(
    "bad",
    [0, -1, "abc", None, float("nan"), float("inf")],
    ids=["zero", "negative", "not-a-number", "none", "nan", "inf"],
)
def test_invalid_budget_falls_back_to_default(sent_signals, bad):
    """非法预算（<=0 / 非数字 / 非有限）→ 回退默认 10s，不抛。"""
    proc = FakeProcess(exit_when="never")

    hard = coding_tool._cooperative_stop_process(proc, bad)

    assert hard is True
    waited = sum(t for t in proc.wait_timeouts if t)
    assert waited == pytest.approx(0.7 * 10.0)


@pytest.mark.parametrize(
    ("given", "expected"),
    [(1.0, 2.0), (2.0, 2.0), (30.0, 30.0), (100.0, 60.0)],
    ids=["below-min", "at-min", "in-range", "above-max"],
)
def test_budget_is_clamped(sent_signals, given, expected):
    """预算被夹进 [2, 60] —— 太小没有意义，太大会把 stop 路径拖死。"""
    proc = FakeProcess(exit_when="never")

    coding_tool._cooperative_stop_process(proc, given)

    waited = sum(t for t in proc.wait_timeouts if t)
    assert waited == pytest.approx(0.7 * expected)


def test_missing_stdout_does_not_raise(sent_signals):
    """``process.stdout`` 是 None（没有管道）→ 不抛，继续走后面的阶段。"""
    proc = FakeProcess(exit_when="never")
    proc.stdout = None

    hard = coding_tool._cooperative_stop_process(proc, 10.0)

    assert hard is True
    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]


def test_broken_stdout_close_does_not_raise(sent_signals):
    """stdout 已经关过 / close 抛错 → 不抛，继续走后面的阶段。"""

    class _ExplodingStdout:
        def close(self):
            raise OSError("already closed")

    proc = FakeProcess(exit_when="never")
    proc.stdout = _ExplodingStdout()

    hard = coding_tool._cooperative_stop_process(proc, 10.0)

    assert hard is True
    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]


def test_tool_without_process_is_skipped(sent_signals):
    """登记了但当前没有子进程 → 不算数，也不碰任何信号。"""
    tool = FakeTool(FakeProcess(exit_when="stdout_closed"))
    tool._current_process = None
    coding_tool.register_live_dispatch(tool)

    assert coding_tool.stop_all_live_dispatches(10.0) == 0
    assert sent_signals == []


# ---------------------------------------------------------------------------
# The hooks in ``_run_claude_interactive`` / ``cleanup()``
# ---------------------------------------------------------------------------


def _make_mock_process(on_stdout):
    """A ``Popen`` stand-in whose stdout records the registry on read.

    The read loop consumes ``process.stdout`` line by line, which happens
    *inside* the dispatch — so this is where "was the tool registered
    while its child was running?" can be observed without racing the
    dispatch thread.
    """
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter(())
    proc.stderr.read.return_value = ""
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    proc.stdout = on_stdout()
    return proc


def test_dispatch_registers_the_tool_while_its_child_runs():
    """真的跑一次 dispatch：子进程在跑的时候登记在册，跑完就注销。"""
    observed = {}

    def _stdout():
        observed["during"] = list(coding_tool._LIVE_DISPATCH_TOOLS)
        yield json.dumps({
            "type": "system", "subtype": "init",
            "session_id": "00000000-0000-0000-0000-000000000000",
        }) + "\n"
        yield json.dumps({"type": "result", "result": "ok", "is_error": False}) + "\n"

    tool = ClaudeCodingTool()
    # Inherit mode: no provider lookup, no DB, no network — the dispatch
    # only needs a child to talk to.
    tool.inherit_providers = True
    proc = _make_mock_process(_stdout)

    with patch.object(
        coding_tool.subprocess, "Popen", side_effect=lambda cmd, **kw: proc
    ):
        tool._run_claude_interactive("hi")

    assert observed["during"] == [id(tool)], (
        "the tool must be registered while its dispatch is in flight — "
        "otherwise a stop cannot find the child"
    )
    assert list(coding_tool._LIVE_DISPATCH_TOOLS) == [], (
        "the tool must leave the registry once the dispatch is over"
    )


def test_cleanup_unregisters_the_tool():
    """``cleanup()`` 也注销 —— 它已经把子进程从 ``_current_process`` 拿走了。"""
    tool = ClaudeCodingTool()
    tool.inherit_providers = True
    coding_tool.register_live_dispatch(tool)

    tool.cleanup()

    assert list(coding_tool._LIVE_DISPATCH_TOOLS) == []


def test_failed_dispatch_leaves_no_stale_entry():
    """dispatch 中途抛异常（provider 报错）→ 子进程已被回收，登记表不留陈旧条目。

    这条路径走的是读循环的 ``finally``，不会到达 ``_current_process =
    None``；留在登记表里的工具会让下一次 stop 去停一个早就死了的子进程，
    并且把「被处理的进程数」报成一个假的数。
    """

    def _stdout():
        yield json.dumps({
            "type": "system", "subtype": "init",
            "session_id": "00000000-0000-0000-0000-000000000000",
        }) + "\n"
        raise RuntimeError("provider blew up mid-stream")

    tool = ClaudeCodingTool()
    tool.inherit_providers = True
    proc = _make_mock_process(_stdout)

    with patch.object(
        coding_tool.subprocess, "Popen", side_effect=lambda cmd, **kw: proc
    ):
        with pytest.raises(RuntimeError):
            tool._run_claude_interactive("hi")

    assert list(coding_tool._LIVE_DISPATCH_TOOLS) == []
    assert coding_tool.stop_all_live_dispatches(10.0) == 0


# ---------------------------------------------------------------------------
# The stop must not need the reader thread's lock
# ---------------------------------------------------------------------------

_SILENT_CHILD = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


def test_stop_survives_a_reader_thread_holding_the_pipe():
    """停止序列不能和读 stdout 的线程抢同一把锁。

    CPython 的 buffered reader 在 read 未返回期间一直持有自己的锁，
    而 dispatch 的读循环正是这样：它拿着那把锁阻塞在 read() 上，等子进程
    再写一行。子进程安静的时候（还没开口，或者正卡在别的地方），那个 read
    可以挂很久 —— 旧实现在另一个线程上做 ``stdout.close()``，close 要的正是
    那把锁，于是停止序列整个卡住：读循环等子进程，停止等读循环的锁，子进程
    等管道。停止序列耗完预算什么也没报，stop_report 的 ``children`` 是空的，
    而子进程其实还活着。

    这里用一个**不出声**的子进程复刻那个形状 —— 安静 ⇒ 读循环永远拿着那把
    锁；它也不理 SIGTERM，所以只有把管道打断或升级到 SIGKILL 才能收掉它。
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _SILENT_CHILD],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    def _reader():
        try:
            for _ in proc.stdout:  # noqa: B007 — the loop *is* the point
                pass
        except Exception:  # noqa: BLE001 — the pipe may break under us
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    # 让读循环真的进入那个阻塞的 read()，再动手停。
    time.sleep(0.5)

    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool)
    records: List[Dict[str, Any]] = []

    stopper = threading.Thread(
        target=coding_tool.stop_all_live_dispatches,
        args=(2.0, records),
        daemon=True,
    )
    stopper.start()
    # 预算 2s → 三段等待合计 1.4s；一个健康的停止远快于此。
    stopper.join(timeout=8.0)

    try:
        assert not stopper.is_alive(), "the stop sequence deadlocked"
        assert len(records) == 1
        assert records[0]["pid"] == proc.pid
        assert records[0]["stage"] in {
            coding_tool._STOP_STAGE_STDOUT,
            coding_tool._STOP_STAGE_SIGTERM,
            coding_tool._STOP_STAGE_SIGKILL,
        }
    finally:
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:  # noqa: BLE001 — the child may already be gone
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
