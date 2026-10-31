"""Schema management for the state-machine WAL base layer.

The :func:`migrate` factory creates the six ``plan_*`` tables, the
four ``scheduler_*`` tables and the ``schema_version`` bootstrap
table the state-machine refactor relies on.  Every ``CREATE TABLE``
uses ``IF NOT EXISTS`` so the function is safe to call repeatedly —
calling ``migrate`` on an already-migrated database is a no-op (the
second call still returns without raising).

The six ``plan_*`` tables
-------------------------
The state-machine refactor uses one SQLite database as the single
source of truth for plan-level state.  Each table owns one concern:

  * ``plan_routing``      (current phase / substage / version)
  * ``plan_execution``    (run / pid / attempt counts)
  * ``plan_verification`` (verification round / status)
  * ``plan_artifacts``    (artifact status / content_hash)
  * ``plan_tasks``        (per-task runtime state — task #v4 split)
  * ``plan_decisions``    (human-in-the-loop decisions — HITL)

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

v6 (2026-10-16) drops ``plan_execution.current_phase``.  This is the
*other* half of the same split, left behind when v5 collapsed only the
one inside ``plan_routing``.  It was documented as a "display-only
mirror", but it was not a second vocabulary and not a second owner: all
five of its writers passed a routing word, none ever wrote a terminal
phase, and the only reader (``GET /api/plans``) therefore reported
``executing`` for plans the routing column had already finished.  With
it gone there is exactly one workflow-state column in the database.
Like v5 the drop is a full table rebuild and is one-way — take a copy
of ``state.db`` before the first v6 boot.

The HITL table (2026-10-21) adds ``plan_decisions`` — the storage for
human-in-the-loop decisions.  A decision is asked by one actor (an
executor thread, a verification thread, a route handler) and answered
by another (a human, on another thread and possibly on another
machine); the two never share memory, so the only place they can meet
is this database.  The row carries the whole fact — the question, the
options, the deadline and the answer — so an answer outlives the
process that asked the question.  It is a pure addition: no existing
column changes shape and no existing row is touched, so it needs no
rebuild and no backfill.  The context columns added to it later
(``vp_id`` / ``failure_class`` / ``round`` / ``answer_note``) follow
the same rule — they are appended with ``ALTER TABLE ... ADD
COLUMN``, which leaves every existing row exactly as it was — so they
need no version bump either.

Unlike v5 and v6 this table is NOT applied through the version-gated
schema step: it is created by :func:`_ensure_plan_decisions`, which
guards on the table itself and runs on every ``migrate`` call.  The
reason is that the version number is a *cache* of "the shape-changing
migrations have been applied", and the shape-changing migrations are
exactly the ones that need a rebuild (v5, v6) or a data lift (v4).
A new table is neither: ``CREATE TABLE IF NOT EXISTS`` is safe to
re-run, and the table's presence is a better logbook than a version
row.  The presence guard is what makes the table appear on
a database that was created before it existed, and it is why a
database that somehow reached the current version without the table
heals itself on the next ``migrate`` instead of failing every later
read with *"no such table: plan_decisions"*.

v7 (2026-10-27) adds the three scheduler tables —
``scheduler_queue`` / ``scheduler_slot`` / ``scheduler_command``.  The
scheduler keeps three facts of its own: the queue order (which plan
runs next), the slot occupancy (when a plan took a concurrency slot)
and the manual commands (a promote / pause / resume / start an
operator asked for).  Everything else the scheduler needs — the
phase, the running process, a pending human decision — is *derived*
from the ``plan_*`` tables on every tick, so the scheduler never
copies a plan fact and there is no second source of truth.

The fourth scheduler table, ``scheduler_setting`` (2026-10-29), holds
the knobs an operator changes while the scheduler runs — the capacity
override above all.  It is a pure addition in the same sense as the
other three: ``CREATE TABLE IF NOT EXISTS``, no existing table changes
shape, no existing row is touched, and no schema version is bumped —
the presence guard below is what creates it on a database that already
reached v7.

The three tables live in this database, not in a side-car file or a
second SQLite file: scheduling facts and plan facts must be readable
in one transaction, otherwise "is this plan running?" and "is this
plan in the queue?" are two facts that can disagree.  That is why the
DDL goes through the same :func:`migrate` entry point as everything
else rather than through a migration path of its own, and why
:data:`CURRENT_SCHEMA_VERSION` is bumped to 7 — the version number
is what tells an existing database that it needs the new tables.  The
fourth table, ``scheduler_setting``, was added later and deliberately
did *not* move the version number: it is created by the same
presence-guarded step, on every ``migrate`` call.

Like the HITL table, the scheduler tables are a pure addition: no
existing table changes shape and no existing row is touched.  They
are created with ``CREATE TABLE IF NOT EXISTS`` and their indexes with
``CREATE [UNIQUE] INDEX IF NOT EXISTS``, so re-running the migration
is a no-op.

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
#: DDL change so the upgrade script can branch on it.  v7 is the
#: scheduler migration: the three ``scheduler_*`` tables.  The
#: ``plan_decisions`` table is applied by the unconditional,
#: presence-guarded :func:`_ensure_plan_decisions` step rather than
#: by the version-gated schema pass — see the module docstring for
#: why.
CURRENT_SCHEMA_VERSION = 7


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
  -- NO workflow-phase column here (schema v6).  Before v6 this table
  -- carried a second ``current_phase`` beside ``plan_routing``'s — a
  -- "display-only mirror" that was supposed to describe the execution
  -- window only.  It never did.  Every one of its five writers passed
  -- a *routing* vocabulary word (``executing`` / ``verification`` /
  -- ``ready``), none of them ever wrote a terminal phase, and so the
  -- column could not represent a finished plan at all: on real data it
  -- read ``executing`` forever while ``plan_routing`` advanced to
  -- ``completed`` / ``failed``.  A strictly-lossy second copy of a
  -- value the authority column already holds exactly.  ``current_phase``
  -- is the survivor — one column, one value.  See
  -- ``_v6_drop_execution_current_phase``.
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

#: Human-in-the-loop decisions (schema v7, 2026-10-21).
#:
#: A decision is asked by one actor and answered by another, and those
#: two may live in different threads — or in different processes
#: entirely.  They share no memory, so the only place they can meet is
#: this database, and the row has to carry the whole fact on its own:
#: what was asked, which answers were on the table, what the fallback
#: would have been, when the answer is due, and who gave it.
#:
#: Column notes:
#:   * ``decision_id``   — the identity of the *question*, minted when
#:     the question is asked and kept when it is answered, so an audit
#:     trail can cite the exchange rather than a wall-clock moment.
#:   * ``options``      — JSON array of the answers that were offered.
#:     JSON rather than a join table: the option set belongs to the
#:     question, it never changes afterwards, and a new option set
#:     must not need a schema change.
#:   * ``default_option`` — the answer to fall back to when the
#:     deadline passes.  ``NULL`` means "there is no default": the
#:     decision then ends ``unroutable`` rather than being answered
#:     on the human's behalf.
#:   * ``mode``         — ``auto`` (a missed deadline falls back to
#:     ``default_option``) or ``strict`` (a missed deadline ends the
#:     decision ``unroutable``).  Closed vocabulary, mirrored by
#:     :class:`hitl.models.DecisionMode`.
#:   * ``status``       — closed vocabulary, mirrored by
#:     :class:`hitl.models.DecisionStatus`.
#:   * ``answer``       — the answer, JSON-encoded so it can be a
#:     bare option or a richer object.
#:   * ``deadline_at``  — when the answer is due.  Derived from
#:     ``timeout_sec`` at creation time and stored, so a reader does
#:     not have to re-derive it (and cannot get it wrong).
#:   * ``heartbeat_at`` — the last sign of life from the process
#:     holding the decision open.  A decision whose owner died is
#:     then distinguishable from one nobody is working on.
#:   * ``audit_seq``    — a counter bumped on every write, so two
#:     readers can order two rows without trusting the wall clock.
#:   * ``vp_id``        — the verification point the question is about,
#:     when it is about one.
#:   * ``failure_class``— the class of the failure that raised it
#:     (``criterion_defect`` / ``deliverable_defect`` / ...).
#:   * ``round``        — which round of the verification loop raised
#:     it.  The three above are what makes a decision *about* a
#:     specific failure rather than about the plan in general: an
#:     audit reader has to be able to find the decisions taken about
#:     one verification point.
#:   * ``answer_note``  — the free-form remark that travels with the
#:     answer.  It is a column and not only a line in the jsonl
#:     mirror because the mirror is a *copy*: its write failures are
#:     swallowed on purpose, so a note that lived only there could be
#:     lost while the answer it explains stayed committed.
#:
#: The four context columns are appended rather than interleaved, so
#: that a table created by this DDL and one upgraded in place (see
#: :func:`_add_missing_plan_decision_columns`) carry the same column
#: order — a reader comparing ``PRAGMA table_info`` output sees one
#: shape, not two.
#:
#: Deliberately NO foreign key to ``plan_execution``: a decision may be
#: raised before a plan has an execution row at all (an interview-stage
#: question), and the audit trail of a human answer must not vanish
#: when an execution row cascades away.
_DDL_PLAN_DECISIONS = """
CREATE TABLE IF NOT EXISTS plan_decisions (
  decision_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  source TEXT NOT NULL,
  task_id TEXT,
  question TEXT NOT NULL,
  options TEXT NOT NULL,
  default_option TEXT,
  mode TEXT NOT NULL DEFAULT 'auto',
  timeout_sec INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  answer TEXT,
  answered_by TEXT,
  answered_at TEXT,
  deadline_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  heartbeat_at TEXT,
  audit_seq INTEGER NOT NULL DEFAULT 0,
  vp_id TEXT,
  failure_class TEXT,
  round INTEGER,
  answer_note TEXT
)
"""

#: The columns added to ``plan_decisions`` after the table shipped, and
#: the type each one carries.  They are applied to an existing table
#: by :func:`_add_missing_plan_decision_columns`; the types here are
#: the same ones the DDL above declares, so an upgraded table and a
#: freshly created one are the same shape.
_PLAN_DECISION_CONTEXT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("vp_id", "TEXT"),
    ("failure_class", "TEXT"),
    ("round", "INTEGER"),
    ("answer_note", "TEXT"),
)

#: The only lookup shape a reader needs: "this plan's decisions, by
#: state" — the question every HITL reader asks first.
_DDL_PLAN_DECISIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_plan_decisions_plan_status
  ON plan_decisions(plan_id, status)
"""

_DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
)
"""

# ---------------------------------------------------------------------------
# Schema v7 — the scheduler's own facts (2026-10-27)
# ---------------------------------------------------------------------------
#
# The scheduler derives every plan fact (phase, running process, pending
# human decision) from the ``plan_*`` tables on every tick.  What it
# cannot derive is its own bookkeeping, and that is exactly what these
# three tables hold:
#
#   * ``scheduler_queue``   — the order in which queued plans may run.
#   * ``scheduler_slot``    — when a plan took a concurrency slot.
#   * ``scheduler_command`` — the manual commands an operator issued.
#
# They live in the same database as the plan facts on purpose: "is this
# plan running?" and "is this plan in the queue?" have to be readable
# in one transaction, otherwise they are two facts that can disagree.

#: The queue.  ``plan_id`` is the primary key (one row per queued plan);
#: ``seq`` is the monotonic tie-breaker that makes the order a total
#: order — it is UNIQUE so two plans can never claim the same place in
#: the queue, which would make "who runs next" ambiguous.  The order is
#: ``(promote_order IS NULL), promote_order, priority, seq``: a promoted
#: plan runs first (most recently promoted first), then by priority,
#: then by arrival.  ``promote_order`` is NULL for a plan that was
#: never promoted.
_DDL_SCHEDULER_QUEUE = """
CREATE TABLE IF NOT EXISTS scheduler_queue (
  plan_id       TEXT PRIMARY KEY,
  seq           INTEGER NOT NULL,
  priority      INTEGER NOT NULL DEFAULT 0,
  promote_order INTEGER,
  enqueued_at   TEXT NOT NULL,
  updated_at    TEXT NOT NULL
)
"""

_DDL_SCHEDULER_QUEUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduler_queue_seq
  ON scheduler_queue(seq)
"""

