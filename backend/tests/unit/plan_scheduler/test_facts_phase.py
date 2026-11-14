"""The queued and the running rows carry the plan's phase.

Background
----------
The scheduler's panel shows a line of plans: the ones waiting and the
ones working.  What each of those plans is *doing* was not on the row
— ``queued_plans()`` answered ``plan_id``, ``priority``, ``seq`` and
``due``, and ``active_plans()`` answered ``plan_id``, ``side``,
``workspace`` and ``started_at``.  A panel that says "running" without
saying *what* is running says the same word for a plan that is
executing and for one that is repairing.

The phase is already written down — it is the plan's own
``plan_routing.current_phase`` — and the facts reader already knows how
to read it (``PlanFactsReader._phase()``).  What was missing was the
last hop: the row the panel receives has to carry it.

These cases pin that hop:

* a queued row carries the phase the routing row holds;
* an active row carries the phase the routing row holds;
* a phase nobody has a label for is passed through as it is — the
  label is the routing layer's job, not the facts layer's;
* a plan with no routing row says ``None`` rather than raising — the
  reader has no opinion about a plan it cannot date;
* the engine's queue row and its running row carry the phase the
  facts handed over, unchanged.

The engine's queue row is read the way the tick reads it — through
``_read_state()`` — because the read path is where the phase was
lost.  A case that handed the state in by hand would have skipped the
hop that dropped it, and the panel would have shown no phase at all.

Why a real database
-------------------
The claim being pinned is a claim about a *column*: the value that
sits in ``plan_routing.current_phase`` has to arrive in the row.  A
stand-in connection could hold the same tuples and still not pin the
SQL the derivation actually reads.  The database here is the production
schema, created by the production migration.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader(conn: sqlite3.Connection, project_root: Path, **kwargs: Any) -> Any:
    """A facts reader over ``conn``.

    The import happens inside the helper so that a missing module is
    reported as the failure of the case that needed it, rather than as
    a collection error that hides every other case in this file.
    """
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, project_root, **kwargs)


def _now() -> str:
    """The current UTC time, as the state database writes it."""
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _clock() -> float:
    """A fixed "now", so no rule can depend on the wall."""
    return datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """A state database on the production schema."""
    from state_machine.db.schema import migrate

    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        migrate(connection)
        yield connection
    finally:
        connection.close()


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing row: the phase it is in."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, phase, _now()),
    )


def _execution_row(
    conn: sqlite3.Connection,
    plan_id: str,
    started_at: Optional[str] = None,
    exec_pid: Optional[int] = None,
    exec_status: str = "running",
) -> None:
    """Write the plan's execution row: the subprocess and its start time."""
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, exec_pid, exec_status, started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, exec_pid, exec_status, started_at, _now()),
    )


def _plan_dir(plans_root: Path, plan_id: str) -> Path:
    """Give the plan a directory under the plans root.

    A queued plan whose directory is gone is not waiting any more, so
    the case has to give it one.
    """
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


# ---------------------------------------------------------------------------
# The facts rows
# ---------------------------------------------------------------------------


