"""2026-11-02 —— planning / judgment 两个阶段共用同一套心跳。

§4 实测：一次 ``coding_query_json`` 跑了 9993 秒，期间**零写入**。
planning 阶段（``generate_verification_plan`` 里那次调用）一行进度都没有；
judgment 阶段只有 ``_judgment_heartbeat`` 写的那一行，不是周期性的。
从外面看，"在等 provider" 和 "卡死了" 是同一件事 —— 看门狗只能按陈旧
阈值把一个健康的 round 判死。

这一轮把两个阶段接到同一套心跳上（``coding_tool._QueryProgressReporter``，
任务 9 建的），契约四条：

  * planning 的慢调用 → 日志里出现 ``phase="planning"`` 的进度行；
  * judgment 的慢调用 → ``phase="judgment"``；
  * 快调用 → 一行都不写，返回值与直接调 ``query_json`` 完全一致；
  * 调用抛错 → 心跳线程必须已经结束（``finally`` 里停）。

进度行落在 ``execution.log`` —— 和生产一致（看门狗读的三个文件之一就是
它），所以这里断言的是**日志文件里的字节**，不是 mock 的调用记录。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from execution_logger import ExecutionLogger
from verification_agent import VerificationAgent


#: The event name the reporter writes into the log.
PROGRESS_EVENT = "coding_query_progress"

#: The reporter's own lower bound for its interval (seconds).
FAST_INTERVAL = "1"

#: How long the fake provider takes to answer — long enough for exactly
#: one heartbeat to fire at ``FAST_INTERVAL``.
SLOW_CALL_SEC = 1.4

#: The name of the reporter's thread (``_QueryProgressReporter.THREAD_NAME``).
PROGRESS_THREAD = "pdt-query-progress"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _short_progress_interval(monkeypatch):
    """Shorten the reporter's interval to its lower bound.

    The reporter clamps its interval into ``[1 s, 120 s]`` and reads the
    operator's knob from the environment, so one second is the fastest a
    test can honestly go.
    """
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", FAST_INTERVAL)


def _plan_payload() -> dict:
    """A plan the planning phase accepts without a regeneration round."""
    return {
        "verification_points": [
            {
                "id": "VP-001",
                "title": "Verification point 1",
                "related_prd_criteria": "Relevant PRD criterion",
                "verification_method": "code_review",
                "priority": "high",
                "expected_result": "Expected outcome",
            }
        ]
    }


def _report_payload() -> dict:
    """A judgment the report phase accepts."""
    return {
        "overall_status": "PASSED",
        "summary": "all good",
        "verification_results": [
            {
                "id": "VP-001",
                "status": "PASSED",
                "actual_result": "ok",
                "evidence": "log line",
            }
        ],
        "requirement_deviations": [],
    }


def _execution_results() -> dict:
    return {
        "verification_points": [
            {
                "id": "VP-001",
                "title": "Verification point 1",
                "related_prd_criteria": "Relevant PRD criterion",
                "verification_method": "code_review",
                "priority": "high",
                "expected_result": "Expected outcome",
            }
        ],
        "execution_results": [
            {
                "id": "VP-001",
                "status": "PASSED",
                "actual_result": "ok",
                "evidence": "log line",
            }
        ],
    }


def _agent(tmp_path: Path, coding_tool) -> VerificationAgent:
    """A real agent whose round log lands in ``tmp_path``.

    The agent's logger is the ``ExecutionLogger`` that writes
    ``<plans_dir>/<plan_id>/execution.log`` — the same file production
    writes and the same file the watchdog reads.
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / "plan-heartbeat"
    project_dir = tmp_path / "proj"
    plan_dir.mkdir(parents=True)
    project_dir.mkdir()
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=coding_tool,
    )
    agent.logger = ExecutionLogger(plan_dir.name, plans_dir=plans_dir)
    return agent


def _progress_events(plan_dir: Path) -> list[dict]:
    """The heartbeat lines in the round log, in the order they landed."""
    log = plan_dir / "execution.log"
    if not log.exists():
        return []
    events: list[dict] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == PROGRESS_EVENT:
            events.append(entry)
    return events


