"""Framework-fix persistence: the eight new tables and the two
``plan_routing`` columns.

Background
----------
The framework-fix lane needs somewhere to put the facts it produces.  A
framework defect is discovered by a verification point, reproduced on a
clean ``main``, registered so that a second plan hitting the same defect
finds it instead of starting a second repair, attached to the plans that
are waiting for it, audited event by event, and finally re-verified once
the fix is merged.  Each of those is a *fact*, not a piece of process
state: it has to outlive the process that produced it, and two actors
(executor thread, verification thread, coordinator) have to be able to
read the same value.

The state database is already the single source of truth for plan-level
state, so the framework-fix facts live there too — in the same
transaction as the plan facts they refer to, so that "is this plan
blocked on a framework fix?" and "which fix plan is this plan waiting
for?" cannot disagree.

This module pins the storage half of that contract:

  1. **Eight tables** — ``framework_fix_hit`` (every hit and its
     disposition), ``framework_fix_registry`` (the defect registry, one
     row per canonical fingerprint), ``framework_fix_alias`` (the
     semantic-review aliases), ``framework_fix_attach`` (which plan / VP
     / round is waiting on which fix), ``framework_fix_event`` (the
     append-only audit chain), ``framework_fix_merge_lease`` (at most
     one un-merged fix PR per repository), ``framework_fix_repro`` (the
     per-repository reproduction queue) and
     ``plan_verification_result`` (the append-only per-tree verification
     results).

  2. **Two ``plan_routing`` columns** — ``plan_kind`` distinguishes an
     ordinary plan from a framework-fix plan, and ``framework_fix``
     carries the framework-fix state block of that plan.  Both are
     added to an existing table in place, so an operator's ``state.db``
     is upgraded rather than left behind.

  3. **Idempotence** — ``migrate`` runs on every connection open, so
     running it twice must not raise and must not change the schema.

The column sets are spelled out here rather than imported from the
schema module: the point of the gate is that the *shape* is a contract,
so a test that reads the shape out of the code under test proves
nothing.

Test command::

    backend/.venv/bin/python3 -m pytest backend/tests/unit/db/test_framework_fix_schema.py -v
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate


#: The eight tables the framework-fix lane needs.  Spelled out by hand
#: so that a missing table is a test failure rather than a silent gap
#: in the runtime state.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "framework_fix_hit",
        "framework_fix_registry",
        "framework_fix_alias",
        "framework_fix_attach",
        "framework_fix_event",
        "framework_fix_merge_lease",
        "framework_fix_repro",
        "plan_verification_result",
    }
)


#: The columns each framework-fix table must carry.  Spelled out by hand
#: for the same reason as :data:`EXPECTED_TABLES`.
EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "framework_fix_hit": frozenset(
        {
            "hit_id",
            "fingerprint",
            "plan_id",
            "vp_id",
            "task_id",
            "round",
            "classification",
            "blocking",
            "disposition",
            "evidence_path",
            "first_seen_at",
            "created_at",
        }
    ),
    "framework_fix_registry": frozenset(
        {
            "fingerprint",
            "fix_plan_id",
            "repo",
            "status",
            "key_json",
            "framework_files_json",
            "evidence_summary_json",
            "semantic_review_json",
            "first_seen_at",
            "created_at",
            "merged_at",
            "merge_commit",
            "retention_until",
        }
    ),
    "framework_fix_alias": frozenset(
        {"alias_fingerprint", "canonical_fingerprint", "created_at"}
    ),
    "framework_fix_attach": frozenset(
        {
            "attach_id",
            "fingerprint",
            "plan_id",
            "vp_id",
            "round",
            "source",
            "status",
            "held_since",
            "resolved_at",
            "result_id",
        }
    ),
    "framework_fix_event": frozenset(
        {
            "event_id",
            "fingerprint",
            "fix_plan_id",
            "event_type",
            "payload_json",
            "created_at",
        }
    ),
    "framework_fix_merge_lease": frozenset(
        {"repo", "fix_plan_id", "pr_url", "acquired_at"}
    ),
    "framework_fix_repro": frozenset(
        {
            "repro_id",
            "repo",
            "plan_id",
            "failure_ref",
            "base_ref",
            "status",
            "request_json",
            "result_json",
            "queued_at",
            "started_at",
            "finished_at",
        }
    ),
    "plan_verification_result": frozenset(
        {
            "result_id",
            "plan_id",
            "round",
            "vp_id",
            "tree_fingerprint",
            "base_main_commit",
            "clean",
            "verdict",
            "source",
            "fix_plan_id",
            "evidence_path",
            "created_at",
        }
    ),
}


#: A ``plan_routing`` table as it looked BEFORE the framework-fix
#: columns existed.  Spelled out by hand — the upgrade path has to be
#: tested against a shape the test states, not one it reads out of the
#: code under test.
PRE_FRAMEWORK_FIX_PLAN_ROUTING_DDL = """
CREATE TABLE plan_routing (
  plan_id TEXT PRIMARY KEY,
  current_phase TEXT NOT NULL DEFAULT 'interview',
  substage TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  completed_phases TEXT,
  review_rounds TEXT,
  flags TEXT,
  verification TEXT,
  last_updated TEXT,
  updated_at TEXT NOT NULL
)
"""


def _table_names(conn: sqlite3.Connection) -> list[str]:
    """Return the user table names on ``conn`` (sorted)."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    return [row[0] for row in cur.fetchall()]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return the column names of ``table`` in declaration order."""
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the index names defined on ``table``."""
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


# ---------------------------------------------------------------------------
# The eight tables
# ---------------------------------------------------------------------------


def test_tables_created(tmp_path: Path) -> None:
    """A migrated database carries the eight framework-fix tables.

    Steps:

      1. Create a fresh ``state.db`` through the production connection
         factory and run the canonical ``migrate``.
      2. Read the table names back from ``sqlite_master``.
      3. Assert all eight framework-fix tables are present.

    A missing table is not a cosmetic gap: the registry, the attach
    records and the audit chain have nowhere to live, so the lane
    cannot run at all.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)

        tables = set(_table_names(conn))
        missing = EXPECTED_TABLES - tables
        assert not missing, (
            f"migrate must create the eight framework-fix tables; "
            f"missing {sorted(missing)!r} (present: {sorted(tables)!r})"
        )
    finally:
        conn.close()


