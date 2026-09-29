"""VP-v5 anchor: ``plan_routing`` carries exactly ONE workflow-state column.

Background
----------
Before v5 ``plan_routing`` had two columns describing the same thing in
two vocabularies::

    stage          -- routing / scheduler vocabulary, written by
                      ``RoutingRepository.try_mark_stage`` and by raw
                      ``UPDATE plan_routing SET stage = ?`` call sites
    current_phase  -- public phase vocabulary, written by
                      ``PlanState.transition_to`` / ``force_set_phase``

Neither writer touched the other's column.  Agreement was maintained by
~10 hand-written "mirror" sites, and ``plan_state._PLAN_PHASE_TO_ROUTING_STAGE``
translated between the vocabularies for the one writer that remembered
to apply it.

That split produced two live failure modes (both observed on the real
``state.db`` before the collapse):

  * ``stage='tasks_ready'`` / ``current_phase='verification_repairing'``
    on the same row -- the scheduler read one column, the API read the
    other, and they disagreed about whether the plan was runnable.
  * A raw ``UPDATE ... SET stage = 'completed'`` wrote *phase*
    vocabulary into the *stage* column, bypassing the mapping entirely.
    The column ended up holding a mix of both vocabularies depending on
    which writer touched it last.

v5 drops ``stage``; ``current_phase`` is the survivor (it carries the
richer vocabulary, and ``stage`` was a lossy projection of it).  The
collapse is a table rebuild, so these tests pin both the shape AND the
data-preservation guarantee.

Contract pinned here
--------------------
1. A fresh database has no ``stage`` column and reports version 5.
2. Collapsing a v4 database preserves every non-``stage`` column
   byte-for-byte and rewrites ``current_phase`` under the documented
   rule (non-empty kept verbatim; NULL/empty derived from ``stage``).
3. Every migrated ``current_phase`` is in ``plan_state.VALID_PHASES`` --
   a row must never land on a stage-only word like ``tasks_ready``.
4. Re-running ``migrate`` after v5 is a no-op.
5. A database that has the old ``stage`` column but no ``schema_version``
   row is still collapsed (the guard is column presence, not the logbook).
6. The declared DDL itself has no ``stage`` column -- so the column
   cannot quietly reappear the way it did once before.

NOTE: the vocabulary in this file is deliberately the PRE-v5 one --
``tasks_ready`` / ``terminal_done`` / ``terminal_failed`` are what a v4
database actually contains.  A repo-wide search-and-replace that
"updates" these to phase words destroys the fixture's meaning.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plan_state import VALID_PHASES
from state_machine.db.schema import (
    CURRENT_SCHEMA_VERSION,
    _DDL_PLAN_ROUTING,
    migrate,
)

#: The pre-v5 column list, spelled out rather than imported — the whole
#: point of the migration is that the old shape no longer exists in the
#: source tree, so the fixture has to hand-write it.
_V4_ROUTING_DDL = """
CREATE TABLE plan_routing (
  plan_id TEXT PRIMARY KEY,
  stage TEXT NOT NULL,
  substage TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  current_phase TEXT,
  completed_phases TEXT,
  review_rounds TEXT,
  flags TEXT,
  verification TEXT,
  last_updated TEXT,
  updated_at TEXT NOT NULL
)
"""

#: Rows covering every backfill branch.  ``current_phase`` is non-empty
#: for the first group (kept verbatim), NULL/empty for the second
#: (derived from ``stage``), and the two disagreeing rows are the real
#: ones that were found on the live database.
_V4_ROWS: tuple[dict, ...] = (
    # -- current_phase present: kept verbatim, even against a conflicting stage
    {
        "plan_id": "agreeing-plan",
        "stage": "verification_running",
        "current_phase": "verification_running",
    },
    {
        "plan_id": "conflicting-plan",
        "stage": "tasks_ready",
        "current_phase": "verification_repairing",
    },
    {
        "plan_id": "phase-vocab-in-stage-column",
        "stage": "completed",
        "current_phase": "completed",
    },
    # -- current_phase NULL: derived from stage via the reverse map
    {"plan_id": "null-phase-tasks-ready", "stage": "tasks_ready", "current_phase": None},
    {"plan_id": "null-phase-terminal-done", "stage": "terminal_done", "current_phase": None},
    {"plan_id": "null-phase-terminal-failed", "stage": "terminal_failed", "current_phase": None},
    {"plan_id": "null-phase-vocab-shared", "stage": "prd_review", "current_phase": None},
    {"plan_id": "null-phase-idle", "stage": "verification_idle", "current_phase": None},
    # -- current_phase empty string: same branch as NULL
    {"plan_id": "empty-phase", "stage": "interview", "current_phase": ""},
)

#: ``stage`` value -> expected ``current_phase`` for rows that needed a
#: backfill.  Rows whose ``current_phase`` was already populated keep it.
_BACKFILL_EXPECTED = {
    "null-phase-tasks-ready": "ready",
    "null-phase-terminal-done": "completed",
    "null-phase-terminal-failed": "failed",
    "null-phase-vocab-shared": "prd_review",
    "null-phase-idle": "verification",
    "empty-phase": "interview",
}

#: Columns that must survive the rebuild untouched.
_CARRIED_COLUMNS = (
    "substage",
    "version",
    "completed_phases",
    "review_rounds",
    "flags",
    "verification",
    "last_updated",
    "updated_at",
)


def _build_v4_db(path: Path) -> sqlite3.Connection:
    """Create a pre-v5 ``state.db`` carrying :data:`_V4_ROWS`.

    The connection is returned open so the caller can run the migration
    against it; the caller owns closing it.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(_V4_ROUTING_DDL)
    # A v4 database has a schema_version row at 4.  The migration must
    # still fire — that is the "upgrade an existing install" path.
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version (version) VALUES (4)")
    for row in _V4_ROWS:
        conn.execute(
            "INSERT INTO plan_routing ("
            " plan_id, stage, substage, version, current_phase,"
            " completed_phases, review_rounds, flags, verification,"
            " last_updated, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["plan_id"],
                row["stage"],
                f"sub-{row['plan_id']}",
                7,
                row["current_phase"],
                '["interview"]',
                '{"prd": 2}',
                '{"arch_enabled": true}',
                '{"status": "pending"}',
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:00:00Z",
            ),
        )
    return conn


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(plan_routing)")]


