"""
Tests for the TaskRefiner validate→auto-fix→secondary correction loop
and the RefinerExhausted signal.

Rule (enforced by subtask):
  1. When ``TaskRefiner`` is configured with a ``project_dir`` it must
     validate every candidate task list produced by the LLM.
  2. Validation failures that are auto-fixable (currently only
     ``depends_on`` inconsistency) are repaired automatically.
  3. If validation still fails after auto-fix, the refiner performs a
     secondary correction — another LLM call with the validation errors
     injected into the context — up to ``MAX_REFINER_ROUNDS`` times.
  4. If the task list is still invalid after the allowed rounds, the
     refiner raises :class:`RefinerExhausted` and writes a watchdog
     signal file (``plans/{plan_id}/_watchdog_signal.json``) with
     ``source="refiner"``.
"""

import os
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import refiner as refiner_mod  # noqa: E402
from refiner import RefinerExhausted, TaskRefiner  # noqa: E402


def _make_task(task_id: str, **overrides) -> dict:
    """Build a minimally valid task dict.

    ``files_to_modify`` points at a path that :func:`_seed_project`
    materialises. ``TaskOutputValidator`` step 3 rejects a declared
    path that does not exist on disk, and rejects the
    unknown-modifications sentinel outright (it means "should modify
    files, list unknown" and routes through the subagent fill loop).
    Without a real, existing path the refiner's validation loop can
    never pass, whatever the LLM returns.
    """
    base = {
        "id": task_id,
        "title": f"task {task_id}",
        "description": f"description for {task_id}",
        # 2026-09-15: the validator's step-1 schema check now rejects an
        # empty test_command (the field's model default is "" and the old
        # ``is None`` check could never fire). The fixture must satisfy
        # the enforced contract — a bare-file pytest selector is not
        # existence-checked by step 4, so the seeded placeholder is a
        # safe target.
        "test_command": "pytest src/placeholder.py -v",
        "depends_on": [],
        "files_to_modify": ["src/placeholder.py"],
    }
    base.update(overrides)
    return base


def _seed_project(project_dir: Path, *paths: str) -> None:
    """Create every path the fixture tasks declare in ``files_to_modify``."""
    for raw in paths:
        target = project_dir / raw
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()


def test_auto_fix_resolves_depends_on_without_secondary_correction(tmp_path):
    """Validate→auto-fix resolves a depends_on inconsistency in one pass."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _seed_project(project_dir, "src/placeholder.py")
    coding_tool = MagicMock()
    refiner = TaskRefiner(coding_tool=coding_tool, project_dir=str(project_dir))

    # The LLM returns task 1 whose description references task-2 but whose
    # depends_on is empty. Auto-fix should append "2", validation passes,
    # and no secondary correction is needed.
    #
    # Both tasks MUST be in the returned list. Step 2 rejects a
    # description-referenced id that is absent from the plan ("token not
    # in plan and has no children — would create a phantom dep that
    # runtime is_dependency_ready can never satisfy", validator commit
    # 07ff361), and auto_fix no longer injects such an id — so a
    # single-task candidate can never be repaired into a valid list.
    candidate = _make_task("1", description="depends on task-2", depends_on=[])
    coding_tool.query_json.return_value = {"tasks": [candidate, _make_task("2")]}

    result = refiner.refine(
        requirement="r",
        tasks=[_make_task("1"), _make_task("2")],
        last_coder_response="resp",
        last_result="result",
        exit_code=1,
        last_task_id="1",
        project_dir=str(project_dir),
    )

    by_id = {t["id"]: t for t in result}
    assert by_id["1"]["depends_on"] == ["2"], (
        f"auto-fix must append the description-referenced id verbatim "
        f"(task ids are bare, not 'task-N'); got "
        f"{by_id['1']['depends_on']!r}"
    )
    assert coding_tool.query_json.call_count == 1, (
        f"auto-fix alone must resolve this; got "
        f"{coding_tool.query_json.call_count} LLM round(s)"
    )


def test_secondary_correction_called_when_auto_fix_insufficient(tmp_path):
    """When auto-fix cannot repair the list, the refiner calls the LLM again."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _seed_project(project_dir, "src/placeholder.py")
    coding_tool = MagicMock()
    refiner = TaskRefiner(coding_tool=coding_tool, project_dir=str(project_dir))

    bad_tasks = [_make_task("1", title="")]  # step-1 failure, not auto-fixable
    good_tasks = [_make_task("1", title="fixed")]
    coding_tool.query_json.side_effect = [
        {"tasks": bad_tasks},
        {"tasks": good_tasks},
    ]

    result = refiner.refine(
        requirement="r",
        tasks=[_make_task("1")],
        last_coder_response="resp",
        last_result="result",
        exit_code=1,
        last_task_id="1",
        project_dir=str(project_dir),
    )

    assert result[0]["title"] == "fixed"
    assert coding_tool.query_json.call_count == 2
    # The second call must include validation errors so the LLM knows what to fix.
    second_context = coding_tool.query_json.call_args_list[1][0][0]
    assert "validation" in second_context.lower() or "title" in second_context.lower()


def test_refiner_exhausted_after_max_rounds(tmp_path, monkeypatch):
    """After MAX_REFINER_ROUNDS the refiner raises and writes a signal."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plans_root = tmp_path / "plans"
    monkeypatch.setattr(refiner_mod.watchdog_signal, "PLANS_ROOT", plans_root)

    coding_tool = MagicMock()
    refiner = TaskRefiner(coding_tool=coding_tool, project_dir=str(project_dir))

    # The LLM always returns an invalid task (empty title). Auto-fix cannot
    # repair step-1 errors, so every round fails and the loop exhausts.
    bad_tasks = [_make_task("1", title="")]
    coding_tool.query_json.return_value = {"tasks": bad_tasks}

    with pytest.raises(RefinerExhausted):
        refiner.refine(
            requirement="r",
            tasks=[_make_task("1")],
            last_coder_response="resp",
            last_result="result",
            exit_code=1,
            last_task_id="1",
            project_dir=str(project_dir),
            plan_id="test-plan",
        )

    signal_path = plans_root / "test-plan" / "_watchdog_signal.json"
    assert signal_path.exists(), "watchdog signal file should be written on exhaustion"
    payload = json.loads(signal_path.read_text())
    assert payload["source"] == "refiner"
    assert payload["plan_id"] == "test-plan"
    assert "exhausted" in payload["reason"].lower() or "round" in payload["reason"].lower()


def test_no_validation_loop_when_project_dir_missing():
    """Backward compatibility: without project_dir the old one-shot path is used."""
    coding_tool = MagicMock()
    refiner = TaskRefiner(coding_tool=coding_tool)

    returned = [_make_task("1", title="")]  # would fail validation if it ran
    coding_tool.query_json.return_value = {"tasks": returned}

    result = refiner.refine(
        requirement="r",
        tasks=[_make_task("1")],
        last_coder_response="resp",
        last_result="result",
        exit_code=1,
        last_task_id="1",
    )

    # Without project_dir we keep the old behaviour: return LLM output verbatim.
    assert result[0]["title"] == ""
    assert coding_tool.query_json.call_count == 1
