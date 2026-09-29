"""
TDD tests for ``verification_orchestrator`` DP7 two-phase loop helpers.

Covers two surfaces:

1. ``select_unstable_vps(round1_results, vp_graph)`` — pure helper
   already implemented by Task 13. Returns the list of VP IDs that
   must be re-checked in Phase 2 (FAILED / PARTIAL seeds plus their
   transitive downstream descendants).

2. ``run_two_phase_round`` and ``run_two_phase_loop`` — the new DP7
   orchestrator surface that splits each verification round into:

       verify_first_pass  →  run every VP, tag stability
       verify_recheck     →  re-run only the unstable VPs
                             (skipped when first_pass was fully stable)

   Each call to a phase is recorded via the ``phase_recorder`` callback
   so callers (e.g. the plan-state writer) can drive ``plan_state``
   transitions into the new ``verify_first_pass`` / ``verify_recheck``
   phases.
"""

from __future__ import annotations

from typing import List

from verification_orchestrator import (
    run_two_phase_loop,
    run_two_phase_round,
    select_unstable_vps,
)


def test_select_unstable_returns_failed_vps():
    """A FAILED VP must appear in the result, even with an empty graph."""
    round1_results = [
        {"vp_id": "VP-001", "status": "FAILED"},
        {"vp_id": "VP-002", "status": "PASSED"},
    ]
    vp_graph: dict = {}

    result = select_unstable_vps(round1_results, vp_graph)

    assert "VP-001" in result
    assert "VP-002" not in result


def test_select_unstable_includes_downstream():
    """A FAILED VP's direct downstream dependents must also appear."""
    round1_results = [
        {"vp_id": "VP-001", "status": "FAILED"},
        {"vp_id": "VP-002", "status": "PASSED"},
    ]
    vp_graph = {
        "VP-001": ["VP-003"],
        "VP-002": [],
        "VP-003": [],
    }

    result = select_unstable_vps(round1_results, vp_graph)

    assert "VP-001" in result
    assert "VP-003" in result
    assert "VP-002" not in result


def test_select_unstable_empty_when_all_passed():
    """When every VP is PASSED, the result is empty."""
    round1_results = [
        {"vp_id": "VP-001", "status": "PASSED"},
        {"vp_id": "VP-002", "status": "PASSED"},
    ]
    vp_graph = {
        "VP-001": ["VP-002"],
        "VP-002": [],
    }

    result = select_unstable_vps(round1_results, vp_graph)

    assert result == []


def test_select_unstable_handles_partial():
    """PARTIAL status is treated identically to FAILED."""
    round1_results = [
        {"vp_id": "VP-001", "status": "PASSED"},
        {"vp_id": "VP-002", "status": "PARTIAL"},
        {"vp_id": "VP-003", "status": "PASSED"},
    ]
    vp_graph: dict = {}

    result = select_unstable_vps(round1_results, vp_graph)

    assert "VP-002" in result
    assert "VP-001" not in result
    assert "VP-003" not in result


def test_select_unstable_transitive_downstream():
    """Downstream propagation is transitive across multiple hops.

    VP-001 → VP-003 → VP-004. When VP-001 FAILED, both VP-003 and
    VP-004 must be re-checked even though they PASSED Phase 1.
    """
    round1_results = [
        {"vp_id": "VP-001", "status": "FAILED"},
        {"vp_id": "VP-002", "status": "PASSED"},
        {"vp_id": "VP-003", "status": "PASSED"},
        {"vp_id": "VP-004", "status": "PASSED"},
    ]
    vp_graph = {
        "VP-001": ["VP-003"],
        "VP-002": [],
        "VP-003": ["VP-004"],
        "VP-004": [],
    }

    result = select_unstable_vps(round1_results, vp_graph)

    assert "VP-001" in result
    assert "VP-003" in result
    assert "VP-004" in result
    assert "VP-002" not in result


# ---------------------------------------------------------------------------
# Two-phase loop tests (DP7 orchestrator surface).
# ---------------------------------------------------------------------------