def _ddl_column_names(ddl: str) -> list[str]:
    """Extract the declared column names from a ``CREATE TABLE`` string.

    Parsed rather than substring-matched on purpose: the v5 DDL
    legitimately *mentions* ``stage`` in its explanatory comments, so a
    naive ``"stage" in ddl`` would always be true.
    """
    body = ddl.split("(", 1)[1].rsplit(")", 1)[0]
    names: list[str] = []
    for raw in body.splitlines():
        line = raw.split("--", 1)[0].strip().rstrip(",")
        if not line:
            continue
        names.append(line.split()[0])
    return names


def test_fresh_database_has_a_single_phase_column(tmp_path: Path) -> None:
    """A brand-new ``state.db`` is born at v5 — no ``stage``, NOT NULL phase."""
    conn = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    migrate(conn)
    try:
        assert "stage" not in _columns(conn), (
            "a fresh install must not create plan_routing.stage; the v5 "
            "collapse exists precisely so the column stops existing"
        )
        assert "current_phase" in _columns(conn)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION == 5
    finally:
        conn.close()


def test_declared_ddl_has_no_stage_column() -> None:
    """Static gate: the column cannot creep back into the DDL.

    This one has already regressed once on this repo (a second
    workflow-state column appeared after a previous single-column
    consolidation and nobody noticed until the two disagreed on a live
    plan).  Pinning the DDL text makes the reappearance a test failure
    rather than a silent data split.
    """
    assert "stage" not in _ddl_column_names(_DDL_PLAN_ROUTING), (
        "_DDL_PLAN_ROUTING declares a `stage` column again. plan_routing "
        "must carry exactly one workflow-state column (`current_phase`); "
        "see tests/unit/db/test_plan_routing_single_phase_column.py."
    )
    assert "current_phase" in _ddl_column_names(_DDL_PLAN_ROUTING)


def test_v4_database_is_collapsed_without_data_loss(tmp_path: Path) -> None:
    """Every non-``stage`` column survives the rebuild byte-for-byte.

    The collapse is a ``CREATE new -> INSERT SELECT -> DROP old ->
    RENAME`` rebuild, so "did we lose anything" is a real question and
    not a formality.
    """
    conn = _build_v4_db(tmp_path / "state.db")
    try:
        before = {
            r[0]: r for r in conn.execute(
                "SELECT plan_id, " + ", ".join(_CARRIED_COLUMNS)
                + " FROM plan_routing"
            )
        }
        migrate(conn)
        assert "stage" not in _columns(conn)

        after = {
            r[0]: r for r in conn.execute(
                "SELECT plan_id, " + ", ".join(_CARRIED_COLUMNS)
                + " FROM plan_routing"
            )
        }
        assert set(before) == set(after), "the rebuild changed the row set"
        for plan_id, old_row in before.items():
            assert old_row == after[plan_id], (
                f"plan_id={plan_id!r} lost or mutated data across the "
                f"v5 rebuild: {old_row} -> {after[plan_id]}"
            )
    finally:
        conn.close()


def test_current_phase_is_backfilled_under_the_documented_rule(tmp_path: Path) -> None:
    """A populated ``current_phase`` wins; an empty one is derived from ``stage``."""
    conn = _build_v4_db(tmp_path / "state.db")
    try:
        migrate(conn)
        phases = dict(conn.execute("SELECT plan_id, current_phase FROM plan_routing"))

        # Rows that already had a phase keep it — including the
        # disagreeing row, where the phase side is authoritative.
        assert phases["agreeing-plan"] == "verification_running"
        assert phases["conflicting-plan"] == "verification_repairing", (
            "when stage and current_phase disagree the phase column must "
            "win; it is the surviving vocabulary and the richer one"
        )
        assert phases["phase-vocab-in-stage-column"] == "completed"

        # Rows that did not get it derived from stage.
        for plan_id, expected in _BACKFILL_EXPECTED.items():
            assert phases[plan_id] == expected, (
                f"plan_id={plan_id!r} backfilled to {phases[plan_id]!r}, "
                f"expected {expected!r}"
            )
    finally:
        conn.close()


