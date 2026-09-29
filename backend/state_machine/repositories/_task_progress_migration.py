"""One-shot migration of legacy ``tasks.json`` runtime fields into SQLite.

Task #3.8 split ``tasks.json`` into a static-only definition file. For
backward compatibility, every existing plan on disk still has the old
shape — runtime fields (``status`` / ``end_ts`` / ``commit_sha`` /
``attempt`` / ``schedule_ts`` / ``failure_reason`` / ``breakdown_count`` /
``_repo_version``) live alongside the static fields.

This helper runs at server startup and for each affected plan:

  1. Reads ``tasks.json``.
  2. Extracts every runtime field per task row into a per-task dict.
  3. Writes those per-task entries into the ``plan_tasks`` table via
     :class:`state_machine.repositories.plan_task_repository.PlanTaskRepository`.
  4. Rewrites ``tasks.json`` with only the static fields (and a
     top-level ``_task_progress_migrated`` sentinel so a re-run is a
     no-op).

Schema v4 normalisation (2026-09-09): per-task state lives in
``plan_tasks`` (one row per task), no longer in the legacy
``plan_execution.task_progress``
JSON column.  Static fields (id / title / description /
test_command / depends_on / files_to_modify) and runtime fields
(status / end_ts / commit_sha / attempt / schedule_ts /
failure_reason / breakdown_count) are all written to ``plan_tasks``
columns.

The migration is **idempotent** — a sentinel in the rewritten
``tasks.json`` blocks a second run from re-extracting the same rows.
Skipped silently when:

  * ``tasks.json`` is missing (no plan) — return.
  * ``tasks.json`` already carries ``_task_progress_migrated: true``.
  * ``plan_execution`` has no row for ``plan_id`` yet (the executor
    has not been started, so there is nothing to migrate onto).
  * No runtime fields exist in any row (nothing to lift).

The result is the file contract pinned by
:data:`STATIC_TASK_FIELDS` — same shape as what
:class:`task_manager.TaskManager.load_tasks` reads on cold start.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "migrate_tasks_json_to_progress",
    "STATIC_TASK_FIELDS",
    "RUNTIME_TASK_FIELDS",
    "MIGRATION_SENTINEL",
]


#: Static-only fields a ``tasks.json`` row is allowed to carry after
#: migration. Anything outside this set is treated as runtime and
#: lifted into SQLite.
STATIC_TASK_FIELDS: frozenset = frozenset(
    {
        "id",
        "title",
        "description",
        "test_command",
        "test_commands",
        "project_dir",
        "model_type",
        "depends_on",
        "provider",
        "files_to_modify",
    }
)

#: Runtime fields lifted into ``plan_execution.task_progress.tasks``.
#: ``_repo_version`` is included so the per-task CAS counter survives
#: the migration (otherwise the first post-migration write would
#: collide with a default ``0`` baseline).
RUNTIME_TASK_FIELDS: frozenset = frozenset(
    {
        "status",
        "end_ts",
        "schedule_ts",
        "attempt",
        "commit_sha",
        "failure_reason",
        "breakdown_count",
        "_repo_version",
    }
)

#: Top-level sentinel written into the migrated file so a re-run is a
#: no-op. Picked at the file level (rather than on the SQLite row)
#: because ``tasks.json`` is the legacy artefact being normalised.
MIGRATION_SENTINEL: str = "_task_progress_migrated"


def _load_json(path: Path) -> Optional[dict]:
    """Read and parse ``path``; return ``None`` on any failure."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic ``tmp + os.replace`` write of ``data`` to ``path``."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def _plan_execution_row_exists(conn: sqlite3.Connection, plan_id: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM plan_execution WHERE plan_id = ?",
        (plan_id,),
    )
    return cur.fetchone() is not None


def _has_runtime_fields(rows: list[dict]) -> bool:
    """True iff at least one row carries a runtime field."""
    for row in rows:
        if any(field in row for field in RUNTIME_TASK_FIELDS):
            return True
    return False