#: The concurrency slots.  The row records *when* the slot was taken
#: and nothing else: which plan is running is a plan fact, derived
#: from ``plan_routing`` / ``plan_execution``.  ``acquired_at`` is the
#: only time source the eviction rule (a plan that has been running
#: long enough may be evicted) is allowed to use.
_DDL_SCHEDULER_SLOT = """
CREATE TABLE IF NOT EXISTS scheduler_slot (
  plan_id     TEXT PRIMARY KEY,
  acquired_at TEXT NOT NULL,
  updated_at  TEXT NOT NULL
)
"""

#: The manual commands (promote / pause / resume / start).  This table
#: is append-only and is *never* read back to make a scheduling
#: decision — the decision lives in the queue and the slots.  It exists
#: so a command survives a backend restart instead of living in the
#: memory of the process that received it.  ``command_id`` is
#: AUTOINCREMENT so the id space is never reused, which keeps "the
#: command the operator issued at 14:03" unambiguous forever.
_DDL_SCHEDULER_COMMAND = """
CREATE TABLE IF NOT EXISTS scheduler_command (
  command_id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    TEXT NOT NULL,
  action     TEXT NOT NULL,
  outcome    TEXT NOT NULL,
  detail     TEXT,
  created_at TEXT NOT NULL
)
"""