def test_no_migrated_phase_lands_outside_the_phase_vocabulary(tmp_path: Path) -> None:
    """A stage-only word (``tasks_ready``, ``terminal_done``) must not survive.

    This is the assertion that would have caught the original split: if
    the collapsed column is fed a stage-vocabulary value, the plan
    becomes unreadable by everything that switches on ``current_phase``.
    """
    conn = _build_v4_db(tmp_path / "state.db")
    try:
        migrate(conn)
        rows = conn.execute("SELECT plan_id, current_phase FROM plan_routing").fetchall()
        assert rows, "fixture produced no rows"
        for plan_id, phase in rows:
            assert phase in VALID_PHASES, (
                f"plan_id={plan_id!r} ended up at {phase!r}, which is not in "
                f"plan_state.VALID_PHASES — a stage-vocabulary word survived "
                f"the collapse"
            )
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-running ``migrate`` after v5 costs a PRAGMA and changes nothing."""
    conn = _build_v4_db(tmp_path / "state.db")
    try:
        migrate(conn)
        first = sorted(conn.execute("SELECT * FROM plan_routing"))
        migrate(conn)
        migrate(conn)
        second = sorted(conn.execute("SELECT * FROM plan_routing"))
        assert first == second
        assert "stage" not in _columns(conn)
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    finally:
        conn.close()


def test_collapse_does_not_depend_on_the_version_logbook(tmp_path: Path) -> None:
    """The guard is column presence, not ``schema_version``.

    A database carrying the old column but no logbook row is still
    collapsed.  Gating on a version number instead would strand that
    database on the old shape forever, which is how the two-column split
    survived a previous cleanup attempt.
    """
    conn = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    conn.execute(_V4_ROUTING_DDL)
    conn.execute(
        "INSERT INTO plan_routing (plan_id, stage, current_phase, updated_at) "
        "VALUES ('no-logbook', 'tasks_ready', NULL, '2026-09-17T00:00:00Z')"
    )
    try:
        migrate(conn)
        assert "stage" not in _columns(conn)
        assert conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = 'no-logbook'"
        ).fetchone()[0] == "ready"
    finally:
        conn.close()


def test_current_phase_rejects_null_after_the_collapse(tmp_path: Path) -> None:
    """``current_phase`` is NOT NULL once v5 has applied.

    Before the collapse a row could exist with ``current_phase IS NULL``
    (``RoutingRepository.insert`` only wrote ``stage``), and readers
    papered over it with ``row.get("current_phase") or row.get("current_phase")
    or "interview"``.  After the collapse there is no second column to
    fall back to, so the NOT NULL constraint has to hold.
    """
    conn = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    migrate(conn)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES ('bad', NULL, '2026-09-17T00:00:00Z')"
            )
    finally:
        conn.close()


def test_collapse_works_on_a_non_autocommit_connection(tmp_path: Path) -> None:
    """``migrate`` must survive a plain ``sqlite3.connect()`` caller.

    ``migrate`` is documented as taking an autocommit connection
    (``isolation_level = None``), but it is also called with plain
    connections — whose default ``isolation_level`` opens an implicit
    transaction on the first DML inside ``_v4_backfill``.  Issuing the
    collapse's ``BEGIN IMMEDIATE`` on top of that raises *"cannot start
    a transaction within a transaction"*, which is exactly what happened
    the first time this migration shipped: two integration tests that
    read rows back through a helper calling ``migrate(sqlite3.connect(…))``
    started erroring.

    The fix is to only own the transaction when the connection is in
    autocommit mode.  This test pins both halves: the collapse still
    happens, AND it does not raise on a default connection.
    """
    # NOTE: deliberately NOT ``sqlite3.connect(..., isolation_level=None)``.
    conn = _build_v4_db(tmp_path / "state.db")
    conn.close()

    conn = sqlite3.connect(tmp_path / "state.db")  # default isolation_level
    try:
        # Pre-open an implicit transaction the way _v4_backfill does, so
        # the migration meets a connection that is already in one.
        conn.execute("INSERT INTO plan_routing (plan_id, stage, current_phase, "
                     "updated_at) VALUES ('txn-open', 'tasks_ready', NULL, "
                     "'2026-09-17T00:00:00Z')")
        migrate(conn)
        conn.commit()

        assert "stage" not in _columns(conn), (
            "the collapse must still run on a non-autocommit connection"
        )
        assert conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = 'txn-open'"
        ).fetchone()[0] == "ready"
    finally:
        conn.close()
