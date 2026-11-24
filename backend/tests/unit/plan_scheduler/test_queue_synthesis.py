"""Unit tests: a plan that is waiting without a queue row is in the line.

Background
----------
The line the panel shows is built from the queue **table**: a plan that
is waiting is in the line when the table holds a row for it.  But a plan
can be waiting without a row — a plan imported before the scheduler's
first tick, a plan whose row was lost to a write that could not be
committed — and such a plan was **invisible**: the facts said it was
waiting, the line did not show it, and the operator saw a shorter line
than the one the scheduler was about to run.

Two reads made it worse.  The line was built from the table alone, and
the view that the panel reads re-read the table instead of reading the
line — so even a line that had been given a place for the plan lost it
on the way to the document.

So the line is **synthesized** for the plans that are waiting without a
row: they join the tail, in the order the facts give them, with numbers
that are the numbers their rows will hold once the reconcile has landed
them.  The synthesized rows are the **read model's** rows: nothing is
written, and the table is not the place a plan's place is decided.

What is pinned here
-------------------
``test_queued_without_row_is_visible_at_tail``
    The plan that is waiting without a row is in the document, at the
    tail, with the number its row will hold.
``test_the_synthesized_rows_are_not_written``
    The synthesized rows are the read model's rows: reading the
    document writes nothing.
``test_order_stable_after_reconcile``
    The line the reconcile lands is the line the document showed before
    it: the order does not move when the rows arrive.
``test_the_order_is_stable_when_a_plan_carries_a_priority``
    A plan that asked for a weight keeps it — the synthesized row
    carries the number its row will carry.
``test_synthesized_rows_start_at_one_when_the_queue_is_empty``
    An empty table is not a special case: the first synthesized number
    is one.

Why a real database
-------------------
The claim being pinned is a claim about a **column** — the order of the
tail is decided by ``plan_routing.updated_at`` — and a stand-in
connection could hold the same tuples without pinning the SQL the
derivation actually reads.  The database here is the production schema,
created by the production migration, and the facts reader, the store and
the ledger are the production ones.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence, Tuple

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.engine import SCHEDULER_INTENT_FILE, PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore
from state_machine.db.schema import migrate

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The world the cases drive
# ---------------------------------------------------------------------------

#: The stamp the routing rows carry.  The plan that has been waiting
#: longer carries the earlier one — the rule the facts reader orders the
#: tail by.
EARLIER = "2026-11-23T14:56:00Z"

#: The stamp of the plan that joined the line later.
LATER = "2026-11-23T14:58:00Z"

#: The stamp every queue row carries.  The row's own timestamps are not
#: what the order is decided by.
STAMP = "2026-11-23T14:00:00Z"

#: The phase a plan is in while it waits for its turn to run.
QUEUED = "queued"


class StaticConfigLoader:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """Return the snapshot."""
        return self.config


class World:
    """The world a case drives: one database and the engine over it.

    Attributes:
        engine: the engine under test.
        store: the scheduler store the case reads through.
        conn: the case's own connection.
    """

    def __init__(
        self,
        tmp_path: Path,
        plans: Sequence[Tuple[str, str]],
        queued: Sequence[str],
    ) -> None:
        """Build the world: the plans, their routing rows and their rows.

        Args:
            tmp_path: the directory the case owns.
            plans: the plans that exist, each one a plan id and the
                stamp of its routing row — the stamp that says how long
                the plan has been waiting.  Every plan is a directory
                and a routing row, so every plan is waiting.
            queued: the plans that have a **queue row**.  They are
                numbered from one, in the order they are given.
        """
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        migrate(self.conn)
        self.plans_root = tmp_path / "plans"
        self.plans_root.mkdir(parents=True, exist_ok=True)
        for plan_id, stamp in plans:
            # A plan whose directory is gone is not waiting any more,
            # so every plan has to have one.
            (self.plans_root / plan_id).mkdir(parents=True, exist_ok=True)
            self._routing(plan_id, stamp)
        for seq, plan_id in enumerate(queued, start=1):
            self._queue_row(plan_id, seq)

        self.store = SqliteSchedulerStore()
        self.facts = PlanFactsReader(self.conn, self.plans_root)
        self.ledger = CapacityLedger(self.store, self.facts, conn=self.conn)
        self.engine = PlanSchedulerEngine(
            config_loader=StaticConfigLoader(),
            store=self.store,
            facts=self.facts,
            ledger=self.ledger,
            commands=object(),
            sink=object(),
            conn=self.conn,
            # The plans tree is where the plan's own artifact lives —
            # the one that carries the priority the caller gave it.
            plans_dir=self.plans_root,
        )

    def _routing(self, plan_id: str, stamp: str) -> None:
        """Write the plan's routing row: the phase it is in."""
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, QUEUED, stamp),
        )

    def _queue_row(self, plan_id: str, seq: int) -> None:
        """Write the scheduler's own queue row for the plan."""
        self.conn.execute(
            "INSERT INTO scheduler_queue "
            "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (plan_id, seq, 0, STAMP, STAMP),
        )

    def queue_ids(self) -> List[str]:
        """The plans that have a queue row, in the production order."""
        return [
            str(row.get("plan_id") or "")
            for row in self.store.list_queue(self.conn)
        ]

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - the teardown never raises
            pass


