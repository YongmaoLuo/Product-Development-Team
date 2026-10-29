"""executor 停机：每个子进程的停止结果都落进 plan 事件流。

背景
----
停止序列对每个子进程都留下一条记录
（``{"pid": ..., "stage": ..., "forced": ..., "elapsed_s": ...}``），
但 executor 以前把它整个丢掉了：内存里只知道「停了几条」，进程一退出
就再也答不出「哪个 Claude Code 子进程停在哪一阶段」——那批结果随进程
一起没了，重启后无从查起。

这里钉住的是**逐子进程落盘**：

* 每个子进程一条 ``dispatch_stop_result`` 事件，四个字段齐全；
* 事件落进 plan 的 ``execution.log``（``<plans>/<plan>/execution.log``），
  executor 退出之后仍然读得到；
* 没有子进程 → 一条事件都不写（连日志文件都不该被创建）；
* 写日志炸了 → 吞掉，handler 照样退出 143；
* stage 原样落盘（``stdout`` / ``SIGTERM`` / ``SIGKILL``）；
* 没有 pid → data 里不带 pid 字段。

测试不碰真实进程：停止序列换成记录器。plan 事件流那条用**真的**
``ExecutionLogger``（写到临时目录），其余用记录器替身。
"""

from __future__ import annotations

import json
import logging
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cli
import coding_tool
import execution_logger


class FakePlanLogger:
    """plan 事件流 logger 的替身：记下每一次 ``info``。"""

    def __init__(self) -> None:
        self.calls = []

    def info(self, event, message, **kwargs):
        self.calls.append(
            {
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
            }
        )


def _install_fake_stop(monkeypatch, given):
    """把停止序列换成记录器：原样交回 ``given`` 这些记录。"""

    def fake_stop(seconds, records=None):
        if records is not None:
            records.extend(given)
        return len(given)

    monkeypatch.setattr(cli, "stop_all_live_dispatches", fake_stop)
    return fake_stop


@pytest.fixture
def plan_logger(monkeypatch):
    """把 plan 事件流换成记录器，返回它。"""
    fake = FakePlanLogger()
    monkeypatch.setattr(execution_logger, "get_logger", lambda *a, **k: fake)
    return fake


def test_each_child_writes_one_event(monkeypatch, plan_logger):
    """两个子进程 → 两条事件，一条对一个子进程，四个字段齐全。"""
    given = [
        {"pid": 76767, "stage": "SIGKILL", "forced": True, "elapsed_s": 1.02},
        {"pid": 76768, "stage": "SIGTERM", "forced": False, "elapsed_s": 0.4},
    ]
    _install_fake_stop(monkeypatch, given)

    cli._stop_live_dispatches(4.0)

    assert len(plan_logger.calls) == 2, "one event per stopped child"
    first, second = plan_logger.calls
    assert first["event"] == "dispatch_stop_result"
    assert first["message"] == "child pid=76767 stopped at SIGKILL"
    assert first["data"] == given[0]
    assert second["event"] == "dispatch_stop_result"
    assert second["message"] == "child pid=76768 stopped at SIGTERM"
    assert second["data"] == given[1]


def test_event_lands_in_the_plan_stream(monkeypatch, tmp_path):
    """事件真的落进 ``<plans>/<plan>/execution.log``，executor 退出后仍读得到。"""
    plans_dir = tmp_path / "plans"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_PLAN_ID", "plan-stop-events")
    _install_fake_stop(
        monkeypatch,
        [{"pid": 4242, "stage": "SIGKILL", "forced": True, "elapsed_s": 0.5}],
    )

    cli._stop_live_dispatches(4.0)

    log_file = plans_dir / "plan-stop-events" / "execution.log"
    assert log_file.exists(), "the plan event stream must carry the stop result"
    entries = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stops = [e for e in entries if e.get("event") == "dispatch_stop_result"]
    assert len(stops) == 1
    assert stops[0]["level"] == "INFO"
    assert stops[0]["message"] == "child pid=4242 stopped at SIGKILL"
    assert stops[0]["data"] == {
        "pid": 4242,
        "stage": "SIGKILL",
        "forced": True,
        "elapsed_s": 0.5,
    }


