"""2026-09-14 executor: superseded / SPLIT VPs must not re-run.

When the repair-phase judge decides a VP is too big
(``vp_split.VpSplitter``), it writes the children into
``verification_plan.json`` and stamps the parent with
``superseded_by``, and records the parent's ``SPLIT`` verdict in
state.db. Both halves of the contract are pinned here:

  * **fresh init** — ``_build_items_from_plan`` drops superseded parents
    so a restart with no cached verdicts still runs only the children;
  * **resume** — a ``SPLIT`` verdict lands in the skipped bucket, which
    BaseExecutor's ``completed_set`` treats as terminal success, so a
    resumed round skips the parent (re-running it would burn the whole
    suite again — the exact cost the split exists to avoid).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock

import pytest

from verification_executor import VerificationExecutor


def _executor(plan: Dict[str, Any], plan_dir: Path) -> VerificationExecutor:
    return VerificationExecutor(
        verification_plan=plan,
        plan_id="plan-split",
        plan_dir=plan_dir,
        sub_agent_runner=Mock(return_value={"status": "PASSED"}),
    )


PARENT = {
    "id": "VP-023",
    "title": "Nightly CI 全过",
    "verification_method": "automated_test",
    "test_command": "pytest tests/ -v",
    "superseded_by": ["VP-023-1", "VP-023-2"],
}
CHILD_A = {
    "id": "VP-023-1",
    "title": "Nightly CI 全过（拆分1: visual）",
    "verification_method": "automated_test",
    "parent_vp_id": "VP-023",
    "split_depth": 1,
    "test_command": "pytest tests/visual -v",
}
CHILD_B = {
    "id": "VP-023-2",
    "title": "Nightly CI 全过（拆分2: bench）",
    "verification_method": "automated_test",
    "parent_vp_id": "VP-023",
    "split_depth": 1,
    "test_command": "pytest tests/bench -v",
}
OTHER = {
    "id": "VP-006",
    "title": "函数体 diff",
    "verification_method": "code_review",
}


def test_fresh_init_skips_superseded_parent(tmp_path):
    executor = _executor(
        {"verification_points": [OTHER, PARENT, CHILD_A, CHILD_B]}, tmp_path
    )
    ids = [vp["id"] for vp in executor._build_items_from_plan()]
    assert ids == ["VP-006", "VP-023-1", "VP-023-2"], (
        "the superseded parent must be replaced by its children, "
        "not run alongside them"
    )


def test_no_superseded_flag_keeps_every_vp(tmp_path):
    """A plan that never split must be untouched by the filter."""
    clean_parent = {k: v for k, v in PARENT.items() if k != "superseded_by"}
    executor = _executor(
        {"verification_points": [OTHER, clean_parent]}, tmp_path
    )
    ids = [vp["id"] for vp in executor._build_items_from_plan()]
    assert ids == ["VP-006", "VP-023"]


def test_split_verdict_lands_in_skipped_bucket():
    """Resume half: SPLIT joins SKIPPED so the parent is part of
    BaseExecutor's completed_set (terminal success → skipped)."""
    executor = VerificationExecutor(
        verification_plan={"verification_points": [OTHER]},
        plan_id="plan-split",
        plan_dir=Path("/tmp"),
        sub_agent_runner=Mock(),
    )
    executor._verdicts = {
        "VP-006": {"status": "FAILED"},
        "VP-023": {
            "status": "SPLIT",
            "reasons": ["split into 2 sub-VP(s): VP-023-1, VP-023-2"],
            "evidence": {"child_vp_ids": ["VP-023-1", "VP-023-2"]},
        },
    }
    executor._backfill_index_lists_from_verdicts()

    assert "VP-023" in executor.skipped_vps, (
        "a split parent must be terminal-skipped — otherwise the resume "
        "path re-runs the giant VP the split was meant to replace"
    )
    assert "VP-023" not in executor.failed_vps
    assert executor.failed_vps == ["VP-006"]
