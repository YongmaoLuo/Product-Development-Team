"""Every scheduler decision leaves a trace in the log.

Background
----------
A plan executor that is stopped leaves the operator with one question:
who stopped it, and why.  The scheduler answers that question in a
**list in memory** (``engine._tick_decisions``) and nowhere else.  The
list is read by the panel while the process lives, and it is gone when
the process ends -- so after the fact there is a stack of executors
that stopped and no written reason for any of them.

That is a property of the code, not of one run: any reader of
``engine._decide`` sees that it appends a record and returns.  So the
fix is the same shape as the defect -- the decision is written down
where it survives.

These cases pin the other half of the answer: the scheduler says what
it decides, in the log, in a form that can be read after the fact.

* a decision writes one INFO record naming the action, the plan and
  the reason;
* the convergence over capacity says which plans it interrupted, in a
  WARNING, so the operator can see the eviction itself and not only
  the individual decisions.

The tests call the private methods on purpose: the point of the case
is the record that comes out, and the decision path is reachable from
the tick without a database, a service or a worker thread.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


def _engine() -> Any:
    """Build an engine with placeholder collaborators.

    The decision path reads nothing from the facts, the ledger, the
    command executor or the sink: it appends a record and writes a
    log line.  Placeholders are therefore enough, and the case stays
    free of a database and a worker thread.
    """
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine

    engine = PlanSchedulerEngine(
        config_loader=_ConfigLoader(SchedulerConfig()),
        store=object(),
        facts=object(),
        ledger=object(),
        commands=object(),
        sink=object(),
        conn=sqlite3.connect(":memory:"),
    )
    engine._config = engine._read_config()
    return engine


def _running(plan_id: str, created_at: float, elapsed: float) -> Dict[str, Any]:
    """Build one running entry, the way the policy reads it."""
    return {
        "plan_id": plan_id,
        "created_at": created_at,
        "acquired_at": created_at,
        "elapsed": elapsed,
    }


def _over_capacity_state() -> Dict[str, Any]:
    """Build a state that is one plan over its capacity.

    Two plans hold a slot and there is room for one, so exactly one of
    them is interrupted.  The younger one (``plan-younger``, created
    last) is the one the policy picks.
    """
    return {
        "capacity": 1,
        "used": 2,
        "min_runtime_seconds": 0,
        "running": [
            _running("plan-older", 1_000.0, 900.0),
            _running("plan-younger", 2_000.0, 900.0),
        ],
    }


# ---------------------------------------------------------------------------
# 1. A decision writes a log record
# ---------------------------------------------------------------------------


def test_decide_writes_one_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """One decision leaves one INFO record naming action, plan and reason.

    The record is the attribution: it says what the scheduler did, to
    whom and why.  The three fields are all of it -- the log carries
    no internal object, so a line can be read without a session.
    """
    engine = _engine()

    with caplog.at_level(logging.INFO, logger="plan_scheduler.engine"):
        engine._decide("stop", "plan-42", "over_capacity")

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("scheduler decision:")
    ]
    assert len(records) == 1, (
        "one decision must leave one record: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    message = records[0].getMessage()
    assert message == (
        "scheduler decision: action=stop plan_id=plan-42 reason=over_capacity"
    ), message
    assert records[0].levelno == logging.INFO, (
        f"a decision is not a warning: {records[0].levelname}"
    )


def test_decide_does_not_change_the_decision_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log is added beside the decision, not instead of it.

    The list the panel reads is the record of what happened; the log
    is the copy that survives the process.  The second must not
    replace the first.
    """
    engine = _engine()

    with caplog.at_level(logging.INFO, logger="plan_scheduler.engine"):
        engine._decide("admit", "plan-7", "running")

    assert len(engine._tick_decisions) == 1, engine._tick_decisions
    decision = engine._tick_decisions[0]
    assert decision["action"] == "admit", decision
    assert decision["plan_id"] == "plan-7", decision
    assert decision["reason"] == "running", decision
    assert set(decision) == {"ts", "action", "plan_id", "reason"}, decision


# ---------------------------------------------------------------------------
# 2. The convergence over capacity says what it interrupted
# ---------------------------------------------------------------------------


