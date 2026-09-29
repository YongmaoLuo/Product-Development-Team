"""
TDD tests for the SQLite WAL state-machine base layer.

Background
----------
This module provides the single SQLite WAL "source of truth" that the
state-machine refactor will use to replace ad-hoc JSON files.  Three
contracts are pinned by the tests below:

  1. ``open(db_path)`` applies four PRAGMAs:
        - journal_mode = WAL
        - busy_timeout = 5000 ms
        - synchronous = NORMAL
        - isolation_level = None  (autocommit-style control)
     The PRAGMAs are visible on the returned Connection immediately.

  2. ``migrate(conn)`` creates five tables in this exact order:
        plan_routing
        plan_execution
        plan_verification
        plan_artifacts
        schema_version
     Idempotent: calling ``migrate`` twice does not raise and does not
     duplicate the tables.

Edge cases (boundary conditions):
  - Empty ``db_path`` -> ``ValueError``
  - Non-existent parent directory of ``db_path`` is created by ``open``
  - Inaccessible ``db_path`` (read-only parent dir) -> ``PermissionError``
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_names(conn: sqlite3.Connection) -> list[str]:
    """Return the user table names defined on ``conn`` (sorted)."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    return [row[0] for row in cur.fetchall()]


def _pragma_journal_mode(conn: sqlite3.Connection) -> str:
    """Return the current value of ``PRAGMA journal_mode``."""
    return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _pragma_busy_timeout(conn: sqlite3.Connection) -> int:
    """Return the current value of ``PRAGMA busy_timeout`` (ms)."""
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


def _pragma_synchronous(conn: sqlite3.Connection) -> int:
    """Return the current value of ``PRAGMA synchronous`` (0..3)."""
    return conn.execute("PRAGMA synchronous").fetchone()[0]


# ---------------------------------------------------------------------------
# Tests for the ``open`` contract
# ---------------------------------------------------------------------------


def test_open_creates_wal_mode(tmp_path: Path) -> None:
    """``open`` enables WAL journal mode on the returned connection."""
    from state_machine.db.connection import open as open_db

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)

    try:
        # WAL is reported (lowercase 'wal' by SQLite convention).
        assert _pragma_journal_mode(conn) == "wal"
    finally:
        conn.close()


def test_open_sets_busy_timeout_5000(tmp_path: Path) -> None:
    """``open`` sets ``busy_timeout`` to 5000 ms on the returned connection."""
    from state_machine.db.connection import open as open_db

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)

    try:
        assert _pragma_busy_timeout(conn) == 5000
    finally:
        conn.close()


def test_open_sets_synchronous_normal_and_autocommit(tmp_path: Path) -> None:
    """``open`` sets ``synchronous = NORMAL`` and ``isolation_level = None``.

    ``isolation_level`` is readable from the Connection attribute even
    after the PRAGMAs have been applied; this pins the autocommit
    contract that downstream callers (and the migrate wrapper) rely on.
    """
    from state_machine.db.connection import open as open_db

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)

    try:
        # SQLite maps SYNCHRONOUS values to integers:
        #   0 = OFF, 1 = NORMAL, 2 = FULL, 3 = EXTRA
        assert _pragma_synchronous(conn) == 1
        # isolation_level == None means autocommit mode.
        assert conn.isolation_level is None
    finally:
        conn.close()


def test_open_rejects_empty_db_path() -> None:
    """``open`` raises ``ValueError`` when ``db_path`` is empty."""
    from state_machine.db.connection import open as open_db

    with pytest.raises(ValueError):
        open_db("")


def test_open_rejects_unwritable_db_path(tmp_path: Path) -> None:
    """``open`` raises ``PermissionError`` when target parent is not writable.

    On POSIX, chmod 0o555 on the parent directory removes write
    permission for the current user so the SQLite ``open`` call fails
    to create the file (it tries to create it because the file does
    not exist yet).
    """
    import os
    import sys

    from state_machine.db.connection import open as open_db

    # Real-Path semantics: skip if the test environment is degenerate
    # (e.g. running as root, where chmod a-w is a no-op for root).
    if sys.platform == "win32":
        pytest.skip("POSIX-only chmod semantics")

    read_only_parent = tmp_path / "readonly"
    read_only_parent.mkdir()
    db_path = read_only_parent / "state.db"

    # Best-effort chmod 0o555. If the platform does not enforce it
    # (e.g. when the test runs as root), the SQLite open will still
    # succeed — that's fine; we only verify the behavior when the
    # permission IS enforced.
    try:
        os.chmod(read_only_parent, 0o555)  # nosec B103 — intentional read-only mask for the negative test
        try:
            with pytest.raises(PermissionError):
                open_db(db_path)
        finally:
            # Restore so tmp_path cleanup works.
            os.chmod(read_only_parent, 0o755)  # nosec B103 — restore writable mask after the negative test
    except PermissionError:
        # Already correctly raised before we even invoked open_db.
        pass


