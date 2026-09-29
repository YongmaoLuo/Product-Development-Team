"""Tests for previous-failure-feedback injection into repair prompts.

2026-09-12: a VP that fails the same way twice means the previous
repair did not take. That information belongs in the next
repair-generation prompt, so the agent knows the earlier approach
was invalid and does not walk the same path again.

These tests cover both pieces:
1. ``_build_repair_contents_prompt`` includes a "previous attempts"
   section listing each prior round's actual_result + evidence.
2. ``VerificationOrchestrator._failure_history`` accumulates per-VP
   failure observations across rounds.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from repair_generator import _build_repair_contents_prompt


class TestRepairPromptFeedback(unittest.TestCase):
    """Verify the prompt builder injects previous-failure feedback."""

    def test_no_feedback_when_first_occurrence(self) -> None:
        """When no prior attempts, prompt stays as before."""
        failed = [
            {
                "id": "VP-006",
                "title": "fix A",
                "priority": "high",
                "actual_result": "diff not empty",
                "evidence": "all lines are +",
            },
        ]
        prompt = _build_repair_contents_prompt(
            failed_vps=failed,
            round_number=1,
            plan_dir=Path("/tmp/plan"),
            project_dir=Path("/tmp/proj"),
            previous_failure_feedback=None,
        )
        assert "上次修复尝试未生效" not in prompt, (
            "First-occurrence prompt must NOT contain feedback section"
        )
        assert "VP-006" in prompt
        assert "diff not empty" in prompt

    def test_includes_feedback_section_when_prior_attempts(self) -> None:
        """When prior attempts exist for the failing VP, prompt
        includes the warning header + the actual_result / evidence
        verbatim so the LLM sees what the previous attempt observed.
        """
        failed = [
            {
                "id": "VP-006",
                "title": "fix A",
                "priority": "high",
                "actual_result": "diff not empty",
                "evidence": "all lines are +",
            },
        ]
        previous_failure_feedback = {
            "VP-006": [
                {
                    "round": 1,
                    "actual_result": "instrumentation block found",
                    "evidence": "SELECT_ENTERING_CONSOLIDATION_CALL_COUNT present",
                },
            ],
        }
        prompt = _build_repair_contents_prompt(
            failed_vps=failed,
            round_number=2,
            plan_dir=Path("/tmp/plan"),
            project_dir=Path("/tmp/proj"),
            previous_failure_feedback=previous_failure_feedback,
        )
        # Header is present
        assert "上次修复尝试未生效" in prompt, (
            "Prompt must contain the 'previous attempts failed' warning"
        )
        assert "不要重复相同的方案" in prompt, (
            "Prompt must instruct the agent to NOT repeat the prior approach"
        )
        # The actual_result / evidence from the prior round is rendered
        assert "instrumentation block found" in prompt
        assert "SELECT_ENTERING_CONSOLIDATION_CALL_COUNT present" in prompt
        # Round number is rendered
        assert "Round 1" in prompt
        # The CURRENT round's actual_result is still present too
        assert "diff not empty" in prompt

    def test_filters_feedback_to_current_failing_vps(self) -> None:
        """Feedback for VPs that have now PASSED is excluded from
        the prompt — it would just confuse the agent.
        """
        failed = [
            {"id": "VP-006", "title": "fix A", "priority": "high",
             "actual_result": "still broken", "evidence": "x"},
        ]
        # VP-027 was attempted in round 1 but is no longer in failures
        previous_failure_feedback = {
            "VP-006": [{"round": 1, "actual_result": "before-fix",
                        "evidence": "stale"}],
            "VP-027": [{"round": 1, "actual_result": "SHOULD-NOT-APPEAR",
                        "evidence": "this-vp-passed-now"}],
        }
        prompt = _build_repair_contents_prompt(
            failed_vps=failed,
            round_number=2,
            plan_dir=Path("/tmp/plan"),
            project_dir=Path("/tmp/proj"),
            previous_failure_feedback=previous_failure_feedback,
        )
        assert "before-fix" in prompt, "VP-006 feedback should appear"
        assert "SHOULD-NOT-APPEAR" not in prompt, (
            "VP-027 (now passed) feedback must NOT appear in prompt"
        )

    def test_accumulates_multiple_rounds(self) -> None:
        """When the same VP fails across 3 rounds, all 3 prior
        attempts' actual_result / evidence are shown to the LLM.
        """
        failed = [{"id": "VP-006", "title": "fix", "priority": "high",
                   "actual_result": "round-3-fail", "evidence": "x"}]
        previous_failure_feedback = {
            "VP-006": [
                {"round": 1, "actual_result": "round-1-fail", "evidence": "e1"},
                {"round": 2, "actual_result": "round-2-fail", "evidence": "e2"},
                {"round": 3, "actual_result": "round-3-fail", "evidence": "e3"},
            ],
        }
        prompt = _build_repair_contents_prompt(
            failed_vps=failed,
            round_number=4,
            plan_dir=Path("/tmp/plan"),
            project_dir=Path("/tmp/proj"),
            previous_failure_feedback=previous_failure_feedback,
        )
        assert "round-1-fail" in prompt
        assert "round-2-fail" in prompt
        # 2026-09-14: the heading is "次" (attempts) rather than "轮"
        # (rounds) — history entries can come from state.db's verdict
        # list, which carries no round number.
        assert "之前 3 次失败" in prompt


class TestFailureHistoryAccumulation(unittest.TestCase):
    """Verify the orchestrator's failure history tracking."""

    def _make_orchestrator_state(self):
        """Construct an isolated orchestrator without going through
        ``VerificationOrchestrator.__init__`` (which sets up DB-backed
        plan_state). We only need the failure-history helpers.
        """
        from verification.orchestrator import VerificationOrchestrator
        orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
        orch._failure_history = {}
        orch._previous_failed_ids = None
        orch._consecutive_same_failure_rounds = 0
        orch._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 5
        return orch

    def test_history_grows_per_vp(self) -> None:
        """Each call to _extract_failed_vps_with_evidence appends to
        the per-VP history list (one entry per round)."""
        orch = self._make_orchestrator_state()
        report_round1 = {
            "verification_results": [
                {"id": "VP-006", "status": "FAILED",
                 "actual_result": "round-1", "evidence": "e1"},
            ],
        }
        report_round2 = {
            "verification_results": [
                {"id": "VP-006", "status": "FAILED",
                 "actual_result": "round-2", "evidence": "e2"},
            ],
        }
        # Round 1
        vps_1 = orch._extract_failed_vps_with_evidence(report_round1)
        for vp_id, vp_data in vps_1.items():
            orch._failure_history.setdefault(vp_id, []).append({
                "round": 1,
                "actual_result": vp_data.get("actual_result", ""),
                "evidence": vp_data.get("evidence", ""),
            })
        # Round 2
        vps_2 = orch._extract_failed_vps_with_evidence(report_round2)
        for vp_id, vp_data in vps_2.items():
            orch._failure_history.setdefault(vp_id, []).append({
                "round": 2,
                "actual_result": vp_data.get("actual_result", ""),
                "evidence": vp_data.get("evidence", ""),
            })
        history = orch._failure_history.get("VP-006", [])
        assert len(history) == 2, (
            f"Expected 2 history entries; got {len(history)}"
        )
        assert history[0]["actual_result"] == "round-1"
        assert history[1]["actual_result"] == "round-2"

    def test_history_drops_vp_when_passes(self) -> None:
        """When a VP passes in a round, its history is cleared so
        the next round's prompt isn't polluted with stale failures.
        """
        orch = self._make_orchestrator_state()
        # Round 1: VP-006 fails
        orch._failure_history["VP-006"] = [
            {"round": 1, "actual_result": "x", "evidence": "y"},
        ]
        # Round 2: VP-006 passes (current_failed_ids = {})
        current_failed_ids = set()
        for vp_id in list(orch._failure_history.keys()):
            if vp_id not in current_failed_ids:
                orch._failure_history.pop(vp_id, None)
        assert "VP-006" not in orch._failure_history


if __name__ == "__main__":
    unittest.main()