def test_first_pass_runs_all_vps():
    """verify_first_pass must execute every VP exactly once."""
    executed: List[str] = []

    def executor(vp_id: str) -> str:
        executed.append(vp_id)
        return "PASSED"

    phases: List[str] = []

    def recorder(phase: str) -> None:
        phases.append(phase)

    run_two_phase_round(
        vp_ids=["VP-001", "VP-002", "VP-003"],
        executor=executor,
        vp_graph={},
        phase_recorder=recorder,
    )

    assert set(executed) == {"VP-001", "VP-002", "VP-003"}
    # Every VP runs in first_pass exactly once (no recheck here).
    assert executed.count("VP-001") == 1
    assert executed.count("VP-002") == 1
    assert executed.count("VP-003") == 1
    assert "verify_first_pass" in phases


def test_recheck_runs_only_unstable():
    """verify_recheck must execute only the unstable VPs (FAILED seed).

    VP-001 fails first_pass → unstable. VP-002 passes first_pass →
    stable, so it MUST NOT be re-executed in the recheck phase.
    """
    executed: List[str] = []

    def executor(vp_id: str) -> str:
        executed.append(vp_id)
        return "FAILED" if vp_id == "VP-001" else "PASSED"

    phases: List[str] = []

    def recorder(phase: str) -> None:
        phases.append(phase)

    run_two_phase_round(
        vp_ids=["VP-001", "VP-002"],
        executor=executor,
        vp_graph={},
        phase_recorder=recorder,
    )

    # First pass: both VPs executed. Recheck: only the unstable VP-001.
    assert executed.count("VP-001") == 2
    assert executed.count("VP-002") == 1
    assert "verify_first_pass" in phases
    assert "verify_recheck" in phases


def test_skip_recheck_when_all_stable():
    """When every VP is stable after first_pass, no recheck phase is entered."""
    phases: List[str] = []

    def recorder(phase: str) -> None:
        phases.append(phase)

    run_two_phase_round(
        vp_ids=["VP-001", "VP-002", "VP-003"],
        executor=lambda vp_id: "PASSED",
        vp_graph={},
        phase_recorder=recorder,
    )

    # All stable → recheck phase is skipped entirely.
    assert phases == ["verify_first_pass"]
    assert "verify_recheck" not in phases


def test_phase_transitions_first_pass_to_recheck():
    """Phase order must be verify_first_pass → verify_recheck when unstable.

    This pins the contract that plan_state transitions out of
    verify_first_pass and into verify_recheck in that order — never the
    reverse, never skipping the first_pass record.
    """
    phases: List[str] = []

    def recorder(phase: str) -> None:
        phases.append(phase)

    run_two_phase_round(
        vp_ids=["VP-001", "VP-002"],
        executor=lambda vp_id: "FAILED" if vp_id == "VP-001" else "PASSED",
        vp_graph={},
        phase_recorder=recorder,
    )

    assert phases == ["verify_first_pass", "verify_recheck"]
    # Order matters: first_pass always before recheck.
    assert phases.index("verify_first_pass") < phases.index("verify_recheck")


def test_max_six_vp_executions_across_three_rounds():
    """3 rounds × (1 first_pass + 1 recheck) = at most 6 VP executions.

    With a single VP that always fails, every round has an unstable
    seed, so recheck fires every round. Across the 3-round max the
    orchestrator must cap VP executions at 6 (3 first_pass + 3 recheck).
    """
    executed: List[str] = []

    def executor(vp_id: str) -> str:
        executed.append(vp_id)
        return "FAILED"  # always fail → always forces recheck

    run_two_phase_loop(
        max_rounds=3,
        vp_ids=["VP-001"],
        executor=executor,
        vp_graph={},
    )

    # 3 first_pass executions + 3 recheck executions = 6 total.
    assert len(executed) == 6
    assert all(vp == "VP-001" for vp in executed)


