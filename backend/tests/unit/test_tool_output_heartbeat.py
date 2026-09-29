"""2026-09-14 工具输出心跳 —— 活的 pytest 不再被当成"沉默"。

思路：一个活着的 pytest 即使在很长的时间里（超过 15 分钟）也会持续输出
stdout，例如进度条。只要把这些输出写进 subagent 自带的 log，就能消掉
"没有输出而超过 15 分钟被判死"这个情况。

代码里本来就有半个基础设施: ``coding_tool`` 的 stdout 读循环每收到一行
就更新 ``_last_output_ts``（``coding_tool.py`` READ LOOP）。但那个时间戳
只被注册表看门狗的第二机会检查用到，**从不写进 plan 目录的日志** ——
而验证看门狗恰恰按 plan 目录日志的 mtime 判活。于是 22 分钟、进度条一直
在刷的 pytest 被判成"沉默"，round 被盖 ``verification_log_stale``。

本文件钉住补上的那一半:
  * ``_pump_tool_output`` 在工具调用期间采样 ``_last_output_ts``，**只在
    有新输出时**写 ``tool_output_heartbeat`` 事件（刷新 attempt log mtime，
    顺带刷新 registry handle，并留下最后一行供人看）；
  * 没有新输出时不写 —— 真卡死的工具仍然该被看门狗抓住；
  * 采样/写日志任何异常都不得干扰 VP。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from verification_subagent import VerificationSubAgent


class _FakeTool:
    def __init__(self):
        self._last_output_ts = None
        self._last_output_line = ""


def _agent(interval: float = 0.05) -> VerificationSubAgent:
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    sub.OUTPUT_HEARTBEAT_INTERVAL_SEC = interval
    sub.registry = None
    return sub


def _beats(log_path: Path) -> list:
    out = []
    if not log_path.exists():
        return out
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "tool_output_heartbeat":
            out.append(entry["data"])
    return out


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """有界轮询，替代固定 ``time.sleep`` 窗口。

    泵跑在自己的线程上，所以"睡 150ms 再断言"其实是**对调度器下注**。
    验证阶段的 VP（全量 pytest）日常把 load average 顶到 10 以上，此时泵线程
    可能整个 150ms 窗口都拿不到 CPU —— 2026-09-15 实测同一份代码连跑 10 次
    有 1 次挂在这个断言上。改成轮询条件、给足上限，就与负载解耦了。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_pump_writes_heartbeat_when_output_advances(tmp_path):
    sub = _agent()
    tool = _FakeTool()
    log_path = tmp_path / "vp.log"
    stop = threading.Event()
    thread = threading.Thread(
        target=sub._pump_tool_output,
        args=("VP-023", tool, log_path, None, stop),
        daemon=True,
    )

    # First sample: nothing streamed yet → no heartbeat.
    thread.start()
    time.sleep(0.12)
    assert _beats(log_path) == [], "no stdout → no heartbeat"

    # Now the tool streams (a pytest progress line).
    tool._last_output_ts = time.monotonic()
    tool._last_output_line = "tests/test_x.py ....   [ 58%]"
    assert _wait_for(lambda: _beats(log_path)), (
        "streaming output must produce a heartbeat"
    )
    beats = _beats(log_path)
    assert beats[-1]["vp_id"] == "VP-023"
    assert "58%" in beats[-1]["last_line"]

    # Same timestamp again → silent (idempotent on the ts value).
    # 负向断言不能靠短睡眠，否则它测的是"泵这一轮抢到 CPU 了没有"。
    # 先用 _wait_for 确保泵已经把这一拍处理完，再看窗口内不再增长。
    _wait_for(lambda: len(_beats(log_path)) >= len(beats), timeout=1.0)
    settled = len(_beats(log_path))
    time.sleep(0.2)
    assert len(_beats(log_path)) == settled, (
        "re-sampling an unchanged _last_output_ts must not re-log"
    )

    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_pump_stops_promptly(tmp_path):
    sub = _agent(interval=5.0)
    stop = threading.Event()
    thread = threading.Thread(
        target=sub._pump_tool_output,
        args=("VP-1", _FakeTool(), tmp_path / "vp.log", None, stop),
        daemon=True,
    )
    thread.start()
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive(), "the pump must exit as soon as stop is set"


def test_pump_refreshes_registry_progress(tmp_path):
    sub = _agent()
    calls = []

    class _Registry:
        def mark_progress(self, plan_id, handle, stage=""):
            calls.append((plan_id, stage))

    sub.registry = _Registry()
    sub.plan_id = "plan-x"
    tool = _FakeTool()
    tool._last_output_ts = time.monotonic()
    stop = threading.Event()
    thread = threading.Thread(
        target=sub._pump_tool_output,
        args=("VP-9", tool, tmp_path / "vp.log", object(), stop),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: ("plan-x", "tool_streaming") in calls), (
        "the pump must publish streaming progress to the registry"
    )
    stop.set()
    thread.join(timeout=2)


def test_pump_swallows_tool_errors(tmp_path):
    """A tool object that raises on attribute access must not kill the
    pump thread (and definitely not the VP)."""
    class _Exploding:
        @property
        def _last_output_ts(self):
            raise RuntimeError("boom")

    stop = threading.Event()
    thread = threading.Thread(
        target=_agent()._pump_tool_output,
        args=("VP-1", _Exploding(), tmp_path / "vp.log", None, stop),
        daemon=True,
    )
    thread.start()
    time.sleep(0.12)
    stop.set()
    thread.join(timeout=2)


def test_tool_records_last_output_line():
    """coding_tool stores the streaming line itself, not just its
    timestamp — the heartbeat carries operator-visible progress."""
    import inspect
    import coding_tool

    src = inspect.getsource(coding_tool)
    assert "self._last_output_line = line[:400]" in src, (
        "the stdout read loop must store the last line for the heartbeat"
    )
