"""Unit tests for the unclaimed-failing-files wiring in the verification loop.

Why this exists
---------------
Section 1 of the plan: when a repair round's gate fails on a file that
**no task declared**, the round must not be judged dead. The missing
piece was the wiring: :mod:`verification.scope_claims` knows how to build
the "failing file -> the task that declares it" table, and
:mod:`repair_generator` knows how to render an unclaimed-file list into
the prompt — but the orchestrator computed neither, so the list was
never handed to the refiner and the round ended on a verdict instead of
on another iteration.

What is pinned here
-------------------
* the unclaimed files reach the generator (``unclaimed_files=``);
* a round that named no failing file neither writes the table nor passes
  the argument — an empty table would read as a conclusion;
* the table lands in the plan directory as
  ``verification_scope_claims.json``;
* a missing task list makes every failing file unclaimed instead of
  crashing the round;
* a file a task *does* declare is claimed, not unclaimed;
* a table that cannot be written is a warning, not a reason to end the
  round;
* the generator's own ``RepairGenerationError`` semantics are unchanged.

No network / database — pure in-process unit tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The artefact the ownership table is written to.
ARTIFACT_NAME = "verification_scope_claims.json"

#: The round's own output: one case failed, in a file no task declares.
RUN_OUTPUT = "FAILED backend/coding_tool.py::test_a - assert 1 == 2"

#: The failing file the run output names.
FAILED_FILE = "backend/coding_tool.py"

#: The plan's task list — task 3 declares a *different* file.
PLAN_TASKS = [{"id": "3", "files_to_modify": ["backend/agent.py"]}]

#: A repair row the generator produced.
REPAIR_CONTENT = {
    "failed_vp_id": "VP-1",
    "title": "fix the coding tool",
    "description": "adjust the coding tool",
    "acceptance_criteria": "the case passes again",
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Hermetic state.db — the orchestrator persists through it."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _write_plan_state(plan_dir: Path) -> None:
    state = {
        "plan_id": plan_dir.name,
        "current_phase": "executing",
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


def _failed_report(run_output: str) -> dict:
    """The round's report — the failed point carries the round's output."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-1",
                "status": "FAILED",
                "title": "coding tool",
                "actual_result": run_output,
                "evidence": run_output,
            },
        ],
        "requirement_deviations": [],
    }


def _failed_vps() -> list:
    return [{"id": "VP-1", "title": "coding tool", "priority": "high"}]


