"""Heartbeat tick isolation across the sweep's three passes.

``HeartbeatMonitor._check_once`` runs three sweeps in one tick:

  1. the execution sweep — ``_lazy_check_execution(plan_id)`` per plan,
  2. the verification sweep — ``_lazy_check_verification(plan_id)`` per
     plan,
  3. the sub-agent sweep — ``_lazy_check_sub_agents()`` once.

Only the verification sweep had a per-plan guard (added in ``aae121c0``).
The other two were bare calls, so a raising execution plan aborted the
tick **before** the verification sweep ran — every verification plan
went unchecked that tick — and a raising sub-agent sweep aborted the
tick after both per-plan sweeps.  ``HeartbeatMonitor._tick``'s
``except Exception: pass`` swallowed both, so the lost coverage was
invisible: a plan could stay unchecked indefinitely while the watchdog
still reported a healthy sweep.

These cases pin the contract for the whole tick:

  - a raising execution plan does not skip the execution plans after it,
  - a raising execution plan does not skip the verification sweep,
  - a raising sub-agent sweep does not escape the tick, and the two
    per-plan sweeps before it have already run,
  - the verification sweep's per-plan isolation is unchanged,
  - the idle-server early exit is unchanged: with all three registries
    empty, none of the three sweeps runs.
"""

import logging
from unittest.mock import Mock, call

import pytest

import server
from server import HeartbeatMonitor


def _execution_record(plan_id: str) -> dict:
    """A minimal ``_execution_state`` entry — the shape the sweep reads."""
    return {
        "plan_id": plan_id,
        "status": "running",
        "pid": None,
        "ended_at": None,
    }


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
    """Empty the three registries the sweep reads, and stub the sweeps.

    The registry stubbing keeps the cases hermetic: the real
    ``_lazy_check_sub_agents`` would read the process-wide sub-agent
    registry, whose contents depend on what other cases left behind.
    """
    monkeypatch.setattr(server, "_execution_state", {})
    monkeypatch.setattr(server, "_verification_state", {})

    for plan_id in list(server.sub_agent_registry._by_plan.keys()):
        server.sub_agent_registry.cleanup_for_plan(plan_id)

    yield


def test_first_execution_plan_exception_does_not_skip_second(monkeypatch, caplog):
    """A raising execution plan must not skip the execution plans after it.

    This is the regression: the exception aborted the loop, so ``e2`` was
    never checked in that tick and nothing said so.
    """
    monkeypatch.setattr(
        server,
        "_execution_state",
        {"e1": _execution_record("e1"), "e2": _execution_record("e2")},
    )
    execution_check = Mock(side_effect=[RuntimeError("boom"), None])
    monkeypatch.setattr(server, "_lazy_check_execution", execution_check)

    with caplog.at_level(logging.ERROR, logger="server"):
        # Must not raise: the failure belongs to e1, not to the tick.
        HeartbeatMonitor(interval=30)._check_once()

    assert execution_check.call_args_list == [call("e1"), call("e2")], (
        "the execution plan after the failing one must still be checked in "
        f"the same tick; got {execution_check.call_args_list!r}"
    )

    # The failure is named: an unnamed failure would be a silent gap.
    error_messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "server" and r.levelno >= logging.ERROR
    ]
    assert any("e1" in m for m in error_messages), (
        "the log record must name the execution plan that failed; got "
        f"{error_messages!r}"
    )


def test_execution_failure_does_not_skip_verification_pass(monkeypatch):
    """A raising execution plan must not abort the tick before the
    verification sweep.

    The verification sweep is a *separate* pass: an execution plan that
    blows up says nothing about the verification plans, so they must
    still be checked in the same tick.
    """
    monkeypatch.setattr(
        server,
        "_execution_state",
        {"e1": _execution_record("e1"), "e2": _execution_record("e2")},
    )
    monkeypatch.setattr(
        server,
        "_verification_state",
        {"v1": _verification_record("v1")},
    )
    execution_check = Mock(side_effect=[RuntimeError("boom"), None])
    verification_check = Mock()
    sub_agent_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_execution", execution_check)
    monkeypatch.setattr(server, "_lazy_check_verification", verification_check)
    monkeypatch.setattr(server, "_lazy_check_sub_agents", sub_agent_check)

    HeartbeatMonitor(interval=30)._check_once()

    assert verification_check.call_args_list == [call("v1")], (
        "the verification sweep must still run after an execution plan "
        f"failed; got {verification_check.call_args_list!r}"
    )
    assert sub_agent_check.call_count == 1, (
        "the sub-agent sweep must still run after an execution plan failed; "
        f"got {sub_agent_check.call_count} call(s)"
    )


