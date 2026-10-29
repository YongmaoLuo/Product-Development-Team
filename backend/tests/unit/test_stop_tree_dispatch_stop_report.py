"""单进程停止报告：协作式停机必须能说出「这个子进程停在哪一阶段」。

背景
----
``_cooperative_stop_process`` 只返回 bool（是否被迫 SIGKILL）。R4 要求
**每个** Claude Code 子进程的停止结果可落盘，光有一个 bool 不够 —— 落盘
之后没人能回答「它是在关 stdout 时自己走的，还是被 SIGKILL 弄死的」。

于是停止路径需要一个带阶段的结构化报告：

    {"pid": 1234, "stage": "SIGTERM", "forced": False, "elapsed_s": 0.831}

``stage`` 取值固定为 ``already_gone`` / ``stdout`` / ``SIGTERM`` / ``SIGKILL``。

原来的 bool 契约**不能破**：``tests/unit/test_cooperative_stop_sequence.py``
依赖它，而这里的 ``test_bool_wrapper_keeps_contract`` 把同一个契约在新
文件里重新钉一遍 —— 那条老用例只是「不再作为门禁命令」，不是被废掉了。

测试不碰真实进程：假 process 按「什么时候才肯退出」来回答 ``wait``，
假 ``kill_process_group`` 记录信号。断言落在**阶段名**上，而不是函数内部
的计数器上。
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool


class _FakeStdout:
    """``process.stdout`` 的替身：记录自己被关掉了没有。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    """``subprocess.Popen`` 的最小替身。

    ``exit_on`` 决定 ``wait`` 什么时候才肯返回（也就是子进程「什么时候
    才肯退出」）：

    * ``"stdout_closed"`` —— stdout 被关掉就自己退出；
    * ``"sigterm"`` —— 只在收到 SIGTERM 之后退出；
    * ``"sigkill"`` —— 只在收到 SIGKILL 之后退出。

    ``already_gone=True`` 模拟「停它的时候它早就死了」。
    """

    def __init__(
        self,
        *,
        exit_on: str = "stdout_closed",
        pid: int = 424242,
        already_gone: bool = False,
    ) -> None:
        self.pid = pid
        self.stdout = _FakeStdout()
        self.exit_on = exit_on
        self.signals: List[int] = []
        self.wait_timeouts: List[Optional[float]] = []
        self._gone = already_gone

    def poll(self):
        return 0 if self._gone else None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self._gone:
            return 0
        if self.exit_on == "stdout_closed" and self.stdout.closed:
            self._gone = True
            return 0
        if self.exit_on == "sigterm" and signal.SIGTERM in self.signals:
            self._gone = True
            return 0
        if self.exit_on == "sigkill" and signal.SIGKILL in self.signals:
            self._gone = True
            return 0
        raise subprocess.TimeoutExpired(cmd="fake-claude", timeout=timeout)


@pytest.fixture
def sent_signals(monkeypatch) -> List[int]:
    """把 ``kill_process_group`` 换成记录器，返回收到的信号列表。"""
    sent: List[int] = []

    def _fake_kill_process_group(proc, *, sig=signal.SIGKILL, wait_timeout=5.0):
        sent.append(sig)
        # 同时喂给假进程 —— 它要靠信号才知道自己该不该退出。
        signals = getattr(proc, "signals", None)
        if isinstance(signals, list):
            signals.append(sig)
        return None

    monkeypatch.setattr(coding_tool, "kill_process_group", _fake_kill_process_group)
    return sent


def test_reports_stdout_stage(sent_signals):
    """stdout 一关子进程就自己走 → 阶段是 stdout，没被逼到发信号。"""
    proc = _FakeProcess(exit_on="stdout_closed")

    report = coding_tool._cooperative_stop_process_report(proc, total_seconds=4.0)

    assert report["stage"] == "stdout"
    assert report["forced"] is False
    assert sent_signals == []
    assert report["pid"] == proc.pid


def test_reports_sigterm_stage(sent_signals):
    """stdout 关了它不走，SIGTERM 才走 → 阶段是 SIGTERM，仍然不是硬杀。"""
    proc = _FakeProcess(exit_on="sigterm")

    report = coding_tool._cooperative_stop_process_report(proc, total_seconds=4.0)

    assert report["stage"] == "SIGTERM"
    assert report["forced"] is False
    assert sent_signals == [signal.SIGTERM]


def test_reports_sigkill_stage(sent_signals):
    """只有 SIGKILL 才走 → 阶段是 SIGKILL，且 forced 为 True。"""
    proc = _FakeProcess(exit_on="sigkill")

    report = coding_tool._cooperative_stop_process_report(proc, total_seconds=4.0)

    assert report["stage"] == "SIGKILL"
    assert report["forced"] is True
    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]


def test_already_exited_reports_gone(sent_signals):
    """开工前就已经死了 → 阶段是 already_gone，一个信号都不发。"""
    proc = _FakeProcess(already_gone=True)

    report = coding_tool._cooperative_stop_process_report(proc, total_seconds=4.0)

    assert report["stage"] == "already_gone"
    assert report["forced"] is False
    assert sent_signals == []
    # 已经没了的进程不该再被碰一下（连 stdout 都不关）。
    assert proc.stdout.closed is False


def test_bool_wrapper_keeps_contract(sent_signals):
    """bool 契约不变：协作式停机 False，被迫硬杀 True。

    只断言「旧行为还在」不够 —— 那样它在改实现之前就是绿的，证明不了任何
    东西。所以这里同时断言 bool **由带阶段的报告派生**：同一份输入下，
    bool 与报告的 ``forced`` 必须说同一件事。改实现之前没有报告，这条
    断言必然红。

    注意 legacy bool 的方向：``False`` = 没有被迫硬杀（协作式停机），
    ``True`` = 只能靠 SIGKILL 收场 —— 也就是报告里的 ``forced``。
    """
    for exit_on, expected in (("stdout_closed", False), ("sigkill", True)):
        # 两个假进程：同一个配置，但报告与 wrapper 各用一个 ——
        # 第一个跑完就「死了」，再问它会得到 already_gone，问不出阶段。
        report_proc = _FakeProcess(exit_on=exit_on)
        report = coding_tool._cooperative_stop_process_report(report_proc, 4.0)
        bool_proc = _FakeProcess(exit_on=exit_on)
        hard = coding_tool._cooperative_stop_process(bool_proc, 4.0)

        assert hard is expected
        assert hard is report["forced"]


def test_elapsed_is_reported_in_milliseconds(sent_signals):
    """elapsed_s 是 monotonic 差值，保留 3 位小数 —— 落盘要能直接读。"""
    proc = _FakeProcess(exit_on="stdout_closed")

    report = coding_tool._cooperative_stop_process_report(proc, total_seconds=4.0)

    assert isinstance(report["elapsed_s"], float)
    assert report["elapsed_s"] >= 0.0
    assert report["elapsed_s"] == round(report["elapsed_s"], 3)
    # 报告的字段是固定的四个 —— 落盘的 schema 不能随实现漂。
    assert set(report) == {"pid", "stage", "forced", "elapsed_s"}