class _CapturingGenerator:
    """A repair generator that records the keyword arguments it was given."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []
        self.kwargs: dict = {}

    def generate_repair_contents(self, failed_vps, round_number, **kwargs):
        self.calls.append({
            "failed_vps": list(failed_vps),
            "round_number": round_number,
        })
        self.kwargs = dict(kwargs)
        return list(self.contents)


def _run_cycle(
    tmp_path: Path,
    contents,
    *,
    run_output: str = RUN_OUTPUT,
    plan_tasks=None,
    generator=None,
    gen_side_effect=None,
    logger=None,
    block_artifact: bool = False,
):
    """Run one failed verification round through the orchestrator.

    Returns ``(result, assembler_mock, generator, plan_dir)``.

    ``plan_tasks=None`` means *no* ``tasks.json`` at all — the plan's
    task list is missing, so nothing can be claimed.
    ``block_artifact=True`` puts a directory where the artefact goes, so
    the write cannot succeed.
    """
    from verification import VerificationOrchestrator

    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_plan_state(plan_dir)
    if plan_tasks is not None:
        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": plan_tasks}), encoding="utf-8"
        )
    if block_artifact:
        (plan_dir / ARTIFACT_NAME).mkdir()

    report = _failed_report(run_output)

    with patch(
        "verification.verification_report_reader.extract_failed_vps_with_paths",
        return_value=_failed_vps(),
    ), patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch("repair_generator.RepairTaskAssembler") as MockAssembler:
        MockAssembler.return_value.assemble.return_value = []

        orch = VerificationOrchestrator(plan_dir, project_dir, object())
        orch.verification_agent.run_full_verification.return_value = report
        if generator is not None:
            orch.repair_generator = generator
        elif gen_side_effect is not None:
            orch.repair_generator.generate_repair_contents.side_effect = (
                gen_side_effect
            )
        else:
            orch.repair_generator.generate_repair_contents.return_value = list(
                contents
            )
        if logger is not None:
            orch.logger = logger

        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report, round_number=1)

    return result, MockAssembler, orch.repair_generator, plan_dir


def _assembled_rows(assembler_mock) -> list:
    """The content rows the assembler was handed."""
    return list(assembler_mock.return_value.assemble.call_args[0][0])


def _artifact(plan_dir: Path) -> dict:
    return json.loads(
        (plan_dir / ARTIFACT_NAME).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# The unclaimed list reaches the refiner
# ---------------------------------------------------------------------------


def test_unclaimed_passed_to_generator(tmp_path, state_db):
    """The files no task declares are handed to the refiner.

    Without this the round ends on a verdict; with it the refiner can
    produce a task for the file (or widen an existing one), so the loop
    keeps iterating.
    """
    generator = _CapturingGenerator([REPAIR_CONTENT])

    result, assembler, _, _ = _run_cycle(
        tmp_path, [REPAIR_CONTENT], generator=generator,
    )

    assert generator.kwargs.get("unclaimed_files") == [FAILED_FILE]
    # and the round still produced repair tasks
    assert [
        row["failed_vp_id"] for row in _assembled_rows(assembler)
    ] == ["VP-1"]
    assert result["repair_generation_error"] is None


def test_no_failed_files_skips(tmp_path, state_db):
    """A round that named no failing file writes nothing and passes nothing.

    An empty table would read as a conclusion ("nobody owns anything"),
    which is not what an output without a failing case says.
    """
    generator = _CapturingGenerator([REPAIR_CONTENT])

    result, _, _, plan_dir = _run_cycle(
        tmp_path, [REPAIR_CONTENT], run_output="", generator=generator,
    )

    assert "unclaimed_files" not in generator.kwargs
    assert not (plan_dir / ARTIFACT_NAME).exists()
    assert generator.calls, "the generator was never called"


# ---------------------------------------------------------------------------
# The table lands in the plan directory
# ---------------------------------------------------------------------------


def test_scope_claims_artifact_written(tmp_path, state_db):
    """The ownership table is written where an operator can read it."""
    _, _, _, plan_dir = _run_cycle(tmp_path, [REPAIR_CONTENT])

    artifact = plan_dir / ARTIFACT_NAME
    assert artifact.exists()

    table = _artifact(plan_dir)
    assert table["unclaimed"] == [FAILED_FILE]
    assert table["claims"] == {}
    assert table["out_of_project"] == []


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_missing_task_list_makes_every_failing_file_unclaimed(
    tmp_path, state_db,
):
    """No task list means nothing can be claimed — and no crash.

    Every failing file is then unclaimed, which is the honest reading:
    the plan states no owner for it.
    """
    generator = _CapturingGenerator([REPAIR_CONTENT])

    result, _, _, plan_dir = _run_cycle(
        tmp_path, [REPAIR_CONTENT], plan_tasks=None, generator=generator,
    )

    assert generator.kwargs.get("unclaimed_files") == [FAILED_FILE]
    table = _artifact(plan_dir)
    assert table["claims"] == {}
    assert table["unclaimed"] == [FAILED_FILE]
    assert result["repair_generation_error"] is None


def test_a_declared_file_is_claimed_not_unclaimed(tmp_path, state_db):
    """A failing file a task declares belongs to that task."""
    generator = _CapturingGenerator([REPAIR_CONTENT])

    _, _, _, plan_dir = _run_cycle(
        tmp_path, [REPAIR_CONTENT],
        run_output="FAILED backend/agent.py::test_a - boom",
        plan_tasks=[{"id": "3", "files_to_modify": ["backend/agent.py"]}],
        generator=generator,
    )

    table = _artifact(plan_dir)
    assert table["claims"] == {"backend/agent.py": ["3"]}
    assert table["unclaimed"] == []
    assert generator.kwargs.get("unclaimed_files") == []


def test_artifact_write_failure_does_not_stop_the_round(
    tmp_path, state_db, capsys,
):
    """A table that cannot be written is a warning, not a dead round.

    The unclaimed list still has to reach the refiner — losing the file
    on disk is a nuisance, losing it in the prompt is the bug.
    """
    logger = Mock()
    generator = _CapturingGenerator([REPAIR_CONTENT])

    result, assembler, _, _ = _run_cycle(
        tmp_path, [REPAIR_CONTENT], generator=generator, logger=logger,
        block_artifact=True,
    )

    assert generator.kwargs.get("unclaimed_files") == [FAILED_FILE]
    assert [
        row["failed_vp_id"] for row in _assembled_rows(assembler)
    ] == ["VP-1"]
    assert result["repair_generation_error"] is None
    assert logger.warning.call_args_list, "the write failure was not logged"
    assert any(
        "scope_claims" in str(call[0][0])
        for call in logger.warning.call_args_list
    )


def test_generator_error_semantics_are_unchanged(tmp_path, state_db):
    """A generator that raises still reads as a generation failure.

    The unclaimed list must not turn a broken generator into a silent
    "no repair tasks" verdict.
    """
    from repair_generator import RepairGenerationError

    result, _, _, _ = _run_cycle(
        tmp_path, [],
        gen_side_effect=RepairGenerationError(
            "repair-content LLM call failed for round 1: boom",
            round_number=1,
            failed_vp_ids=["VP-1"],
        ),
    )

    assert result["repair_generation_error"]
    assert "boom" in result["repair_generation_error"]


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))
