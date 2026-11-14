"""``scheduler_slot`` carries a priority column — the shape and the upgrade.

Background
----------
The slot table used to record only *when* a plan took a concurrency
slot.  Priority lived in ``scheduler_queue`` alone — and the queue row
is deleted the moment the plan is admitted, so a *running* plan could
not be read for its priority at all.  The eviction rule ("who may be
evicted, and who must not") has nothing to compare without it.

The fix is additive: ``scheduler_slot`` gains a ``priority`` column
(``INTEGER NOT NULL DEFAULT 0``).  ``CREATE TABLE IF NOT EXISTS``
cannot change a table that already exists, so a database written by the
older backend keeps the three columns it was created with — the
migration therefore has to *add* the column in place rather than
rebuild the table.

This module pins both halves of that contract:

* a fresh database is created with the column;
* an old three-column table gains the column, and the rows that were
  already there read back as priority 0;
* the schema version does not move — the upgrade is guarded by the
  *presence of the column*, not by a version number.

The "old" database here is a real SQLite file whose ``scheduler_slot``
table carries the three-column shape the backend shipped before the
priority column existed.  The upgrade path under test therefore starts
from a shape a real operator's database really had.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: The ``scheduler_slot`` shape the backend shipped before the priority
#: column existed: three columns, no priority.
_OLD_SLOT_DDL = (
    "CREATE TABLE scheduler_slot ("
    "plan_id TEXT PRIMARY KEY, "
    "acquired_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)"
)

#: The schema version this repository is on.  The priority column does
#: not move it: the upgrade is guarded by the presence of the column,
#: exactly like ``scheduler_setting`` was added without a bump.
_EXPECTED_SCHEMA_VERSION = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path) -> sqlite3.Connection:
    """Open a throwaway state DB with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db

    return open_db(db_path)


def _slot_columns(conn: sqlite3.Connection) -> list[str]:
    """The column names of ``scheduler_slot``, in SQLite's own order.

    ``PRAGMA table_info`` answers with one row per column; the name is
    the second field.  A table that does not exist yields no rows at
    all, which is why the caller has to know the table is there.
    """
    return [row[1] for row in conn.execute("PRAGMA table_info(scheduler_slot)")]


def _make_old_slot_db(db_path: Path) -> sqlite3.Connection:
    """Return a connection to a database with the *old* slot table.

    The slot table is created with the three-column shape the backend
    shipped before the priority column existed, and one row is put in
    it — the row has to survive the upgrade, and its priority has to
    read back as 0 rather than NULL.
    """
    conn = _open(db_path)
    conn.execute(_OLD_SLOT_DDL)
    conn.execute(
        "INSERT INTO scheduler_slot (plan_id, acquired_at, updated_at) "
        "VALUES (?, ?, ?)",
        ("old", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
    )
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fresh_database_has_the_priority_column(tmp_path: Path) -> None:
    """A new database is created with the priority column in the DDL.

    The upgrade path below only covers a database that already existed
    when the column was introduced.  A database created *after* that
    point must come out of the DDL with the column — otherwise the two
    paths would disagree about the shape of the same table.
    """
    from state_machine.db.schema import migrate

    conn = _open(tmp_path / "state.db")
    try:
        migrate(conn)

        columns = _slot_columns(conn)
        assert "priority" in columns, (
            f"a fresh database must create scheduler_slot with a priority "
            f"column; its columns are {columns!r}"
        )
    finally:
        conn.close()


def test_old_slot_table_gains_the_column(tmp_path: Path) -> None:
    """A slot table written before the column gains it, in place.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op for a table that is
    already there, so the old three-column table keeps the three
    columns it was created with unless something adds the fourth.  The
    upgrade is *additive*: the old columns keep their names, their
    order and their values.
    """
    from state_machine.db.schema import migrate

    conn = _make_old_slot_db(tmp_path / "state.db")
    try:
        assert _slot_columns(conn) == ["plan_id", "acquired_at", "updated_at"], (
            "the premise of this case is a slot table without the priority "
            "column"
        )

        migrate(conn)

        columns = _slot_columns(conn)
        assert "priority" in columns, (
            f"migrate() must add the priority column to an existing "
            f"scheduler_slot; its columns are {columns!r}"
        )
        assert columns[:3] == ["plan_id", "acquired_at", "updated_at"], (
            f"the upgrade must not disturb the columns the table already "
            f"had; its columns are {columns!r}"
        )
    finally:
        conn.close()


def test_existing_rows_read_priority_zero(tmp_path: Path) -> None:
    """The rows that were already there read back as priority 0.

    The column is ``NOT NULL DEFAULT 0``, so a row written before the
    column existed has a priority of 0 rather than NULL: the eviction
    rule compares numbers, and NULL would not compare.
    """
    from state_machine.db.schema import migrate

    conn = _make_old_slot_db(tmp_path / "state.db")
    try:
        migrate(conn)

        row = conn.execute(
            "SELECT priority FROM scheduler_slot WHERE plan_id = ?", ("old",)
        ).fetchone()
        assert row is not None, "the pre-existing slot row must survive the upgrade"
        assert row[0] == 0, (
            f"a row written before the priority column existed reads back "
            f"as 0; it read back as {row[0]!r}"
        )
    finally:
        conn.close()


def test_schema_version_stays_seven(tmp_path: Path) -> None:
    """The upgrade does not move the schema version.

    The version number is what tells a database that it needs the
    scheduler's tables, and it is pinned at 7 by the static gate.  The
    priority column is added by the presence-guarded step that runs on
    every ``migrate``, so it needs no version of its own — and the
    version row must say 7 both before and after the upgrade.
    """
    from state_machine.db.schema import CURRENT_SCHEMA_VERSION, migrate

    assert CURRENT_SCHEMA_VERSION == _EXPECTED_SCHEMA_VERSION, (
        f"CURRENT_SCHEMA_VERSION is {CURRENT_SCHEMA_VERSION}; the static "
        f"gate pins it at {_EXPECTED_SCHEMA_VERSION}"
    )

    conn = _make_old_slot_db(tmp_path / "state.db")
    try:
        migrate(conn)

        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == _EXPECTED_SCHEMA_VERSION, (
            f"the upgraded database must record version "
            f"{_EXPECTED_SCHEMA_VERSION}; it recorded {version!r}"
        )
    finally:
        conn.close()


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    """A second ``migrate`` adds no second column and rewrites no row.

    ``migrate`` runs on every connection open, so the upgrade step has
    to be idempotent: a database that has already been upgraded must
    come out of the next ``migrate`` exactly as it went in.  The
    failure this pins is the one the ``ALTER`` branch could cause — a
    second ``priority`` column, or a row whose priority was reset.
    """
    from state_machine.db.schema import migrate

    conn = _make_old_slot_db(tmp_path / "state.db")
    try:
        migrate(conn)
        columns_after_first = _slot_columns(conn)

        migrate(conn)
        migrate(conn)

        columns_after_third = _slot_columns(conn)
        assert columns_after_third == columns_after_first, (
            f"migrate() must be idempotent; the columns went from "
            f"{columns_after_first!r} to {columns_after_third!r}"
        )
        assert columns_after_third.count("priority") == 1, (
            f"the slot table must carry exactly one priority column; it "
            f"carries {columns_after_third!r}"
        )

        row = conn.execute(
            "SELECT priority FROM scheduler_slot WHERE plan_id = ?", ("old",)
        ).fetchone()
        assert row == (0,), (
            f"the pre-existing slot row must keep its priority across "
            f"repeated migrations; it reads {row!r}"
        )
    finally:
        conn.close()
