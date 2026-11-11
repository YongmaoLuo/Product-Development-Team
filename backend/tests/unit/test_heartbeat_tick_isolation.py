"""Heartbeat tick isolation: one plan's failure must not swallow the others.

``HeartbeatMonitor._check_once`` walks ``_verification_state`` and calls
``_lazy_check_verification(plan_id)`` once per plan.  Before the fix the
call had no per-plan guard, so a single plan that raised aborted the
whole loop: the plans *after* it were never checked in that tick.  The
outer ``HeartbeatMonitor._tick`` guard (``except Exception: pass``)
swallowed the error, so the loss of coverage was invisible — a stuck
plan stayed unchecked while the log said nothing.

These cases pin the isolation contract:

  - a plan that raises does not stop the plans after it from being
    checked in the same tick,
  - the failure is logged with the offending ``plan_id`` (and with the
    traceback attached, so the failure is not silent),
  - a tick in which nothing raises still checks every plan exactly once,
  - the idle-server early exit is unchanged: with all three registries
    empty, no lazy check runs at all.
"""

import logging
from unittest.mock import Mock, call

import pytest

import server
from server import HeartbeatMonitor


def _verification_record(plan_id: str) -> dict:
    """A minimal ``_verification_state`` entry — the shape the sweep reads."""
    return {
        "plan_id": plan_id,
        "verification_status": "running",
        "verification_round": 1,
        "verification_max_rounds": 3,
        "stop_reason": None,
    }


@pytest.fixture(autouse=True)
def _isolated_registries(monkeypatch):
    """Empty the three registries the sweep reads, and stub the other two
    lazy checks so each case observes the verification pass alone.

    The stubbing also keeps the cases hermetic: the real
    ``_lazy_check_sub_agents`` would read the process-wide sub-agent
    registry, whose contents depend on what other cases left behind.
    """
    monkeypatch.setattr(server, "_execution_state", {})
    monkeypatch.setattr(server, "_verification_state", {})

    execution_check = Mock()
    sub_agent_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_execution", execution_check)
    monkeypatch.setattr(server, "_lazy_check_sub_agents", sub_agent_check)

    for plan_id in list(server.sub_agent_registry._by_plan.keys()):
        server.sub_agent_registry.cleanup_for_plan(plan_id)

    yield


def test_first_plan_exception_does_not_skip_second(monkeypatch):
    """A raising plan must not prevent the plans after it from being checked.

    This is the regression: the exception used to abort the loop, so
    ``p2`` was never checked in that tick and nothing said so.
    """
    monkeypatch.setattr(
        server,
        "_verification_state",
        {"p1": _verification_record("p1"), "p2": _verification_record("p2")},
    )
    lazy_check = Mock(side_effect=[RuntimeError("boom"), None])
    monkeypatch.setattr(server, "_lazy_check_verification", lazy_check)

    # Must not raise: the failure belongs to p1, not to the tick.
    HeartbeatMonitor(interval=30)._check_once()

    assert lazy_check.call_args_list == [call("p1"), call("p2")], (
        "the plan after the failing one must still be checked in the "
        f"same tick; got {lazy_check.call_args_list!r}"
    )


def test_exception_is_logged_with_plan_id(monkeypatch, caplog):
    """The failure is logged with the offending plan_id and its traceback.

    Without this the isolation would trade a visible abort for a silent
    gap — the very failure mode the tick guard was hiding.
    """
    monkeypatch.setattr(
        server,
        "_verification_state",
        {"p1": _verification_record("p1"), "p2": _verification_record("p2")},
    )
    lazy_check = Mock(side_effect=[RuntimeError("boom"), None])
    monkeypatch.setattr(server, "_lazy_check_verification", lazy_check)

    with caplog.at_level(logging.ERROR, logger="server"):
        HeartbeatMonitor(interval=30)._check_once()

    error_records = [
        r for r in caplog.records if r.name == "server" and r.levelno >= logging.ERROR
    ]
    assert error_records, (
        "the failing plan must be logged; caplog captured "
        f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]!r}"
    )
    assert any("p1" in r.getMessage() for r in error_records), (
        "the log record must name the plan that failed; got "
        f"{[r.getMessage() for r in error_records]!r}"
    )
    assert any(r.exc_info for r in error_records), (
        "the log record must carry the traceback; got "
        f"{[(r.getMessage(), r.exc_info) for r in error_records]!r}"
    )


def test_all_plans_run_when_none_raise(monkeypatch):
    """A healthy tick still checks every plan exactly once."""
    monkeypatch.setattr(
        server,
        "_verification_state",
        {"p1": _verification_record("p1"), "p2": _verification_record("p2")},
    )
    lazy_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_verification", lazy_check)

    HeartbeatMonitor(interval=30)._check_once()

    assert lazy_check.call_args_list == [call("p1"), call("p2")]


def test_idle_server_early_exit_unchanged(monkeypatch):
    """With all three registries empty, no lazy check runs at all.

    The early exit is a deliberate cost optimisation (an idle server
    must not take three registry locks every 30s).  The per-plan
    isolation must not weaken it.
    """
    lazy_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_verification", lazy_check)
    execution_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_execution", execution_check)
    sub_agent_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_sub_agents", sub_agent_check)

    assert server._execution_state == {}
    assert server._verification_state == {}
    assert server.sub_agent_registry._by_plan == {}

    HeartbeatMonitor(interval=30)._check_once()

    assert lazy_check.call_count == 0
    assert execution_check.call_count == 0
    assert sub_agent_check.call_count == 0