# ---------------------------------------------------------------------------
# round1_stable_vps field tests (DP7-3 schema extension).
# ---------------------------------------------------------------------------


def test_round1_stable_vps_field_in_two_phase_round_result():
    """run_two_phase_round must return a ``round1_stable_vps`` field.

    The field records the VP IDs that were judged ``stable`` (PASSED)
    on the first pass — the same set that the recheck phase is
    expected to skip. It is the canonical "what got cached as stable
    in round 1" surface, exposed in the orchestrator return value so
    downstream code can persist it to ``verification_report.json``.
    """
    report = run_two_phase_round(
        vp_ids=["VP-001", "VP-002", "VP-003"],
        executor=lambda vp_id: "PASSED",
        vp_graph={},
    )

    assert "round1_stable_vps" in report
    # All three VPs PASSED → all three are stable in round 1.
    assert sorted(report["round1_stable_vps"]) == ["VP-001", "VP-002", "VP-003"]


def test_round1_stable_vps_excludes_failed_vps():
    """VP IDs that FAILED first_pass must NOT appear in round1_stable_vps."""
    report = run_two_phase_round(
        vp_ids=["VP-001", "VP-002", "VP-003"],
        executor=lambda vp_id: "FAILED" if vp_id == "VP-002" else "PASSED",
        vp_graph={},
    )

    assert "VP-001" in report["round1_stable_vps"]
    assert "VP-002" not in report["round1_stable_vps"]
    assert "VP-003" in report["round1_stable_vps"]


def test_recheck_skips_stable_vps():
    """VPs listed in ``round1_stable_vps`` must not be re-executed.

    The recheck phase should ONLY re-execute unstable VPs. Stable VPs
    (those PASSED on first_pass) must NOT appear in the executor's
    invocation log during the recheck phase — that is the contract
    the field exists to make auditable.
    """
    executed: List[str] = []

    def executor(vp_id: str) -> str:
        executed.append(vp_id)
        return "FAILED" if vp_id == "VP-001" else "PASSED"

    report = run_two_phase_round(
        vp_ids=["VP-001", "VP-002", "VP-003"],
        executor=executor,
        vp_graph={},
    )

    # round1_stable_vps lists the PASSED VPs from first_pass.
    assert sorted(report["round1_stable_vps"]) == ["VP-002", "VP-003"]

    # Recheck phase executed exactly the unstable VPs (FAILED in first_pass).
    # In first_pass all three ran; in recheck only VP-001 ran.
    recheck_invoked = executed[3:]  # first_pass took the first 3 slots
    assert sorted(recheck_invoked) == ["VP-001"]

    # Stable VPs (round1_stable_vps) were never called in recheck.
    for stable_vp in report["round1_stable_vps"]:
        assert recheck_invoked.count(stable_vp) == 0


def test_round1_stable_vps_complete_when_all_pass():
    """When every VP is stable in first_pass, round1_stable_vps == all VPs."""
    vp_ids = ["VP-A", "VP-B", "VP-C", "VP-D"]
    report = run_two_phase_round(
        vp_ids=vp_ids,
        executor=lambda vp_id: "PASSED",
        vp_graph={},
    )

    assert sorted(report["round1_stable_vps"]) == sorted(vp_ids)
    # Recheck is skipped, so the report should also reflect that.
    assert report["entered_recheck"] is False
    assert report["recheck_results"] == {}


def test_round1_stable_vps_empty_when_all_fail():
    """When every VP is unstable, round1_stable_vps is the empty list."""
    report = run_two_phase_round(
        vp_ids=["VP-A", "VP-B"],
        executor=lambda vp_id: "FAILED",
        vp_graph={},
    )

    assert report["round1_stable_vps"] == []
    # All unstable → all re-executed in recheck.
    assert report["entered_recheck"] is True