def test_no_children_writes_nothing(monkeypatch, tmp_path):
    """没有活跃 dispatch → 一条事件都不写（连 plan 的日志文件都不创建）。"""
    plans_dir = tmp_path / "plans"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_PLAN_ID", "plan-stop-empty")

    # The real stop sequence runs against an empty registry — that is
    # what "no live dispatch" means — and the records channel it was
    # given is watched on the way through.
    real_stop = cli.stop_all_live_dispatches
    seen = {}

    def spy(seconds, records=None):
        seen["records"] = records
        return real_stop(seconds, records=records)

    coding_tool._LIVE_DISPATCH_TOOLS.clear()
    try:
        monkeypatch.setattr(cli, "stop_all_live_dispatches", spy)
        cli._stop_live_dispatches(4.0)
    finally:
        coding_tool._LIVE_DISPATCH_TOOLS.clear()

    assert seen["records"] == [], "the per-child results must be asked for"
    log_file = plans_dir / "plan-stop-empty" / "execution.log"
    assert not log_file.exists(), "a stop with no children must write nothing"


def test_logger_failure_is_swallowed(monkeypatch):
    """写日志炸了 → 吞掉；handler 仍然退出 143。"""
    attempts = []

    class ExplodingLogger:
        def info(self, *args, **kwargs):
            attempts.append((args, kwargs))
            raise RuntimeError("the plan log is broken")

    monkeypatch.setattr(
        execution_logger, "get_logger", lambda *a, **k: ExplodingLogger()
    )
    _install_fake_stop(
        monkeypatch,
        [{"pid": 7, "stage": "SIGTERM", "forced": False, "elapsed_s": 0.2}],
    )

    cli._stop_live_dispatches(4.0)  # 不冒泡

    assert attempts, "the stop result must be written to the plan stream"

    exit_calls = []
    monkeypatch.setattr(cli, "_stop_in_progress", False)
    monkeypatch.setattr(cli, "_exit_now", exit_calls.append)

    cli._on_sigterm(signal.SIGTERM, None)

    assert exit_calls == [143], "a broken log must not keep the executor alive"


def test_stage_is_preserved_verbatim(monkeypatch, plan_logger):
    """stage 原样落盘：``stdout`` / ``SIGTERM`` / ``SIGKILL``，一个字都不改。"""
    stages = ["stdout", "SIGTERM", "SIGKILL"]
    given = [
        {
            "pid": 100 + i,
            "stage": stage,
            "forced": stage == "SIGKILL",
            "elapsed_s": 0.1,
        }
        for i, stage in enumerate(stages)
    ]
    _install_fake_stop(monkeypatch, given)

    cli._stop_live_dispatches(4.0)

    assert [call["data"]["stage"] for call in plan_logger.calls] == stages
    assert [call["message"] for call in plan_logger.calls] == [
        "child pid=100 stopped at stdout",
        "child pid=101 stopped at SIGTERM",
        "child pid=102 stopped at SIGKILL",
    ]


def test_missing_pid_is_omitted_from_data(monkeypatch, plan_logger):
    """pid 为 None → data 里不带 pid 字段，消息里也不带。"""
    _install_fake_stop(
        monkeypatch,
        [{"pid": None, "stage": "already_gone", "forced": False, "elapsed_s": 0.0}],
    )

    cli._stop_live_dispatches(4.0)

    assert len(plan_logger.calls) == 1
    data = plan_logger.calls[0]["data"]
    assert "pid" not in data, "a null pid must not be reported as a pid"
    assert data["stage"] == "already_gone"
    assert plan_logger.calls[0]["message"] == "child stopped at already_gone"


def test_without_plan_id_only_the_module_log_is_written(monkeypatch, caplog):
    """没有 ``PDT_PLAN_ID`` → plan 事件流没有，模块日志有，且不抛。"""
    monkeypatch.setattr(execution_logger, "get_logger", lambda *a, **k: None)
    _install_fake_stop(
        monkeypatch,
        [{"pid": 99, "stage": "SIGKILL", "forced": True, "elapsed_s": 0.9}],
    )

    with caplog.at_level(logging.INFO, logger=cli._stop_logger.name):
        cli._stop_live_dispatches(4.0)

    messages = [record.getMessage() for record in caplog.records]
    assert "child pid=99 stopped at SIGKILL" in messages