def test_every_framework_fix_table_carries_its_columns(tmp_path: Path) -> None:
    """Each of the eight tables carries the column set it is contracted to.

    The shape is the contract: a reader that asks the registry for
    ``evidence_summary_json`` and finds no such column has a broken
    query, and a writer that stores ``disposition`` into a table
    without it stores nothing at all.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)

        for table, expected in EXPECTED_COLUMNS.items():
            columns = _columns(conn, table)
            assert set(columns) == expected, (
                f"{table} must carry exactly {sorted(expected)!r}; "
                f"got {sorted(columns)!r}"
            )
    finally:
        conn.close()


def test_the_output_shape_of_a_migrated_database(tmp_path: Path) -> None:
    """The documented output shape holds: eight tables, ``normal``, ``{}``.

    This is the contract the callers read: a freshly migrated database
    has the eight tables, and a plan row that says nothing about
    framework fixes reads as an ordinary plan with an empty framework
    block.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)

        tables = len(EXPECTED_TABLES & set(_table_names(conn)))
        conn.execute(
            "INSERT INTO plan_routing (plan_id, updated_at) VALUES (?, ?)",
            ("p-normal", "2026-11-23T00:00:00Z"),
        )
        row = conn.execute(
            "SELECT plan_kind, framework_fix FROM plan_routing WHERE plan_id = ?",
            ("p-normal",),
        ).fetchone()

        assert row is not None
        assert {
            "tables": tables,
            "plan_kind": row[0],
            "framework_fix": row[1],
        } == {
            "tables": 8,
            "plan_kind": "normal",
            "framework_fix": "{}",
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The two plan_routing columns
# ---------------------------------------------------------------------------


def test_plan_kind_default(tmp_path: Path) -> None:
    """A row written without a ``plan_kind`` reads back as ``normal``.

    The column is what tells an ordinary plan apart from a
    framework-fix plan, and the overwhelming majority of plans are
    ordinary.  A row that predates the column therefore has to read as
    ``normal`` — a plan whose kind is unknown must not be mistaken for
    a framework-fix plan (nor the other way round).
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)

        conn.execute(
            "INSERT INTO plan_routing (plan_id, updated_at) VALUES (?, ?)",
            ("p-old", "2026-11-23T00:00:00Z"),
        )
        row = conn.execute(
            "SELECT plan_kind, framework_fix FROM plan_routing WHERE plan_id = ?",
            ("p-old",),
        ).fetchone()

        assert row is not None, "the row must have been written"
        assert row[0] == "normal", (
            f"a plan written without a plan_kind must read back as 'normal'; "
            f"got {row[0]!r}"
        )
        assert row[1] == "{}", (
            f"a plan written without a framework_fix block must read back "
            f"as '{{}}'; got {row[1]!r}"
        )
    finally:
        conn.close()


def test_an_existing_plan_routing_table_gains_the_two_columns(
    tmp_path: Path,
) -> None:
    """A ``plan_routing`` written before the columns existed is upgraded.

    This is the upgrade path an operator's own ``state.db`` takes: the
    table is already there with ten columns, and the two framework-fix
    columns have to be *added* — ``CREATE TABLE IF NOT EXISTS`` cannot
    change a table that already exists.  The row that was already
    written must survive, and it must read as an ordinary plan.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        conn.execute(PRE_FRAMEWORK_FIX_PLAN_ROUTING_DDL)
        conn.execute(
            "INSERT INTO plan_routing (plan_id, updated_at) VALUES (?, ?)",
            ("p-legacy", "2026-11-23T00:00:00Z"),
        )
        assert "plan_kind" not in _columns(conn, "plan_routing")
        assert "framework_fix" not in _columns(conn, "plan_routing")

        migrate(conn)

        columns = _columns(conn, "plan_routing")
        assert "plan_kind" in columns, (
            "migrate must add plan_kind to an existing plan_routing table; "
            f"got {columns!r}"
        )
        assert "framework_fix" in columns, (
            "migrate must add framework_fix to an existing plan_routing "
            f"table; got {columns!r}"
        )

        row = conn.execute(
            "SELECT plan_kind, framework_fix FROM plan_routing WHERE plan_id = ?",
            ("p-legacy",),
        ).fetchone()
        assert row is not None, "the upgrade must not lose the existing row"
        assert row[0] == "normal"
        assert row[1] == "{}"
    finally:
        conn.close()


def test_the_plan_routing_columns_are_not_duplicated(tmp_path: Path) -> None:
    """Re-running ``migrate`` does not add a second copy of a column.

    ``ALTER TABLE ... ADD COLUMN`` raises *"duplicate column name"* when
    the column is already there, so an unguarded step would make the
    second ``migrate`` call fail outright.  The guard is the column's
    own presence, so the step is a no-op on a current database.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        first = _columns(conn, "plan_routing")

        migrate(conn)
        second = _columns(conn, "plan_routing")

        assert second == first, (
            f"a second migrate changed the plan_routing columns: "
            f"{first!r} -> {second!r}"
        )
        assert len(second) == len(set(second)), (
            f"migrate duplicated a column: {second!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_migrate_idempotent(tmp_path: Path) -> None:
    """``migrate`` is idempotent, and the new tables participate in it.

    Calling ``migrate`` twice must not raise (``CREATE TABLE`` without
    ``IF NOT EXISTS`` would raise ``OperationalError`` on the second
    call) and must not change the schema.  The assertion deliberately
    covers the eight new tables: an idempotency check that ignores them
    would pass even if the new DDL were not idempotent.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        first = _table_names(conn)
        first_columns = {table: _columns(conn, table) for table in EXPECTED_TABLES}
        first_routing = _columns(conn, "plan_routing")

        # The second call must NOT raise.
        migrate(conn)

        second = _table_names(conn)
        second_columns = {
            table: _columns(conn, table) for table in EXPECTED_TABLES
        }
        second_routing = _columns(conn, "plan_routing")

        assert first == second, (
            f"a second migrate changed the table set: {first!r} -> {second!r}"
        )
        for table in EXPECTED_TABLES:
            assert first_columns[table] == second_columns[table], (
                f"a second migrate changed the {table} columns: "
                f"{first_columns[table]!r} -> {second_columns[table]!r}"
            )
        assert first_routing == second_routing, (
            f"a second migrate changed the plan_routing columns: "
            f"{first_routing!r} -> {second_routing!r}"
        )
        assert EXPECTED_TABLES <= set(second), (
            f"the framework-fix tables must exist after migrate; got "
            f"{sorted(second)!r}"
        )
    finally:
        conn.close()


def test_migrate_idempotent_on_an_existing_database(tmp_path: Path) -> None:
    """A database that already carries the tables survives a re-migrate.

    This is the upgrade path: a ``state.db`` written before the
    framework-fix tables existed is opened by a newer backend, which
    runs ``migrate`` against it.  The DDL must apply there too, and
    applying it twice must still be a no-op.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        for table in EXPECTED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for index in (
            "idx_framework_fix_hit_fingerprint",
            "idx_framework_fix_repro_running",
            "idx_pvr_plan_vp_tree",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        assert not (EXPECTED_TABLES & set(_table_names(conn)))

        # The upgrade path: migrate against the pre-existing database.
        migrate(conn)
        assert EXPECTED_TABLES <= set(_table_names(conn)), (
            "migrate must create the framework-fix tables on a database "
            f"that was created before them; got {_table_names(conn)!r}"
        )

        # And once more, on a database that now has the tables.
        migrate(conn)
        assert EXPECTED_TABLES <= set(_table_names(conn))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The per-repository serialisation of reproductions
# ---------------------------------------------------------------------------


def test_repro_running_rows_are_unique_per_repository(tmp_path: Path) -> None:
    """At most one running reproduction per repository.

    The reproduction protocol runs the failing command twice on a clean
    ``main``; two reproductions of the same repository at the same time
    would race for the same temporary worktree and could hand each other
    a contaminated result.  The serialisation is enforced by the
    database, not by the caller: a *partial* unique index over the
    running rows only, so that a queue of finished reproductions for the
    same repository is not blocked by the index.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)

        indexes = _index_names(conn, "framework_fix_repro")
        assert "idx_framework_fix_repro_running" in indexes, (
            "framework_fix_repro must carry the partial unique index that "
            f"serialises reproductions per repository; got {sorted(indexes)!r}"
        )

        def _insert(repro_id: str, repo: str, status: str) -> None:
            conn.execute(
                "INSERT INTO framework_fix_repro ("
                " repro_id, repo, plan_id, base_ref, status, request_json,"
                " queued_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    repro_id,
                    repo,
                    "p1",
                    "origin/main",
                    status,
                    "{}",
                    "2026-11-23T00:00:00Z",
                ),
            )

        _insert("r-queued", "repo-a", "queued")
        _insert("r-finished", "repo-a", "finished")
        _insert("r-running", "repo-a", "running")

        with pytest.raises(sqlite3.IntegrityError):
            _insert("r-running-2", "repo-a", "running")

        # A different repository is a different queue: it may run at
        # the same time.
        _insert("r-running-other", "repo-b", "running")

        rows = conn.execute(
            "SELECT count(*) FROM framework_fix_repro WHERE status = 'running'"
        ).fetchone()
        assert rows[0] == 2, (
            f"exactly one running reproduction per repository; got {rows[0]}"
        )
    finally:
        conn.close()
