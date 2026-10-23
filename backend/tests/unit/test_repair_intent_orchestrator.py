"""Unit tests for the repair intent gate inside the verification loop.

Why this exists
---------------
:func:`repair_generator.apply_intent_gate` knows how to drop a repair
row that crosses the plan's own constraint, and
:func:`repair_generator._plan_intent` knows how to read those
constraints back out of the plan directory. Neither was wired into
:func:`verification.orchestrator.VerificationOrchestrator.check_cycle_conditions`
though: the loop generated the repair content and handed *every* row to
the assembler, so a fix that rewrites the Rust crate the plan says to
leave alone became a repair task and was executed.

A dropped row is not enough on its own — the plan still has a failure
that this round cannot repair, and only an operator can say whether the
constraint or the requirement gives way. So the round's result has to
carry the conflict, and the conflict has to be logged.

What is pinned here
-------------------
* a row the gate rejects never reaches the assembler;
* the result carries ``intent_conflicts`` and ``needs_operator_decision``;
* a clean round raises no flag;
* the generator is handed the plan's own constraints, so the model can
  avoid the conflict in the first place;
* every conflict is logged as a WARNING named ``repair_intent_conflict``.

Plus the boundary conditions the implementation has to honour: a
generation failure leaves both new fields empty, a missing logger only
prints (and never raises), and one conflict does not abort the round —
the clean rows still become repair tasks.

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


#: The constraint the plan states about itself.
PLAN_CONSTRAINT = "不碰 Rust"

#: The row the model itself judged a violation: it wants to rewrite the
#: Rust crate the plan puts off limits.
VIOLATING_CONTENT = {
    "failed_vp_id": "VP-1",
    "title": "fix the rust signal",
    "description": "rewrite the signal computation in the rust crate",
    "acceptance_criteria": "tests/test_signals.py passes",
    "files_to_modify": ["engine_rs/src/signals.rs"],
    "intent_check": {
        "violates": True,
        "constraint": PLAN_CONSTRAINT,
        "reason": "要改 engine_rs，违反计划约束",
    },
}

#: The row that stays inside the plan's scope.
CLEAN_CONTENT = {
    "failed_vp_id": "VP-2",
    "title": "fix the backend handler",
    "description": "adjust the backend handler",
    "acceptance_criteria": "tests/test_handler.py passes",
    "files_to_modify": ["backend/repair_generator.py"],
    "intent_check": {"violates": False},
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


def _failed_report() -> dict:
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-1", "status": "FAILED", "title": "rust signal"},
            {"id": "VP-2", "status": "FAILED", "title": "backend handler"},
        ],
        "requirement_deviations": [],
    }


def _failed_vps() -> list:
    return [
        {"id": "VP-1", "title": "rust signal", "priority": "high"},
        {"id": "VP-2", "title": "backend handler", "priority": "high"},
    ]


class _OldSignatureGenerator:
    """A repair generator that predates the ``plan_intent`` parameter.

    The real generator always accepts it; this double stands in for a
    collaborator whose signature is older than the orchestrator's — the
    round must survive it rather than die on a keyword argument.
    """

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def generate_repair_contents(self, failed_vps, round_number,
                                 previous_failure_feedback=None,
                                 repair_outcomes=None):
        self.calls.append({
            "failed_vps": list(failed_vps),
            "round_number": round_number,
        })
        return list(self.contents)


def _run_cycle(
    tmp_path: Path,
    contents,
    *,
    gen_side_effect=None,
    logger=None,
    generator=None,
):
    """Run one failed verification round through the orchestrator.

    Returns ``(result, assembler_mock, generator_mock)`` — the result
    payload the auto-loop reads, the patched assembler (so the test can
    see which rows reached it) and the patched repair generator (so the
    test can see what it was asked for).

    ``generator`` replaces the patched generator with a hand-written
    double — used to stand in for a collaborator whose signature is
    older than the orchestrator's.
    """
    from verification import VerificationOrchestrator

    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_plan_state(plan_dir)
    # The plan's own constraint, in the file the extractor reads.
    (plan_dir / "import.json").write_text(
        f"{PLAN_CONSTRAINT}。", encoding="utf-8"
    )

    report = _failed_report()

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

    return result, MockAssembler, orch.repair_generator


def _assembled_rows(assembler_mock) -> list:
    """The content rows the assembler was handed."""
    return list(assembler_mock.return_value.assemble.call_args[0][0])


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_violating_content_is_not_assembled(tmp_path, state_db):
    """A row that crosses the plan's own constraint never becomes a
    repair task — the assembler must not see it."""
    _, assembler, _ = _run_cycle(
        tmp_path, [VIOLATING_CONTENT, CLEAN_CONTENT],
    )

    assembled = _assembled_rows(assembler)

    assert [row["failed_vp_id"] for row in assembled] == ["VP-2"]
    assert all(
        row["failed_vp_id"] != "VP-1" for row in assembled
    ), "the violating row reached the assembler"


def test_result_carries_intent_conflicts_and_flag(tmp_path, state_db):
    """The conflict is *reported*, not silently dropped: the round's
    result carries it and asks for an operator decision."""
    result, _, _ = _run_cycle(tmp_path, [VIOLATING_CONTENT, CLEAN_CONTENT])

    assert result["intent_conflicts"] == [
        {
            "vp_id": "VP-1",
            "constraint": PLAN_CONSTRAINT,
            "reason": "要改 engine_rs，违反计划约束",
        },
    ]
    assert result["needs_operator_decision"] is True


def test_clean_round_flag_is_false(tmp_path, state_db):
    """No conflict → no flag. A plan that needs no decision must not be
    handed one."""
    result, _, _ = _run_cycle(tmp_path, [CLEAN_CONTENT])

    assert result["intent_conflicts"] == []
    assert result["needs_operator_decision"] is False


def test_plan_intent_passed_to_generator(tmp_path, state_db):
    """The generator is told what the plan constrains — the model can
    then avoid the conflict instead of being caught by it."""
    _, _, generator = _run_cycle(tmp_path, [CLEAN_CONTENT])

    kwargs = generator.generate_repair_contents.call_args.kwargs

    assert "plan_intent" in kwargs
    assert PLAN_CONSTRAINT in kwargs["plan_intent"]


def test_warning_is_logged(tmp_path, state_db):
    """Every conflict is a WARNING named ``repair_intent_conflict``."""
    logger = Mock()

    _run_cycle(
        tmp_path, [VIOLATING_CONTENT, CLEAN_CONTENT], logger=logger,
    )

    assert logger.warning.call_args_list, "the conflict was not logged"
    assert logger.warning.call_args_list[0][0][0] == "repair_intent_conflict"


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_generation_failure_leaves_both_fields_empty(tmp_path, state_db):
    """A generation failure is not an intent conflict: the round never
    got far enough to know whether the fix would have crossed the
    constraint."""
    from repair_generator import RepairGenerationError

    result, _, _ = _run_cycle(
        tmp_path, [],
        gen_side_effect=RepairGenerationError(
            "repair-content LLM call failed for round 1: boom",
            round_number=1,
            failed_vp_ids=["VP-1"],
        ),
    )

    assert result["intent_conflicts"] == []
    assert result["needs_operator_decision"] is False


def test_missing_logger_only_prints(tmp_path, state_db, capsys):
    """Without a logger the conflict is still visible — printed, never
    raised."""
    result, _, _ = _run_cycle(tmp_path, [VIOLATING_CONTENT, CLEAN_CONTENT])

    captured = capsys.readouterr()

    assert result["needs_operator_decision"] is True
    assert "repair_intent_conflict" in captured.out
    assert "VP-1" in captured.out


def test_one_conflict_does_not_abort_the_round(tmp_path, state_db):
    """A conflict is a fact about the plan, not a reason to end the
    round: the clean rows still become repair tasks."""
    result, assembler, _ = _run_cycle(
        tmp_path, [VIOLATING_CONTENT, CLEAN_CONTENT],
    )

    assert result["status"] == "verification_failed"
    assert [row["failed_vp_id"] for row in _assembled_rows(assembler)] == ["VP-2"]


def test_generator_without_plan_intent_kwarg_does_not_crash(tmp_path, state_db):
    """A generator whose signature predates ``plan_intent`` must not
    kill the round.

    The orchestrator hands the plan's constraints to the generator so
    the model can avoid the conflict rather than be caught by it. That
    is a courtesy to the model, not a precondition for the round: a
    collaborator that does not know the parameter still has to be
    callable, because the deterministic intent gate downstream is what
    actually holds the line.
    """
    generator = _OldSignatureGenerator([CLEAN_CONTENT])

    result, _, _ = _run_cycle(tmp_path, [], generator=generator)

    assert generator.calls, "the generator was never called"
    assert result["intent_conflicts"] == []
    assert result["needs_operator_decision"] is False


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))
