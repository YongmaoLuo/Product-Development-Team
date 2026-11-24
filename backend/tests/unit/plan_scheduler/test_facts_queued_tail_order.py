"""The tail of the queue is the same tail on every read.

Background
----------
A plan that is waiting but has no queue row yet — a plan imported
before the scheduler's first tick, or a plan whose queue row was lost
to a write that could not be committed — still belongs to the line.
``PlanFactsReader.queued_plans()`` reports it, and the scheduler puts
it at the tail.

The tail used to be whatever SQLite happened to hand back: every such
plan got the same sort key — ``(True, 0, priority, _NO_SEQ)`` — so
two plans with the same priority came back in the order the query
returned them, which is an accident of the storage layer, not a
decision.  Two reads of the same database could answer differently,
and the order the panel showed was not the order the line ran in.

The rule is the one the restart recovery already follows (arch
decision 3): a plan without a queue row joins the tail in the order of
its own ``plan_routing.updated_at`` and then its plan id.  The tail is
then the same tail on every read, on every restart, and on every
machine.

These cases pin that rule:

* two rowless plans come back in the order of their timestamps;
* the row carries the timestamp, so the caller can see why the plan is
  where it is;
* two rowless plans with the same timestamp fall back to the plan id,
  and so do two whose timestamp is empty — the order stays decided;
* the plans that *do* have a queue row keep the order their rows
  give them — the new tie-breaker only touches the tail.

Why a real database
-------------------
The claim being pinned is a claim about a *column* — the value in
``plan_routing.updated_at`` has to decide the order — and a stand-in
connection could hold the same tuples without pinning the SQL the
derivation actually reads.  The database here is the production
schema, created by the production migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, List

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader(conn: sqlite3.Connection, plans_root: Path, **kwargs: Any) -> Any:
    """A facts reader over ``conn``.

    The import happens inside the helper so that a missing module is
    reported as the failure of the case that needed it, rather than as
    a collection error that hides every other case in this file.
    """
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, plans_root, **kwargs)


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


def _plan_dir(plans_root: Path, plan_id: str) -> Path:
    """Give the plan a directory under the plans root.

    A queued plan whose directory is gone is not waiting any more, so
    the case has to give it one.
    """
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _routing(
    conn: sqlite3.Connection, plan_id: str, updated_at: str, phase: str = "queued"
) -> None:
    """Write the plan's routing row: the phase it is in and its timestamp."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, phase, updated_at),
    )


def _queue_row(
    conn: sqlite3.Connection,
    plan_id: str,
    seq: int,
    priority: int = 0,
    promote_order: Any = None,
) -> None:
    """Write the plan's queue row: where in the line it stands."""
    conn.execute(
        "INSERT INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            seq,
            priority,
            promote_order,
            "2026-11-23T14:00:00Z",
            "2026-11-23T14:00:00Z",
        ),
    )


def _queued(
    conn: sqlite3.Connection, plans_root: Path
) -> List[dict]:
    """The queued plans, as the reader answers."""
    return _reader(conn, plans_root).queued_plans()


# ---------------------------------------------------------------------------
# The tail
# ---------------------------------------------------------------------------


