"""Schema management for the state-machine WAL base layer.

The :func:`migrate` factory creates the five ``plan_*`` tables plus
the ``schema_version`` bootstrap table the state-machine refactor
relies on.  Every ``CREATE TABLE`` uses ``IF NOT EXISTS`` so the
function is safe to call repeatedly — calling ``migrate`` on an
already-migrated database is a no-op (the second call still returns
without raising).

The five ``plan_*`` tables
-------------------------
The state-machine refactor uses one SQLite database as the single
source of truth for plan-level state.  Each table owns one concern:

  * ``plan_routing``      (current phase / substage / version)
  * ``plan_execution``    (run / pid / attempt counts)
  * ``plan_verification`` (verification round / status)
  * ``plan_artifacts``    (artifact status / content_hash)
  * ``plan_tasks``        (per-task runtime state — task #v4 split)

v4 (2026-09-09) splits the per-task
runtime state OUT of ``plan_execution.task_progress`` JSON column into
a proper relational ``plan_tasks`` table.  The v4 migration block:

  1. Creates ``plan_tasks`` with the canonical column set.
  2. Backfills any pre-existing rows from
     ``plan_execution.task_progress`` JSON (idempotent via
     ``INSERT OR IGNORE``).
  3. Leaves ``plan_execution.task_progress`` as a deprecated empty
     column. The drop is deferred to v5 once every writer is gone.

v5 (2026-09-17) collapses ``plan_routing.stage`` into
``current_phase``.  The two columns described the same thing in two
vocabularies, were written by disjoint code paths, and had to be kept
in agreement by hand.  ``current_phase`` survives (it carries the
richer vocabulary; ``stage`` was a lossy projection of it) and ``stage``
is dropped by a full table rebuild — the drop is one-way, so take a
copy of ``state.db`` before the first v5 boot.

The ``schema_version`` table is the migration-logbook: a single
INTEGER column primary key recording which schema version is live.
This is the bootstrap version (1) so the first ``INSERT`` is
``INSERT INTO schema_version (version) VALUES (1)``.
"""

from __future__ import annotations

import json
import sqlite3

__all__ = ["migrate", "CURRENT_SCHEMA_VERSION"]

#: Schema version this module produces.  Bumped in tandem with any
#: DDL change so the upgrade script can branch on it.
CURRENT_SCHEMA_VERSION = 5


# Each CREATE TABLE statement is kept as a top-level string constant
# so the SQL is readable without escaping inside a Python string.
# The exact column shape is the contract that downstream repository
# code depends on; do not reorder or rename columns without bumping
# CURRENT_SCHEMA_VERSION and writing an upgrade migration.

_DDL_PLAN_ROUTING = """
CREATE TABLE IF NOT EXISTS plan_routing (
  plan_id TEXT PRIMARY KEY,
  -- The single workflow-state column (v5).  Before v5 this table
  -- carried BOTH ``stage`` and ``current_phase``: two columns that
  -- had to agree, written by disjoint code paths, and — because
  -- ``stage`` was a lossy projection of ``current_phase``
  -- (``failed`` / ``stopped`` / ``verification_failed`` /
  -- ``verification_loop_stopped`` all collapsed to
  -- ``terminal_failed``) — the projection leaked back into the
  -- source whenever a raw ``UPDATE ... SET stage`` bypassed the
  -- mapping.  ``current_phase`` carries the full vocabulary, so it
  -- is the survivor.  See ``_v5_collapse_routing_stage``.
  current_phase TEXT NOT NULL DEFAULT 'interview',
  substage TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  -- Phase/State data lifted from ``plans/{id}/plan_state.json``
  -- (task #3.7). These JSON columns let ``plan_state.json`` be
  -- deleted entirely without losing the per-plan workflow knobs
  -- (review rounds, feature flags, etc.) the rest of the codebase
  -- reads via :class:`PlanState`.
  completed_phases TEXT,
  review_rounds TEXT,
  flags TEXT,
  verification TEXT,
  last_updated TEXT,
  updated_at TEXT NOT NULL
)
"""

_DDL_PLAN_EXECUTION = """
CREATE TABLE IF NOT EXISTS plan_execution (
  plan_id TEXT PRIMARY KEY,
  current_phase TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  project_dir TEXT,
  stop_reason TEXT,
  task_progress TEXT,
  next_run_at TEXT,
  card_state TEXT,
  flags TEXT,
  exec_pid INTEGER,
  exec_status TEXT,
  started_at TEXT,
  updated_at TEXT NOT NULL
)
"""