def migrate_tasks_json_to_progress(
    plan_id: str,
    tasks_file: Path,
    conn: sqlite3.Connection,
) -> bool:
    """Migrate ``tasks_file`` runtime fields into ``plan_execution.task_progress``.

    Parameters
    ----------
    plan_id:
        The plan whose runtime state to migrate.
    tasks_file:
        Path to the legacy ``tasks.json``.
    conn:
        An open SQLite connection (autocommit mode). The function does
        NOT commit; the caller owns the txn.

    Returns
    -------
    bool
        ``True`` if a migration happened (file rewritten, SQLite
        updated), ``False`` if the call was a no-op.

    Notes
    -----
    No exception swallowing: a real read/write failure propagates so
    the caller can surface it. The function is idempotent at the
    file level via the ``MIGRATION_SENTINEL`` flag.
    """
    from state_machine.repositories.plan_task_repository import (
        ALLOWED_TASK_FIELDS,
        PlanTaskRepository,
    )

    data = _load_json(tasks_file)
    if data is None:
        return False

    # Sentinel check only meaningful for envelope-shape files.
    if isinstance(data, dict) and data.get(MIGRATION_SENTINEL) is True:
        # Already migrated — leave the file alone.
        return False

    # Resolve the row list — envelope or legacy bare-list.
    if isinstance(data, dict):
        rows = list(data.get("tasks", []) or [])
    elif isinstance(data, list):
        rows = list(data)
    else:
        return False

    if not _has_runtime_fields(rows):
        # Nothing to lift — but mark the file as migrated so the static
        # file contract is documented on disk.
        if isinstance(data, dict):
            data[MIGRATION_SENTINEL] = True
            _atomic_write_json(tasks_file, data)
            return True
        return False

    # The executor must have created a plan_execution row already.
    # If not, the runtime has nowhere to land — skip silently so the
    # next start (when the executor runs) can retry.
    if not _plan_execution_row_exists(conn, plan_id):
        return False

    repo = PlanTaskRepository(conn)

    # ---- Build the new per-task payload (split into static + runtime) ----
    for row in rows:
        task_id = row.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        # Split the legacy row into static + runtime fields.  Static
        # fields go via ``add_task`` (inserts the row with pending
        # status); runtime fields go via ``update_task`` (writes
        # status / end_ts / etc.).  SQLite-wins semantics: if the row
        # already exists, skip the static insert (it would clobber
        # the current task definition) and still write any runtime
        # fields that the on-disk row carries.
        static_entry = {
            k: v for k, v in row.items()
            if k in STATIC_TASK_FIELDS and k != "id"
        }
        static_entry["id"] = task_id
        runtime_entry: dict[str, Any] = {}
        for field in RUNTIME_TASK_FIELDS:
            if field in row and row[field] is not None:
                # ``_repo_version`` is dropped in v4 — version is
                # just an audit counter, not a CAS baseline.
                if field == "_repo_version":
                    continue
                runtime_entry[field] = row[field]

        # SQLite-wins: only add if row doesn't exist
        if repo.get_version(plan_id, task_id) == 0:
            try:
                repo.add_task(plan_id, static_entry)
            except Exception:
                # Static fields may include non-allow-listed keys
                # (e.g. test_commands — only ``test_command`` is in
                # the static allow-list).  Drop non-allow-listed
                # keys and retry.
                allowed_static = {
                    "id", "title", "description", "test_command",
                    "depends_on", "files_to_modify", "model_type",
                    "provider", "project_dir",
                }
                filtered = {
                    k: v for k, v in static_entry.items()
                    if k in allowed_static
                }
                repo.add_task(plan_id, filtered)

        if runtime_entry:
            # v4: ``expected_version=0`` → last-writer-wins
            # (no application-level CAS).
            repo.update_task(
                plan_id, task_id, runtime_entry, expected_version=0,
            )

    # ---- Rewrite ``tasks.json`` in the static-only shape ----
    if isinstance(data, dict):
        # Build the static-only row list.
        static_rows = []
        for row in rows:
            static_rows.append(
                {k: v for k, v in row.items() if k in STATIC_TASK_FIELDS}
            )
        # Preserve envelope fields, drop runtime metadata, add sentinel.
        new_data = {
            "requirement": data.get("requirement", ""),
            "stop_reason": data.get("stop_reason", None),
            "reason_detail": data.get("reason_detail", None),
            "tasks": static_rows,
            MIGRATION_SENTINEL: True,
        }
    else:
        # Legacy bare-list — wrap in envelope with empty metadata.
        static_rows = [
            {k: v for k, v in row.items() if k in STATIC_TASK_FIELDS}
            for row in rows
        ]
        new_data = {
            "requirement": "",
            "stop_reason": None,
            "reason_detail": None,
            "tasks": static_rows,
            MIGRATION_SENTINEL: True,
        }

    _atomic_write_json(tasks_file, new_data)
    return True