def test_queued_row_carries_phase(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A queued row carries the phase the routing row holds.

    The phase is read from the routing row and passed on as it is — the
    facts layer does not map it to a label, because a label is the
    routing layer's business and a second mapping would be a second
    answer to the same question.
    """
    plans_root = tmp_path / "plans"
    _plan_dir(plans_root, "p-1")
    _routing(conn, "p-1", "queued")

    queued = _reader(conn, plans_root).queued_plans()

    assert [entry["plan_id"] for entry in queued] == ["p-1"]
    assert queued[0]["phase"] == "queued"


def test_active_row_carries_phase(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An active row carries the phase the routing row holds.

    The plan is executing, so the routing row says ``executing`` and
    the running row says the same word.  The value is the plan's own,
    not the reader's guess.
    """
    _routing(conn, "p-1", "executing")
    _execution_row(conn, "p-1", exec_pid=os.getpid())

    active = _reader(conn, tmp_path / "plans").active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-1"]
    assert active[0]["phase"] == "executing"


def test_unknown_phase_is_passed_through(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A phase nobody has a label for is passed on as it is.

    The facts layer does not know the phase vocabulary — that is the
    routing layer's — so a value it has never seen is not an error and
    not a reason to drop the row.  The row carries the word, and the
    label is worked out further up.
    """
    _routing(conn, "p-1", "verification_command_audit")
    _execution_row(conn, "p-1", exec_pid=os.getpid())

    active = _reader(conn, tmp_path / "plans").active_plans()

    assert active[0]["phase"] == "verification_command_audit"


def test_missing_phase_is_none(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A plan with no routing row has no phase, and that is not an error.

    The reader answers "I don't know" and the caller decides what to do
    with that.  Raising here would take the whole scheduler down over a
    row that is allowed to be absent.
    """
    _execution_row(conn, "p-1", exec_pid=os.getpid())

    active = _reader(conn, tmp_path / "plans").active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-1"]
    assert active[0]["phase"] is None


# ---------------------------------------------------------------------------
# The engine rows
# ---------------------------------------------------------------------------


class _Facts:
    """A facts reader that says what the case needs it to say."""

    def __init__(
        self,
        queued: Optional[List[Dict[str, Any]]] = None,
        active: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._queued = list(queued or [])
        self._active = list(active or [])

    def queued_plans(self) -> List[Dict[str, Any]]:
        """The plans that are waiting."""
        return list(self._queued)

    def active_plans(self) -> List[Dict[str, Any]]:
        """The plans that are working."""
        return list(self._active)

    def has_pending_decision(self, plan_id: str) -> bool:
        """Nobody is waiting for an answer."""
        return False

    def workspace_key(self, plan_id: str) -> Optional[str]:
        """The plan works in no known directory."""
        return None


class _Store:
    """A store whose queue holds the rows the case gives it."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = list(rows)

    def list_queue(self, conn: Any) -> List[Dict[str, Any]]:
        """The queue, in the order the case wrote it."""
        return list(self._rows)

    def list_slots(self, conn: Any) -> List[Dict[str, Any]]:
        """Nobody holds a slot."""
        return []

    def get_setting(self, conn: Any, key: str) -> Any:
        """No setting is in force."""
        return None


class _Ledger:
    """A ledger that holds no workspace."""

    def used(self) -> int:
        """Nobody holds compute."""
        return 0

    def workspace_busy(self, workspace: Optional[str]) -> bool:
        """No workspace is held."""
        return False


def _engine(
    facts: Any, store: Any, conn: sqlite3.Connection
) -> Any:
    """An engine over the collaborators the case gives it."""
    from plan_scheduler.engine import PlanSchedulerEngine

    return PlanSchedulerEngine(
        config_loader=object(),
        store=store,
        facts=facts,
        ledger=_Ledger(),
        commands=object(),
        sink=object(),
        conn=conn,
    )


def test_engine_queued_row_carries_phase() -> None:
    """The engine's queue row carries the phase the facts handed over.

    The line is the facts' answer and the queue table's answer
    together, so the phase the facts read travels with the row the
    panel is given.

    The state is read the way the tick reads it — through
    ``_read_state()`` — and not handed in by hand: the read path is
    where the phase was lost, and a case that built the state itself
    would have skipped the hop that dropped it.
    """
    connection = sqlite3.connect(":memory:")
    try:
        facts = _Facts(
            queued=[
                {
                    "plan_id": "p-1",
                    "priority": 0,
                    "seq": 1,
                    "due": True,
                    "phase": "queued",
                }
            ]
        )
        store = _Store(
            [{"plan_id": "p-1", "seq": 1, "priority": 0, "promote_order": None}]
        )
        engine = _engine(facts, store, connection)

        state = engine._read_state()

        assert [row["plan_id"] for row in state["queued"]] == ["p-1"]
        assert state["queued"][0]["phase"] == "queued"
        assert [row["plan_id"] for row in state["queue"]] == ["p-1"]
        assert state["queue"][0]["phase"] == "queued"
    finally:
        connection.close()


def test_engine_running_row_carries_phase() -> None:
    """The engine's running row carries the phase the facts handed over.

    The value is passed on unchanged: the engine does not parse it and
    does not map it to a label.
    """
    connection = sqlite3.connect(":memory:")
    try:
        facts = _Facts(
            active=[
                {
                    "plan_id": "p-1",
                    "side": "execution",
                    "workspace": None,
                    "started_at": None,
                    "phase": "executing",
                }
            ]
        )
        engine = _engine(facts, _Store([]), connection)

        running = engine._running_view(
            [{"plan_id": "p-1", "acquired_at": "2026-10-29T21:00:00"}],
            1793307612.0,
        )

        assert [row["plan_id"] for row in running] == ["p-1"]
        assert running[0]["phase"] == "executing"
    finally:
        connection.close()


def test_engine_running_row_without_phase_is_none() -> None:
    """A facts row without a phase says ``None`` rather than raising.

    A row that predates the phase field is not an error — the engine
    answers "I don't know", and the caller decides what to do with that.
    """
    connection = sqlite3.connect(":memory:")
    try:
        facts = _Facts(
            active=[
                {
                    "plan_id": "p-1",
                    "side": "execution",
                    "workspace": None,
                    "started_at": None,
                }
            ]
        )
        engine = _engine(facts, _Store([]), connection)

        running = engine._running_view(
            [{"plan_id": "p-1", "acquired_at": "2026-10-29T21:00:00"}],
            1793307612.0,
        )

        assert len(running) == 1
        assert running[0]["phase"] is None
    finally:
        connection.close()