_DDL_PLAN_VERIFICATION = """
CREATE TABLE IF NOT EXISTS plan_verification (
  plan_id TEXT PRIMARY KEY,
  verification_status TEXT NOT NULL,
  round INTEGER NOT NULL DEFAULT 0,
  max_rounds INTEGER NOT NULL DEFAULT 3,
  verification_stop_reason TEXT,
  runtime_state TEXT,
  executor_state TEXT,
  progress_state TEXT,
  results TEXT,
  verdicts TEXT,
  -- Full per-VP execution envelope (verification_points +
  -- execution_results + executed_at). Populated by Phase 1 of the
  -- verification agent; consumed by Phase 3 (judgment). Stored in
  -- SQLite to remove the redundant
  -- ``plans/{id}/verification_execution_results.json`` file (the
  -- old on-disk cache that has been gradually replaced by the
  -- ``plan_verification`` row since task 2-7).
  execution_results TEXT,
  started_at TEXT,
  updated_at TEXT NOT NULL
)
"""

_DDL_PLAN_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS plan_artifacts (
  plan_id TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  file_path TEXT NOT NULL,
  status TEXT NOT NULL,
  content_hash TEXT,
  created_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (plan_id, artifact_type)
)
"""

#: Per-task runtime + static fields. Plan v4 (2026-09-09) splits these
#: out of the ``plan_execution.task_progress`` JSON column into a
#: proper relational table so SQLite's row-level atomic writes replace
#: the application-level read-modify-write + ``_repo_version`` CAS
#: (the 2026-09-09 same-id-loop bug root cause).
#:
#: Columns covered:
#:   * Runtime (the dispatcher writes these per execution):
#:     ``status`` / ``end_ts`` / ``schedule_ts`` / ``attempt`` /
#:     ``commit_sha`` / ``failure_reason`` / ``breakdown_count``
#:   * Static (refiner / verification-repair writes these once):
#:     ``title`` / ``description`` / ``test_command`` /
#:     ``files_to_modify`` / ``depends_on`` / ``model_type`` /
#:     ``project_dir`` / ``provider`` / ``task_group`` /
#:     ``execution_group`` / ``priority`` / ``acceptance_criteria`` /
#:     ``failed_vp_id`` / ``round``
#:   * CAS (audit-only; no longer used for conflict detection):
#:     ``_repo_version`` (default 0, incremented on every write)
#:   * ``updated_at``: bumped on every write, used by the JSON-static
#:     gate and audit tooling.
_DDL_PLAN_TASKS = """
CREATE TABLE IF NOT EXISTS plan_tasks (
  plan_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  status TEXT,
  end_ts TEXT,
  schedule_ts TEXT,
  attempt INTEGER,
  commit_sha TEXT,
  failure_reason TEXT,
  breakdown_count INTEGER,
  _repo_version INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  description TEXT,
  test_command TEXT,
  files_to_modify TEXT,
  depends_on TEXT,
  model_type TEXT,
  project_dir TEXT,
  provider TEXT,
  task_group TEXT,
  execution_group TEXT,
  priority TEXT,
  acceptance_criteria TEXT,
  failed_vp_id TEXT,
  round INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (plan_id, task_id),
  FOREIGN KEY (plan_id) REFERENCES plan_execution(plan_id) ON DELETE CASCADE
)
"""

_DDL_PLAN_TASKS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_plan_tasks_plan_status
  ON plan_tasks(plan_id, status)
"""

_DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
)
"""

# Order matters: the five ``plan_*`` tables come first, then the
# ``schema_version`` bootstrap table.  The order is pinned so the
# ``ORDER BY name`` test returns a deterministic list.
_DDL_ORDER: tuple[str, ...] = (
    _DDL_PLAN_ROUTING,
    _DDL_PLAN_EXECUTION,
    _DDL_PLAN_VERIFICATION,
    _DDL_PLAN_ARTIFACTS,
    _DDL_PLAN_TASKS,
    _DDL_PLAN_TASKS_INDEX,
    _DDL_SCHEMA_VERSION,
)


#: Runtime columns lifted from ``plan_execution.task_progress.tasks``
#: into ``plan_tasks``. Order matters: keys are inserted in the same
#: order they appear here, so tests can rely on column ordinals.
_V4_RUNTIME_COLUMNS: tuple[str, ...] = (
    "status",
    "end_ts",
    "schedule_ts",
    "attempt",
    "commit_sha",
    "failure_reason",
    "breakdown_count",
    "_repo_version",
)


def _v4_backfill(conn: sqlite3.Connection) -> None:
    """Backfill pre-v4 ``plan_execution.task_progress`` JSON into ``plan_tasks``.

    Iterates every row in ``plan_execution`` whose ``task_progress``
    column is non-empty, decodes the JSON, and ``INSERT OR IGNORE``s
    one row per ``(plan_id, task_id)`` into ``plan_tasks``. Skips
    rows whose JSON is corrupt (logs to stderr, does not raise) so a
    single bad row does not block the upgrade.

    Idempotent: ``INSERT OR IGNORE`` + the v4 migration's own idempotent
    CREATE TABLE means re-running ``migrate`` after v4 is a no-op.
    """
    import sys

    cur = conn.execute(
        "SELECT plan_id, task_progress FROM plan_execution "
        "WHERE task_progress IS NOT NULL "
        "AND task_progress != '' "
        "AND task_progress != '{}'"
    )
    plan_rows = cur.fetchall()

    now_iso = _now_iso()
    for plan_id, raw in plan_rows:
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            print(
                f"[schema.v4_backfill] skipping plan_id={plan_id!r}: "
                f"corrupt task_progress JSON (truncated={raw[:60]!r}...)",
                file=sys.stderr,
            )
            continue
        if not isinstance(decoded, dict):
            continue
        tasks_map = decoded.get("tasks") or {}
        if not isinstance(tasks_map, dict):
            continue
        for task_id, entry in tasks_map.items():
            if not isinstance(task_id, str) or not isinstance(entry, dict):
                continue
            # Build the column→value map from the JSON entry.
            values: dict[str, object] = {}
            for col in _V4_RUNTIME_COLUMNS:
                if col in entry and entry[col] is not None:
                    values[col] = entry[col]
            values.setdefault("_repo_version", 0)
            values["updated_at"] = now_iso

            cols_list = ["plan_id", "task_id"] + list(values.keys())
            placeholders = ", ".join("?" for _ in cols_list)
            conn.execute(
                f"INSERT OR IGNORE INTO plan_tasks ({', '.join(cols_list)}) "
                f"VALUES ({placeholders})",
                (plan_id, task_id, *values.values()),
            )


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, second precision.

    Centralised here so the schema module does not depend on
    :mod:`execution_repository` (avoid a circular import — schema is
    loaded first when state_machine boots).
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Reverse of ``plan_state._PLAN_PHASE_TO_ROUTING_STAGE`` for the v5
#: backfill only.  Migration-local on purpose: the forward mapping is
#: deleted outright in v5 (nothing should translate vocabularies at
#: runtime any more), but a pre-v5 row whose ``current_phase`` is NULL
#: still carries a ``stage`` that needs translating back into a phase
#: before the column is dropped.
#:
#: ``verification_idle`` maps to ``verification``: both mean "the plan
#: is in the verification phase and no round is running".  Keeping them
#: as two spellings is exactly the vocabulary drift v5 removes.
_V5_STAGE_TO_PHASE: dict[str, str] = {
    "tasks_ready": "ready",
    "tasks_queued": "queued",
    "terminal_done": "completed",
    "terminal_failed": "failed",
    "verification_idle": "verification",
}


def _v5_collapse_routing_stage(conn: sqlite3.Connection) -> None:
    """Drop ``plan_routing.stage``; ``current_phase`` becomes the only column.

    The collapse is a full table rebuild (SQLite's documented
    ``CREATE new → INSERT SELECT → DROP old → RENAME`` sequence)
    because the outgoing column carries a ``NOT NULL`` constraint —
    the migration cannot simply stop writing it.

    Backfill rule, per row:

      * ``current_phase`` non-empty → **kept verbatim**.  It carries
        the richer vocabulary and is the survivor of the two columns,
        so it wins every disagreement.
      * ``current_phase`` NULL / empty → derived from ``stage`` via
        :data:`_V5_STAGE_TO_PHASE`; anything not in that map is already
        shared vocabulary and passes through.
      * neither → ``'interview'`` (the column default).

    Guarded by a column-presence check rather than a version number so
    it is self-describing and idempotent: once ``stage`` is gone the
    body is skipped.  The check runs *inside* the transaction so two
    processes racing the same upgrade serialise — the loser re-reads and
    sees the rebuilt table.

    Transaction handling
    --------------------
    ``migrate`` is documented as taking an autocommit connection
    (``isolation_level = None``, as produced by
    :func:`state_machine.db.connection.open`), and for those we open an
    explicit ``BEGIN IMMEDIATE`` so the read-guard + rebuild + rename
    cannot interleave with another process.

    It is ALSO called with plain ``sqlite3.connect(path)`` connections
    — whose default ``isolation_level`` starts an implicit transaction
    on the first DML in :func:`_v4_backfill`.  Issuing ``BEGIN
    IMMEDIATE`` there raises *"cannot start a transaction within a
    transaction"*, so we detect that case and let the caller's own
    transaction provide atomicity (SQLite DDL is transactional, so the
    rebuild is still all-or-nothing — it simply commits when the
    caller commits).
    """
    owns_txn = conn.isolation_level is None
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(plan_routing)")}
        if "stage" not in cols:
            if owns_txn:
                conn.execute("COMMIT")
            return

        # Step 1: materialise the surviving value into ``current_phase``
        # for every row that does not already have one.
        cur = conn.execute(
            "SELECT plan_id, stage FROM plan_routing "
            "WHERE current_phase IS NULL OR current_phase = ''"
        )
        backfilled = 0
        for plan_id, stage in cur.fetchall():
            phase = _V5_STAGE_TO_PHASE.get(stage, stage) or "interview"
            conn.execute(
                "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
                (phase, plan_id),
            )
            backfilled += 1

        # Step 2: rebuild without ``stage``.
        conn.execute("DROP TABLE IF EXISTS plan_routing_v5")
        conn.execute(_DDL_PLAN_ROUTING.replace(
            "CREATE TABLE IF NOT EXISTS plan_routing",
            "CREATE TABLE plan_routing_v5",
            1,
        ))
        conn.execute(
            "INSERT INTO plan_routing_v5 ("
            " plan_id, current_phase, substage, version, completed_phases,"
            " review_rounds, flags, verification, last_updated, updated_at"
            ") SELECT "
            " plan_id, COALESCE(NULLIF(current_phase, ''), 'interview'),"
            " substage, version, completed_phases, review_rounds, flags,"
            " verification, last_updated, updated_at "
            "FROM plan_routing"
        )
        moved = conn.execute("SELECT count(*) FROM plan_routing_v5").fetchone()[0]
        conn.execute("DROP TABLE plan_routing")
        conn.execute("ALTER TABLE plan_routing_v5 RENAME TO plan_routing")
        if owns_txn:
            conn.execute("COMMIT")
    except BaseException:
        if owns_txn:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                # ROLLBACK can fail if the txn is already unwound; the
                # original exception is what matters.
                pass
        raise

    print(
        f"[schema.v5] plan_routing.stage dropped — moved {moved} rows "
        f"({backfilled} backfilled from stage)"
    )


def migrate(conn: sqlite3.Connection) -> None:
    """Create the five ``plan_*`` tables and the ``schema_version`` row.

    Idempotent: uses ``CREATE TABLE IF NOT EXISTS`` for every DDL
    statement.  Calling ``migrate`` repeatedly on the same database
    is a no-op (no rows are inserted into ``schema_version`` after
    the first call).

    The v5 step is a table *rebuild*, not an ``ALTER`` — it is the
    one migration that touches existing rows.  It re-reads the live
    column set under ``BEGIN IMMEDIATE`` on every call, so re-running
    ``migrate`` after v5 has applied costs one ``PRAGMA`` and returns.

    Fast path (2026-09-23): when ``schema_version`` already records
    ``CURRENT_SCHEMA_VERSION``, the schema-shape work is skipped — no
    DDL, and crucially no ``BEGIN IMMEDIATE``.  This matters because
    callers open a fresh connection per request (e.g. ``/api/plans``,
    polled every few seconds) and used to run the whole migration each
    time, with the v5 step taking a **write lock** on every request for
    a schema that settled long ago.  The version check is one read; the
    v4 data backfill below still runs (it is the tested contract for
    stray legacy ``task_progress`` JSON and is idempotent), so a
    current-schema call costs two reads and no locks.

    Parameters
    ----------
    conn:
        An open :class:`sqlite3.Connection` produced by
        :func:`state_machine.db.connection.open`.  The connection
        must be in autocommit mode (``isolation_level = None``) so
        the DDL applies immediately without an implicit transaction.
    """
    # The only failure mode absorbed here is the version table not
    # existing yet (a pre-v1 or brand-new database); a real error —
    # lock contention, I/O, corruption — must propagate exactly as it
    # did before this fast path existed.
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        row = None

    if row is None or row[0] < CURRENT_SCHEMA_VERSION:
        _migrate_schema(conn)

    # The v4 data lift runs on EVERY call, current schema or not.
    # Production code no longer writes ``plan_execution.task_progress``
    # (the only writer, ``ExecutionRepository.update_task_progress``,
    # has no remaining callers), but the lift is the pinned, tested
    # contract for any stray legacy JSON that lands in the column —
    # ``test_db_schema_v4.py`` inserts it directly and expects the next
    # ``migrate`` to lift it.  It is idempotent (INSERT OR IGNORE) and,
    # on a database with no legacy rows, costs one empty SELECT — far
    # cheaper than the write lock the old always-full migrate took.
    _v4_backfill(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema-shape DDL and record the version.

    Called by :func:`migrate` only when the recorded version is missing
    or behind.  Everything here is a no-op shapewise on a current
    database, which is exactly why ``migrate`` skips it there: the v5
    collapse ends in ``BEGIN IMMEDIATE``, and taking a write lock on
    every per-request connection open was the 2026-09-23 hot-path
    regression.
    """
    for ddl in _DDL_ORDER:
        conn.execute(ddl)

    # Schema v2 upgrade: lift the JSON columns that used to live in
    # ``plan_state.json`` (current_phase / completed_phases /
    # review_rounds / flags / verification / last_updated) into
    # ``plan_routing`` (task #3.7). Each ``ALTER TABLE`` is wrapped
    # in try/except because pre-v2 databases already have these
    # columns (the IF NOT EXISTS in CREATE TABLE) and SQLite returns
    # "duplicate column name" otherwise. Re-running the upgrade is
    # therefore safe — the "if not exists" semantic is implemented
    # by catching the OperationalError.
    _v2_columns = (
        ("current_phase", "TEXT"),
        ("completed_phases", "TEXT"),
        ("review_rounds", "TEXT"),
        ("flags", "TEXT"),
        ("verification", "TEXT"),
        ("last_updated", "TEXT"),
    )
    for col_name, col_type in _v2_columns:
        try:
            conn.execute(
                f"ALTER TABLE plan_routing ADD COLUMN {col_name} {col_type}"
            )
        except sqlite3.OperationalError:
            # Column already exists (fresh-install v2 schema picked
            # it up via CREATE TABLE, or this upgrade ran before).
            pass

    # Schema v3 upgrade: add the ``execution_results`` column to
    # ``plan_verification``. The CREATE TABLE for plan_verification
    # already declares this column (task 2-7 / commit 6862698e), but
    # databases that were created before that commit only have the v2
    # schema — 11 columns without ``execution_results``. ``CREATE TABLE
    # IF NOT EXISTS`` is a no-op when the table already exists, so a
    # pre-existing v2 plan_verification row set never gets the new
    # column. Wrapping the ALTER in try/except handles both the
    # fresh-install case (CREATE TABLE picks it up → duplicate column
    # error) and the upgrade case (ALTER succeeds).
    try:
        conn.execute(
            "ALTER TABLE plan_verification ADD COLUMN execution_results TEXT"
        )
    except sqlite3.OperationalError:
        # Column already exists (fresh-install v3 schema picked it
        # up via CREATE TABLE, or this upgrade ran before).
        pass

    # NOTE: the v4 data backfill is NOT here.  It lives in migrate()
    # proper and runs on every call regardless of the recorded version
    # — see migrate()'s docstring for why (pinned contract + idempotent
    # + cheap), and test_db_schema_v4.py for the contract itself.

    # ----------------------------------------------------------------
    # Schema v5 upgrade (2026-09-17): collapse ``plan_routing.stage``
    # into ``current_phase``. The table used to carry both — two columns
    # that had to agree, written by disjoint code paths
    # (``try_mark_stage`` / raw ``UPDATE`` owned ``stage``;
    # ``transition_to`` / ``force_set_phase`` owned ``current_phase``),
    # kept in sync by ~10 hand-written mirror sites. ``stage`` is a
    # lossy projection of ``current_phase``, so ``current_phase`` is the
    # survivor and the projection table
    # (``plan_state._PLAN_PHASE_TO_ROUTING_STAGE``) is deleted.
    #
    # Unlike the v2/v3 ALTERs above this is a destructive rebuild — see
    # ``_v5_collapse_routing_stage`` for the guard and the backfill rule.
    # ----------------------------------------------------------------
    _v5_collapse_routing_stage(conn)

    # Record the bootstrap schema version.  We use ``INSERT OR
    # IGNORE`` so a second migrate() call (where the row already
    # exists) does not raise ``IntegrityError``. A v1 database that
    # was upgraded to v2 already has version=1; bump it to 2.
    cur = conn.execute("SELECT version FROM schema_version").fetchone()
    if cur is None:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION,),
        )
    elif cur[0] < CURRENT_SCHEMA_VERSION:
        conn.execute(
            "UPDATE schema_version SET version = ?", (CURRENT_SCHEMA_VERSION,)
        )