# ---------------------------------------------------------------------------
# Tests for the ``migrate`` contract
# ---------------------------------------------------------------------------


def test_migrate_creates_five_tables_and_version(tmp_path: Path) -> None:
    """``migrate`` creates five plan_* tables plus the ``schema_version`` table.

    Schema v4 normalisation (2026-09-09): per-task state lives in its own
    ``plan_tasks`` table (one row per task) instead of being smuggled
    inside
    ``plan_execution.task_progress`` JSON.  This adds a fifth plan_*
    table on top of the previous four (artifacts / execution /
    routing / verification).
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        names = _table_names(conn)
        assert names == [
            "plan_artifacts",
            "plan_execution",
            "plan_routing",
            "plan_tasks",
            "plan_verification",
            "schema_version",
        ]
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """``migrate`` is idempotent: calling it twice yields the same schema."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        first = _table_names(conn)
        # Second call must NOT raise (cursor.execute would raise on
        # duplicate CREATE TABLE without IF NOT EXISTS).
        migrate(conn)
        second = _table_names(conn)
        assert first == second
    finally:
        conn.close()


def test_migrate_records_schema_version(tmp_path: Path) -> None:
    """``migrate`` records the current schema version in ``schema_version``.

    The exact integer is not pinned by the spec, but the table must
    contain at least one nonnegative row so callers can check
    ``schema_version`` to decide whether an upgrade is needed.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) >= 1
        for (version,) in rows:
            assert isinstance(version, int)
            assert version >= 0
    finally:
        conn.close()


class _RecordingConn:
    """Delegates to a real connection while recording every statement.

    ``sqlite3.Connection.execute`` is a read-only attribute, so
    ``mock.patch.object`` cannot wrap it — a delegating proxy is the
    only seam.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)
        self.statements: list[str] = []

    def execute(self, sql: str, *args, **kwargs):
        self.statements.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


def test_migrate_fast_path_issues_no_ddl_once_current(tmp_path: Path) -> None:
    """On an up-to-date DB ``migrate()`` must issue no DDL and take no lock.

    2026-09-23: ``migrate()`` runs on every connection open, including
    the per-request connections serving ``/api/plans`` (polled every few
    seconds).  The full path ends in ``_v5_collapse_routing_stage``,
    which takes ``BEGIN IMMEDIATE`` — a **write lock** — on every call
    even though the schema settled long ago, serialising every hot-path
    request behind a migration no-op.  On a current-version database
    the call must be reads only: the version check plus the idempotent
    v4 backfill scan.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)  # brings the DB to CURRENT_SCHEMA_VERSION

        proxy = _RecordingConn(conn)
        migrate(proxy)

        schema_writes = [
            s for s in proxy.statements
            if s.strip().upper().startswith(("CREATE", "ALTER", "DROP", "BEGIN"))
        ]
        assert schema_writes == [], (
            f"migrate() on a current-version DB must not issue DDL or take "
            f"a lock; observed: {schema_writes}"
        )
        assert proxy.statements, "the fast path should still do its two reads"
        assert all(
            s.lstrip().upper().startswith("SELECT") for s in proxy.statements
        ), f"the fast path must be reads only; observed: {proxy.statements}"
    finally:
        conn.close()


def test_migrate_fast_path_still_upgrades_a_behind_db(tmp_path: Path) -> None:
    """A DB whose recorded version is older must take the full path.

    Regression risk of the fast path: if the guard is inverted, old
    databases would silently stop upgrading.  Simulate a v4-era DB by
    recording an older version before the v5 collapse has run, then
    confirm migrate() still applies the v5 rebuild.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import CURRENT_SCHEMA_VERSION, migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        # Roll the recorded version back and re-introduce the v5
        # ``stage`` column the collapse removes.
        conn.execute(
            "UPDATE schema_version SET version = ?",
            (CURRENT_SCHEMA_VERSION - 1,),
        )
        conn.execute("ALTER TABLE plan_routing ADD COLUMN stage TEXT")

        migrate(conn)

        cols = [r[1] for r in conn.execute("PRAGMA table_info(plan_routing)")]
        assert "stage" not in cols, (
            "v5 collapse must still run for a behind-version DB; the "
            "fast path must not have swallowed the upgrade"
        )
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION, version
    finally:
        conn.close()


def test_migrate_fast_path_absorbs_only_the_missing_table(tmp_path: Path) -> None:
    """A real error (e.g. lock contention) on the version read must propagate.

    The fast path catches OperationalError only when it means "the
    schema_version table does not exist yet" — the pre-v1 case that must
    fall through to the schema migration.  Any other OperationalError is
    a genuine failure and must not be mistaken for "needs full migrate".
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)

        proxy = _RecordingConn(conn)

        def locked(_sql, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        proxy.execute = locked  # type: ignore[method-assign]
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            migrate(proxy)
    finally:
        conn.close()