#: The lookup a reader needs: "this plan's commands, oldest first".
_DDL_SCHEDULER_COMMAND_INDEX = """
CREATE INDEX IF NOT EXISTS idx_scheduler_command_plan
  ON scheduler_command(plan_id, created_at)
"""

#: The runtime settings.  One row per key, one value per row: the
#: scheduler's own knobs that an operator may change while the scheduler
#: runs.
#:
#: This table exists because the *file* configuration cannot hold a
#: runtime override.  ``backend/config.yaml`` is tracked by git and
#: carries the comments that explain it, so it may not be rewritten by a
#: YAML dump — and a knob the operator turns at runtime must not rewrite
#: a tracked file either.  The state database is the other place a fact
#: can live: it is already the scheduler's own storage, it is not under
#: version control, and it is read on every tick.
#:
#: ``key`` is the primary key, so a key holds exactly one value: writing
#: the same key twice replaces the value instead of adding a second row
#: that a reader would then have to choose between.
_DDL_SCHEDULER_SETTING = """
CREATE TABLE IF NOT EXISTS scheduler_setting (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
"""

#: Every object the v7 migration owns, with the DDL that creates it.
#: The name is the presence key: an object that exists needs no DDL.
_SCHEDULER_OBJECTS: tuple[tuple[str, str], ...] = (
    ("scheduler_queue", _DDL_SCHEDULER_QUEUE),
    ("idx_scheduler_queue_seq", _DDL_SCHEDULER_QUEUE_INDEX),
    ("scheduler_slot", _DDL_SCHEDULER_SLOT),
    ("scheduler_command", _DDL_SCHEDULER_COMMAND),
    ("idx_scheduler_command_plan", _DDL_SCHEDULER_COMMAND_INDEX),
    ("scheduler_setting", _DDL_SCHEDULER_SETTING),
)

