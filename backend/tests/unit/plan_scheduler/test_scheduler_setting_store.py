"""Unit tests: the scheduler's runtime settings in the state database.

The scheduler's capacity is configured in ``backend/config.yaml`` — a
file that is tracked by git and carries the comments that explain it,
so it may not be rewritten by a YAML dump.  An operator who wants the
scheduler to run six plans at a time instead of four needs that number
to change *while the scheduler runs*, and that number must not rewrite
a tracked file to get there.

So the override is a row in the state database, next to the scheduler's
other facts.  This file pins the three operations that row answers to:

1. **A setting that was set reads back as it was written.**
2. **A key holds one value.**  Writing it twice replaces the value —
   the key is the primary key, so a second write cannot leave a second
   row behind for a reader to choose between.
3. **Deleting a setting puts the configured value back.**  Deleting it
   a second time is not an error: there was nothing left to delete.

And the storage contract underneath them: ``migrate`` creates the
table, and it creates it on a database that already reached v7 — the
presence guard is what does that, not a schema version bump.

These tests run against a real SQLite database, because the contract
they hold is a *storage* contract: what survives is what is in the
table, not what a mock was told to expect.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Set

import pytest

from plan_scheduler.store import CAPACITY_OVERRIDE_KEY, SqliteSchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_migrated(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a database that has the scheduler's tables."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
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


def _tables(conn: sqlite3.Connection) -> Set[str]:
    """The user table names of ``conn``."""
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _rows(conn: sqlite3.Connection) -> list:
    """The rows of ``scheduler_setting``, read back raw."""
    return conn.execute(
        "SELECT key, value, updated_at FROM scheduler_setting"
    ).fetchall()


# ---------------------------------------------------------------------------
# 1. A setting that was set reads back as it was written
# ---------------------------------------------------------------------------


def test_set_then_get_setting(conn: sqlite3.Connection) -> None:
    """What was written is what comes back.

    The capacity override is the case this exists for: the operator
    writes ``6``, and the scheduler that reads the setting a moment
    later has to see ``6`` — not a number it parsed and re-rendered, and
    not a missing value it silently falls back to the configuration file
    for.
    """
    store = SqliteSchedulerStore()

    store.set_setting(
        conn, CAPACITY_OVERRIDE_KEY, "6", stamp="2026-10-29T21:00:00"
    )

    assert store.get_setting(conn, CAPACITY_OVERRIDE_KEY) == "6"


def test_get_unset_setting_is_none(conn: sqlite3.Connection) -> None:
    """A setting that was never written has no value.

    ``None`` and ``""`` are different answers, and only one of them
    means "the operator did not set this": an empty string is a value
    the operator picked on purpose.
    """
    store = SqliteSchedulerStore()

    assert store.get_setting(conn, CAPACITY_OVERRIDE_KEY) is None


# ---------------------------------------------------------------------------
# 2. A key holds one value
# ---------------------------------------------------------------------------


def test_set_twice_keeps_last_value(conn: sqlite3.Connection) -> None:
    """The second write wins, and it leaves one row behind.

    The key is the primary key, so writing it again replaces the value
    and the timestamp instead of adding a second row.  Two rows under
    one key would give a reader two answers to "what is the capacity".
    """
    store = SqliteSchedulerStore()

    store.set_setting(
        conn, CAPACITY_OVERRIDE_KEY, "6", stamp="2026-10-29T21:00:00"
    )
    store.set_setting(
        conn, CAPACITY_OVERRIDE_KEY, "8", stamp="2026-10-29T21:05:00"
    )

    assert store.get_setting(conn, CAPACITY_OVERRIDE_KEY) == "8"

    rows = _rows(conn)
    assert len(rows) == 1, f"a key may only hold one row: {rows!r}"
    assert str(rows[0][0]) == CAPACITY_OVERRIDE_KEY
    assert str(rows[0][1]) == "8"
    assert str(rows[0][2]) == "2026-10-29T21:05:00", "the timestamp was not updated"


# ---------------------------------------------------------------------------
# 3. Deleting a setting puts the configured value back
# ---------------------------------------------------------------------------


def test_delete_setting(conn: sqlite3.Connection) -> None:
    """Deleting returns ``True`` once and ``False`` after that.

    The second delete is not an error: the settings may have changed
    under the caller, and there was nothing left to remove.  After the
    delete the setting reads back as unset — which is what puts the
    configured capacity back in charge.
    """
    store = SqliteSchedulerStore()
    store.set_setting(
        conn, CAPACITY_OVERRIDE_KEY, "6", stamp="2026-10-29T21:00:00"
    )

    assert store.delete_setting(conn, CAPACITY_OVERRIDE_KEY) is True
    assert store.delete_setting(conn, CAPACITY_OVERRIDE_KEY) is False
    assert store.get_setting(conn, CAPACITY_OVERRIDE_KEY) is None


def test_delete_unset_setting_is_false(conn: sqlite3.Connection) -> None:
    """Deleting a setting that was never written changes nothing."""
    store = SqliteSchedulerStore()

    assert store.delete_setting(conn, CAPACITY_OVERRIDE_KEY) is False


# ---------------------------------------------------------------------------
# The storage underneath: migrate creates the table
# ---------------------------------------------------------------------------


def test_migrate_creates_setting_table(tmp_path: Path) -> None:
    """A fresh database gets the table from ``migrate``.

    ``migrate`` is the only entry point that creates schema objects:
    a table that is not created here is a table the deployment does
    not have.
    """
    conn = _open_migrated(tmp_path / "state.db")
    try:
        assert "scheduler_setting" in _tables(conn)
    finally:
        conn.close()


def test_migrate_heals_a_v7_database_without_the_setting_table(
    tmp_path: Path,
) -> None:
    """A database that already reached v7 gets the table on the next
    ``migrate`` — no schema version bump.

    The presence guard in ``_SCHEDULER_OBJECTS`` is what does this: it
    asks what is there rather than what version the database claims,
    so a database written before the table existed heals itself instead
    of failing every later read with *"no such table:
    scheduler_setting"*.  The version number does not move, because
    adding a table is not a shape-changing migration.
    """
    from state_machine.db.schema import CURRENT_SCHEMA_VERSION, migrate

    conn = _open_migrated(tmp_path / "state.db")
    try:
        conn.execute("DROP TABLE scheduler_setting")
        assert "scheduler_setting" not in _tables(conn)

        migrate(conn)

        assert "scheduler_setting" in _tables(conn)
        version = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert int(version[0]) == CURRENT_SCHEMA_VERSION, (
            "adding a table must not move the schema version"
        )
    finally:
        conn.close()
