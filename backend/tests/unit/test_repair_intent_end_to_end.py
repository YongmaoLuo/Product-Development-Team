"""End-to-end acceptance for the repair intent gate.

Why this exists
---------------
Every other test of the intent gate replaces one of its parts with a
double: the gate with a stub, the assembler with a mock, the plan
directory with a hand-written constraint.  Each of them proves that its
own part behaves; none of them proves that the *chain* holds — that a
plan which says "不碰 Rust" really does stop a repair that rewrites
``engine_rs``, and that the conflict survives the round.

This file runs that chain for real, in the production order:

    plan_dir/import.json         → _plan_intent
      → _build_repair_contents_prompt
      → (the model)              → _parse_repair_contents_response
      → apply_intent_gate
      → RepairTaskAssembler
      → the round's result
      → _build_repair_round_entry → the snapshot on disk

Only the model call is a double — everything else is the production
code.  The round is driven through
:class:`verification.VerificationOrchestrator` rather than by calling
the helpers in the test's own order, because the orchestrator is where
the wiring lives: a chain assembled by the test would keep passing
after the wiring was removed.

What is pinned here
-------------------
* a row that rewrites the Rust crate the plan puts off limits never
  becomes a repair task, and the conflict is reported — even though the
  model itself judged the row fine;
* a row that stays inside the plan's scope still becomes a task;
* the round snapshot carries the conflict and asks for an operator
  decision, and the conflict survives on disk;
* a plan with no material of its own constrains nothing;
* a round in which every row conflicts produces no task at all — the
  conflicts are the report.

Plus the regression pin: the repair path's own file-scope filter and
its task assembler still behave the way they did before the gate
existed (the gate must not have changed what a repair task may touch).

No network, no database, no model: the model call is the only stub, and
the state database is redirected into the test's tmp directory.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The constraint the plan states about itself. The Rust crate is off
#: limits — that is the whole point of this acceptance.
PLAN_CONSTRAINT = "不碰 Rust"

#: The plan's own material — the file the intent extractor reads. The
#: first clause states a scope that names no region (it carries no path
#: token), so the only region the plan puts off limits is the Rust
#: crate. A material that also named ``backend/`` would put the backend
#: half of this fixture off limits too, and the "the clean row still
#: becomes a task" case would stop meaning anything.
PLAN_MATERIAL = "本期只修后端信号层；不碰 Rust。"

#: The row the model produced for the Rust verification point. The model
#: itself says the fix is fine (``violates=false``) — what crosses the
#: constraint is the file it declared.
RUST_ROW = {
    "failed_vp_id": "VP-1",
    "title": "修复 Rust 信号计算",
    "description": "改写 engine_rs 的信号计算实现",
    "acceptance_criteria": "pytest backend/tests/unit/test_signals.py 通过",
    "files_to_modify": ["engine_rs/src/signals.rs"],
    "intent_check": {"violates": False},
}

#: The row that stays inside the plan's scope.
BACKEND_ROW = {
    "failed_vp_id": "VP-2",
    "title": "修复后端处理器",
    "description": "调整 backend/repair_generator.py 的处理器",
    "acceptance_criteria": "pytest backend/tests/unit/test_handler.py 通过",
    "files_to_modify": ["backend/repair_generator.py"],
    "intent_check": {"violates": False},
}

#: The row the *model* judged a violation. The model's own answer is
#: taken at face value — no path check is needed to drop it.
MODEL_VIOLATION_ROW = {
    "failed_vp_id": "VP-2",
    "title": "修复后端处理器",
    "description": "调整 backend/repair_generator.py 的处理器",
    "acceptance_criteria": "pytest backend/tests/unit/test_handler.py 通过",
    "files_to_modify": ["backend/repair_generator.py"],
    "intent_check": {
        "violates": True,
        "constraint": PLAN_CONSTRAINT,
        "reason": "该修复要求改动计划禁改的区域",
    },
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
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8",
    )


def _write_verification_report(plan_dir: Path) -> None:
    """The round's report on disk — what the failed-VP reader reads."""
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-1",
                "status": "FAILED",
                "title": "Rust 信号计算",
                "actual_result": "signal mismatch: expected 42, got 0",
                "evidence": "engine_rs/src/signals.rs:88",
            },
            {
                "id": "VP-2",
                "status": "FAILED",
                "title": "后端处理器",
                "actual_result": "handler raised KeyError",
                "evidence": "backend/repair_generator.py:120",
            },
        ],
        "requirement_deviations": [],
    }
    (plan_dir / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8",
    )


