"""2026-09-14 端到端: 拆分判定真的会触发并落盘。

前面的用例分别覆盖了判断者、拆分器、挂点；这里把
``check_cycle_conditions`` 整体驱动一遍，证明"一轮失败 → 判断者判 split
→ 子 VP 进入 verification_plan.json → 父 VP 的 SPLIT verdict 落库 →
结果 payload 带 vp_splits" 这条链在真实入口上闭合。

背景: round 3 从未走到这一步 —— 22:58 的看门狗误标把计划置 failed,
23:16 的 Illegal transition 又把 auto-loop 掀翻。所以"拆分能不能触发"
一直没有实证。本文件补上这段证据（不依赖任何真实 LLM / 真实 state.db）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from verification.orchestrator import VerificationOrchestrator


PLAN_ID = "plan-e2e-split"

REPORT = {
    "overall_status": "FAILED",
    "summary": "5 of 30 failed",
    "verification_results": [
        {
            "id": "VP-006",
            "title": "函数体 diff 为空",
            "status": "FAILED",
            "actual_result": "函数体 diff 非空",
            "evidence": "git diff 显示新增逻辑",
        },
        {
            "id": "VP-023",
            "title": "Nightly CI 全过",
            "status": "FAILED",
            "actual_result": "124 failed, 1931 passed in 1338s",
            "evidence": "pytest tail",
        },
    ],
    "requirement_deviations": [],
}


class _Tool:
    """Coding tool that judges VP-023 as 'too big → split'."""

    def __init__(self):
        self.prompts: List[str] = []

    def query_json(self, prompt, system_instruction, timeout=None):
        self.prompts.append(prompt)
        # The judge prompt asks for a repair-vs-split DECISION; the
        # repair-content prompt asks for repair TASKS. Keying on the
        # output contract (not on the word "repair", which both contain)
        # is what makes this double faithful.
        if "repair 或 split" in prompt:
            return {"decisions": [{
                "vp_id": "VP-023", "action": "split",
                "split_hint": "by_directory", "reason": "整树 22 分钟",
            }]}
        # repair-content generation (only VP-006 should reach here)
        return {"tasks": [{
            "failed_vp_id": "VP-006",
            "title": "修 VP-006 的 diff",
            "description": "恢复 9/2 行为冻结",
            "acceptance_criteria": "git diff 为空",
        }]}


class _PlanState:
    def __init__(self):
        self.calls: List[str] = []

    def get_verification_round(self) -> int:
        return 4

    def get_verification_max_rounds(self) -> int:
        return 6

    def __getattr__(self, name):
        def _c(*a, **k):
            self.calls.append(name)
        return _c


class _Repo:
    def __init__(self):
        self.verdicts: List[Dict[str, Any]] = []

    def summary(self, plan_id):
        return {"verdicts": []}

    def append_verdict(self, plan_id, verdict):
        self.verdicts.append(verdict)


@pytest.fixture
def orch(tmp_path, monkeypatch):
    plan_dir = tmp_path / PLAN_ID
    plan_dir.mkdir()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (plan_dir / "verification_plan.json").write_text(json.dumps({
        "verification_points": [
            {"id": "VP-006", "title": "函数体 diff 为空",
             "verification_method": "code_review",
             "test_command": "git diff HEAD"},
            {"id": "VP-023", "title": "Nightly CI 全过",
             "verification_method": "automated_test",
             "test_command": "pytest tests/ -v"},
        ],
        # A realistic project tree for the splitter's collect step.
        "__note__": "groups come from the stubbed collect_test_files",
    }, ensure_ascii=False), encoding="utf-8")

    # ``_judge_and_split`` reads the failed VPs from the report ON DISK
    # (``extract_failed_vps_with_paths``), not from the dict passed to
    # ``check_cycle_conditions`` — the round's report is already written
    # by the time the repair phase runs.
    (plan_dir / "verification_report.json").write_text(
        json.dumps(REPORT, ensure_ascii=False), encoding="utf-8"
    )

    # Isolate the state.db writes the orchestrator performs (RP-* rows).
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))

    from vp_split import VpSplitter
    monkeypatch.setattr(
        VpSplitter, "collect_test_files",
        lambda self, scope, timeout=300: [
            "tests/visual/test_a.py", "tests/bench/test_b.py",
        ],
    )

    o = VerificationOrchestrator.__new__(VerificationOrchestrator)
    o.plan_dir = plan_dir
    o.project_dir = project_dir
    o.coding_tool = _Tool()
    o.plan_state = _PlanState()
    o.verif_repo = _Repo()
    o._failure_history = {}
    o._previous_failed_ids = None
    o._consecutive_same_failure_rounds = 0
    o._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 3
    o._current_repair_tasks = []
    o._current_vp_splits = []
    o._waiting_for_user = False
    o.logger = None

    class _Gen:
        coding_tool = o.coding_tool

        def generate_repair_contents(self, failed_vps, round_number,
                                     previous_failure_feedback=None,
                                     repair_outcomes=None):
            from repair_generator import _build_repair_contents_prompt
            prompt = _build_repair_contents_prompt(
                failed_vps, round_number, plan_dir, project_dir,
                previous_failure_feedback=previous_failure_feedback,
                repair_outcomes=repair_outcomes,
            )
            return o.coding_tool.query_json(prompt, "", 600).get("tasks", [])

    o.repair_generator = _Gen()
    return o


def test_failed_round_triggers_split_and_persists_it(orch):
    result = orch.check_cycle_conditions(REPORT, round_number=4)

    # 1) the split shows up on the result payload
    splits = result["vp_splits"]
    assert len(splits) == 1, f"expected VP-023 to be split; got {splits}"
    split = splits[0]
    assert split["vp_id"] == "VP-023"
    assert split["hint"] == "by_directory"
    assert len(split["child_vp_ids"]) == 2
    assert split["persisted"] is True

    # 2) children are in the plan, parent superseded (next round's universe)
    data = json.loads(
        (orch.plan_dir / "verification_plan.json").read_text(encoding="utf-8")
    )
    by_id = {e["id"]: e for e in data["verification_points"]}
    assert by_id["VP-023"]["superseded_by"] == split["child_vp_ids"]
    for child_id in split["child_vp_ids"]:
        assert by_id[child_id]["parent_vp_id"] == "VP-023"
        assert "pytest tests/" in by_id[child_id]["test_command"]

    # 3) the parent's SPLIT verdict reached state.db (resume half)
    assert any(v["status"] == "SPLIT" and v["vp_id"] == "VP-023"
               for v in orch.verif_repo.verdicts)

    # 4) VP-006 was NOT split — it got an ordinary repair task
    assert "VP-006" not in json.dumps(splits)
    assert [t.get("failed_vp_id") for t in result["repair_tasks"]] == ["VP-006"]


def test_round_that_only_splits_returns_no_repair_tasks(orch):
    """The pure-split signal the auto-loop switches on: splits present,
    repair list empty."""
    from server import _round_is_pure_split

    # Only VP-023 fails this round — the judge reads the report ON DISK,
    # so the fixture's report must be replaced, not just the argument.
    only_split = dict(
        REPORT, verification_results=[REPORT["verification_results"][1]],
    )
    (orch.plan_dir / "verification_report.json").write_text(
        json.dumps(only_split, ensure_ascii=False), encoding="utf-8"
    )

    result = orch.check_cycle_conditions(only_split, round_number=4)

    assert result["vp_splits"], "split expected"
    assert result["repair_tasks"] == []
    assert _round_is_pure_split(result) is True, (
        "the auto-loop must see this as a pure-split round (skip executor)"
    )


def test_split_failure_history_is_recorded(orch):
    """The round's failures land in the persisted history so the NEXT
    round's repair prompt carries '上次方案无效' feedback."""
    from verification import failure_history as fh

    orch.check_cycle_conditions(REPORT, round_number=4)

    history = fh.load(orch.plan_dir)
    assert "VP-023" in history
    assert history["VP-023"][0]["round"] == 4