# Order matters: the six ``plan_*`` tables come first, then the
# ``schema_version`` bootstrap table.  The order is pinned so the
# ``ORDER BY name`` test returns a deterministic list.
_DDL_ORDER: tuple[str, ...] = (
    _DDL_PLAN_ROUTING,
    _DDL_PLAN_EXECUTION,
    _DDL_PLAN_VERIFICATION,
    _DDL_PLAN_ARTIFACTS,
    _DDL_PLAN_TASKS,
    _DDL_PLAN_TASKS_INDEX,
    _DDL_PLAN_DECISIONS,
    _DDL_PLAN_DECISIONS_INDEX,
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


def _v6_drop_execution_current_phase(conn: sqlite3.Connection) -> None:
    """Drop ``plan_execution.current_phase``; ``plan_routing`` is the only column.

    The exact shape of the v5 collapse (full table rebuild, because the
    outgoing column carries ``NOT NULL``), pointed at the other table.

    Why the values are DISCARDED rather than backfilled
    ----------------------------------------------------
    A backfill would mean copying ``plan_execution.current_phase`` into
    ``plan_routing.current_phase`` — i.e. letting the mirror overwrite
    the authority.  That is the reverse of what v5 did (where
    ``current_phase`` won over ``stage``), and here the mirror never
    wins:

      * ``plan_routing.current_phase`` is the declared authority
        (:mod:`plan_status`, ``plan_status.PlanStatus.phase``) and the
        only source ``routes.plans._build_plan_status`` reads.
      * The mirror was never written with anything the authority column
        does not already hold.  All five of its writers passed a
        *routing* vocabulary word, and none ever wrote a terminal
        phase, so it cannot even represent a finished plan.  Measured
        on real data before this migration: one distinct value
        (``executing``), and zero rows holding a word absent from the
        routing vocabulary.
      * Every row that has a ``plan_execution`` row also has a
        ``plan_routing`` row (verified: 0 orphans), so the drop cannot
        strand a plan without any phase at all.

    So the column is strictly lossy: it can only ever be *behind* the
    authority, never ahead of it and never richer.  Copying it forward
    would import that lag into the one column everything else reads.

    Guarded by a column-presence check rather than a version number, for
    the same reason as v5: self-describing and idempotent, and the check
    runs inside the transaction so two processes racing the same upgrade
    serialise.  Transaction handling (autocommit vs. caller-owned) is
    identical to :func:`_v5_collapse_routing_stage`.
    """
    owns_txn = conn.isolation_level is None
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(plan_execution)")}
        if "current_phase" not in cols:
            if owns_txn:
                conn.execute("COMMIT")
            return

        conn.execute("DROP TABLE IF EXISTS plan_execution_v6")
        conn.execute(_DDL_PLAN_EXECUTION.replace(
            "CREATE TABLE IF NOT EXISTS plan_execution",
            "CREATE TABLE plan_execution_v6",
            1,
        ))
        conn.execute(
            "INSERT INTO plan_execution_v6 ("
            " plan_id, attempt_count, project_dir, stop_reason,"
            " task_progress, next_run_at, card_state, flags, exec_pid,"
            " exec_status, started_at, updated_at"
            ") SELECT "
            " plan_id, attempt_count, project_dir, stop_reason,"
            " task_progress, next_run_at, card_state, flags, exec_pid,"
            " exec_status, started_at, updated_at "
            "FROM plan_execution"
        )
        moved = conn.execute("SELECT count(*) FROM plan_execution_v6").fetchone()[0]
        discarded = conn.execute(
            "SELECT count(*) FROM plan_execution WHERE current_phase IS NOT NULL"
        ).fetchone()[0]
        conn.execute("DROP TABLE plan_execution")
        conn.execute("ALTER TABLE plan_execution_v6 RENAME TO plan_execution")
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
        f"[schema.v6] plan_execution.current_phase dropped — moved {moved} "
        f"rows ({discarded} stale mirror values discarded, not merged: "
        f"plan_routing.current_phase is the authority)"
    )