def test_sub_agent_failure_does_not_escape_the_tick(monkeypatch, caplog):
    """A raising sub-agent sweep is logged and swallowed.

    It is the tick's last pass, so nothing in this tick is lost — but
    the exception must not escape into ``_tick``'s bare
    ``except Exception: pass``, or the failure would leave no trace.
    """
    monkeypatch.setattr(
        server,
        "_execution_state",
        {"e1": _execution_record("e1")},
    )
    monkeypatch.setattr(
        server,
        "_verification_state",
        {"v1": _verification_record("v1")},
    )
    execution_check = Mock()
    verification_check = Mock()
    sub_agent_check = Mock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(server, "_lazy_check_execution", execution_check)
    monkeypatch.setattr(server, "_lazy_check_verification", verification_check)
    monkeypatch.setattr(server, "_lazy_check_sub_agents", sub_agent_check)

    with caplog.at_level(logging.ERROR, logger="server"):
        # Must not raise out of _check_once.
        HeartbeatMonitor(interval=30)._check_once()

    # The two per-plan sweeps ran before the sub-agent pass.
    assert execution_check.call_args_list == [call("e1")]
    assert verification_check.call_args_list == [call("v1")]

    error_messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "server" and r.levelno >= logging.ERROR
    ]
    assert error_messages, (
        "the sub-agent sweep failure must be logged; caplog captured "
        f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]!r}"
    )
    # The sub-agent sweep has no per-plan granularity, so its record
    # carries no plan_id — one failing pass must not be attributed to
    # some plan.
    assert not any("plan_id" in m for m in error_messages), (
        "the sub-agent sweep failure must not name a plan; got "
        f"{error_messages!r}"
    )


def test_existing_verification_isolation_unchanged(monkeypatch):
    """The verification sweep's per-plan isolation is not regressed.

    Added by the follow-up task that widened the isolation to the whole
    tick: the earlier fix (``aae121c0``) put the per-plan guard on the
    verification sweep, and this pins it against a later change to the
    other two sweeps silently undoing it.
    """
    monkeypatch.setattr(
        server,
        "_verification_state",
        {"v1": _verification_record("v1"), "v2": _verification_record("v2")},
    )
    verification_check = Mock(side_effect=[RuntimeError("boom"), None])
    monkeypatch.setattr(server, "_lazy_check_verification", verification_check)

    HeartbeatMonitor(interval=30)._check_once()

    assert verification_check.call_args_list == [call("v1"), call("v2")], (
        "the verification plan after the failing one must still be checked "
        f"in the same tick; got {verification_check.call_args_list!r}"
    )


def test_idle_server_early_exit_unchanged(monkeypatch):
    """With all three registries empty, none of the three sweeps runs.

    The early exit is a deliberate cost optimisation (an idle server
    must not take three registry locks every 30s).  The per-pass
    isolation must not weaken it.
    """
    execution_check = Mock()
    verification_check = Mock()
    sub_agent_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_execution", execution_check)
    monkeypatch.setattr(server, "_lazy_check_verification", verification_check)
    monkeypatch.setattr(server, "_lazy_check_sub_agents", sub_agent_check)

    assert server._execution_state == {}
    assert server._verification_state == {}
    assert server.sub_agent_registry._by_plan == {}

    HeartbeatMonitor(interval=30)._check_once()

    assert execution_check.call_count == 0
    assert verification_check.call_count == 0
    assert sub_agent_check.call_count == 0
