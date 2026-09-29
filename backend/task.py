"""
Task Data Model
================

SubTask data model representing a single task in the development workflow.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Two-value sentinel scheme (2026-09-09). The single
# ``UNKNOWN_MODIFICATIONS`` value is replaced by two distinct
# constants with two distinct meanings:
#
#   * ``NO_FILE_CHANGES_SENTINEL`` = "this task is read-only /
#     pure investigation / no file modifications". The
#     validator's step-3 gate accepts it directly (author intent).
#     The dispatcher runs the task; it commits an empty diff and
#     moves on.
#
#   * ``UNKNOWN_MODIFICATIONS_SENTINEL`` = "this task is intended
#     to modify files, but the plan author / refiner did not (or
#     could not) list which ones". The validator's step-3 gate
#     rejects this so the dispatcher's subagent fill loop fires
#     and asks the project layout. After ``MAX_FILES_FILL_ATTEMPTS``
#     rounds, if the subagent still cannot determine the file
#     list, the dispatcher falls back to ``NO_FILE_CHANGES_SENTINEL``
#     to break the loop (an "unknown → read-only" assumption is
#     safer than hanging the executor forever).
#
# Why two constants instead of one:
#   * A single sentinel conflates "author declared read-only"
#     with "author did not specify files". Conflating these
#     caused the 2026-09-09 audit's silent-fail pathology: a
#     refiner that forgot to specify ``files_to_modify`` was
#     indistinguishable from a deliberately-read-only task.
#   * The two constants let the validator express "read-only
#     accepted unconditionally" vs "unknown-modifications
#     triggers fill loop" without needing a separate marker
#     flag or source-discrimination logic.
NO_FILE_CHANGES_SENTINEL = ["__NO_FILE_CHANGES__"]
UNKNOWN_MODIFICATIONS_SENTINEL = ["__UNKNOWN_MODIFICATIONS__"]


def is_no_file_changes(files: List[str]) -> bool:
    """Return ``True`` iff ``files`` equals the NO_FILE_CHANGES sentinel list."""
    return list(files) == list(NO_FILE_CHANGES_SENTINEL)


def is_unknown_modifications(files: List[str]) -> bool:
    """Return ``True`` iff ``files`` equals the UNKNOWN_MODIFICATIONS sentinel list."""
    return list(files) == list(UNKNOWN_MODIFICATIONS_SENTINEL)


# Legacy single-sentinel compatibility shim. Older ``tasks.json``
# files written before the two-constant scheme only carry the
# unknown-modifications sentinel value. Treat that as the legacy read-only fallback to
# keep pre-migration plans dispatchable; migration (see
# ``scripts/migrate_sentinels.py`` in the plan) rewrites them to
# the correct constant.
def is_sentinel(files: List[str]) -> bool:
    """Legacy: True iff ``files`` matches either sentinel.

    Retained for backwards compatibility — the validator's step-3
    gate uses :func:`is_no_file_changes` /
    :func:`is_unknown_modifications` to discriminate the two
    meanings, but downstream code that only needs "is this some
    sentinel value?" can keep using ``is_sentinel``.
    """
    return (
        is_no_file_changes(files) or is_unknown_modifications(files)
    )


class SubTask(BaseModel):
    """Represents a single subtask in the autonomous coding workflow."""

    id: str
    title: str
    description: str
    test_command: str = ""
    test_commands: Optional[List[str]] = Field(default_factory=list)
    status: str = "pending"
    updated_time: Optional[str] = None
    failure_reason: Optional[str] = None
    project_dir: Optional[str] = None
    model_type: str = "medium"
    depends_on: List[str] = Field(default_factory=list)
    breakdown_count: int = 0
    provider: Optional[str] = None
    # ``task_group`` is stamped by the orchestrator on repair tasks
    # (``"repair-round-N"``, and legacy ``"repair-round-N"`` for the
    # pre-v9 ``RP-*`` id scheme). It is behaviour-bearing: the refiner's
    # reconcile loop uses ``startswith("repair")`` on this field to
    # refuse to delete, edit or drop a task whose id is bound to a
    # verification round it never saw (see ``refiner_structure``).
    #
    # 2026-09-16 bug fix: the field was missing from this model, and
    # ``model_config = ConfigDict(extra="ignore")`` silently discarded
    # it on every ``SubTask(**row)`` construction. That made the guard a
    # no-op — ``t.get("task_group")`` in the refiner path is read off
    # ``[t.model_dump() for t in task_manager.tasks]``, which never
    # carried the key, so every ``repair-*`` row the refiner failed to
    # echo back was deleted from ``plan_tasks``. The 2026-09-12 guard
    # that was supposed to prevent exactly that only ever had a
    # source-text test, which kept passing.
    task_group: Optional[str] = None
    files_to_modify: List[str] = Field(
        default_factory=lambda: list(UNKNOWN_MODIFICATIONS_SENTINEL)
    )
    # 2026-08-19 audit: a task that declares ``verification_only=True``
    # is exempt from the empty-output gate. Mark perf-benchmark / smoke
    # / harness-style tasks with this so the subagent can pass test
    # commands without producing a file diff. Defaults to False so the
    # existing behaviour is preserved for every pre-fix tasks.json.
    verification_only: bool = False
    # 2026-09-11: per-task git commit SHA produced by
    # ``AutonomousAgent._commit_task_changes``. Optional because most
    # states (``pending`` / ``in_progress`` / ``failed``) have no
    # commit yet, and the runtime overlay (server.py
    # ``_RUNTIME_OVERLAY_FIELDS``) carries the canonical value into
    # the per-task row at write time. Without this field declared on
    # the Pydantic model, Pydantic v2's strict ``__setattr__`` raises
    # ``ValueError: object has no field commit_sha`` whenever the
    # executor tries to assign it.
    commit_sha: Optional[str] = None

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @staticmethod
    def _truncate(text: Optional[str], limit: int) -> str:
        if text is None:
            return ""
        if len(text) <= limit:
            return text
        return text[: limit - 50] + "\n\n[... description truncated ...]"

    @field_validator("title", mode="before")
    @classmethod
    def _validate_title(cls, v: Any) -> str:
        return cls._truncate(v, 200)

    @field_validator("description", mode="before")
    @classmethod
    def _validate_description(cls, v: Any) -> str:
        return cls._truncate(v, 8000)

    @field_validator("test_command", mode="before")
    @classmethod
    def _validate_test_command(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("test_commands", mode="before")
    @classmethod
    def _validate_test_commands(cls, v: Any) -> List[str]:
        if v is None:
            return []
        return list(v)

    @field_validator("status", mode="before")
    @classmethod
    def _validate_status(cls, v: Any) -> str:
        return "pending" if v is None else str(v)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _validate_depends_on(cls, v: Any) -> List[str]:
        if v is None:
            return []
        return list(v)

    @field_validator("breakdown_count", mode="before")
    @classmethod
    def _validate_breakdown_count(cls, v: Any) -> int:
        if v is None or v == "":
            return 0
        return int(v)

    @field_validator("model_type", mode="before")
    @classmethod
    def _validate_model_type(cls, v: Any) -> str:
        if v is None:
            return "medium"
        value = str(v).strip().lower()
        if value in ("complex", "high", "hard", "advanced"):
            return "complex"
        if value in ("medium", "mid", "normal", "default"):
            return "medium"
        return "medium"

    @field_validator("provider", mode="after")
    @classmethod
    def _validate_provider(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    @field_validator("files_to_modify", mode="before")
    @classmethod
    def _validate_files_to_modify(cls, v: Any) -> List[str]:
        # Missing field (``None``) → pessimistic "unknown modifications"
        # sentinel so the dispatcher serialises the task into its own
        # micro-layer. This is the right default for code that has
        # been in production since 2026-Q2 — most on-disk ``tasks.json``
        # files pre-date the explicit-empty-list convention and an
        # empty list there means "old plan, I have no idea what this
        # touches" rather than "the LLM authoritatively declared no
        # modifications". An explicit empty list ``[]`` is preserved
        # as-is so a plan author who DOES know the task is read-only
        # (e.g. "调研 / 定位 / 文档") gets the lightweight "no file
        # conflict" semantics in :func:`_build_micro_layers` instead
        # of being serialised for safety. Without this, every
        # audit-style task in a plan is serialised, so the whole layer
        # runs solo instead of in parallel.
        if v is None:
            return list(UNKNOWN_MODIFICATIONS_SENTINEL)
        if v == []:
            return []

        if not isinstance(v, (list, tuple)):
            raise ValueError("files_to_modify must be a list of strings")

        normalized: List[str] = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("each entry in files_to_modify must be a string")

            # Reject empty paths; they are not valid relative file names.
            if item == "":
                raise ValueError("files_to_modify entries must not be empty strings")

            # Absolute paths are not allowed; tasks must declare project-relative
            # modifications only.
            if os.path.isabs(item) or re.match(r"^[A-Za-z]:[\\/]", item):
                raise ValueError(f"files_to_modify paths must be relative: {item!r}")

            # Reverse-directory traversal is forbidden.
            if ".." in Path(item).parts:
                raise ValueError(
                    f"files_to_modify paths must not contain '..': {item!r}"
                )

            normalized.append(item)

        return normalized

    def get_test_commands(self) -> List[str]:
        """Return the list of test commands to run for this task.

        If test_commands is set, returns that list.
        Otherwise falls back to test_command (single command or empty).
        """
        if self.test_commands:
            return self.test_commands
        if self.test_command:
            return [self.test_command]
        return []

    def model_dump(
        self,
        *,
        mode: str = "python",
        exclude_none: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Convert task to dictionary.
        """
        result: Dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "test_command": self.test_command,
            "status": self.status,
            "model_type": self.model_type,
        }
        if self.test_commands:
            result["test_commands"] = list(self.test_commands)
        if self.depends_on:
            result["depends_on"] = list(self.depends_on)
        if self.breakdown_count:
            # Only serialise when non-zero so tasks that never entered
            # the breakdown flow don't pick up a noisy ``breakdown_count: 0``
            # row on every persist.
            result["breakdown_count"] = self.breakdown_count
        if not exclude_none or self.updated_time is not None:
            result["updated_time"] = self.updated_time
        if not exclude_none or self.failure_reason is not None:
            result["failure_reason"] = self.failure_reason
        if not exclude_none or self.project_dir is not None:
            result["project_dir"] = self.project_dir
        # Mirror the same opt-in pattern as ``breakdown_count`` above:
        # only serialise ``provider`` when set, so existing tasks.json
        # files that never carried the field stay byte-identical on
        # round-trip.
        if self.provider:
            result["provider"] = self.provider
        # Same opt-in pattern: only serialise files_to_modify when it
        # differs from the sentinel so legacy tasks.json files remain
        # unchanged unless the task explicitly declares a modification
        # list.
        if self.files_to_modify != UNKNOWN_MODIFICATIONS_SENTINEL:
            result["files_to_modify"] = list(self.files_to_modify)
        # 2026-08-19 audit: only serialise verification_only when True
        # so existing tasks.json files that never carried the field
        # stay byte-identical on round-trip.
        if self.verification_only:
            result["verification_only"] = True
        # Same opt-in pattern as ``provider``: only serialise
        # ``task_group`` when set. Without this the field is readable on
        # the model but absent from ``model_dump()`` — and the refiner
        # path reads the group off ``model_dump()`` output, so the
        # repair-task guard would silently stop matching again.
        if self.task_group:
            result["task_group"] = self.task_group
        return result

    def model_dump_json(self, *, indent: Optional[int] = None, **kwargs: Any) -> str:
        """Serialise the task to JSON using the custom dict representation."""
        return json.dumps(
            self.model_dump(exclude_none=kwargs.get("exclude_none", False)),
            indent=indent,
        )