def test_converge_over_capacity_warns_about_the_victims(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A convergence that interrupts somebody says which ones.

    The individual decisions are INFO lines; the eviction itself is a
    WARNING, because it is the event an operator on call has to see
    and it is the one that answers "why did that plan stop".
    """
    engine = _engine()

    with caplog.at_level(logging.INFO, logger="plan_scheduler.engine"):
        victims = engine._converge_over_capacity(_over_capacity_state())

    assert victims == ["plan-younger"], (
        f"the policy picks the youngest: {victims!r}"
    )
    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1, (
        "one convergence, one warning: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    message = warnings[0].getMessage()
    assert "plan-younger" in message, message
    assert "plan-older" not in message, (
        f"only the interrupted plan is named: {message}"
    )


def test_converge_over_capacity_warns_nothing_when_it_fits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A scheduler inside its limit says nothing about an eviction.

    There was no eviction, so there is nothing to warn about.
    """
    engine = _engine()
    state = _over_capacity_state()
    state["used"] = 1

    with caplog.at_level(logging.INFO, logger="plan_scheduler.engine"):
        victims = engine._converge_over_capacity(state)

    assert victims == []
    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert warnings == [], (
        f"no eviction, no warning: {[r.getMessage() for r in warnings]!r}"
    )


def test_converge_over_capacity_decisions_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every victim of a convergence leaves a decision record too.

    The convergence is the path that stops a plan, so it is the path
    that has to be attributable: one INFO record per victim, each
    naming the plan and the reason.
    """
    engine = _engine()

    with caplog.at_level(logging.INFO, logger="plan_scheduler.engine"):
        engine._converge_over_capacity(_over_capacity_state())

    messages: List[str] = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("scheduler decision:")
    ]
    assert messages == [
        "scheduler decision: action=stop plan_id=plan-younger reason=over_capacity"
    ], messages


# ---------------------------------------------------------------------------
# 3. The tick is the production entry, and it says what it decided
# ---------------------------------------------------------------------------


class _StaticConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, **settings: Any) -> None:
        from plan_scheduler.config import SchedulerConfig

        self._config = SchedulerConfig(**settings)

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


class _Facts:
    """The plan facts, as a case writes them down."""

    def __init__(self, running: List[Dict[str, Any]]) -> None:
        self.running = list(running)

    def queued_plans(self) -> List[Dict[str, Any]]:
        """Nobody is waiting."""
        return []

    def active_plans(self) -> List[Dict[str, Any]]:
        """The plans that are working."""
        return list(self.running)

    def has_pending_decision(self, plan_id: str) -> bool:
        """No plan of the case's is parked in front of a human."""
        return False

    def workspace_key(self, plan_id: str) -> str:
        """The directory a plan works in."""
        return "workspace-{0}".format(plan_id)


class _Store:
    """The scheduler's own store, as a case writes it down."""

    def __init__(self, slots: List[Dict[str, Any]]) -> None:
        self.slots = list(slots)

    def list_queue(self, conn: Any) -> List[Dict[str, Any]]:
        """The line is empty."""
        return []

    def list_slots(self, conn: Any) -> List[Dict[str, Any]]:
        """The slots that are taken."""
        return list(self.slots)

    def get_setting(self, conn: Any, key: str) -> Any:
        """No capacity override is in force."""
        return None

    def remove(self, conn: Any, plan_id: str) -> None:
        """Take a plan out of the line."""
        return None

    def enqueue(self, conn: Any, plan_id: str, priority: int = 0) -> None:
        """Put a plan in the line."""
        return None

    def upsert_slot(self, conn: Any, plan_id: str, stamp: str) -> None:
        """Give a plan its slot."""
        return None

    def delete_slot(self, conn: Any, plan_id: str) -> None:
        """Give a plan's slot back."""
        return None

    def transaction(self, conn: Any) -> Any:
        """A transaction that commits nothing."""
        from contextlib import nullcontext

        return nullcontext()


class _Ledger:
    """The capacity ledger, as a case writes it down."""

    def __init__(self, used: int) -> None:
        self._used = int(used)

    def used(self) -> int:
        """How many slots are taken."""
        return self._used

    def reconcile(self) -> None:
        """Bring the slots in line with the facts."""
        return None

    def workspace_busy(self, key: Any) -> bool:
        """No workspace of the case's is taken."""
        return False


class _Sink:
    """The exit the tick's decisions go through."""

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Remember the decisions and hand back what they produced."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return batch


def test_the_tick_says_what_it_decided(caplog: pytest.LogCaptureFixture) -> None:
    """A tick that stops a plan leaves the reason in the log.

    This is the case that pins the whole chain: the tick is the
    production entry, the converge phase is one of its phases, and the
    phase calls ``_decide``.  If the log line were written somewhere
    else, or the phase did not reach the decision, this case would
    see no record and fail.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    now = 1_700_000_000.0
    store = _Store(
        [
            {"plan_id": "plan-older", "acquired_at": now - 200.0},
            {"plan_id": "plan-younger", "acquired_at": now - 100.0},
        ]
    )
    facts = _Facts(
        [{"plan_id": "plan-older"}, {"plan_id": "plan-younger"}]
    )
    engine = PlanSchedulerEngine(
        config_loader=_StaticConfigLoader(
            enabled=True,
            capacity=1,
            min_runtime_seconds=0.0,
            tick_seconds=0.01,
        ),
        store=store,
        facts=facts,
        ledger=_Ledger(used=2),
        commands=object(),
        sink=_Sink(),
        clock=lambda: now,
        conn=sqlite3.connect(":memory:"),
    )

    with caplog.at_level(logging.INFO, logger="plan_scheduler.engine"):
        engine._run_tick()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("scheduler decision:")
    ]
    assert messages == [
        "scheduler decision: action=stop plan_id=plan-younger reason=over_capacity"
    ], messages

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "over its capacity" in record.getMessage()
    ]
    assert len(warnings) == 1, warnings
    assert "plan-younger" in warnings[0], warnings[0]
