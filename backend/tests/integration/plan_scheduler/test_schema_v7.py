"""Schema v7 migration — the three scheduler tables on a real SQLite file.

Background
----------
The scheduler's own facts (queue order, slot occupancy, manual commands)
live in the *same* database as the plan facts, so there is exactly one
migration entry point: ``state_machine.db.schema.migrate``.  Schema
version 6 -> 7 creates the three scheduler tables and their indexes.

These tests run against a real SQLite file (never ``:memory:``): the
contract they pin is DDL behaviour — ``CREATE TABLE IF NOT EXISTS``
idempotency, a UNIQUE index actually rejecting a duplicate ``seq``,
and the migration leaving the pre-existing rows untouched — and none of
those are observable through a stand-in.

The "old" database is a *real* v6 database, not a hand-written copy of
the old DDL: it is produced by the production migration and then rolled
back to the v6 shape (the v7 tables removed, the version row set to 6).
Hand-writing the old DDL would let the test pass against a schema that
never existed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

#: The schema version this module migrates *from*.
V6 = 6

#: The schema version this module migrates *to*.
V7 = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path) -> sqlite3.Connection:
    """Open a throwaway state DB with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db

    return open_db(db_path)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    """The user table names defined on ``conn``."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in cur.fetchall()}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    """The explicitly created index names on ``conn``."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    return {row[0] for row in cur.fetchall()}


def _make_v6_db(db_path: Path) -> sqlite3.Connection:
    """Return a connection to a database shaped like a v6 database.

    The v6 shape is: the ``plan_*`` tables, no scheduler table, and a
    version row saying ``6``.  It is built by the production migration
    and then rolled back — the v7 addition is dropped and the version
    row is set back to 6 — so the upgrade path under test starts from
    a schema the production code actually produced.
    """
    from state_machine.db.schema import migrate

    conn = _open(db_path)
    migrate(conn)
    for table in ("scheduler_queue", "scheduler_slot", "scheduler_command"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("UPDATE schema_version SET version = ?", (V6,))
    return conn


def _plan_routing_rows(conn: sqlite3.Connection) -> list[tuple]:
    """Every ``plan_routing`` row, ordered, as plain tuples."""
    cur = conn.execute(
        "SELECT plan_id, current_phase, substage, version, updated_at "
        "FROM plan_routing ORDER BY plan_id"
    )
    return [tuple(row) for row in cur.fetchall()]


def _insert_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    """Insert one ``plan_routing`` row (the plan's routing fact)."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, "queued", "2026-10-27T00:00:00Z"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_v7_tables_created(tmp_path: Path) -> None:
    """``migrate`` creates the three scheduler tables and both indexes.

    The tables are the scheduler's own facts — queue order, slot
    occupancy, manual commands — and they must live in the state DB so
    the scheduler and the plan facts share one database and one
    migration entry point.
    """
    from state_machine.db.schema import CURRENT_SCHEMA_VERSION, migrate

    assert CURRENT_SCHEMA_VERSION == V7, (
        f"the scheduler tables are schema v7; CURRENT_SCHEMA_VERSION is "
        f"{CURRENT_SCHEMA_VERSION}"
    )

    conn = _open(tmp_path / "state.db")
    try:
        migrate(conn)

        tables = _table_names(conn)
        for table in ("scheduler_queue", "scheduler_slot", "scheduler_command"):
            assert table in tables, (
                f"migrate() must create {table!r}; tables are {sorted(tables)!r}"
            )

        indexes = _index_names(conn)
        for index in ("idx_scheduler_queue_seq", "idx_scheduler_command_plan"):
            assert index in indexes, (
                f"migrate() must create {index!r}; indexes are {sorted(indexes)!r}"
            )

        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == V7, version
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """Migrating a v6 database twice raises nothing and duplicates nothing.

    The migration is run on every connection open (including the
    per-request connections that serve ``/api/plans``), so it has to
    be safe to re-run: no exception, and no second copy of an index.
    """
    from state_machine.db.schema import migrate

    conn = _make_v6_db(tmp_path / "state.db")
    try:
        tables_before = _table_names(conn)

        migrate(conn)
        migrate(conn)

        tables_after = _table_names(conn)
        assert tables_after == tables_before | {
            "scheduler_queue",
            "scheduler_slot",
            "scheduler_command",
        }, f"unexpected table set after migrating: {sorted(tables_after)!r}"

        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == V7, version

        # A second ``CREATE INDEX`` on the same name would be a no-op
        # ("IF NOT EXISTS"), but a duplicated index would also be a
        # schema change SQLite refuses to make silently — assert the
        # index exists exactly once.
        for index in ("idx_scheduler_queue_seq", "idx_scheduler_command_plan"):
            count = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='index' AND name = ?",
                (index,),
            ).fetchone()[0]
            assert count == 1, (
                f"{index!r} must exist exactly once after two migrations; "
                f"found {count}"
            )
    finally:
        conn.close()


def test_existing_rows_untouched(tmp_path: Path) -> None:
    """The v6 -> v7 migration leaves the pre-existing rows as they were.

    The scheduler tables are an *addition*: no existing column changes
    shape and no existing row is rewritten.  A migration that rewrote
    the routing rows would corrupt the plan facts the scheduler is
    supposed to derive its decisions from.
    """
    from state_machine.db.schema import migrate

    conn = _make_v6_db(tmp_path / "state.db")
    try:
        _insert_plan(conn, "plan-a")
        _insert_plan(conn, "plan-b")
        before = _plan_routing_rows(conn)
        assert len(before) == 2, before

        migrate(conn)

        after = _plan_routing_rows(conn)
        assert after == before, (
            f"the migration must not change existing rows; "
            f"before={before!r} after={after!r}"
        )
        assert len(after) == 2, after
    finally:
        conn.close()


def test_seq_unique_index_enforced(tmp_path: Path) -> None:
    """Two queue rows cannot share a ``seq`` — the index rejects it.

    ``seq`` is the tie-breaker that makes the queue order a total
    order; two rows sharing one would make "who runs next" ambiguous.
    The guarantee is the UNIQUE index, so it is asserted on real
    SQLite rather than on the DDL text.
    """
    from state_machine.db.schema import migrate

    conn = _open(tmp_path / "state.db")
    try:
        migrate(conn)

        conn.execute(
            "INSERT INTO scheduler_queue "
            "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("plan-a", 1, 0, None, "2026-10-27T00:00:00Z", "2026-10-27T00:00:00Z"),
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scheduler_queue "
                "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("plan-b", 1, 0, None, "2026-10-27T00:00:01Z", "2026-10-27T00:00:01Z"),
            )

        rows = conn.execute(
            "SELECT plan_id FROM scheduler_queue ORDER BY plan_id"
        ).fetchall()
        assert [row[0] for row in rows] == ["plan-a"], rows
    finally:
        conn.close()
