"""2026-09-14 orchestrator 分流挂点: repair vs split。

用户指令::

    "repairing task generator 同时也要充当一个判断者的角色，他需要去
     判断到底是生成新的修复任务，还是拆分当前的 VP ... 如果他没有拆出
     新的修复任务，那执行状态就没有什么东西可以执行，他就直接跳过，
     他就会重新进入到验证状态。"

Covered:
  * split-routed VPs leave the repair candidate set, repair-routed ones
    stay (the repair LLM only sees the latter);
  * the split writes both surfaces — verification_plan.json children +
    the parent's SPLIT verdict through the repo;
  * a splitter refusal (no useful grouping) keeps the VP in the repair
    set instead of silently dropping the failure;
  * ANY exception in the judge/splitter degrades to "repair everything"
    (the helper never raises — splitting must not strand a round);
  * the result payload carries ``vp_splits``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from verification.orchestrator import VerificationOrchestrator


PLAN_ID = "plan-split-hook"


class _FakeRepo:
    def __init__(self):
        self.verdicts: List[Dict[str, Any]] = []

    def append_verdict(self, plan_id, verdict):
        self.verdicts.append((plan_id, verdict))


def _write_plan(plan_dir: Path) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(json.dumps({
        "verification_points": [
            {
                "id": "VP-006", "title": "函数体 diff",
                "verification_method": "code_review",
                "test_command": "git diff HEAD",
            },
            {
                "id": "VP-023", "title": "Nightly CI 全过",
                "verification_method": "automated_test",
                "test_command": "pytest tests/ -v",
            },
        ]
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def orch(tmp_path):
    plan_dir = tmp_path / PLAN_ID
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _write_plan(plan_dir)
    repo = _FakeRepo()
    o = VerificationOrchestrator(
        plan_dir, project_dir, coding_tool=object(), verif_repo=repo,
    )
    o._test_repo = repo  # type: ignore[attr-defined]
    # The splitter's file collection would shell out to pytest; stub it.
    from vp_split import VpSplitter
    with patch.object(VpSplitter, "collect_test_files", lambda self, scope, timeout=300: [
        "tests/test_a.py", "tests/visual/test_x.py",
    ]):
        yield o


FAILED_VPS = [
    {"id": "VP-023", "title": "Nightly CI 全过",
     "actual_result_summary": "117 failed in 1095s"},
    {"id": "VP-006", "title": "函数体 diff",
     "actual_result_summary": "函数体被改写"},
]


def _decisions(orch, mapping):
    """Patch ``VpSplitJudge.decide`` with a fixed mapping."""
    return patch(
        "vp_split.VpSplitJudge.decide",
        lambda self, candidates: mapping,
    )


def test_split_routed_vp_leaves_repair_set(orch):
    mapping = {
        "VP-023": {"action": "split", "split_hint": "by_directory",
                   "reason": "18 分钟整树跑"},
        "VP-006": {"action": "repair", "split_hint": "", "reason": "缺陷明确"},
    }
    with _decisions(orch, mapping):
        repair_candidates, vp_splits = orch._apply_vp_split_decisions(
            FAILED_VPS, round_number=2,
        )

    assert [vp["id"] for vp in repair_candidates] == ["VP-006"], (
        "only repair-routed VPs may reach the repair LLM"
    )
    assert len(vp_splits) == 1
    split = vp_splits[0]
    assert split["vp_id"] == "VP-023"
    assert split["hint"] == "by_directory"
    assert len(split["child_vp_ids"]) == 2
    assert split["persisted"] is True

    # Children landed in the plan, parent marked superseded.
    data = json.loads(
        (orch.plan_dir / "verification_plan.json").read_text(encoding="utf-8")
    )
    by_id = {e["id"]: e for e in data["verification_points"]}
    assert by_id["VP-023"]["superseded_by"] == split["child_vp_ids"]
    for child_id in split["child_vp_ids"]:
        assert child_id in by_id
        assert by_id[child_id]["parent_vp_id"] == "VP-023"

    # Parent's SPLIT verdict reached state.db (via the repo).
    assert orch._test_repo.verdicts, "SPLIT verdict must be persisted"
    _, verdict = orch._test_repo.verdicts[0]
    assert verdict["vp_id"] == "VP-023"
    assert verdict["status"] == "SPLIT"


def test_splitter_refusal_keeps_vp_in_repair_set(orch):
    """A declined split (single group) must NOT drop the failure."""
    from vp_split import VpSplitter
    mapping = {"VP-023": {"action": "split", "split_hint": "by_directory",
                          "reason": "big"}}
    with _decisions(orch, mapping), \
         patch.object(VpSplitter, "collect_test_files",
                      lambda self, scope, timeout=300: ["tests/visual/a.py"]):
        repair_candidates, vp_splits = orch._apply_vp_split_decisions(
            [FAILED_VPS[0]], round_number=2,
        )

    assert vp_splits == []
    assert [vp["id"] for vp in repair_candidates] == ["VP-023"]


def test_exception_in_judge_degrades_to_repair_all(orch):
    """The helper must never raise — a broken judge means 'repair
    everything', the pre-2026-09-14 behaviour."""
    with patch("vp_split.VpSplitJudge.decide",
               side_effect=RuntimeError("LLM provider exploded")):
        repair_candidates, vp_splits = orch._apply_vp_split_decisions(
            FAILED_VPS, round_number=2,
        )

    assert vp_splits == []
    assert [vp["id"] for vp in repair_candidates] == ["VP-023", "VP-006"]


def test_no_judge_decision_keeps_everything_repairable(orch):
    """An empty decision map (e.g. nothing eligible) must keep all VPs
    in the repair set."""
    with _decisions(orch, {}):
        repair_candidates, vp_splits = orch._apply_vp_split_decisions(
            FAILED_VPS, round_number=2,
        )
    assert vp_splits == []
    assert len(repair_candidates) == 2


def test_candidates_carry_prior_failure_rounds(orch):
    """2026-09-14: the judge sees how many times the VP already failed
    before this round (same persisted history the repair prompt uses) —
    a VP that keeps failing is evidence it is too big to fix in one
    piece."""
    captured: Dict[str, Any] = {}

    def _capture(self, candidates):
        captured["candidates"] = list(candidates)
        return {}

    orch._failure_history = {
        # Two PRIOR rounds …
        "VP-023": [
            {"round": 2, "actual_result": "a", "evidence": ""},
            {"round": 3, "actual_result": "b", "evidence": ""},
            # … and this round's own entry, which must not be counted.
            {"round": 4, "actual_result": "c", "evidence": ""},
        ],
    }
    with patch("vp_split.VpSplitJudge.decide", _capture):
        orch._apply_vp_split_decisions(FAILED_VPS, round_number=4)

    by_id = {str(c["id"]): c for c in captured["candidates"]}
    assert by_id["VP-023"]["prior_failure_rounds"] == 2
    assert by_id["VP-006"]["prior_failure_rounds"] == 0
