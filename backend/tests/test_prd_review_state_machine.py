"""
TDD tests for plan_state._infer_phase full-validation logic.

Bug fixed: _infer_phase used to return "prd_approved" whenever
review.json existed, regardless of whether every decision point had
actually been accepted.  This let a partial review (e.g. 2 of 8
decision points accepted) silently advance the plan to
"prd_approved" and skip the rest of the review loop.

The corrected behaviour:
  - prd.json has zero decision_points   → prd_approved
    (backward compatibility: empty PRD is trivially "approved")
  - prd.json has N decision_points, all
    corresponding review items accepted  → prd_approved
  - prd.json has N decision_points, but
    review is partial or any item has
    been reset to pending                → prd_review
"""

import json
from pathlib import Path

import pytest

from plan_state import PlanState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_decision_points(n: int) -> list:
    """Build n synthetic decision points with sequential indices."""
    return [
        {
            "index": i,
            "title": f"决策点 {i}",
            "context": "context",
            "problem": "problem",
            "evidence": "evidence",
            "action": "action",
            "impact": "impact",
            "alternatives": [],
        }
        for i in range(n)
    ]


def _make_review_items(decision_points: list, statuses: dict) -> list:
    """Build review items, one per decision point, with the given status map.

    ``statuses`` is a {index: status} dict; indices not present default
    to "pending" so partial-review fixtures are easy to construct.
    """
    items = []
    for dp in decision_points:
        idx = dp["index"]
        items.append({
            "index": idx,
            "title": dp.get("title", ""),
            "status": statuses.get(idx, "pending"),
            "note": "",
        })
    return items


def _write_plan(plan_dir: Path, decision_points: list, review_items: list) -> Path:
    """Materialise a plan directory with prd.json + review.json.

    No plan_state.json is written so that ``_infer_phase`` is the path
    exercised (not ``_load_state``).  This isolates the bug.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "prd.json").write_text(
        json.dumps(
            {
                "title": "test plan",
                "overview": "test",
                "decision_points": decision_points,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "review.json").write_text(
        json.dumps(
            {
                "items": review_items,
                "total": len(review_items),
                "accepted": sum(
                    1 for i in review_items if i.get("status") == "accepted"
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# test_infer_phase_*
# ---------------------------------------------------------------------------


class TestInferPhase:
    def test_infer_phase_all_accepted(self, tmp_path):
        """8/8 accept → 返回 prd_approved."""
        plan_dir = tmp_path / "plan-all-accepted"
        dps = _make_decision_points(8)
        statuses = {i: "accepted" for i in range(8)}
        _write_plan(plan_dir, dps, _make_review_items(dps, statuses))

        ps = PlanState(plan_dir)
        # _infer_phase is called from _default_state when plan_state.json
        # is missing.  The default state's current_phase must be
        # prd_approved.
        assert ps.get_current_phase() == "prd_approved"

    def test_infer_phase_partial_accepted(self, tmp_path):
        """2/8 accept → 返回 prd_review（而非 prd_approved）。"""
        plan_dir = tmp_path / "plan-partial"
        dps = _make_decision_points(8)
        statuses = {0: "accepted", 1: "accepted"}  # only 2 of 8
        _write_plan(plan_dir, dps, _make_review_items(dps, statuses))

        ps = PlanState(plan_dir)
        # BUG 修复前：返回 prd_approved（错误）
        # 修复后：返回 prd_review
        assert ps.get_current_phase() == "prd_review"

    def test_infer_phase_empty_prd(self, tmp_path):
        """0 decision_points → 直接 prd_approved（兼容空 PRD）。"""
        plan_dir = tmp_path / "plan-empty-prd"
        # decision_points = []; review.json is missing entirely (or empty)
        _write_plan(plan_dir, [], [])

        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "prd_approved"

    def test_infer_phase_reverse_updated(self, tmp_path):
        """accept 后 reset 为 pending → 重新降级 prd_review。"""
        plan_dir = tmp_path / "plan-reset"
        dps = _make_decision_points(8)
        # Simulate: user accepted all 8, then reset index 0 back to pending.
        statuses = {i: "accepted" for i in range(1, 8)}
        statuses[0] = "pending"
        _write_plan(plan_dir, dps, _make_review_items(dps, statuses))

        ps = PlanState(plan_dir)
        # Once any decision point drops back to pending, the plan
        # must drop back to prd_review.
        assert ps.get_current_phase() == "prd_review"


# ---------------------------------------------------------------------------
# test_is_prd_review_complete_helper
# ---------------------------------------------------------------------------


class TestIsPrdReviewCompleteHelper:
    """Edge cases for the new _is_prd_review_complete helper."""

    def _call(self, plan_dir: Path) -> bool:
        ps = PlanState(plan_dir)
        return ps._is_prd_review_complete()

    def test_helper_no_prd_file_returns_true(self, tmp_path):
        """无 prd.json → True（向后兼容，_infer_phase 不会调用到这里）。"""
        plan_dir = tmp_path / "no-prd"
        plan_dir.mkdir(parents=True, exist_ok=True)
        assert self._call(plan_dir) is True

    def test_helper_empty_decision_points_returns_true(self, tmp_path):
        """prd.json 无 decision_points → True（向后兼容空 PRD）。"""
        plan_dir = tmp_path / "empty-dp"
        _write_plan(plan_dir, [], [])
        assert self._call(plan_dir) is True

    def test_helper_all_accepted_returns_true(self, tmp_path):
        """N 个 decision_points，全部 accepted → True。"""
        plan_dir = tmp_path / "all-acc"
        dps = _make_decision_points(4)
        statuses = {i: "accepted" for i in range(4)}
        _write_plan(plan_dir, dps, _make_review_items(dps, statuses))
        assert self._call(plan_dir) is True

    def test_helper_partial_or_reset_returns_false(self, tmp_path):
        """N 个 decision_points，但部分 accepted 或 reset 为 pending → False。"""
        plan_dir = tmp_path / "partial"
        dps = _make_decision_points(4)
        # 2 accepted, 1 skipped (not accepted), 1 pending
        statuses = {0: "accepted", 1: "accepted", 2: "skipped", 3: "pending"}
        _write_plan(plan_dir, dps, _make_review_items(dps, statuses))
        assert self._call(plan_dir) is False

        # Now reset all to accepted, then flip one back to pending
        statuses2 = {i: "accepted" for i in range(4)}
        statuses2[2] = "pending"
        _write_plan(plan_dir, dps, _make_review_items(dps, statuses2))
        assert self._call(plan_dir) is False