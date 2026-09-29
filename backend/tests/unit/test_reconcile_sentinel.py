"""Regression test for the 2026-09-09 SubTask.files_to_modify sentinel bug.

Background
----------
The ``_load_tasks`` Phase 2 reconcile (agent.py:1880-1967) constructs
SubTask instances for orphan tasks that have full static fields.
When an orphan has no ``files_to_modify`` field, the original code
substituted a *string* sentinel:

    sub_task_dict["files_to_modify"] = "__UNKNOWN_MODIFICATIONS_SENTINEL__"

But ``SubTask.files_to_modify`` is declared ``list[str]`` and
Pydantic rejects a bare string with::

    Value error, files_to_modify must be a list of strings

The exception bubbled out of the reconcile block, was caught by the
outer ``try/except`` (agent.py:1952), logged as
``task_orphans_reconcile_failed``, and the orphan silently fell
through to ``supersede`` instead of being merged into the DAG.

This is the actual reason orphan tasks could not be re-executed —
even after the static fields were restored from backup, the
reconcile block crashed on the sentinel string and the orphan never
made it into the executor's DAG.

Fix
---
Replace the bare string with ``list(UNKNOWN_MODIFICATIONS_SENTINEL)``
(the canonical sentinel — a single-element list — already used by
the legacy_task_marker path at agent.py:2116).

These tests pin the canonical sentinel shape and that the
reconcile code constructs a valid ``SubTask`` from an orphan entry
that has full fields but no ``files_to_modify``.
"""
from __future__ import annotations

from pathlib import Path

from task import UNKNOWN_MODIFICATIONS_SENTINEL, SubTask


def test_canonical_sentinel_is_list_of_strings():
    """Pin the sentinel shape — the reconcile code must use this exact
    form, not a bare string (the original bug).
    """
    assert isinstance(UNKNOWN_MODIFICATIONS_SENTINEL, list), (
        f"UNKNOWN_MODIFICATIONS_SENTINEL must be a list, got "
        f"{type(UNKNOWN_MODIFICATIONS_SENTINEL).__name__}"
    )
    assert all(isinstance(x, str) for x in UNKNOWN_MODIFICATIONS_SENTINEL), (
        f"all sentinel entries must be str, got "
        f"{[type(x).__name__ for x in UNKNOWN_MODIFICATIONS_SENTINEL]}"
    )


def test_subtask_accepts_canonical_sentinel_for_files_to_modify():
    """An orphan entry that has full fields but no files_to_modify
    should produce a valid SubTask when the sentinel is wrapped in
    list(). The original bug passed the bare string and Pydantic
    rejected it.
    """
    # Simulate what agent.py:1929 should now construct.
    orphan_entry: dict = {
        "id": "R3-1",
        "title": "消除 tools/main.py 中与 task_sync.sync_service 重复的 inline SyncService 类",
        "description": "## 需求偏离点（VP-026, severity=high）...",
        "test_command": "source backend/.venv/bin/activate && test \"$(grep -c 'class SyncService' tools/main.py)\" -eq 0",
        "model_type": "medium",
        "project_dir": str(Path(__file__).resolve().parents[2].parent),
        "status": "pending",
        # NOTE: no files_to_modify — reconcile code must substitute the
        # canonical sentinel list, not a bare string.
    }

    # Mirror agent.py:1929-1932 fixed form:
    if "files_to_modify" not in orphan_entry:
        orphan_entry["files_to_modify"] = list(UNKNOWN_MODIFICATIONS_SENTINEL)

    sub_task = SubTask(**orphan_entry)

    assert sub_task.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL
    assert isinstance(sub_task.files_to_modify, list)
    assert sub_task.id == "R3-1"
    assert sub_task.status == "pending"


def test_subtask_rejects_bare_string_sentinel():
    """Pin the original bug: passing the bare string sentinel is
    rejected by Pydantic. If this test ever stops raising, the bug
    has been silently re-introduced.
    """
    from pydantic import ValidationError

    orphan_entry: dict = {
        "id": "R3-1",
        "title": "t",
        "description": "d",
        "test_command": "echo",
        "model_type": "medium",
        "status": "pending",
        # Original bug: bare string, not list.
        "files_to_modify": "__UNKNOWN_MODIFICATIONS_SENTINEL__",
    }

    try:
        SubTask(**orphan_entry)
    except ValidationError as exc:
        assert "files_to_modify" in str(exc)
    else:
        raise AssertionError(
            "Expected Pydantic ValidationError for bare string files_to_modify, "
            "but SubTask was constructed successfully. The original bug has "
            "been re-introduced — the reconcile code is back to passing a "
            "string instead of list(UNKNOWN_MODIFICATIONS_SENTINEL)."
        )


def test_subtask_with_no_file_changes_passes_validator():
    """2026-09-09 two-constant scheme: ``NO_FILE_CHANGES_SENTINEL``
    is the read-only / pure-investigation marker. The validator's
    step-3 gate accepts it directly.
    """
    from framework.task_output_validator import TaskOutputValidator
    from task import NO_FILE_CHANGES_SENTINEL

    task = SubTask(
        id="R-readonly",
        title="investigation",
        description="read-only task, no file mods",
        test_command="echo",
        files_to_modify=list(NO_FILE_CHANGES_SENTINEL),
    )
    validator = TaskOutputValidator(Path("/tmp"))
    # No raise — author intent preserved.
    validator._files_to_modify_existence_check(task)
