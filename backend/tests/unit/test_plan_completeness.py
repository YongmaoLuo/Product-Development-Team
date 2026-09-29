"""全量关卡完整性护栏（2026-09-18, D5）。

契约来源：一次重新生成的计划里，Phase 2 的 Nightly CI 关卡凭空消失 ——
项目里有 ``scripts/ci_local.py``，PRD 验收标准第 9 条就是"Nightly CI 全过"，
但计划里一条对应关卡都没有，而且没有任何东西会提醒这一点。

本文件钉住三道缝：
  * ``detect_full_gate_entries`` 只认文件系统、不猜正文；
  * ``find_missing_gates`` 每种入口只报一条缺口（Phase 2 上限就两条关卡）；
  * ``annotate_plan`` 把缺口写进计划顶层，缺口消失时清干净。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_plan_completeness import (  # noqa: E402
    GAP_FIELD,
    annotate_plan,
    detect_full_gate_entries,
    find_missing_gates,
    render_gap_feedback,
)


def _touch(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _plan(*vps):
    return {"verification_points": list(vps)}


def _ci_gate(vp_id="VP-017"):
    return {"id": vp_id, "verification_method": "full_ci",
            "verification_phase": 2, "phase_order": 1, "ci_entry": "pytest tests/"}


def _e2e_gate(vp_id="VP-018"):
    return {"id": vp_id, "verification_method": "e2e",
            "verification_phase": 2, "phase_order": 2,
            "target_url": "http://127.0.0.1:3000/x"}


# ---------------------------------------------------------------------------
# detect_full_gate_entries
# ---------------------------------------------------------------------------


class TestDetection:
    def test_no_project_dir_returns_empty(self):
        assert detect_full_gate_entries(None) == []
        assert detect_full_gate_entries("") == []

    def test_missing_project_dir_returns_empty(self, tmp_path):
        assert detect_full_gate_entries(tmp_path / "nope") == []

    def test_bare_project_has_no_entries(self, tmp_path):
        assert detect_full_gate_entries(tmp_path) == []

    def test_ci_local_script_is_detected(self, tmp_path):
        _touch(tmp_path, "scripts/ci_local.py")
        kinds = {e.kind for e in detect_full_gate_entries(tmp_path)}
        assert kinds == {"ci"}

    def test_nightly_glob_is_detected(self, tmp_path):
        _touch(tmp_path, "scripts/ci_nightly_prompt.txt")
        entries = detect_full_gate_entries(tmp_path)
        assert any(e.kind == "ci" for e in entries)

    def test_github_nightly_workflow_is_detected(self, tmp_path):
        _touch(tmp_path, ".github/workflows/nightly.yml")
        assert any(e.kind == "ci" for e in detect_full_gate_entries(tmp_path))

    def test_playwright_config_at_root(self, tmp_path):
        _touch(tmp_path, "playwright.config.ts")
        assert any(e.kind == "e2e" for e in detect_full_gate_entries(tmp_path))

    def test_playwright_config_in_first_level_subdir(self, tmp_path):
        _touch(tmp_path, "frontend-app/playwright.config.ts")
        entries = detect_full_gate_entries(tmp_path)
        assert [
            e.evidence for e in entries if e.kind == "e2e"
        ] == ["frontend-app/playwright.config.ts"]

    def test_tests_e2e_dir_is_detected(self, tmp_path):
        _touch(tmp_path, "tests/e2e/spec.ts")
        assert any(e.kind == "e2e" for e in detect_full_gate_entries(tmp_path))

    @pytest.mark.parametrize("dirname", [
        "node_modules", ".venv", "venv1", ".claude", "dist", "target",
    ])
    def test_skipped_dirs_are_not_scanned(self, tmp_path, dirname):
        _touch(tmp_path, f"{dirname}/playwright.config.ts")
        assert detect_full_gate_entries(tmp_path) == []

    def test_agent_worktrees_are_not_scanned(self, tmp_path):
        """.claude/worktrees 是别的 agent 的项目副本，不是本项目的事实。"""
        _touch(tmp_path, ".claude/worktrees/agent-x/playwright.config.ts")
        assert detect_full_gate_entries(tmp_path) == []

    def test_both_kinds_together(self, tmp_path):
        _touch(tmp_path, "scripts/ci_local.py")
        _touch(tmp_path, "playwright.config.ts")
        kinds = sorted(e.kind for e in detect_full_gate_entries(tmp_path))
        assert kinds == ["ci", "e2e"]


# ---------------------------------------------------------------------------
# find_missing_gates
# ---------------------------------------------------------------------------


class TestFindMissingGates:
    def test_no_entries_means_no_gaps(self, tmp_path):
        assert find_missing_gates(_plan(_ci_gate()), []) == []

    def test_garbage_plan_means_no_gaps(self, tmp_path):
        _touch(tmp_path, "scripts/ci_local.py")
        entries = detect_full_gate_entries(tmp_path)
        assert find_missing_gates("not-a-plan", entries) == []
        assert find_missing_gates({"verification_points": None}, entries) == []

    def test_missing_ci_gate_is_reported(self, tmp_path):
        _touch(tmp_path, "scripts/ci_local.py")
        entries = detect_full_gate_entries(tmp_path)
        gaps = find_missing_gates(_plan({"id": "VP-001"}), entries)
        assert len(gaps) == 1
        assert gaps[0].kind == "ci"
        assert gaps[0].expected_method == "full_ci"
        assert gaps[0].evidence == "scripts/ci_local.py"

    def test_present_ci_gate_closes_the_gap(self, tmp_path):
        _touch(tmp_path, "scripts/ci_local.py")
        entries = detect_full_gate_entries(tmp_path)
        assert find_missing_gates(_plan(_ci_gate()), entries) == []

    def test_a_phase_one_full_ci_is_not_a_satisfied_gate(self, tmp_path):
        """门禁必须在 Phase 2 —— Phase 1 的 full_ci 是计划内部矛盾。"""
        _touch(tmp_path, "scripts/ci_local.py")
        entries = detect_full_gate_entries(tmp_path)
        straggler = dict(_ci_gate(), verification_phase=1)
        gaps = find_missing_gates(_plan(straggler), entries)
        assert [g.kind for g in gaps] == ["ci"]

    def test_e2e_gate_closes_the_e2e_gap(self, tmp_path):
        _touch(tmp_path, "playwright.config.ts")
        entries = detect_full_gate_entries(tmp_path)
        assert find_missing_gates(_plan(_e2e_gate()), entries) == []

    def test_a_phase_two_ui_validation_also_closes_the_e2e_gap(self, tmp_path):
        """Phase 2 上的浏览器驱动 VP 就是 E2E 门禁，叫哪个名字都算。

        护栏查的是覆盖，不是命名 —— 因为名字不同就把一份本来就有 E2E
        门禁的计划打回重生成，只会换来一条重复的关卡。
        """
        _touch(tmp_path, "playwright.config.ts")
        entries = detect_full_gate_entries(tmp_path)
        renamed = {
            "id": "VP-018", "verification_method": "ui_validation",
            "verification_phase": 2, "phase_order": 2,
            "target_url": "http://127.0.0.1:3000/x",
        }
        assert find_missing_gates(_plan(renamed), entries) == []

    def test_a_phase_two_api_test_is_not_the_e2e_gate(self, tmp_path):
        """非浏览器驱动的方法顶不了 E2E 门禁。"""
        _touch(tmp_path, "playwright.config.ts")
        entries = detect_full_gate_entries(tmp_path)
        lookalike = {
            "id": "VP-018", "verification_method": "api_test",
            "verification_phase": 2, "phase_order": 2,
            "request": {"method": "GET", "url": "http://127.0.0.1:1/x"},
            "assertions": [{"name": "ok", "status": 200}],
        }
        gaps = find_missing_gates(_plan(lookalike), entries)
        assert [g.kind for g in gaps] == ["e2e"]

    def test_a_phase_one_e2e_is_not_a_satisfied_gate(self, tmp_path):
        """E2E 是 Phase 2 的东西 —— 标在 Phase 1 上不算数。"""
        _touch(tmp_path, "playwright.config.ts")
        entries = detect_full_gate_entries(tmp_path)
        straggler = dict(_e2e_gate(), verification_phase=1)
        gaps = find_missing_gates(_plan(straggler), entries)
        assert [g.kind for g in gaps] == ["e2e"]

    def test_at_most_one_gap_per_kind(self, tmp_path):
        """一个仓库里有四个 CI 脚本，也只欠**一条** CI 关卡。"""
        for rel in ("scripts/ci_local.py", "scripts/ci_prgate.py",
                    "scripts/ci_nightly_prompt.txt",
                    ".github/workflows/nightly.yml"):
            _touch(tmp_path, rel)
        entries = detect_full_gate_entries(tmp_path)
        assert len([e for e in entries if e.kind == "ci"]) > 1
        gaps = find_missing_gates(_plan(), entries)
        assert [g.kind for g in gaps] == ["ci"]
        assert gaps[0].alternatives, "同类入口要作为备选带出去"

    def test_both_kinds_missing_reports_both(self, tmp_path):
        _touch(tmp_path, "scripts/ci_local.py")
        _touch(tmp_path, "playwright.config.ts")
        entries = detect_full_gate_entries(tmp_path)
        gaps = find_missing_gates(_plan(), entries)
        assert sorted(g.kind for g in gaps) == ["ci", "e2e"]


# ---------------------------------------------------------------------------
# render_gap_feedback
# ---------------------------------------------------------------------------


class TestRenderFeedback:
    def test_ci_gap_feedback_names_the_field(self):
        from verification_plan_completeness import MissingGate

        text = render_gap_feedback([MissingGate(
            kind="ci", label="全量 CI 门禁", evidence="scripts/ci_local.py",
            expected_method="full_ci",
        )])
        assert "full_ci" in text
        assert "ci_entry" in text
        assert "scripts/ci_local.py" in text
        assert "phase_order: 1" in text

    def test_e2e_gap_feedback_names_the_field(self):
        from verification_plan_completeness import MissingGate

        text = render_gap_feedback([MissingGate(
            kind="e2e", label="全量 E2E 套件", evidence="playwright.config.ts",
            expected_method="e2e",
        )])
        assert "e2e" in text
        assert "target_url" in text
        assert "phase_order: 2" in text

    def test_alternatives_are_offered(self):
        from verification_plan_completeness import MissingGate

        text = render_gap_feedback([MissingGate(
            kind="ci", label="全量 CI 门禁", evidence="scripts/ci_local.py",
            expected_method="full_ci",
            alternatives=["scripts/ci_prgate.py"],
        )])
        assert "scripts/ci_prgate.py" in text

    def test_feedback_says_one_gate_per_kind(self):
        from verification_plan_completeness import MissingGate

        text = render_gap_feedback([MissingGate(
            kind="ci", label="全量 CI 门禁", evidence="x", expected_method="full_ci",
        )])
        assert "一条" in text


# ---------------------------------------------------------------------------
# annotate_plan
# ---------------------------------------------------------------------------


class TestAnnotatePlan:
    def test_gap_is_written_onto_the_plan(self):
        from verification_plan_completeness import MissingGate

        plan = _plan(_ci_gate())
        findings = annotate_plan(plan, [MissingGate(
            kind="e2e", label="全量 E2E 套件", evidence="playwright.config.ts",
            expected_method="e2e",
        )])
        assert findings
        assert plan[GAP_FIELD]["missing"] == findings
        assert findings[0]["kind"] == "e2e"

    def test_no_gap_leaves_no_marker(self):
        plan = _plan(_ci_gate())
        assert annotate_plan(plan, []) == []
        assert GAP_FIELD not in plan

    def test_a_stale_marker_is_cleared(self):
        plan = _plan(_ci_gate())
        plan[GAP_FIELD] = {"missing": [{"kind": "ci"}]}
        annotate_plan(plan, [])
        assert GAP_FIELD not in plan

    def test_non_dict_plan_is_safe(self):
        assert annotate_plan("nope", []) == []


# ---------------------------------------------------------------------------
# 接进生成回路（verification_agent）
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


class TestWiring:
    def test_fix_guidance_covers_the_gate_family(self, tmp_path):
        agent = _make_agent(tmp_path)
        text = agent._violation_fix_guidance([], [], [{"kind": "ci"}])
        assert "ci_entry" in text
        assert "full_ci" in text
        assert "只补缺的那几条关卡" in text

    def test_fix_guidance_stays_silent_without_gaps(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent._violation_fix_guidance([], [], []) == ""
        assert agent._violation_fix_guidance([], []) == ""

    def test_reused_plan_gets_the_gap_annotated(self, tmp_path):
        """复用磁盘计划的路径上只标注、不重问 —— 但缺口必须可见。"""
        agent = _make_agent(tmp_path)
        _touch(agent.project_dir, "scripts/ci_local.py")
        disk_plan = _plan(_e2e_gate())
        agent.verification_plan_file.write_text(
            __import__("json").dumps(disk_plan), encoding="utf-8",
        )

        plan = agent.generate_verification_plan()

        assert plan[GAP_FIELD]["missing"][0]["kind"] == "ci"
        assert plan["verification_points"] == disk_plan["verification_points"]

    def test_reused_plan_without_gaps_is_clean(self, tmp_path):
        import json

        agent = _make_agent(tmp_path)
        _touch(agent.project_dir, "scripts/ci_local.py")
        agent.verification_plan_file.write_text(
            json.dumps(_plan(_ci_gate())), encoding="utf-8",
        )

        plan = agent.generate_verification_plan()

        assert GAP_FIELD not in plan
