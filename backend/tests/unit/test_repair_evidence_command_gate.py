"""W2 (2026-10-11): a VP's ``evidence_command`` must reach the repair task
as its ``test_command``.

The bug this pins
-----------------
``verification_plan.json`` names the runnable command ``evidence_command``.
``test_command`` — the key ``verification_report_reader`` read at all three
of its sites — is a legacy spelling that **no real plan carries**: on
``plans/<the 2026-10-10 plan>/verification_plan.json``, 0 of 21 VPs
have ``test_command`` and 20 of 21 have ``evidence_command``.

So ``extract_vp_test_commands`` returned ``{}`` on every real plan,
``RepairTaskAssembler._resolve_test_command`` had nothing to work with,
``_api_replay_command`` returned ``""`` for the 20 ``code_review`` VPs, and
every repair task was emitted with ``test_command=""``. All 34 of them then
tripped ``agent._cross_verify`` -> ``test_cross_verify_unverified``: judged
purely on the sub-agent's own report. The other flag those chain-head tasks
carried was ``task_empty_diff_no_changes``.

The fixture below is deliberately ``verification_method="code_review"`` and
carries **only** ``evidence_command``. That is the shape that broke: it has
no ``test_command`` to fall back on, and it is not an ``api_test``, so the
``_api_replay_command`` branch cannot rescue it either.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from repair_generator import RepairTaskAssembler  # noqa: E402
from verification.verification_report_reader import (  # noqa: E402
    _vp_runnable_command,
    extract_failed_vps_with_paths,
    extract_vp_test_commands,
)


_EVIDENCE_CMD = "cargo test -p app-tauri remote::e2e -- --nocapture"


def _write_plan(tmp_path: Path, vps: list[dict]) -> Path:
    """Write a ``verification_plan.json`` shaped like a real one."""
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "verification_plan.json"
    plan_path.write_text(
        json.dumps({"verification_points": vps}), encoding="utf-8",
    )
    return plan_path


def _write_report(tmp_path: Path, failed_ids: list[str]) -> Path:
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    report_path = plan_dir / "verification_report.json"
    report_path.write_text(json.dumps({
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": vp_id,
                "status": "FAILED",
                "title": f"{vp_id} failed",
                "actual_result": "assertion failed",
                "evidence": "see logs",
            }
            for vp_id in failed_ids
        ],
    }), encoding="utf-8")
    return report_path


def _code_review_vp(vp_id: str, **extra) -> dict:
    """A ``code_review`` VP whose only command is ``evidence_command``."""
    vp = {
        "id": vp_id,
        "title": f"{vp_id} code review",
        "verification_method": "code_review",
        "evidence_command": _EVIDENCE_CMD,
    }
    vp.update(extra)
    return vp


# ---------------------------------------------------------------------------
# extract_vp_test_commands — the map the assembler consumes
# ---------------------------------------------------------------------------


def test_extract_vp_test_commands_reads_evidence_command(tmp_path):
    plan_path = _write_plan(tmp_path, [_code_review_vp("VP-001")])
    commands = extract_vp_test_commands(plan_path)
    assert commands == {"VP-001": _EVIDENCE_CMD}


def test_extract_vp_test_commands_falls_back_to_test_command(tmp_path):
    """Legacy alias still resolves, so an older plan keeps working."""
    plan_path = _write_plan(tmp_path, [{
        "id": "VP-002",
        "verification_method": "code_review",
        "test_command": "npm test -- --grep legacy",
    }])
    commands = extract_vp_test_commands(plan_path)
    assert commands == {"VP-002": "npm test -- --grep legacy"}


def test_evidence_command_wins_over_the_legacy_alias(tmp_path):
    plan_path = _write_plan(tmp_path, [{
        "id": "VP-003",
        "evidence_command": "cargo test --evidence",
        "test_command": "cargo test --legacy",
    }])
    commands = extract_vp_test_commands(plan_path)
    assert commands == {"VP-003": "cargo test --evidence"}


def test_blank_evidence_command_falls_through_to_the_alias(tmp_path):
    plan_path = _write_plan(tmp_path, [{
        "id": "VP-004",
        "evidence_command": "   ",
        "test_command": "cargo test --legacy",
    }])
    commands = extract_vp_test_commands(plan_path)
    assert commands == {"VP-004": "cargo test --legacy"}


def test_a_vp_with_no_command_at_all_is_absent_not_empty_string(tmp_path):
    plan_path = _write_plan(tmp_path, [{
        "id": "VP-005", "verification_method": "ui_validation",
    }])
    assert extract_vp_test_commands(plan_path) == {}


# ---------------------------------------------------------------------------
# The path-based reader's own two sites
# ---------------------------------------------------------------------------


def test_path_based_reader_summarises_the_evidence_command(tmp_path):
    """The prompt-side summary used to be blank for every real VP."""
    plan_path = _write_plan(tmp_path, [_code_review_vp("VP-001")])
    report_path = _write_report(tmp_path, ["VP-001"])

    failed = extract_failed_vps_with_paths(report_path, plan_path)
    assert len(failed) == 1
    entry = failed[0]
    assert _EVIDENCE_CMD in entry["test_command_summary"]
    # The pointer must name the key that actually exists in the file.
    assert entry["test_command_path"].endswith("/VP-001/evidence_command")


def test_inlining_reader_surfaces_the_evidence_command(tmp_path):
    from verification.verification_report_reader import (
        extract_failed_vps_from_report,
    )
    plan_path = _write_plan(tmp_path, [_code_review_vp("VP-001")])
    report_path = _write_report(tmp_path, ["VP-001"])
    failed = extract_failed_vps_from_report(report_path, plan_path)
    assert failed[0]["test_command"] == _EVIDENCE_CMD


def test_vp_runnable_command_helper_returns_empty_when_neither_key():
    assert _vp_runnable_command({"id": "VP-x"}) == ""
    assert _vp_runnable_command({"evidence_command": None}) == ""


# ---------------------------------------------------------------------------
# End to end: evidence_command -> the repair task's test_command
# ---------------------------------------------------------------------------


def test_a_code_review_repair_is_gated_by_the_vps_evidence_command(tmp_path):
    """The whole point, end to end.

    A ``code_review`` VP has no ``api_test`` replay to fall back on, so
    before this fix its repair task could only ever carry ``""``. Now it
    carries the command that actually exercises the VP's subject.
    """
    vp = _code_review_vp("VP-001")
    plan_path = _write_plan(tmp_path, [vp])
    report_path = _write_report(tmp_path, ["VP-001"])

    failed_vps = extract_failed_vps_with_paths(report_path, plan_path)
    commands = extract_vp_test_commands(plan_path)

    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=failed_vps,
        vp_test_commands=commands,
        plan_path=str(plan_path),
    )
    tasks = assembler.assemble([{
        "failed_vp_id": "VP-001",
        "title": "修复 VP-001",
        "description": "d",
        "files_to_modify": ["src-tauri/src/remote/e2e.rs"],
    }])

    assert len(tasks) == 1, "the task must be emitted, not refused"
    assert tasks[0]["test_command"] == _EVIDENCE_CMD
    assert tasks[0]["files_to_modify"] == ["src-tauri/src/remote/e2e.rs"]
    assert assembler.test_command_sources["repair-r1-01"] == "vp"
    assert assembler.unverifiable_no_test_command == 0
    assert assembler.dropped_empty_files_to_modify == 0


def test_a_vp_with_no_command_anywhere_is_emitted_and_labelled(tmp_path):
    """The other side of the gate: no command -> still a task, labelled.

    This replaced a test that asserted the row was refused. The refusal
    was wrong for a shape that is structural rather than situational: a
    plan whose points are declarative carries no command on any VP, the
    content prompt never asks for one, and so *every* repair derived
    from such a point died — the round repaired nothing while reporting
    a plausible count of refusals.

    What replaces it keeps the loss visible instead of making it a
    refusal: the row is emitted, counted in
    ``unverifiable_no_test_command``, and given a source that says
    *which kind* of nothing it is. This fixture's method is
    ``ui_validation`` — an artifact deliverable — so the label is
    ``audit_style``.
    """
    plan_path = _write_plan(tmp_path, [{
        "id": "VP-009",
        "verification_method": "ui_validation",
    }])
    report_path = _write_report(tmp_path, ["VP-009"])
    failed_vps = extract_failed_vps_with_paths(report_path, plan_path)

    assembler = RepairTaskAssembler(
        round_number=2,
        failed_vps=failed_vps,
        vp_test_commands=extract_vp_test_commands(plan_path),
        plan_path=str(plan_path),
    )
    tasks = assembler.assemble([{
        "failed_vp_id": "VP-009",
        "title": "修复 VP-009",
        "description": "d",
        "files_to_modify": ["src/ui.rs"],
    }])

    assert len(tasks) == 1, "an artifact-deliverable point still needs repairing"
    assert tasks[0]["test_command"] == ""
    assert assembler.test_command_sources["repair-r2-01"] == "audit_style"
    assert assembler.unverifiable_no_test_command == 1