@pytest.fixture()
def world(tmp_path: Path) -> Iterator[World]:
    """One plan with a queue row and two plans waiting without one."""
    the_world = World(
        tmp_path,
        (("x", EARLIER), ("p1", EARLIER), ("p2", LATER)),
        ("x",),
    )
    try:
        yield the_world
    finally:
        the_world.close()


def _queue_of(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The queue rows of a document, as a list of dicts."""
    return [dict(row) for row in document.get("queue", [])]


def _ids(document: Dict[str, Any]) -> List[str]:
    """The plan ids of a document's queue, in the order it shows them."""
    return [str(row.get("plan_id") or "") for row in _queue_of(document)]


def _seqs(document: Dict[str, Any]) -> List[int]:
    """The numbers of a document's queue, in the order it shows them."""
    return [int(row.get("seq") or 0) for row in _queue_of(document)]


def _priorities(document: Dict[str, Any]) -> List[int]:
    """The priorities of a document's queue, in the order it shows them."""
    return [int(row.get("priority") or 0) for row in _queue_of(document)]


def _write_intent(plan_dir: Path, priority: int) -> None:
    """Write the plan's own artifact: the priority the caller gave it.

    The artifact is the plan's fact and not the scheduler's: it says
    what the caller asked for, and the scheduler's copy of it is the
    number the queue row carries.
    """
    (plan_dir / SCHEDULER_INTENT_FILE).write_text(
        json.dumps({"priority": priority}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 1. The plan that has no row is in the line
# ---------------------------------------------------------------------------


def test_queued_without_row_is_visible_at_tail(world: World) -> None:
    """A plan that is waiting without a row is in the document.

    The queue table holds one plan; the facts say two more are waiting.
    The document shows all three — the two without a row at the tail,
    in the order the facts give them (the one that has been waiting
    longer stands in front), and with the numbers their rows will hold
    once the reconcile has landed them.

    The numbers are the tail's numbers: the table's highest number is
    one, so the two synthesized rows take two and three.  They are the
    numbers the reconcile hands out, so the line does not move when the
    rows arrive.
    """
    document = world.engine.snapshot()

    assert _ids(document) == ["x", "p1", "p2"], (
        "the plans that are waiting without a row are not in the line: "
        "{0!r}".format(_ids(document))
    )
    assert _seqs(document) == [1, 2, 3], (
        "the synthesized rows did not take the tail's numbers: {0!r}".format(
            _seqs(document)
        )
    )


def test_the_synthesized_rows_are_not_written(world: World) -> None:
    """The synthesized rows are the read model's rows and nothing else.

    The document may show a plan the table has never heard of, but the
    table is not changed by being read: the row is written by the
    reconcile, in the reconcile's own transaction, and not by a read.
    """
    world.engine.snapshot()

    assert world.queue_ids() == ["x"], (
        "the read wrote to the queue table: {0!r}".format(world.queue_ids())
    )


# ---------------------------------------------------------------------------
# 2. The line does not move when the rows land
# ---------------------------------------------------------------------------


def test_order_stable_after_reconcile(world: World) -> None:
    """The line the reconcile lands is the line the document showed.

    The reconcile is the phase that gives the waiting plans their rows.
    It enqueues them in the facts' order, so the rows it writes carry
    the numbers the synthesized rows already carried — and the line the
    operator was looking at is the line the scheduler runs.
    """
    before = world.engine.snapshot()

    world.engine._state = world.engine._read_state()
    world.engine._phase_reconcile_queue()

    after = world.engine.snapshot()

    assert _ids(after) == _ids(before), (
        "the line moved when the rows landed: {0!r} became {1!r}".format(
            _ids(before), _ids(after)
        )
    )
    assert _seqs(after) == _seqs(before), (
        "the numbers moved when the rows landed: {0!r} became {1!r}".format(
            _seqs(before), _seqs(after)
        )
    )
    assert world.queue_ids() == ["x", "p1", "p2"], (
        "the reconcile did not land the rows: {0!r}".format(world.queue_ids())
    )


# ---------------------------------------------------------------------------
# 3. The order is stable whatever the priorities are
# ---------------------------------------------------------------------------


def test_the_order_is_stable_when_a_plan_carries_a_priority(
    tmp_path: Path,
) -> None:
    """A plan that asked for a weight keeps it when its row lands.

    The priority of a plan that has no row is the one the caller gave
    it, read out of the plan's own artifact — the number its row will
    carry.  A synthesized row that carried the default instead would
    **move** the plan when the row lands: the plan would come forward
    in the line, and the panel would show one order while the
    scheduler ran another.
    """
    world = World(tmp_path, (("x", EARLIER), ("p1", EARLIER)), ("x",))
    try:
        _write_intent(world.plans_root / "p1", 5)

        before = world.engine.snapshot()

        world.engine._state = world.engine._read_state()
        world.engine._phase_reconcile_queue()

        after = world.engine.snapshot()

        assert _ids(before) == ["x", "p1"], (
            "the line does not hold the plan that asked for a weight: "
            "{0!r}".format(_ids(before))
        )
        assert _priorities(before) == [0, 5], (
            "the synthesized row does not carry the priority the caller "
            "gave: {0!r}".format(_priorities(before))
        )
        assert _ids(after) == _ids(before), (
            "the line moved when the row landed: {0!r} became {1!r}".format(
                _ids(before), _ids(after)
            )
        )
        assert _priorities(after) == _priorities(before), (
            "the priority moved when the row landed: {0!r} became "
            "{1!r}".format(_priorities(before), _priorities(after))
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 4. The empty queue is not a special case
# ---------------------------------------------------------------------------


def test_synthesized_rows_start_at_one_when_the_queue_is_empty(
    tmp_path: Path,
) -> None:
    """An empty table hands out the first number, one.

    The numbers are the table's own: the highest number in the table
    plus one, and the highest number in an empty table is zero.  A first
    number of zero would be a number the table never handed out.
    """
    world = World(tmp_path, (("p1", EARLIER), ("p2", LATER)), ())
    try:
        document = world.engine.snapshot()

        assert _ids(document) == ["p1", "p2"], (
            "the waiting plans are not in the line: {0!r}".format(
                _ids(document)
            )
        )
        assert _seqs(document) == [1, 2], (
            "the first synthesized number is not one: {0!r}".format(
                _seqs(document)
            )
        )
    finally:
        world.close()
