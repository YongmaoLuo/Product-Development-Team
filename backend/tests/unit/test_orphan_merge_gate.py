"""
The orphan merge gate in ``AutonomousAgent._load_tasks`` Phase 2.

Background
--------------------------------------
``_load_tasks`` walks ``plan_tasks`` rows whose id is not in
``tasks.json`` (orphans) and decides, per row, between:

  * **merge** — the row carries real content; build a runnable
    ``SubTask`` from it;
  * **placeholder** — the row has no static fields; synthesise a
    content-free ``SubTask`` so the dispatcher can still see it.

The merge gate used to require ``title``, ``test_command`` AND
``description``. two orphan rows had a title and a
full description but no ``test_command``, so they fell through to the
placeholder branch — and the placeholder's
``"recovered from plan_tasks DB (no static fields on disk...)"``
description REPLACED the real one. The subagent then reported, quite
accurately, that it had "no description, no files_to_modify, and no
test_command", and the task was marked failed for a defect the loader
had introduced moments earlier.

These tests pin the gate. The behavioural half (that the gate is
actually consulted on the real code path) is guarded structurally at
the bottom, because ``_load_tasks`` needs a full agent + plan dir +
SQLite to drive — see ``tests/test_repair_single_writer.py`` for the
heavyweight harness that covers the path end to end.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import (  # noqa: E402
    _ORPHAN_MERGE_REQUIRED_FIELDS,
    _orphan_has_mergeable_content,
)


def _entry(**kw) -> dict:
    """A ``plan_tasks`` orphan row, shaped as ``iter_orphan_tasks`` yields."""
    base = {
        "id": "repair-r4-01",
        "title": None,
        "description": None,
        "test_command": None,
        "files_to_modify": None,
        "status": "pending",
    }
    base.update(kw)
    return base


DESC = (
    "背景：PRD/架构设计决策点 5 明确要求『盘整路径行为冻结』——"
    "select_entering_consolidation 的函数体相对 9/2 基线不得有实质性 diff。"
)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_title_and_description_merge_without_a_test_command():
    """The regression: this row used to be destroyed by the placeholder.

    This is the exact shape those orphans had:
    ``plan_tasks`` — title and description present, no test_command.
    """
    assert _orphan_has_mergeable_content(
        _entry(
            title="恢复 select_entering_consolidation 函数体的行为冻结",
            description=DESC,
            test_command=None,
        )
    ) is True


def test_empty_string_command_still_merges():
    """``""`` and ``None`` are the same gap, not different verdicts."""
    assert _orphan_has_mergeable_content(
        _entry(title="t", description=DESC, test_command="")
    ) is True


def test_a_concrete_command_still_merges():
    """The common case must not regress while fixing the rare one."""
    assert _orphan_has_mergeable_content(
        _entry(title="t", description=DESC, test_command="pytest -q")
    ) is True


@pytest.mark.parametrize(
    "missing", ["title", "description"]
)
def test_each_required_field_is_still_required(missing):
    """Dropping ``test_command`` must not drop the other two."""
    fields = {"title": "t", "description": DESC}
    fields[missing] = None
    assert _orphan_has_mergeable_content(_entry(**fields)) is False


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_whitespace_only_values_do_not_count(blank):
    """A whitespace title is no more actionable than a missing one."""
    assert _orphan_has_mergeable_content(
        _entry(title=blank, description=DESC)
    ) is False
    assert _orphan_has_mergeable_content(
        _entry(title="t", description=blank)
    ) is False


def test_a_command_alone_is_not_enough():
    """A probe command with no description is still unusable."""
    assert _orphan_has_mergeable_content(
        _entry(title=None, description=None, test_command="pytest -q")
    ) is False


def test_fully_empty_row_still_goes_to_the_placeholder():
    """The placeholder branch must keep working for genuine residue.

    title-less orphans are
    description-less rows — they should still be surfaced as
    placeholders (when non-terminal) rather than merged.
    """
    assert _orphan_has_mergeable_content(_entry()) is False


def test_missing_entry_is_not_mergeable():
    """Defensive: a malformed entry must not raise on the load path."""
    assert _orphan_has_mergeable_content(None) is False


def test_test_command_is_not_a_required_field():
    """Pin the field list so a future edit cannot quietly restore it."""
    assert "test_command" not in _ORPHAN_MERGE_REQUIRED_FIELDS, (
        "test_command was re-added to the orphan merge gate; rows with "
        "a title and description but no command will once again have "
        "their real content replaced by a placeholder"
    )
    assert set(_ORPHAN_MERGE_REQUIRED_FIELDS) == {"title", "description"}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _load_tasks_code() -> str:
    """``_load_tasks`` body, with comment lines removed."""
    src = (BACKEND_DIR / "agent.py").read_text(encoding="utf-8")
    start = src.index("    def _load_tasks(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    return "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_load_tasks_consults_the_gate():
    """Guard the call site — a predicate nothing calls proves nothing."""
    assert "_orphan_has_mergeable_content(entry)" in _load_tasks_code(), (
        "_load_tasks no longer routes the orphan decision through "
        "_orphan_has_mergeable_content"
    )


def test_load_tasks_does_not_reintroduce_an_inline_field_tuple():
    """The old inline tuple is what drifted; keep the gate single-sourced."""
    body = _load_tasks_code()
    assert "_ORPHAN_REQUIRED_FIELDS" not in body, (
        "_load_tasks declares its own required-fields tuple again; the "
        "gate contract lives in _ORPHAN_MERGE_REQUIRED_FIELDS"
    )


def test_merged_orphans_without_a_command_are_logged():
    """Operators must be able to tell this case apart in execution.log."""
    body = _load_tasks_code()
    assert "task_orphan_merged_without_test_command" in body, (
        "merging a command-less orphan is now silent; the operator "
        "cannot distinguish a generation defect from a task that "
        "genuinely needs no command"
    )
