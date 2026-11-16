"""The scheduler's read model can see the plans that are parked.

Background
----------
A plan parked for quota is neither working nor waiting: it holds no
slot, so the running line does not name it, and it is in no line, so
the queue does not name it either.  The read model therefore could not
show the one plan whose state an operator most needs to see — the one
that stopped for want of provider capacity and will start again by
itself once the window opens.

The parked set is read by the facts
(:meth:`plan_scheduler.facts.PlanFactsReader.parked_plans`) and handed
to the engine, which puts it into the state of the tick.  This file
pins the *reading*: what the document does with that state, and what
the row the panel receives carries.

What changed
------------
The document gained a **seventh** key, ``parked``.  It used to carry
exactly six — ``capacity``, ``queue_length``, ``over_capacity``,
``queue``, ``running`` and ``generated_at`` — and the six were a closed
contract, pinned by three tests.  A seventh key is a decision, and a
decision that is not written down is a decision the next person to
touch the whitelist undoes.  So the key is written down here, and in
``tests/unit/plan_scheduler/test_row_shape_contract.py``.

The six keys did not move
-------------------------
Adding a key is not a change to the others.  The six keep their names,
their types and their meanings; the rows of the queue and of the
running line keep their key sets; and the two lists are still lists.
A reader written against the six keys is still reading a document it
was written for — it simply finds one more key, which is what a
backward-compatible addition looks like.

The parked list is a list
-------------------------
Nothing parked is an empty list and never ``null``: the panel asks
"which plans are parked?" on every poll and walks the answer, and
``null`` is not an answer a caller can walk.

The reason is the quota layer's own word
----------------------------------------
A parked row's ``reason`` says why the plan stopped — ``waiting_for_quota``
— and that word is **not** in the queue policy's closed vocabulary
(:data:`plan_scheduler.policy.REASONS`).  It is passed on as it is:
mapping it through the queue's fallback would tell the operator that a
plan parked for quota is waiting for a *slot*, which is a different
fact and a wrong one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from plan_scheduler.snapshot import build


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: The keys a **parked** row carries — and no others.
#:
#: The phase says what the plan is doing (the routing layer's own
#: word), the kind says which side of the plan stopped (execution or
#: verification), the round says which verification round it stopped in
#: and the reason says why it stopped.
PARKED_ROW_KEYS = {
    "plan_id",
    "kind",
    "round",
    "reason",
    "phase",
}

#: The keys the document carried before ``parked`` was added — they are
#: still there, still the same, and this file says so.
THE_SIX_KEYS = (
    "capacity",
    "queue_length",
    "over_capacity",
    "queue",
    "running",
    "generated_at",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parked_row(**overrides: Any) -> Dict[str, Any]:
    """A parked row, shaped like the row the facts read.

    The overrides let a case drop a field or change a value without
    having to write the whole row out again.
    """
    row: Dict[str, Any] = {
        "plan_id": "p9",
        "kind": "execution",
        "round": 0,
        "reason": "waiting_for_quota",
        "phase": "waiting_for_quota",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The engine's state
# ---------------------------------------------------------------------------


class _Facts:
    """The plan facts, with one parked plan in them.

    The reader hands over the parked set the way the production reader
    does — one row per plan, with the plan, the side, the round, the
    reason and the phase on it.
    """

    def __init__(self, parked: Optional[List[Dict[str, Any]]] = None) -> None:
        self._parked = list(parked or [])

    def queued_plans(self) -> List[dict]:
        """No plan is waiting."""
        return []

    def active_plans(self) -> List[dict]:
        """No plan is running."""
        return []

    def has_pending_decision(self, plan_id: str) -> bool:
        """No plan is waiting for a human."""
        return False

    def parked_plans(self) -> List[Dict[str, Any]]:
        """The parked plans, as the facts read them."""
        return list(self._parked)


class _FactsWithoutThePark:
    """A facts reader that does not know how to read the parked set.

    It stands for a reader written before the park existed: the
    question is one it cannot answer, and the tick must not die for it.
    """

    def queued_plans(self) -> List[dict]:
        """No plan is waiting."""
        return []

    def active_plans(self) -> List[dict]:
        """No plan is running."""
        return []

    def has_pending_decision(self, plan_id: str) -> bool:
        """No plan is waiting for a human."""
        return False


class _Ledger:
    """The concurrency ledger, with no slot taken."""

    def used(self) -> int:
        """No plan holds a slot."""
        return 0


class _ConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection, closed when the test is over."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    connection = open_db(tmp_path / "state.db")
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


def _engine(conn: sqlite3.Connection, facts: Any) -> Any:
    """An engine over the given facts."""
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.store import SqliteSchedulerStore

    engine = PlanSchedulerEngine(
        config_loader=_ConfigLoader(SchedulerConfig(capacity=4)),
        store=SqliteSchedulerStore(),
        facts=facts,
        ledger=_Ledger(),
        commands=object(),
        sink=object(),
        conn=conn,
    )
    engine._config = engine._read_config()
    return engine


# ---------------------------------------------------------------------------
# The parked list
# ---------------------------------------------------------------------------


def test_parked_defaults_to_empty_list() -> None:
    """A state that names no parked plan is an empty list, not a null.

    The state of a tick written before the park existed is a normal
    thing to read — the read model must not fall over a field that is
    allowed to be absent — and the answer it gets for "which plans are
    parked?" is "none", which is a list and not a ``null``.
    """
    result = build({})

    assert result["parked"] == []
    assert isinstance(result["parked"], list)


def test_parked_rows_carry_phase() -> None:
    """A parked row carries the phase the plan is in.

    The phase is the routing layer's own word and it travels as it is:
    the snapshot does not know the phase vocabulary and does not map it
    to a label, because a label is worked out further up.  A row
    without a phase says ``None`` rather than dropping the field — the
    field is part of the row's shape, and a row that sometimes has it
    and sometimes does not is a shape the panel cannot rely on.
    """
    result = build({"parked": [_parked_row()]})

    row = result["parked"][0]
    assert row["phase"] == "waiting_for_quota"
    assert set(row) == PARKED_ROW_KEYS, sorted(row)


def test_parked_row_carries_the_whole_row() -> None:
    """The parked row says which plan, which side, which round, why.

    The plan's id names the plan; the kind says which side of it
    stopped; the round says which verification round it stopped in;
    the reason says why it stopped.  A panel that could not tell a
    parked plan from a queued one would send the operator to the wrong
    place.
    """
    result = build(
        {"parked": [_parked_row(kind="verification", round=2)]}
    )

    row = result["parked"][0]
    assert row["plan_id"] == "p9"
    assert row["kind"] == "verification"
    assert row["round"] == 2
    assert row["reason"] == "waiting_for_quota"


def test_parked_row_missing_fields_falls_back() -> None:
    """A row that carries only a plan id is a row, not an exception.

    The fields are read the same way the queue's and the running
    line's are read: a field that is missing answers the default and
    a field that is present but null answers it too.  A row written by
    an older writer must not take the read model down.

    The defaults are the quota layer's own: a park has exactly one
    reason — the plan is waiting for quota — and a park that names no
    side is the execution side.
    """
    result = build({"parked": [{"plan_id": "p9"}]})

    row = result["parked"][0]
    assert row["plan_id"] == "p9"
    assert row["kind"] == "execution"
    assert row["round"] == 0
    assert row["reason"] == "waiting_for_quota"
    assert row["phase"] is None


def test_parked_row_reads_as_the_quota_layer_reads_it() -> None:
    """A parked row reads the way the quota layer wrote it.

    The row the panel receives is the row the facts read: the plan,
    the side that stopped, the round it stopped in, the reason and the
    phase.  A row that came in with only a plan and a phase is
    completed with the park's own words rather than with blanks.
    """
    result = build(
        {"capacity": 4, "parked": [{"plan_id": "p9", "phase": "waiting_for_quota"}]}
    )

    assert result["parked"] == [
        {
            "plan_id": "p9",
            "kind": "execution",
            "round": 0,
            "reason": "waiting_for_quota",
            "phase": "waiting_for_quota",
        }
    ]


def test_parked_keeps_the_reason_it_was_given() -> None:
    """The reason is the quota layer's word, not the queue policy's.

    ``waiting_for_quota`` is not in the queue's closed vocabulary, and
    it must not be mapped through it: the queue's fallback would say
    the plan is waiting for a *slot*, which is a different fact and a
    wrong one.  The reason travels as it is.
    """
    result = build({"parked": [_parked_row(reason="waiting_for_quota")]})

    assert result["parked"][0]["reason"] == "waiting_for_quota"


# ---------------------------------------------------------------------------
# The six keys did not move
# ---------------------------------------------------------------------------


def test_existing_keys_untouched() -> None:
    """The six keys the document already had are still there, unchanged.

    Adding a key is not a change to the others: the names, the types
    and the meanings are the same, so a reader written against the six
    keys is still reading a document it was written for.
    """
    result = build({})

    for key in THE_SIX_KEYS:
        assert key in result, key
    assert isinstance(result["capacity"], dict)
    assert isinstance(result["queue_length"], int)
    assert isinstance(result["over_capacity"], bool)
    assert isinstance(result["queue"], list)
    assert isinstance(result["running"], list)
    assert isinstance(result["generated_at"], str)
    # The capacity block keeps its own three keys.
    assert set(result["capacity"]) == {"total", "used", "source"}


def test_existing_keys_keep_their_types_when_something_is_parked() -> None:
    """A document that names a parked plan still says the same six things.

    The seventh key is added beside the six, not instead of one of
    them: the queue is still a list of rows, the running line is still
    a list of rows, and the numbers beside them still mean what they
    meant.
    """
    result = build({"parked": [_parked_row()]})

    assert result["queue"] == []
    assert result["running"] == []
    assert result["queue_length"] == 0
    assert result["over_capacity"] is False
    assert result["capacity"]["source"] == "config"
    assert len(result["parked"]) == 1


# ---------------------------------------------------------------------------
# The engine's state carries them
# ---------------------------------------------------------------------------


def test_engine_state_carries_the_parked_plans(
    conn: sqlite3.Connection,
) -> None:
    """The engine's state carries the parked plans, so the panel can see them.

    The state of a tick is what the read model is built from, and a
    parked plan is in neither the queue nor the running line.  Without
    this the read model would have nothing to show about the one plan
    that stopped for want of provider capacity.
    """
    engine = _engine(conn, _Facts([_parked_row()]))

    state = engine._read_state()

    assert state["parked"] == [_parked_row()]


def test_engine_snapshot_shows_the_parked_plans(
    conn: sqlite3.Connection,
) -> None:
    """The document the engine hands the panel names the parked plan."""
    engine = _engine(conn, _Facts([_parked_row()]))

    snapshot = engine.snapshot()

    assert snapshot["parked"] == [_parked_row()]
    assert snapshot["parked"][0]["phase"] == "waiting_for_quota"


def test_engine_state_without_a_parked_reader_is_empty(
    conn: sqlite3.Connection,
) -> None:
    """A reader that cannot answer the parked question costs the tick nothing.

    The parked set is a fact the tick does not need to admit anybody:
    a parked plan holds no slot and is in no line.  A reader written
    before the park existed cannot answer, and the answer degrades to
    "nothing is parked" — the safe direction, since the parked plans
    then wait one tick longer and every other plan is admitted as
    usual.
    """
    engine = _engine(conn, _FactsWithoutThePark())

    state = engine._read_state()

    assert state["parked"] == []