def _ensure_plan_decisions(conn: sqlite3.Connection) -> None:
    """Create ``plan_decisions`` when it is missing.

    Self-describing, in the same spirit as the v5 and v6 steps: the
    *presence of the table* is the logbook, not the ``schema_version``
    row.  That matters because :func:`migrate` deliberately skips the
    schema-shape DDL on a database whose recorded version is already
    current (the 2026-09-23 hot-path fix).  A database that reached
    that state without the table — a backup restored between two
    schema changes, a table dropped by hand, a partially-applied
    upgrade — would then keep failing every later read with *"no such
    table: plan_decisions"*, and no amount of re-running ``migrate``
    would fix it.

    One lookup on ``sqlite_master`` per ``migrate`` call turns that
    into a no-op.  The DDL is ``IF NOT EXISTS``, so two processes
    racing here serialise on SQLite's write lock and the loser gets a
    no-op rather than an error.

    A table that *is* there is not necessarily the current shape: a
    database written by an older backend keeps the columns it was
    created with, so it is upgraded in place rather than left behind
    (see :func:`_add_missing_plan_decision_columns`).
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='plan_decisions'"
    ).fetchone()
    if row is not None:
        _add_missing_plan_decision_columns(conn)
        return
    conn.execute(_DDL_PLAN_DECISIONS)
    conn.execute(_DDL_PLAN_DECISIONS_INDEX)


def _add_missing_plan_decision_columns(conn: sqlite3.Connection) -> None:
    """Add the context columns a table created before them is missing.

    ``CREATE TABLE IF NOT EXISTS`` cannot change a table that already
    exists, so a database written by an older backend would keep the
    18 columns it was created with — and every write that carries
    ``vp_id`` / ``failure_class`` / ``round`` / ``answer_note`` would
    fail against it.  The upgrade is therefore *additive*: read the
    live column set with ``PRAGMA table_info`` and ``ALTER TABLE`` the
    missing ones in.

    The read is what makes it safe to run on every ``migrate``: a
    current table has all four columns, so the step costs one
    ``PRAGMA`` and no write lock — the hot path a per-request
    connection open depends on is untouched.

    ``ALTER TABLE ... ADD COLUMN`` appends, so an upgraded table ends
    up with the same column order as one created by the DDL above.
    """
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(plan_decisions)")
    }
    for name, column_type in _PLAN_DECISION_CONTEXT_COLUMNS:
        if name in existing:
            continue
        try:
            conn.execute(
                f"ALTER TABLE plan_decisions ADD COLUMN {name} {column_type}"
            )
        except sqlite3.OperationalError:
            # Another process added the column between the PRAGMA and
            # the ALTER (SQLite's write lock serialises the two, so
            # this is the only way to lose the race).  The column is
            # there, which is all this step wanted.
            pass


def _ensure_scheduler_tables(conn: sqlite3.Connection) -> None:
    """Create the scheduler tables and their indexes when missing.

    The scheduler's own facts — queue order, slot occupancy, manual
    commands, runtime settings — live next to the plan facts, in this
    database, so the scheduler can read "is this plan running?" and
    "is this plan in the queue?" in one transaction.

    Guarded by the *presence of the objects* rather than by the version
    row, for the same reason as :func:`_ensure_plan_decisions`: the
    presence check is self-describing and idempotent, and a database
    that somehow reached the current version without the tables heals
    itself on the next ``migrate`` instead of failing every later read
    with *"no such table: scheduler_queue"*.  This is also why adding a
    scheduler table needs no schema version bump: a database that
    already reached v7 gets the new table on its next ``migrate``.

    One read of ``sqlite_master`` answers "is anything missing?".  When
    nothing is missing the step costs that single read and issues no
    DDL — which is what keeps the hot path (a fresh connection per
    request) free of write locks.  When something *is* missing the
    whole DDL set is replayed: every statement is ``IF NOT EXISTS``, so
    the objects that were already there are left exactly as they are.
    """
    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        )
    }
    if all(name in present for name, _ddl in _SCHEDULER_OBJECTS):
        return
    for _name, ddl in _SCHEDULER_OBJECTS:
        conn.execute(ddl)


def migrate(conn: sqlite3.Connection) -> None:
    """Create the six ``plan_*`` tables, the four ``scheduler_*``
    tables and the ``schema_version`` row.

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

    # The HITL decision table is ensured on EVERY call, for the same
    # reason the v4 data lift is: it is a self-describing, idempotent
    # step whose guard is the presence of the table itself rather than
    # the version row.  See ``_ensure_plan_decisions`` for why the
    # version-gated fast path cannot be trusted to have created it.
    # The same step brings an *existing* table up to the current shape
    # (the context columns are added to it), so an operator's database
    # written by an older backend is upgraded rather than left behind.
    _ensure_plan_decisions(conn)

    # The scheduler tables are ensured on EVERY call, for the same
    # reason the HITL table is: the scheduler's own facts (queue order,
    # slot occupancy, manual commands, runtime settings) must sit next
    # to the plan facts in this database, and the objects' presence is a
    # better logbook than the version row.  The version bump to 7 is
    # what tells an existing database that the migration ran; the
    # presence guard is what makes a database that somehow missed the
    # tables heal itself instead of failing every later read.
    _ensure_scheduler_tables(conn)


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

    # ----------------------------------------------------------------
    # Schema v6 upgrade (2026-10-16): drop ``plan_execution.current_phase``.
    # The remaining second workflow-state column.  Unlike v5 there is no
    # backfill question to answer — the column was a lossy mirror of
    # ``plan_routing.current_phase``, never written with a terminal
    # phase and never holding a word the routing vocabulary lacks, so
    # merging it forward would only import its lag into the authority.
    # See ``_v6_drop_execution_current_phase``.
    # ----------------------------------------------------------------
    _v6_drop_execution_current_phase(conn)

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
