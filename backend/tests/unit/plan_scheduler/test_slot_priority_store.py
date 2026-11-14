"""Unit tests: the slot's priority, written down and read back.

The ``scheduler_slot`` table carries the priority of the plan that holds
the slot (the column was added on 2026-11-01).  These tests pin both
ends of that path:

1. **What is written is what is read.**  The number ``upsert_slot``
   writes is the number ``list_slots`` reads back.
2. **A second write replaces the first.**  The slot row is one row per
   plan, so re-writing it replaces the priority instead of keeping the
   old one.
3. **Every row reports a priority.**  A reader must be able to ask the
   question "what is this plan's priority?" without first asking
   whether the column exists.
4. **A row that has no priority reads as 0.**  A row written the old
   way (three columns) carries the column's default, and a row whose
   priority is NULL reads as 0 as well — the eviction rule compares
   numbers, and NULL does not compare.

These tests run against a real SQLite database, because the contract
they hold is a *storage* contract: what survives is what is in the
table, not what a mock was told to expect.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from plan_scheduler.store import SqliteSchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the store's transaction contract."""
    from state_machine.db.connection import open as open_db

    return open_db(db_path)


def _open_migrated(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a database that has the scheduler's tables."""
    from state_machine.db.schema import migrate

    conn = _open(db_path)
    migrate(conn)
    return conn


@pytest.fixture()
def conn(tmp_path: Path):
    """A migrated connection, closed when the test is over."""
    connection = _open_migrated(tmp_path / "state.db")
    try:
        yield connection
    finally:
        connection.close()


def _slots(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The slots, read back through the store."""
    return SqliteSchedulerStore().list_slots(conn)


def _slot_of(conn: sqlite3.Connection, plan_id: str) -> Dict[str, Any]:
    """The row of ``plan_id`` among the slots."""
    for row in _slots(conn):
        if row["plan_id"] == plan_id:
            return row
    raise AssertionError("no slot for {0}".format(plan_id))


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_upsert_slot_writes_priority(conn: sqlite3.Connection) -> None:
    """The priority written down is the priority read back."""
    store = SqliteSchedulerStore()

    store.upsert_slot(
        conn, "p1", now="2026-11-01T10:00:00Z", priority=-10
    )

    assert _slot_of(conn, "p1")["priority"] == -10


def test_upsert_slot_replaces_priority_on_conflict(
    conn: sqlite3.Connection,
) -> None:
    """A second write replaces the priority instead of keeping the old one.

    The slot table holds one row per plan, so the priority a plan holds
    is the priority that was written last — the same way the queue
    replaces a priority when a queued plan is enqueued again.
    """
    store = SqliteSchedulerStore()

    store.upsert_slot(
        conn, "p1", now="2026-11-01T10:00:00Z", priority=-10
    )
    store.upsert_slot(
        conn, "p1", now="2026-11-01T11:00:00Z", priority=0
    )

    assert _slot_of(conn, "p1")["priority"] == 0


def test_list_slots_reports_priority(conn: sqlite3.Connection) -> None:
    """Every slot row carries a priority — the reader can always ask."""
    store = SqliteSchedulerStore()

    store.upsert_slot(
        conn, "p1", now="2026-11-01T10:00:00Z", priority=3
    )

    row = _slot_of(conn, "p1")
    assert "priority" in row, "the row does not report a priority"
    assert row["priority"] == 3


def test_row_without_priority_reads_zero(conn: sqlite3.Connection) -> None:
    """A row written the old way — three columns — reads as priority 0."""
    conn.execute(
        "INSERT INTO scheduler_slot (plan_id, acquired_at, updated_at) "
        "VALUES (?, ?, ?)",
        ("legacy", "2026-11-01T09:00:00Z", "2026-11-01T09:00:00Z"),
    )

    assert _slot_of(conn, "legacy")["priority"] == 0


def test_null_priority_reads_zero(tmp_path: Path) -> None:
    """A NULL priority reads as 0 instead of raising."""
    conn = _open(tmp_path / "nullable.db")
    try:
        conn.execute(
            "CREATE TABLE scheduler_slot ("
            "plan_id TEXT PRIMARY KEY, "
            "acquired_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, "
            "priority INTEGER)"
        )
        conn.execute(
            "INSERT INTO scheduler_slot "
            "(plan_id, acquired_at, updated_at, priority) "
            "VALUES (?, ?, ?, NULL)",
            ("p1", "2026-11-01T10:00:00Z", "2026-11-01T10:00:00Z"),
        )

        assert _slot_of(conn, "p1")["priority"] == 0
    finally:
        conn.close()


def test_slot_table_without_the_column_reads_zero(tmp_path: Path) -> None:
    """A slot table that predates the column still answers with a number.

    The column was added after the table was first shipped, so a
    database that has not been migrated yet has no ``priority`` column
    at all.  Reading such a table must not raise: the reader asks for
    a priority and gets 0 — the same answer a row written the old way
    gives.
    """
    conn = _open(tmp_path / "legacy.db")
    try:
        conn.execute(
            "CREATE TABLE scheduler_slot ("
            "plan_id TEXT PRIMARY KEY, "
            "acquired_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO scheduler_slot (plan_id, acquired_at, updated_at) "
            "VALUES (?, ?, ?)",
            ("p1", "2026-11-01T10:00:00Z", "2026-11-01T10:00:00Z"),
        )
        store = SqliteSchedulerStore()

        store.upsert_slot(
            conn, "p2", now="2026-11-01T11:00:00Z", priority=5
        )

        rows = {row["plan_id"]: row for row in _slots(conn)}
        assert rows["p1"]["priority"] == 0
        assert rows["p2"]["priority"] == 0
    finally:
        conn.close()


def test_upsert_slot_keeps_the_old_call_shape(
    conn: sqlite3.Connection,
) -> None:
    """``upsert_slot(conn, plan_id, stamp)`` still means what it meant.

    The priority is the *last* parameter, so the callers that were
    written before the column existed — ``upsert_slot(conn, plan_id,
    stamp)`` — keep working, and the priority they do not mention
    defaults to 0.
    """
    store = SqliteSchedulerStore()

    store.upsert_slot(conn, "p1", "2026-11-01T10:00:00Z")

    assert _slot_of(conn, "p1")["priority"] == 0
