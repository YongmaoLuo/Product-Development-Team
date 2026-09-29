"""Repository layer for per-task runtime + static state.

Schema v4 (2026-09-09): per-task state moves
OUT of the legacy ``plan_execution.task_progress`` JSON column INTO the
proper relational ``plan_tasks`` table created by v4 schema.  This
module is the single read/write path for that table.

Why a real table, not a JSON column
====================================

The legacy implementation (``task #3.8``) kept per-task entries inside
the ``plan_execution.task_progress`` JSON column.  Every write went
through SELECT-JSON-decode-mutate-encode-UPDATE-整列, which is a
read-modify-write at the application level.  Two concurrent writers
could interleave: thread A reads the JSON, thread B reads the JSON,
both decode, both modify, both encode, both UPDATE — whichever
committed last wins, the other's changes are silently lost.

This module replaces the legacy read-modify-write with direct SQLite
``INSERT ... ON CONFLICT ... DO UPDATE`` and ``DELETE`` statements
against the ``plan_tasks`` table.  SQLite's row-level atomic write
guarantees that two writers cannot lose updates — a lost repair-task
row was traced to exactly this race window.

Contract (legacy API preserved for caller compatibility)
======================================================

The public API mirrors the legacy :class:`PlanTaskRepository`:

  * :meth:`get_task` — read one entry.
  * :meth:`get_version` — read per-task ``_repo_version`` (audit only;
    no longer used for CAS conflict detection).
  * :meth:`load_all` — read all entries for a plan.
  * :meth:`update_task` — INSERT-or-UPDATE a runtime entry.
  * :meth:`delete_task` — DELETE one entry.
  * :meth:`iter_orphan_tasks` — yield entries whose task_id is not in
    ``disk_ids``.
  * :meth:`add_task` — refiner / verification-repair INSERT-or-UPDATE
    for a static-fields entry.

Behavioural changes from the legacy API (audit-only):

  * ``TaskProgressNotFound`` is no longer raised — ``plan_tasks`` does
    not depend on ``plan_execution``.  An import of the symbol still
    works but the runtime path never raises it.
  * ``TaskProgressConflictError`` is no longer raised — SQLite's
    row-level atomic write replaces application-level CAS.  A second
    write always succeeds; the second writer wins (last-writer-wins).
    Callers that previously retried on ``ConflictError`` should now no-op.
  * ``_repo_version`` is preserved as an audit-only counter on every
    row, bumped on each write.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

__all__ = [
    "ALLOWED_TASK_FIELDS",
    "ALLOWED_STATIC_TASK_FIELDS",
    "PlanTaskRepository",
    "TaskProgressConflictError",
    "TaskProgressValidationError",
]


#: Field names the dispatcher may write for a single task (runtime).
#: Plan v4: ``failure_reason`` and ``breakdown_count`` are runtime
#: columns on the ``plan_tasks`` table; the legacy allow-list omitted
#: them.  We add them here so the dispatcher can record failure
#: reasons via the same ``update_task`` entry point.
ALLOWED_TASK_FIELDS: frozenset = frozenset(
    {
        "status",
        "commit_sha",
        "attempt",
        "schedule_ts",
        "end_ts",
        "failure_reason",
        "breakdown_count",
    }
)

#: Static task fields that ``add_task`` accepts from refiner / verification
#: repair rounds.
ALLOWED_STATIC_TASK_FIELDS: frozenset = frozenset(
    {
        "id",
        "title",
        "description",
        "test_command",
        "files_to_modify",
        "depends_on",
        "model_type",
        "project_dir",
        "provider",
        "breakdown_count",
        "task_group",
        "execution_group",
        "priority",
        "acceptance_criteria",
        "failed_vp_id",
        "round",
    }
)

#: List-typed columns get JSON-encoded when written and decoded on read
#: so the SQLite TEXT column round-trips the Python list/dict shape.
_LIST_COLUMNS: frozenset = frozenset(
    {"files_to_modify", "depends_on"}
)


_SAFE_TASK_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_COMMIT_SHA_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode_field(key: str, value: Any) -> Any:
    """JSON-encode list-typed columns for SQLite storage."""
    if key in _LIST_COLUMNS and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value


def _decode_field(key: str, value: Any) -> Any:
    """JSON-decode list-typed columns when reading from SQLite."""
    if key in _LIST_COLUMNS and value is not None and isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return value
        return decoded
    return value


# ---------------------------------------------------------------------------
# Exceptions (legacy symbols preserved for caller compatibility)
# ---------------------------------------------------------------------------


class TaskProgressConflictError(Exception):
    """Legacy CAS conflict exception.

    Plan v4: no longer raised by ``update_task`` — SQLite row-level
    atomic writes replaced application-level CAS.  Retained as an
    importable symbol so callers that imported it (e.g. for
    ``except TaskProgressConflictError:`` branches) keep working.
    The branch is now dead code; callers should remove it.
    """

    def __init__(
        self,
        plan_id: str,
        task_id: str,
        expected_version: int,
        current_version: int,
    ) -> None:
        self.plan_id = plan_id
        self.task_id = task_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"TaskProgressConflictError(plan_id={plan_id!r}, "
            f"task_id={task_id!r}, expected={expected_version}, "
            f"current={current_version})"
        )


class TaskProgressValidationError(Exception):
    """Raised when ``update_task`` is asked to write a disallowed field."""

    def __init__(self, task_id: str, forbidden_fields) -> None:
        self.task_id = task_id
        self.forbidden_fields = sorted(set(forbidden_fields))
        super().__init__(
            f"TaskProgressValidationError(task_id={task_id!r}, "
            f"forbidden_fields={self.forbidden_fields!r})"
        )


# ---------------------------------------------------------------------------
# Legacy alias — preserved so callers that imported it keep working
# ---------------------------------------------------------------------------


#: Legacy alias — preserved for caller compatibility.  ``plan_tasks``
#: no longer depends on a ``plan_execution`` row, so this is never
#: raised by the v4 implementation.
TaskProgressNotFound = type(
    "TaskProgressNotFound",
    (Exception,),
    {
        "__init__": lambda self, plan_id, task_id=None: Exception.__init__(
            self,
            f"TaskProgressNotFound(plan_id={plan_id!r}"
            + (f", task_id={task_id!r}" if task_id is not None else "")
            + ")",
        )
    },
)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


#: All columns the repository may write — kept in one place so the
#: ``INSERT ... ON CONFLICT ... DO UPDATE`` SQL stays in sync with
#: the table DDL.
_WRITABLE_RUNTIME_COLUMNS: tuple[str, ...] = (
    "status",
    "end_ts",
    "schedule_ts",
    "attempt",
    "commit_sha",
    "failure_reason",
    "breakdown_count",
    "_repo_version",
    "updated_at",
)

_WRITABLE_STATIC_COLUMNS: tuple[str, ...] = (
    "title",
    "description",
    "test_command",
    "files_to_modify",
    "depends_on",
    "model_type",
    "project_dir",
    "provider",
    "task_group",
    "execution_group",
    "priority",
    "acceptance_criteria",
    "failed_vp_id",
    "round",
    "updated_at",
)

#: The subset of :data:`_WRITABLE_RUNTIME_COLUMNS` that is *runtime state*
#: rather than bookkeeping. ``add_task`` must never write these onto an
#: existing row: the value it carries for them is the insert-branch
#: default (``status="pending"``), so writing them on conflict resets a
#: finished task to "not started yet". See ``add_task``'s docstring.
_RUNTIME_STATE_ONLY_COLUMNS: tuple[str, ...] = tuple(
    col for col in _WRITABLE_RUNTIME_COLUMNS
    if col not in ("_repo_version", "updated_at")
)


class PlanTaskRepository:
    """Single read/write path for per-task state in the v4 ``plan_tasks`` table.

    All public methods route through SQLite's row-level atomic writes;
    no application-level read-modify-write, no per-method mutex, no
    application-level CAS.  Concurrent writers race but never lose
    updates — whichever commits last wins, with no silent failures.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_task(self, plan_id: str, task_id: str) -> Optional[dict[str, Any]]:
        """Return the per-task row as a dict, or ``None`` if absent."""
        cur = self._conn.execute(
            "SELECT status, end_ts, schedule_ts, attempt, commit_sha, "
            "failure_reason, breakdown_count, _repo_version, title, "
            "description, test_command, files_to_modify, depends_on, "
            "model_type, project_dir, provider, task_group, "
            "execution_group, priority, acceptance_criteria, "
            "failed_vp_id, round "
            "FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = (
            "status", "end_ts", "schedule_ts", "attempt", "commit_sha",
            "failure_reason", "breakdown_count", "_repo_version", "title",
            "description", "test_command", "files_to_modify", "depends_on",
            "model_type", "project_dir", "provider", "task_group",
            "execution_group", "priority", "acceptance_criteria",
            "failed_vp_id", "round",
        )
        entry: dict[str, Any] = {}
        for col, val in zip(cols, row):
            entry[col] = _decode_field(col, val)
        return entry

    def get_version(self, plan_id: str, task_id: str) -> int:
        """Return the per-task ``_repo_version`` (0 if absent).

        Plan v4: this is audit-only.  ``update_task`` no longer raises
        on version mismatch (SQLite row-level atomic write replaces
        CAS); callers that pass ``expected_version`` are recorded but
        not enforced.
        """
        cur = self._conn.execute(
            "SELECT _repo_version FROM plan_tasks "
            "WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        )
        row = cur.fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    def load_all(self, plan_id: str) -> dict[str, dict[str, Any]]:
        """Return ``{task_id: entry-dict}`` for the plan; ``{}`` if absent."""
        cur = self._conn.execute(
            "SELECT task_id, status, end_ts, schedule_ts, attempt, "
            "commit_sha, failure_reason, breakdown_count, _repo_version, "
            "title, description, test_command, files_to_modify, "
            "depends_on, model_type, project_dir, provider, task_group, "
            "execution_group, priority, acceptance_criteria, "
            "failed_vp_id, round "
            "FROM plan_tasks WHERE plan_id = ?",
            (plan_id,),
        )
        result: dict[str, dict[str, Any]] = {}
        cols = (
            "status", "end_ts", "schedule_ts", "attempt", "commit_sha",
            "failure_reason", "breakdown_count", "_repo_version", "title",
            "description", "test_command", "files_to_modify", "depends_on",
            "model_type", "project_dir", "provider", "task_group",
            "execution_group", "priority", "acceptance_criteria",
            "failed_vp_id", "round",
        )
        for row in cur.fetchall():
            task_id = row[0]
            entry: dict[str, Any] = {}
            for col, val in zip(cols, row[1:]):
                entry[col] = _decode_field(col, val)
            result[task_id] = entry
        return result

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def update_task(
        self,
        plan_id: str,
        task_id: str,
        fields: dict[str, Any],
        expected_version: int = 0,
    ) -> None:
        """Apply a runtime-state update to ``task_id``.

        Plan v4: SQL is ``INSERT ... ON CONFLICT ... DO UPDATE`` —
        one statement, row-level atomic.  ``expected_version`` is
        accepted for caller compatibility but is NOT used as a CAS
        guard; SQLite's atomic write replaces it.

        Partial update: only the runtime columns present in ``fields``
        are touched.  A column the payload omits keeps whatever the row
        already had — so ``{"commit_sha": sha}`` records the commit and
        leaves ``status`` / ``end_ts`` / ``attempt`` alone.  Callers
        that want a column cleared pass it explicitly as ``None``.
        (A brand-new row is the exception: there is no prior state, so
        omitted columns start NULL.)
        """
        del expected_version  # accepted for caller compat; not used
        # ---- Validation ----
        if not isinstance(task_id, str) or not _SAFE_TASK_ID_RE.fullmatch(task_id):
            raise TaskProgressValidationError(task_id, ["task_id"])
        forbidden = [k for k in fields.keys() if k not in ALLOWED_TASK_FIELDS]
        if forbidden:
            raise TaskProgressValidationError(task_id, forbidden)
        for key, value in fields.items():
            if key == "commit_sha" and value is not None:
                if (
                    not isinstance(value, str)
                    or not _SAFE_COMMIT_SHA_RE.fullmatch(value)
                ):
                    raise TaskProgressValidationError(task_id, ["commit_sha"])

        # ---- Write: one INSERT-or-UPDATE statement ----
        now_iso = _now_iso()
        # Read current version (used to bump _repo_version on the
        # existing path; for the INSERT branch _repo_version starts at 1)
        cur = self._conn.execute(
            "SELECT _repo_version FROM plan_tasks "
            "WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        )
        row = cur.fetchone()
        current_version = int(row[0]) if row else 0
        new_version = current_version + 1

        # Build INSERT-or-UPDATE for runtime fields. Missing fields
        # pass as None so SQLite stores NULL — the schema declares
        # every column nullable except (plan_id, task_id, updated_at).
        # That is correct for the INSERT branch: a brand-new row has no
        # prior state to protect.
        runtime_cols = ("status", "end_ts", "schedule_ts", "attempt",
                        "commit_sha", "failure_reason", "breakdown_count")
        values: list[Any] = [_encode_field(c, fields.get(c)) for c in runtime_cols]
        values.append(new_version)
        values.append(now_iso)

        all_cols = ("plan_id", "task_id") + runtime_cols + (
            "_repo_version", "updated_at",
        )
        placeholders = ", ".join("?" for _ in all_cols)
        # 2026-09-23 — the conflict arm sets ONLY the runtime columns the
        # payload actually carries. ``update_task`` is a partial-update
        # API: ``_persist_commit_sha_to_sqlite`` writes one column, and
        # ``agent._persist_task_status`` copies just the in-memory
        # attributes that are not None. An absent key therefore means
        # "leave it alone", not "clear it".
        #
        # Writing every ``runtime_cols`` entry as ``excluded.*`` made
        # each partial write a full one, clamping the absent keys to
        # NULL — the 0921 (and 0923 re-run) failure mode. The
        # ``update_task_commit_sha`` call eleven lines after
        # ``update_task_status(task, "completed")`` erased the
        # ``completed`` it had just written, so every finished task
        # ended the run with ``status IS NULL`` in ``plan_tasks``.
        # Nothing reads a NULL status as terminal: the progress card
        # counted 0 completed out of 21 for the whole run, and
        # ``_load_tasks`` re-hydrated each one as ``pending`` and
        # re-dispatched it.
        #
        # ``_persist_status_to_sqlite`` used to lean on the clamp to
        # clear ``failure_reason`` on a non-failed status; it now states
        # that explicitly.
        present_runtime_cols = tuple(c for c in runtime_cols if c in fields)
        update_assignments = ", ".join(
            f"{col} = excluded.{col}" for col in present_runtime_cols
            + ("_repo_version", "updated_at")
        )
        self._conn.execute(
            f"INSERT INTO plan_tasks ({', '.join(all_cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(plan_id, task_id) DO UPDATE SET "
            f"{update_assignments}",
            (plan_id, task_id, *values),
        )

    def delete_task(
        self,
        plan_id: str,
        task_id: str,
        expected_version: Optional[int] = None,
    ) -> bool:
        """Remove ``task_id`` from the plan.

        Returns ``True`` if a row was removed, ``False`` if absent.
        ``expected_version`` is accepted for caller compat but is
        audit-only (not enforced).
        """
        del expected_version  # accepted for caller compat; not used
        if not isinstance(task_id, str) or not _SAFE_TASK_ID_RE.fullmatch(task_id):
            raise TaskProgressValidationError(task_id, ["task_id"])
        cur = self._conn.execute(
            "DELETE FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        )
        return cur.rowcount > 0

    def iter_orphan_tasks(
        self,
        plan_id: str,
        disk_ids: set[str],
    ) -> Iterator[dict[str, Any]]:
        """Yield entries from ``plan_tasks`` whose id is NOT in ``disk_ids``."""
        cur = self._conn.execute(
            "SELECT task_id, status, end_ts, schedule_ts, attempt, "
            "commit_sha, failure_reason, breakdown_count, _repo_version, "
            "title, description, test_command, files_to_modify, "
            "depends_on, model_type, project_dir, provider, task_group, "
            "execution_group, priority, acceptance_criteria, "
            "failed_vp_id, round "
            "FROM plan_tasks WHERE plan_id = ?",
            (plan_id,),
        )
        cols = (
            "status", "end_ts", "schedule_ts", "attempt", "commit_sha",
            "failure_reason", "breakdown_count", "_repo_version", "title",
            "description", "test_command", "files_to_modify", "depends_on",
            "model_type", "project_dir", "provider", "task_group",
            "execution_group", "priority", "acceptance_criteria",
            "failed_vp_id", "round",
        )
        for row in cur.fetchall():
            tid = row[0]
            if tid in disk_ids:
                continue
            entry: dict[str, Any] = {"id": tid}
            for col, val in zip(cols, row[1:]):
                entry[col] = _decode_field(col, val)
            yield entry

    def iter_by_task_group_prefix(
        self,
        plan_id: str,
        prefix: str,
    ) -> Iterator[dict[str, Any]]:
        """Yield entries from ``plan_tasks`` whose ``task_group`` starts with ``prefix``.

        2026-09-12 (RP-* persistence bug fix): the repair-task
        loader in :func:`server._load_repair_tasks_for_progress` needs
        to enumerate every ``repair-round-*`` row so the Feishu card
        keeps tracking in-flight repair work even when the on-disk
        ``verification_repair_tasks.json`` snapshot is missing or
        truncated. ``startswith`` matches both legacy ``RP-*`` ids
        (stamped ``task_group="repair"`` by the pre-v9 orchestrator)
        and post-v9 ``R{number}-*`` ids (``task_group="repair-round-N"``).
        """
        if not isinstance(prefix, str) or not prefix:
            return
        cur = self._conn.execute(
            "SELECT task_id, status, end_ts, schedule_ts, attempt, "
            "commit_sha, failure_reason, breakdown_count, _repo_version, "
            "title, description, test_command, files_to_modify, "
            "depends_on, model_type, project_dir, provider, task_group, "
            "execution_group, priority, acceptance_criteria, "
            "failed_vp_id, round "
            "FROM plan_tasks WHERE plan_id = ? AND task_group LIKE ?",
            (plan_id, prefix + "%"),
        )
        cols = (
            "status", "end_ts", "schedule_ts", "attempt", "commit_sha",
            "failure_reason", "breakdown_count", "_repo_version", "title",
            "description", "test_command", "files_to_modify", "depends_on",
            "model_type", "project_dir", "provider", "task_group",
            "execution_group", "priority", "acceptance_criteria",
            "failed_vp_id", "round",
        )
        for row in cur.fetchall():
            tid = row[0]
            entry: dict[str, Any] = {"id": tid}
            for col, val in zip(cols, row[1:]):
                entry[col] = _decode_field(col, val)
            yield entry

    def add_task(
        self,
        plan_id: str,
        task_dict: dict[str, Any],
        expected_version: int = 0,
    ) -> int:
        """Insert a sub-task; an existing row keeps its runtime state.

        ``expected_version`` defaults to ``0`` (insert-only).  Pass
        the current version for replace semantics — same contract as
        the legacy version, but no CAS is enforced (last-writer-wins
        via ``ON CONFLICT DO UPDATE``).

        Runtime state is never clobbered (2026-09-22).  The conflict
        arm updates the STATIC columns only; it does not touch
        ``status`` / ``end_ts`` / ``failure_reason`` / ``attempt`` /
        ``schedule_ts`` / ``commit_sha`` / ``breakdown_count``.  Before
        this, the statement carried ``status = excluded.status`` with
        ``excluded.status`` pinned to ``"pending"``, so any re-add of an
        id that already had a row silently reset a completed task back
        to ``pending``.  That is the same "rewriting the task list
        clears runtime state" hazard that made the 0921 run re-execute
        eight finished tasks: the disk file carries no status, so the
        reset was the only surviving copy of the truth, and it said
        "not done yet".

        Callers that genuinely want to reset a task must do so through
        :meth:`update_task` (an explicit runtime write), not by
        re-adding it.

        Returns the new ``_repo_version``.
        """
        del expected_version  # accepted for caller compat; not used
        if not isinstance(task_dict, dict):
            raise TaskProgressValidationError("<non-dict>", ["task_dict"])
        task_id = task_dict.get("id")
        if not isinstance(task_id, str) or not _SAFE_TASK_ID_RE.fullmatch(task_id):
            raise TaskProgressValidationError(str(task_id), ["id"])
        forbidden = [k for k in task_dict.keys() if k not in ALLOWED_STATIC_TASK_FIELDS]
        if forbidden:
            raise TaskProgressValidationError(task_id, forbidden)

        # Read current version for bump
        cur = self._conn.execute(
            "SELECT _repo_version FROM plan_tasks "
            "WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        )
        row = cur.fetchone()
        current_version = int(row[0]) if row else 0
        new_version = current_version + 1

        now_iso = _now_iso()
        static_cols = _WRITABLE_STATIC_COLUMNS[:-1]  # exclude updated_at
        values: dict[str, Any] = {
            "status": "pending",
            "_repo_version": new_version,
            "updated_at": now_iso,
        }
        for col in static_cols:
            if col in task_dict and col != "id":
                values[col] = _encode_field(col, task_dict[col])

        all_cols = ("plan_id", "task_id") + tuple(values.keys())
        placeholders = ", ".join("?" for _ in all_cols)
        # 2026-09-22 — the conflict arm updates static columns only.
        # ``status`` and the other runtime columns are written on the
        # INSERT branch (a brand-new row starts ``pending``) and are
        # deliberately ABSENT from the SET list so an existing row keeps
        # whatever runtime state it already had. See the docstring.
        _RUNTIME_STATE_COLUMNS = frozenset(_RUNTIME_STATE_ONLY_COLUMNS)
        update_cols = tuple(
            k for k in values.keys() if k not in _RUNTIME_STATE_COLUMNS
        )
        update_assignments = ", ".join(
            f"{col} = excluded.{col}" for col in update_cols
        )
        if not update_assignments:
            # Degenerate: nothing static to update. Leave the existing
            # row untouched rather than emitting invalid SQL.
            cur = self._conn.execute(
                "SELECT _repo_version FROM plan_tasks "
                "WHERE plan_id = ? AND task_id = ?",
                (plan_id, task_id),
            )
            _existing = cur.fetchone()
            if _existing is not None:
                return int(_existing[0])
            update_assignments = f"_repo_version = excluded._repo_version"
        self._conn.execute(
            f"INSERT INTO plan_tasks ({', '.join(all_cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(plan_id, task_id) DO UPDATE SET "
            f"{update_assignments}",
            (plan_id, task_id) + tuple(values[c] for c in values.keys()),
        )
        return new_version