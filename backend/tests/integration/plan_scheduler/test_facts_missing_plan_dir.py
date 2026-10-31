"""A queued plan whose directory is gone is not in the queue.

Background
----------
A ``queued`` row in ``plan_routing`` says that a plan is waiting to
run.  It does not say that the plan still *exists*: a plan whose
directory has been removed — the tree was cleaned up, the import was
rolled back — leaves its row behind, and the row keeps saying
"queued" forever, because nothing else in the system removes it.

Such a row is a **ghost**.  The ghost sits at the head of the queue,
the scheduler waits for a plan that cannot start, and every plan
behind it is blocked.  The ghost does not heal by itself.

The facts layer is where the ghost is caught, because the facts layer
is what answers "who is waiting?".  A plan is waiting only if it *is*
a plan — if there is a directory for it under the plans root.  A plan
whose directory is gone is dropped from the answer, and the operator is
told about it **once**: the warning says what happened, and does not
become a heartbeat that repeats the same sentence on every tick.

What is pinned here
-------------------
``test_missing_plan_dir_excluded``
    The ghost is not in the answer, and the plan that really is there
    still is.
``test_warns_once``
    The operator hears about the ghost once — not once per tick.
``test_does_not_write``
    The facts layer only reads: the routing row is still there with
    the phase it had, and the ghost's queue row has not been deleted.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader(conn: sqlite3.Connection, project_root: Path) -> Any:
    """A facts reader over ``conn``.

    The import happens inside the helper so that a missing module is
    reported as the failure of the case that needed it, rather than as
    a collection error that hides every other case in this file.
    """
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, project_root)


def _iso() -> str:
    """The current UTC time, as the database writes it."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing fact: the phase it is in."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, phase, _iso()),
    )


def _queue_row(
    conn: sqlite3.Connection, plan_id: str, seq: int = 1, priority: int = 0
) -> None:
    """Write the scheduler's own queue row for the plan."""
    conn.execute(
        "INSERT INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (plan_id, seq, priority, _iso(), _iso()),
    )


def _plan_dir(plans_root: Path, plan_id: str) -> Path:
    """Create a plan directory with the files a real plan carries."""
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "prd.json").write_text(
        json.dumps({"title": f"Plan {plan_id}"}), encoding="utf-8"
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    return plan_dir


def _rows(conn: sqlite3.Connection, table: str) -> list:
    """Every row of ``table``, in a comparable shape."""
    return [
        tuple(row)
        for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY plan_id"  # noqa: S608 - fixed names
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# The ghost
# ---------------------------------------------------------------------------


def test_missing_plan_dir_excluded(
    db_factory: Any, tmp_path: Path
) -> None:
    """A queued plan with no directory is not a queued plan.

    The ghost and the real plan are both ``queued`` in ``plan_routing``,
    and the ghost is even ahead of the real one in the queue.  Only the
    plan that exists is handed back: the ghost would otherwise sit at
    the head of the line forever, and nothing would ever run.
    """
    db = db_factory()
    conn = db.connect()
    try:
        plans_root = tmp_path / "plans"
        _plan_dir(plans_root, "p1")
        _routing(conn, "ghost", "queued")
        _routing(conn, "p1", "queued")
        _queue_row(conn, "ghost", seq=1)
        _queue_row(conn, "p1", seq=2)

        queued = _reader(conn, plans_root).queued_plans()

        assert [entry["plan_id"] for entry in queued] == ["p1"], (
            "the plan that exists is waiting; the one whose directory is "
            "gone is not"
        )
        assert queued[0]["plan_id"] == "p1"
    finally:
        conn.close()


def test_warns_once(
    db_factory: Any, tmp_path: Path, caplog: Any
) -> None:
    """The operator hears about the ghost once, not once per tick.

    The scheduler reads the queue over and over.  A warning that repeats
    on every tick turns one fact into a heartbeat that drowns the log;
    a warning that never comes leaves the operator with a queue that
    mysteriously skips a plan.  So: exactly one warning per ghost.
    """
    db = db_factory()
    conn = db.connect()
    try:
        plans_root = tmp_path / "plans"
        _plan_dir(plans_root, "p1")
        _routing(conn, "ghost", "queued")
        _routing(conn, "p1", "queued")

        reader = _reader(conn, plans_root)
        with caplog.at_level(logging.WARNING, logger="plan_scheduler.facts"):
            for _ in range(3):
                reader.queued_plans()

        warnings = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and "no plan directory" in record.getMessage()
        ]

        assert len(warnings) == 1, (
            f"three reads, one warning — got {len(warnings)}: "
            f"{[record.getMessage() for record in warnings]}"
        )
        assert "ghost" in warnings[0].getMessage()
    finally:
        conn.close()


def test_does_not_write(
    db_factory: Any, tmp_path: Path
) -> None:
    """The facts layer reads; it does not repair.

    The ghost's routing row and its queue row are the scheduler's
    business, not the facts layer's: this layer answers a question, and
    the answer is "this plan is not waiting".  Rewriting the phase or
    deleting the queue row here would be a second answer to a question
    another layer owns — and would hide the ghost from the operator
    instead of reporting it.
    """
    db = db_factory()
    conn = db.connect()
    try:
        plans_root = tmp_path / "plans"
        _plan_dir(plans_root, "p1")
        _routing(conn, "ghost", "queued")
        _routing(conn, "p1", "queued")
        _queue_row(conn, "ghost", seq=1)
        _queue_row(conn, "p1", seq=2)

        routing_before = _rows(conn, "plan_routing")
        queue_before = _rows(conn, "scheduler_queue")

        _reader(conn, plans_root).queued_plans()

        assert _rows(conn, "plan_routing") == routing_before, (
            "the routing rows are facts, and this layer does not write them"
        )
        assert _rows(conn, "scheduler_queue") == queue_before, (
            "the queue row belongs to the scheduler, and this layer does "
            "not delete it"
        )
    finally:
        conn.close()