def _alive_progress_threads() -> set:
    """Ids of the live heartbeat threads."""
    return {
        t.ident
        for t in threading.enumerate()
        if t.name == PROGRESS_THREAD and t.is_alive()
    }


# ---------------------------------------------------------------------------
# the four contracts
# ---------------------------------------------------------------------------

def test_planning_emits_heartbeat(tmp_path):
    """A planning call that waits must say so, periodically.

    Before this landed, the planning phase wrote NOTHING while the
    provider was thinking: a 9993-second dispatch looked exactly like a
    hung one from the outside.
    """
    tool = Mock()

    def _slow_query_json(**kwargs):
        time.sleep(SLOW_CALL_SEC)
        return _plan_payload()

    tool.query_json.side_effect = _slow_query_json

    agent = _agent(tmp_path, tool)
    plan = agent.generate_verification_plan(retry_llm=1)

    assert "verification_points" in plan, "the plan must still be generated"

    events = _progress_events(agent.plan_dir)
    assert events, (
        "the planning phase must write a heartbeat while it waits; "
        f"log: {agent.plan_dir / 'execution.log'}"
    )
    assert events[0]["data"]["phase"] == "planning"
    assert events[0]["data"]["elapsed_sec"] >= 1.0
    assert "[CODING_QUERY_PROGRESS]" in events[0]["message"]


def test_judgment_emits_heartbeat(tmp_path):
    """The judgment phase gets the same heartbeat, under its own name."""
    tool = Mock()

    def _slow_query_json(**kwargs):
        time.sleep(SLOW_CALL_SEC)
        return _report_payload()

    tool.query_json.side_effect = _slow_query_json

    agent = _agent(tmp_path, tool)
    agent.start_verification_round(1)
    report = agent.generate_verification_report(
        execution_results=_execution_results(),
        retry_llm=1,
    )

    assert report.get("verification_results"), "the report must still be built"

    events = _progress_events(agent.plan_dir)
    assert events, (
        "the judgment phase must write a heartbeat while it waits; "
        f"log: {agent.plan_dir / 'execution.log'}"
    )
    assert events[0]["data"]["phase"] == "judgment"
    assert events[0]["data"]["elapsed_sec"] >= 1.0


def test_fast_query_no_event(tmp_path):
    """A call that returns inside one interval writes nothing.

    The heartbeat is a side channel: it must not add noise to the log of
    a run whose queries answer immediately, and the return value must be
    exactly what ``query_json`` returned.
    """
    payload = {"answer": 42}
    tool = Mock()
    seen: dict = {}

    def _instant_query_json(**kwargs):
        seen["threads"] = _alive_progress_threads()
        return payload

    tool.query_json.side_effect = _instant_query_json

    agent = _agent(tmp_path, tool)
    before = _alive_progress_threads()

    result = agent._query_with_heartbeat(
        prompt="p", system_instruction="s", phase="planning",
    )

    assert result == payload, "the return value must pass straight through"
    assert seen["threads"] - before, (
        "the heartbeat must have been running during the call"
    )
    assert _progress_events(agent.plan_dir) == [], (
        "a query shorter than one interval must not write a heartbeat"
    )
    assert _alive_progress_threads() <= before, (
        "the heartbeat thread must be gone once the call returned"
    )


def test_exception_stops_reporter(tmp_path):
    """A failed call must not leave its heartbeat running.

    A heartbeat that outlives its dispatch keeps writing into the NEXT
    dispatch's log, so the reporter is stopped in a ``finally``.
    """
    tool = Mock()
    seen: dict = {}

    def _boom(**kwargs):
        seen["threads"] = _alive_progress_threads()
        raise RuntimeError("provider down")

    tool.query_json.side_effect = _boom

    agent = _agent(tmp_path, tool)
    before = _alive_progress_threads()

    with pytest.raises(RuntimeError):
        agent._query_with_heartbeat(
            prompt="p", system_instruction="s", phase="planning",
        )

    assert seen["threads"] - before, (
        "the heartbeat must have been running during the call"
    )
    assert _alive_progress_threads() <= before, (
        "the heartbeat thread must be gone after the call raised"
    )