def _write_verification_plan(plan_dir: Path) -> None:
    """The plan's verification points — the command each VP is judged by.

    The commands target one file each, so the VP-split judge leaves the
    VPs alone (it only considers broad scopes) and the repair task ends
    up carrying a real acceptance command.
    """
    plan = {
        "verification_points": [
            {
                "id": "VP-1",
                "title": "Rust 信号计算",
                "priority": "high",
                "verification_method": "automated_test",
                "evidence_command": (
                    "pytest backend/tests/unit/test_signals.py"
                ),
            },
            {
                "id": "VP-2",
                "title": "后端处理器",
                "priority": "high",
                "verification_method": "automated_test",
                "evidence_command": (
                    "pytest backend/tests/unit/test_handler.py"
                ),
            },
        ],
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8",
    )


def _failed_report() -> dict:
    """The report the round is judged on."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-1", "status": "FAILED", "title": "Rust 信号计算"},
            {"id": "VP-2", "status": "FAILED", "title": "后端处理器"},
        ],
        "requirement_deviations": [],
    }


def _model_double(content_response):
    """The model boundary of the round.

    The repair-content call answers with the model's rows; the bounded
    test-command fill call answers with nothing usable — the ordinary
    "the model could not name an acceptance command" case, which the
    assembler then labels by category.

    Every call is recorded, so a test can read back what the model was
    actually asked.
    """
    from repair_generator import (
        REPAIR_CONTENT_SYSTEM_PROMPT,
        TEST_COMMAND_FILL_SYSTEM_PROMPT,
    )

    calls = []

    def _query(
        coding_tool, session, prompt="", system_instruction="", **kwargs,
    ):
        calls.append({
            "system_instruction": system_instruction,
            "prompt": prompt,
        })
        if system_instruction == TEST_COMMAND_FILL_SYSTEM_PROMPT:
            return {"commands": []}
        if system_instruction == REPAIR_CONTENT_SYSTEM_PROMPT:
            return content_response
        return content_response

    _query.calls = calls
    return _query


def _run_round(
    tmp_path: Path,
    content_response,
    *,
    with_plan_material: bool = True,
):
    """Run one failed verification round through the real chain.

    Returns ``(result, plan_dir, model_calls)`` — the round's result
    payload, the plan directory it ran in, and every call the round made
    to the model.
    """
    from verification import VerificationOrchestrator

    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)

    _write_plan_state(plan_dir)
    _write_verification_report(plan_dir)
    _write_verification_plan(plan_dir)
    if with_plan_material:
        # The fast lane's own material: an arbitrary JSON payload, which
        # is what ``import.json`` is allowed to be.
        (plan_dir / "import.json").write_text(
            json.dumps({"content": PLAN_MATERIAL}, ensure_ascii=False),
            encoding="utf-8",
        )

    model = _model_double(content_response)
    with patch("verification.orchestrator.VerificationAgent"), patch(
        "repair_generator.query_json_in_session", side_effect=model,
    ):
        orch = VerificationOrchestrator(plan_dir, project_dir, object())
        result = orch.check_cycle_conditions(_failed_report(), round_number=1)

    return result, plan_dir, model.calls


def _write_round_snapshot(plan_dir: Path, result: dict) -> dict:
    """Write the round snapshot the way the loop writes it, and read it back.

    The two write sites live in ``verification_loop``; both build the
    entry with :func:`verification_loop._build_repair_round_entry` and
    then dump it. This mirrors that write so the file an operator reads
    after the round is the thing asserted on.
    """
    from verification_loop import _build_repair_round_entry

    entry = _build_repair_round_entry(
        1, result.get("repair_tasks", []), result,
    )
    path = plan_dir / "verification_repair_tasks.json"
    path.write_text(
        json.dumps(
            {"plan_id": plan_dir.name, **entry},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _task_ids(result: dict) -> list:
    return [
        str(task.get("id"))
        for task in result.get("repair_tasks", [])
        if isinstance(task, dict)
    ]


def _vp_ids(result: dict) -> list:
    return [
        str(task.get("failed_vp_id"))
        for task in result.get("repair_tasks", [])
        if isinstance(task, dict)
    ]


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def test_chain_blocks_rust_write_end_to_end(tmp_path, state_db):
    """A plan that says "不碰 Rust" stops the repair that rewrites the
    Rust crate — and the conflict is reported and written down.

    The model itself judged the row fine (``violates=false``); what
    crosses the constraint is the file it declared. This is the case the
    deterministic floor exists for.
    """
    result, plan_dir, model_calls = _run_round(
        tmp_path, {"tasks": [RUST_ROW, BACKEND_ROW]},
    )

    # The plan's own constraint reached the model before it answered —
    # the model was asked to stay inside the plan's scope, and the gate
    # below is what holds the line when it does not.
    from repair_generator import REPAIR_CONTENT_SYSTEM_PROMPT

    content_call = next(
        call for call in model_calls
        if call["system_instruction"] == REPAIR_CONTENT_SYSTEM_PROMPT
    )
    assert PLAN_CONSTRAINT in content_call["prompt"]

    # The Rust row never became a repair task.
    assert "VP-1" not in _vp_ids(result)
    assert _task_ids(result) == ["repair-r1-01"]
    assert _vp_ids(result) == ["VP-2"]

    # The conflict is reported, and the report says why: the model and
    # the deterministic check disagreed.
    conflicts = result["intent_conflicts"]
    assert [c["vp_id"] for c in conflicts] == ["VP-1"]
    assert "模型判断与确定性检查不一致" in conflicts[0]["reason"]
    assert result["needs_operator_decision"] is True

    # And it survives the round, on disk.
    snapshot = _write_round_snapshot(plan_dir, result)
    assert snapshot["summary"]["needs_operator_decision"] is True
    assert [c["vp_id"] for c in snapshot["intent_conflicts"]] == ["VP-1"]


def test_no_import_json_keeps_task(tmp_path, state_db):
    """A plan with no material of its own constrains nothing.

    The same model answer, in a plan directory without ``import.json``:
    the Rust row is no longer a conflict, because there is no constraint
    for it to cross. A plan that says nothing is not a plan that says
    "no".
    """
    result, _, _ = _run_round(
        tmp_path,
        {"tasks": [RUST_ROW, BACKEND_ROW]},
        with_plan_material=False,
    )

    assert _vp_ids(result) == ["VP-1", "VP-2"]
    assert _task_ids(result) == ["repair-r1-01", "repair-r1-02"]
    assert result["intent_conflicts"] == []
    assert result["needs_operator_decision"] is False


def test_all_conflicts_leave_no_tasks(tmp_path, state_db):
    """A round in which every row conflicts produces no task at all.

    One row is dropped by the deterministic path check, the other by the
    model's own judgement — both are conflicts, and both are reported.
    The conflicts are the report; there is nothing left to execute.
    """
    result, plan_dir, _ = _run_round(
        tmp_path, {"tasks": [RUST_ROW, MODEL_VIOLATION_ROW]},
    )

    assert result["repair_tasks"] == []
    assert _task_ids(result) == []
    assert sorted(c["vp_id"] for c in result["intent_conflicts"]) == [
        "VP-1", "VP-2",
    ]
    assert result["needs_operator_decision"] is True

    snapshot = _write_round_snapshot(plan_dir, result)
    assert snapshot["tasks"] == []
    assert snapshot["summary"]["needs_operator_decision"] is True


def test_existing_repair_modules_still_importable():
    """The repair path's own contracts survive the gate.

    The gate decides *whether* a row becomes a task; it must not have
    changed *what* a task may touch. The file-scope filter still
    refuses an absolute path and a ``..`` escape, still de-duplicates,
    and the assembler is still instantiable.
    """
    from repair_generator import RepairTaskAssembler, normalise_declared_files

    assembler = RepairTaskAssembler(round_number=1, failed_vps=[])

    declared = ["/abs.py", "../x.py", "a.py", "a.py"]
    # The assembler's own filter takes the content dict it is given;
    # the module-level normaliser is the same filter on the raw list.
    assert assembler._resolve_files_to_modify(
        {"files_to_modify": declared},
    ) == ["a.py"]
    assert normalise_declared_files(declared) == ["a.py"]


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))