def test_rowless_plans_sorted_by_updated_at_then_plan_id(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two plans without a queue row come back in the order of their
    timestamps.

    Neither of them has a number in the line yet, so the line has to
    be decided by something else — the plan's own ``updated_at`` — or
    the order is an accident of the storage layer and two reads can
    answer differently.  The earlier timestamp is the one that has been
    waiting longer, so it stands in front.
    """
    plans_root = tmp_path / "plans"
    _plan_dir(plans_root, "p-b")
    _plan_dir(plans_root, "p-a")
    _routing(conn, "p-b", "2026-11-23T14:58:00Z")
    _routing(conn, "p-a", "2026-11-23T14:56:00Z")

    queued = _queued(conn, plans_root)

    assert [row["plan_id"] for row in queued] == ["p-a", "p-b"]


def test_rowless_row_carries_updated_at(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The row of a plan without a queue row carries its timestamp.

    The timestamp is the reason the plan stands where it stands, so it
    travels with the row: a caller that has to rebuild the order — the
    engine, when it gives the plan a number — reads the same fact the
    reader ordered by, instead of a second answer to the same question.
    """
    plans_root = tmp_path / "plans"
    _plan_dir(plans_root, "p-a")
    _routing(conn, "p-a", "2026-11-23T14:56:00Z")

    queued = _queued(conn, plans_root)

    assert queued[0]["updated_at"] == "2026-11-23T14:56:00Z"


def test_the_timestamp_decides_before_the_plan_id(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The timestamp is the first tie-breaker, not the plan id.

    The two tie-breakers are read in that order — the plan's own
    ``updated_at`` and then the plan id — so the plan that has been
    waiting longer stands in front even when its plan id sorts first.
    A reader that ordered by the plan id alone would get this the
    wrong way round, and the case is written so that the two orders
    disagree: the id says one thing and the timestamp says the other.
    """
    plans_root = tmp_path / "plans"
    _plan_dir(plans_root, "p-a")
    _plan_dir(plans_root, "p-b")
    # The plan id and the timestamp disagree: "p-a" sorts first as an
    # id, but "p-b" is the one that has been waiting longer.
    _routing(conn, "p-a", "2026-11-23T14:58:00Z")
    _routing(conn, "p-b", "2026-11-23T14:56:00Z")

    queued = _queued(conn, plans_root)

    assert [row["plan_id"] for row in queued] == ["p-b", "p-a"]


def test_rowless_plans_with_the_same_timestamp_fall_back_to_the_plan_id(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two plans with the same timestamp are ordered by their plan id.

    The timestamp alone does not always decide: two plans can be
    written in the same second.  The plan id is the last tie-breaker,
    and it is always there, so the order is always decided.
    """
    plans_root = tmp_path / "plans"
    _plan_dir(plans_root, "p-b")
    _plan_dir(plans_root, "p-a")
    _routing(conn, "p-b", "2026-11-23T14:56:00Z")
    _routing(conn, "p-a", "2026-11-23T14:56:00Z")

    queued = _queued(conn, plans_root)

    assert [row["plan_id"] for row in queued] == ["p-a", "p-b"]


def test_rowless_plans_with_an_empty_timestamp_fall_back_to_the_plan_id(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An empty timestamp still leaves the order decided.

    A row that carries no timestamp is not an error and not a reason
    to drop the plan: the plan is still waiting.  Two such plans are
    ordered by their plan id, so the tail is the same tail on every
    read.
    """
    plans_root = tmp_path / "plans"
    _plan_dir(plans_root, "p-b")
    _plan_dir(plans_root, "p-a")
    _routing(conn, "p-b", "")
    _routing(conn, "p-a", "")

    queued = _queued(conn, plans_root)

    assert [row["plan_id"] for row in queued] == ["p-a", "p-b"]


def test_plans_with_a_queue_row_keep_their_own_order(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The new tie-breaker touches the tail and nothing else.

    A plan that has a queue row stands where its row says it stands,
    and the rows keep their order among themselves.  The plans without
    a row join them at the tail, in the order of their own timestamps.
    """
    plans_root = tmp_path / "plans"
    for plan_id in ("p-queued", "p-rowless-a", "p-rowless-b"):
        _plan_dir(plans_root, plan_id)
    _routing(conn, "p-queued", "2026-11-23T14:00:00Z")
    _routing(conn, "p-rowless-b", "2026-11-23T14:58:00Z")
    _routing(conn, "p-rowless-a", "2026-11-23T14:56:00Z")
    _queue_row(conn, "p-queued", seq=1)

    queued = _queued(conn, plans_root)

    assert [row["plan_id"] for row in queued] == [
        "p-queued",
        "p-rowless-a",
        "p-rowless-b",
    ]