def test_round1_stable_vps_in_two_phase_loop_rounds():
    """run_two_phase_loop must include round1_stable_vps per round."""
    reports = run_two_phase_loop(
        max_rounds=2,
        vp_ids=["VP-001", "VP-002"],
        executor=lambda vp_id: "PASSED",
        vp_graph={},
    )

    assert len(reports) == 2
    for r in reports:
        assert "round1_stable_vps" in r
        assert sorted(r["round1_stable_vps"]) == ["VP-001", "VP-002"]


def test_persistence_old_report_compatible_missing_field():
    """Old verification_report.json (no round1_stable_vps) reads as [].

    Backward-compat contract: a report written before the DP7-3
    schema extension lacks the field. ``load_round1_stable_vps``
    must return ``[]`` rather than raising KeyError / AttributeError.
    """
    import json
    from pathlib import Path

    from verification_persistence import VerificationPersistenceManager

    plan_dir = Path("/tmp/_test_round1_old_report")
    # Start clean
    import shutil
    if plan_dir.exists():
        shutil.rmtree(plan_dir)
    plan_dir.mkdir(parents=True)

    # Write an OLD-style report without the field.
    (plan_dir / "verification_report.json").write_text(
        json.dumps(
            {
                "plan_id": "test",
                "overall_status": "PASSED",
                "verification_results": [],
            }
        )
    )

    mgr = VerificationPersistenceManager(plan_dir)
    result = mgr.load_round1_stable_vps()
    assert result == []

    # Cleanup
    shutil.rmtree(plan_dir)


def test_persistence_round1_stable_vps_round_trip():
    """After writing the field via update_round1_stable_vps, reading back works."""
    from pathlib import Path

    from verification_persistence import VerificationPersistenceManager

    plan_dir = Path("/tmp/_test_round1_round_trip")
    import shutil
    if plan_dir.exists():
        shutil.rmtree(plan_dir)
    plan_dir.mkdir(parents=True)

    mgr = VerificationPersistenceManager(plan_dir)
    # Write the field as the orchestrator would.
    mgr.update_round1_stable_vps(["VP-002", "VP-004"])

    # Read it back.
    result = mgr.load_round1_stable_vps()
    assert sorted(result) == ["VP-002", "VP-004"]

    # Cleanup
    shutil.rmtree(plan_dir)


def test_persistence_round1_stable_vps_read_empty_when_missing_after_partial_update():
    """If a report has other fields but lacks round1_stable_vps, read returns []."""
    import json
    from pathlib import Path

    from verification_persistence import VerificationPersistenceManager

    plan_dir = Path("/tmp/_test_round1_partial_report")
    import shutil
    if plan_dir.exists():
        shutil.rmtree(plan_dir)
    plan_dir.mkdir(parents=True)

    # Partial report — has some fields but NOT round1_stable_vps.
    (plan_dir / "verification_report.json").write_text(
        json.dumps(
            {
                "plan_id": "test",
                "overall_status": "FAILED",
                "requirement_deviations": [],
                # NOTE: no round1_stable_vps
            }
        )
    )

    mgr = VerificationPersistenceManager(plan_dir)
    result = mgr.load_round1_stable_vps()
    assert result == []

    shutil.rmtree(plan_dir)


def test_report_writer_persists_round1_stable_vps(tmp_path=None):
    """``report_writer`` callback receives round1_stable_vps after first_pass.

    The orchestrator writes the round1_stable_vps list via an
    optional callback so the persistence layer (the agent or the
    VerificationPersistenceManager) can attach it to
    verification_report.json. This test pins that contract.
    """
    captured = {}

    def report_writer(payload: dict) -> None:
        captured["round1_stable_vps"] = list(payload.get("round1_stable_vps", []))

    run_two_phase_round(
        vp_ids=["VP-001", "VP-002", "VP-003"],
        executor=lambda vp_id: "FAILED" if vp_id == "VP-002" else "PASSED",
        vp_graph={},
        report_writer=report_writer,
    )

    assert "round1_stable_vps" in captured
    assert sorted(captured["round1_stable_vps"]) == ["VP-001", "VP-003"]
