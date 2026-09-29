"""TaskRepository — single read/write path for ``tasks.json``.

Architectural decision points 1 & 2
-----------------------------------
Decision point 1 ("dispatcher only changes runtime state fields")
forces ``tasks.json`` writes to be channeled through one place —
the dispatcher must not call ``json.dump`` or ``open(..., "w")`` on
``tasks.json`` directly. This repository is that single place.

Decision point 2 ("write goes through a single atomic interface")
binds the write to a CAS protocol: every update reads the current
``version`` for the task, computes the new version, and only
applies the change if the version is still what we read. A
concurrent writer that bumps the version between our read and our
write causes the predicate to fail with :class:`ConflictError`,
and the dispatcher is expected to re-read and retry.

Allowed-fields contract
-----------------------
The dispatcher may only change runtime state fields. The canonical
allow-list is ``{status, commit_sha, attempt, schedule_ts, end_ts}``
(see :data:`ALLOWED_WRITE_FIELDS`). Any attempt to write a
structural field (``depends_on``, ``title``, ``description``,
``files_to_modify``, ...) raises :class:`ValidationError` — this is
the safety net that prevents a future refactor from accidentally
turning the dispatcher into a structural writer.

The atomicity layer
-------------------
Writes happen via a single ``open(tasks_file, "w")`` + ``json.dump``
under :attr:`_lock`. We do not implement temp-file + ``os.replace``
here because the dispatcher's writes are already serialised through
:attr:`_lock`; the original implementation simply used a process-
local :class:`threading.Lock`. We keep that semantic and add the
optimistic-concurrency check on top so a stale read becomes a
:class:`ConflictError` instead of a silent overwrite.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config_paths import resolve_state_db_path


# The single source of truth for "what can the dispatcher write".
# Task 3-1 nailed this allow-list down. Any field outside it is a
# structural field (describes WHAT the task is, not HOW it is
# progressing through the dispatcher), so the repository must
# refuse the write with ``ValidationError`` — otherwise a future
# refactor can silently turn the dispatcher into a structure-mutating
# writer, which is the bug class the architecture decision points
# were created to prevent.
#
# Task #3.8 (per-task runtime → plan_execution.task_progress):
# the dispatcher no longer writes through this class — runtime state
# now goes to ``PlanTaskRepository`` (see
# ``state_machine.repositories.plan_task_repository``). The constant
# is retained as the architectural contract pin: any caller that
# still imports ``ALLOWED_WRITE_FIELDS`` should switch to
# ``ALLOWED_TASK_FIELDS`` from the new module.
ALLOWED_WRITE_FIELDS: frozenset = frozenset(
    {
        "status",
        "end_ts",
    }
)


class ConflictError(Exception):
    """Optimistic-concurrency failure on ``update_status``.

    Raised by :meth:`TaskRepository.update_status` when the row's
    version has advanced between the caller's snapshot read
    (:meth:`get_version`) and the update. The dispatcher is
    expected to re-read, decide whether the update is still needed,
    and either retry up to K times or surface a hard error to
    ``execution.log`` and abort the task.

    The ``reason`` field carries a human-readable explanation that
    the dispatcher writes into the structured log so an operator
    auditing ``execution.log`` can correlate the failure with the
    version-drift event that caused it.
    """

    def __init__(
        self,
        task_id: str,
        reason: str,
        current_version: Optional[int] = None,
    ) -> None:
        self.task_id = task_id
        self.reason = reason
        self.current_version = current_version
        super().__init__(
            f"ConflictError(task_id={task_id!r}, reason={reason!r}, "
            f"current_version={current_version!r})"
        )


class ValidationError(Exception):
    """Raised when ``update_status`` is asked to write a disallowed field.

    The dispatcher is restricted to runtime state fields
    (see :data:`ALLOWED_WRITE_FIELDS`); structural fields
    (``depends_on``, ``title``, ``description``, ``files_to_modify``,
    ``test_command``, ...) MUST be changed by a separate, audited
    code path. Asking :meth:`TaskRepository.update_status` to write
    such a field is a programming bug, not a runtime condition, so
    we raise :class:`ValidationError` immediately and refuse the
    write.
    """

    def __init__(
        self,
        task_id: str,
        forbidden_fields: Iterable[str],
    ) -> None:
        self.task_id = task_id
        self.forbidden_fields = sorted(set(forbidden_fields))
        super().__init__(
            f"ValidationError(task_id={task_id!r}, "
            f"forbidden_fields={self.forbidden_fields!r}): dispatcher is "
            f"only allowed to write fields in "
            f"{sorted(ALLOWED_WRITE_FIELDS)!r}"
        )


class TaskRepository:
    """Read-only loader for the static ``tasks.json`` envelope.

    Task #3.8 split ``tasks.json`` into a static-only definition file.
    Per-task runtime state (``status`` / ``end_ts`` / ``commit_sha`` /
    ``attempt`` / ``schedule_ts`` / ``_repo_version``) moved to
    ``plan_execution.task_progress`` via
    :class:`state_machine.repositories.plan_task_repository.PlanTaskRepository`.

    The class retains:

      * :meth:`load_all` — read the static envelope (requirement,
        stop_reason, reason_detail, tasks). Used by
        :class:`task_manager.TaskManager.load_tasks` and the
        ``/api/execution/{plan_id}/progress`` endpoint.

    The legacy :meth:`update_status` and :meth:`get_version` methods
    are still present as thin shims that forward to
    :class:`PlanTaskRepository` (with a sqlite connection resolved
    from the env-var / repo-root path that mirrors
    ``server._state_db_path``). The shims keep external callers
    compiling while we cut the dispatcher over to the new repo.
    Once all callers have migrated, the shims and the legacy
    ``_lock`` will be removed.

    The repository does NOT validate the *task graph* (cycles,
    missing dependencies) — that remains the responsibility of
    ``_validate_dependencies`` in ``agent.py``.
    """

    def __init__(self, tasks_file: Path):
        self._tasks_file = Path(tasks_file)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    @property
    def tasks_file(self) -> Path:
        """The canonical ``tasks.json`` path this repository reads/writes."""
        return self._tasks_file

    def load_all(self) -> dict:
        """Read the entire ``tasks.json`` document.

        Returns a dict with keys ``requirement``, ``stop_reason``,
        ``reason_detail``, ``tasks`` (a list of row dicts). The
        legacy bare-list shape is normalised into the envelope
        form here, so callers can always rely on the envelope.

        Raises
        ------
        FileNotFoundError
            If ``tasks_file`` does not exist.
        json.JSONDecodeError
            If the file is not valid JSON.
        """
        with open(self._tasks_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return {
                "requirement": data.get("requirement", ""),
                "stop_reason": data.get("stop_reason", None),
                "reason_detail": data.get("reason_detail", None),
                "tasks": list(data.get("tasks", [])),
            }
        # Legacy bare-list shape: synthesise an envelope with empty
        # metadata. The dispatcher only cares about ``tasks``.
        return {
            "requirement": "",
            "stop_reason": None,
            "reason_detail": None,
            "tasks": list(data),
        }

    def get_version(self, task_id: str) -> int:
        """Legacy shim — return the per-task ``_repo_version``.

        Task #3.8 moved per-task runtime state to
        :class:`PlanTaskRepository`. The version is now read from
        ``plan_execution.task_progress.tasks[task_id]._repo_version``
        rather than from ``tasks.json``.

        Raises ``KeyError`` if the row is absent in BOTH ``tasks.json``
        AND SQLite. The dispatcher must migrate to
        ``PlanTaskRepository.get_version`` directly; this shim exists
        only so legacy callers compile.
        """
        try:
            return self._plan_task_repo().get_version(
                self._plan_id(), task_id,
            )
        except Exception:
            # Fall back to the legacy tasks.json read for plans that
            # have not been migrated yet.
            doc = self.load_all()
            for row in doc["tasks"]:
                if row.get("id") == task_id:
                    v = row.get("_repo_version", 0)
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return 0
            raise KeyError(
                f"Task {task_id!r} not found in {self._tasks_file}"
            )

    # ------------------------------------------------------------------
    # Write API — legacy shims (Task #3.8)
    # ------------------------------------------------------------------

    def update_status(
        self,
        task_id: str,
        fields: Dict[str, Any],
        expected_version: int,
    ) -> None:
        """Legacy shim — apply a runtime-state update via :class:`PlanTaskRepository`.

        Task #3.8 moved per-task runtime state into
        ``plan_execution.task_progress.tasks``. This shim forwards to
        ``PlanTaskRepository.update_task`` so callers that still use
        ``TaskRepository.update_status`` keep working during the
        migration window.

        On a validation failure (`` ``TaskProgressValidationError``) or
        version mismatch (``TaskProgressConflictError``) the
        corresponding legacy exception (``ValidationError`` /
        ``ConflictError``) is raised, preserving the original
        contract for tests that still exercise this path.
        """
        # Map legacy exception types to the new ones.
        try:
            from state_machine.repositories.plan_task_repository import (
                PlanTaskRepository,
                TaskProgressConflictError,
                TaskProgressValidationError,
            )
        except ImportError:
            # The new repo is unavailable — fail loud so the caller
            # knows the migration is not yet in effect.
            raise RuntimeError(
                "PlanTaskRepository not importable; "
                "TaskRepository.update_status shim requires it (task #3.8)."
            )
        try:
            self._plan_task_repo().update_task(
                self._plan_id(), task_id, fields, expected_version,
            )
        except TaskProgressValidationError as exc:
            # Re-raise as the legacy ValidationError.
            raise ValidationError(task_id=task_id, forbidden_fields=exc.forbidden_fields)
        except TaskProgressConflictError as exc:
            # Re-raise as the legacy ConflictError.
            raise ConflictError(
                task_id=task_id,
                reason=str(exc),
                current_version=exc.current_version,
            )

    # ------------------------------------------------------------------
    # Internal: sqlite path resolution + PlanTaskRepository factory
    # ------------------------------------------------------------------

    def _plan_id(self) -> str:
        """Derive ``plan_id`` from ``tasks_file``.

        Convention used by the executor (``plans/{plan_id}/tasks.json``
        for canonical, ``project_dir/tasks.json`` for legacy) — the
        directory name is the plan id.
        """
        return self._tasks_file.parent.name

    def _plan_task_repo(self):
        """Open a hermetic-or-canonical SQLite connection + return a repo."""
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        db_path = resolve_state_db_path()
        conn = _open_db(db_path)
        _migrate(conn)
        return PlanTaskRepository(conn)


__all__ = [
    "ALLOWED_WRITE_FIELDS",
    "ConflictError",
    "ValidationError",
    "TaskRepository",
]