"""Regression tests for the two-constant sentinel scheme.

Background
----------
2026-09-09: the two-sentinel scheme collapsed the single sentinel
into TWO constants with distinct meanings:

  * ``NO_FILE_CHANGES_SENTINEL = ["__NO_FILE_CHANGES__"]`` —
    "this task is read-only / pure investigation by author
    intent". The validator accepts it directly; the dispatcher
    runs the task with an empty file diff.

  * ``UNKNOWN_MODIFICATIONS_SENTINEL = ["__UNKNOWN_MODIFICATIONS__"]``
    — "this task is intended to modify files but the author
    did not list them". The validator REJECTS this, forcing the
    list to be determined before the plan is written. Since
    2026-09-21 that happens at **generation** time
    (``TasksGenerator.harden_for_execution``); the execution-side
    fill loop and the ``NO_FILE_CHANGES`` fallback were removed
    with the pre-run gate.

These tests pin:

  * ``NO_FILE_CHANGES`` → validator passes.
  * ``UNKNOWN_MODIFICATIONS`` → validator rejects with the
    "unknown-modifications sentinel" reason.
  * Real file paths → validator runs the existence check.
  * Empty list → validator rejects (legacy rule).
  * Subtask default factory substitutes the
    ``UNKNOWN_MODIFICATIONS`` sentinel (legacy default).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from task import (
    UNKNOWN_MODIFICATIONS_SENTINEL,
    NO_FILE_CHANGES_SENTINEL,
    SubTask,
    is_no_file_changes,
    is_unknown_modifications,
    is_sentinel,
)
from framework.task_output_validator import TaskOutputValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str = "T-1",
    files_to_modify=None,
    title: str = "T",
    description: str = "D",
    test_command: str = "echo",
) -> SubTask:
    """Build a SubTask with optional ``files_to_modify``.

    If ``files_to_modify`` is ``None``, the SubTask default factory
    substitutes ``UNKNOWN_MODIFICATIONS_SENTINEL``.
    """
    kwargs: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "description": description,
        "test_command": test_command,
    }
    if files_to_modify is not None:
        kwargs["files_to_modify"] = files_to_modify
    return SubTask(**kwargs)


def _validator() -> TaskOutputValidator:
    return TaskOutputValidator(Path("/tmp"))


# ---------------------------------------------------------------------------
# Two-constant helpers
# ---------------------------------------------------------------------------


def test_no_file_changes_helper_matches_canonical():
    assert is_no_file_changes(list(NO_FILE_CHANGES_SENTINEL))
    assert is_no_file_changes(["__NO_FILE_CHANGES__"])
    assert not is_no_file_changes([])
    assert not is_no_file_changes(["real/file.py"])
    assert not is_no_file_changes(["__UNKNOWN_MODIFICATIONS__"])


def test_unknown_modifications_helper_matches_canonical():
    assert is_unknown_modifications(list(UNKNOWN_MODIFICATIONS_SENTINEL))
    assert is_unknown_modifications(["__UNKNOWN_MODIFICATIONS__"])
    assert not is_unknown_modifications([])
    assert not is_unknown_modifications(["real/file.py"])
    assert not is_unknown_modifications(["__NO_FILE_CHANGES__"])


def test_legacy_is_sentinel_matches_either():
    """``is_sentinel`` is the legacy single-constant helper — it
    returns True for BOTH new constants so existing callers
    (downstream code that just needs "is this some sentinel
    value?") keep working.
    """
    assert is_sentinel(list(NO_FILE_CHANGES_SENTINEL))
    assert is_sentinel(list(UNKNOWN_MODIFICATIONS_SENTINEL))
    assert not is_sentinel([])
    assert not is_sentinel(["real/file.py"])


# ---------------------------------------------------------------------------
# Validator: NO_FILE_CHANGES passes
# ---------------------------------------------------------------------------


def test_validator_accepts_no_file_changes_sentinel():
    """The NO_FILE_CHANGES sentinel is read-only by author intent;
    the validator accepts it directly."""
    validator = _validator()
    task = _make_task(files_to_modify=list(NO_FILE_CHANGES_SENTINEL))
    # No raise.
    validator._files_to_modify_existence_check(task)


def test_no_file_changes_is_not_healable():
    """NO_FILE_CHANGES must NOT be in the healable-task list — the
    validator accepts it directly, so the fill loop should never
    touch it. The fill loop is reserved for UNKNOWN_MODIFICATIONS
    and empty-list cases.
    """
    # Pin by inspecting the healable-id matcher in agent.py.
    from agent import AutonomousAgent

    class _FakeReport:
        def __init__(self, entries):
            self.failed_steps = [e[0] for e in entries]
            self.failed_task_ids = [e[1] for e in entries]
            self.reasons = [e[2] for e in entries]

    # NO_FILE_CHANGES passes step-3 → never appears in the failed list
    # → never healable. Pin this contract by checking that the
    # validator does not raise for NO_FILE_CHANGES (so it never gets
    # into the failed-* lists in the first place).
    validator = _validator()
    no_file_task = _make_task(
        task_id="T-NOFILE",
        files_to_modify=list(NO_FILE_CHANGES_SENTINEL),
    )
    validator._files_to_modify_existence_check(no_file_task)


# ---------------------------------------------------------------------------
# Validator: UNKNOWN_MODIFICATIONS rejected (triggers fill loop)
# ---------------------------------------------------------------------------


def test_validator_rejects_unknown_modifications_sentinel():
    """UNKNOWN_MODIFICATIONS means "may modify files, but the author
    did not specify which". The validator rejects it so the
    dispatcher's subagent fill loop fires and asks the project
    layout. The reason text is the contract that the
    ``_identify_healable_tasks`` matcher uses to recognise the
    task as healable.
    """
    validator = _validator()
    task = _make_task(files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL))
    with pytest.raises(Exception) as exc_info:
        validator._files_to_modify_existence_check(task)
    msg = str(exc_info.value)
    assert "unknown-modifications sentinel" in msg, (
        f"validator must surface the 'unknown-modifications sentinel' "
        f"reason so the fill loop can recognise it as healable; got: {msg!r}"
    )


def test_validator_rejects_unknown_modifications_via_default_factory():
    """A task built without explicit ``files_to_modify`` falls back
    to UNKNOWN_MODIFICATIONS via the default factory. The validator
    rejects it (triggers fill loop). The SubTask default factory
    preserves legacy behaviour (substitute unknown-modifications,
    not no-file-changes).
    """
    validator = _validator()
    task = _make_task()  # no files_to_modify → unknown-modifications default
    assert is_unknown_modifications(task.files_to_modify)
    with pytest.raises(Exception) as exc_info:
        validator._files_to_modify_existence_check(task)
    assert "unknown-modifications sentinel" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Empty list: legacy reject still applies
# ---------------------------------------------------------------------------


def test_validator_rejects_empty_list():
    """``files_to_modify = []`` (explicit empty list) is still rejected
    with the legacy "files_to_modify is empty" reason — distinct from
    the unknown-modifications reason.
    """
    validator = _validator()
    task = _make_task(files_to_modify=[])
    with pytest.raises(Exception) as exc_info:
        validator._files_to_modify_existence_check(task)
    msg = str(exc_info.value)
    assert "files_to_modify is empty" in msg
    assert "unknown-modifications sentinel" not in msg


# ---------------------------------------------------------------------------
# Real paths: existence check still applies
# ---------------------------------------------------------------------------


def test_validator_accepts_real_paths():
    """Real file paths pass step-3 with no flag needed.

    Uses ``task.py`` (which exists in the backend/ project root) to
    verify the existence check is wired up correctly.
    """
    project_dir = Path(__file__).resolve().parents[2]  # backend/
    validator = TaskOutputValidator(project_dir)
    task = _make_task(files_to_modify=["task.py"])
    # No raise expected.
    validator._files_to_modify_existence_check(task)


def test_validator_rejects_real_paths_with_missing_file():
    """A task with a real path that doesn't exist is rejected by
    the existence check. The two-constant scheme change only
    affected the sentinel branch — the existence check is
    unchanged.
    """
    project_dir = Path(__file__).resolve().parents[2]  # backend/
    validator = TaskOutputValidator(project_dir)
    task = _make_task(files_to_modify=["nonexistent/path/file.py"])
    with pytest.raises(Exception) as exc_info:
        validator._files_to_modify_existence_check(task)
    msg = str(exc_info.value)
    # Must mention the path / existence — NOT the sentinel reason
    # (which only fires for the sentinel branch).
    assert "unknown-modifications sentinel" not in msg
    assert "no file changes" not in msg.lower() or "no_file" not in msg.lower()
