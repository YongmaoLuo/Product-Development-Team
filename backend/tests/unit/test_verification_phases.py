"""两阶段验证计划（Phase 1 子任务级 / Phase 2 全量关卡）单元测试。

契约来源（2026-09-16）：

* Phase 2 只有 Phase 1 全绿才执行；顺序固定 Nightly CI → E2E。
* 允许没有 Phase 2；但 **Phase 1 绝不允许出现 Phase 2 的任务**。
* 既有"有界命令护栏"对 Phase 2 豁免（全量范围在 Phase 2 是职责所在）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _plan(*vps):
    return {"verification_points": list(vps)}


def _vp(vp_id, title="", command="", **extra):
    vp = {"id": vp_id, "title": title, "test_command": command}
    vp.update(extra)
    return vp


# ---------------------------------------------------------------------------
# is_final_gate / 默认阶段
# ---------------------------------------------------------------------------

def test_default_phase_is_core() -> None:
    from verification_phases import is_final_gate
    assert is_final_gate(_vp("VP-001")) is False


def test_explicit_phase_two_is_final_gate() -> None:
    from verification_phases import is_final_gate
    assert is_final_gate(_vp("VP-001", verification_phase=2)) is True


def test_non_dict_vp_is_not_gate() -> None:
    from verification_phases import is_final_gate
    assert is_final_gate("nonsense") is False


# ---------------------------------------------------------------------------
# normalize_plan_phases —— 缺省与显式标注
# ---------------------------------------------------------------------------

def test_normalize_defaults_everything_to_phase_one() -> None:
    from verification_phases import normalize_plan_phases, PHASE_CORE
    plan = _plan(_vp("VP-001", "单元测试 A", "pytest tests/test_a.py"),
                 _vp("VP-002", "API 契约", "curl http://127.0.0.1:8080/x"))
    summary = normalize_plan_phases(plan)
    assert summary["phase1"] == 2 and summary["phase2"] == 0
    assert all(vp["verification_phase"] == PHASE_CORE
               for vp in plan["verification_points"])


def test_normalize_keeps_explicit_phase_two() -> None:
    from verification_phases import normalize_plan_phases, PHASE_FINAL_GATE
    plan = _plan(_vp("VP-001", "单元测试", "pytest tests/x.py"),
                 _vp("VP-002", "全量 Nightly CI", "scripts/ci_local.py",
                     verification_phase=2, phase_order=1))
    summary = normalize_plan_phases(plan)
    assert summary["phase2"] == 1
    assert plan["verification_points"][1]["verification_phase"] == PHASE_FINAL_GATE


def test_normalize_strips_phase_order_from_phase_one() -> None:
    """phase_order 只对 Phase 2 有意义 —— Phase 1 上残留会被清掉。"""
    from verification_phases import normalize_plan_phases
    plan = _plan(_vp("VP-001", "单元测试", "pytest tests/x.py", phase_order=7))
    normalize_plan_phases(plan)
    assert "phase_order" not in plan["verification_points"][0]


# ---------------------------------------------------------------------------
# 阶段判定权在模型：本模块不做字符匹配
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,title", [
    ("cargo test --workspace", "Rust 全量测试"),
    ("docker compose up -d && pytest", "全流程拉起"),
    ("bash scripts/ci_local.py", "本地 CI 总入口"),
    ("npx playwright test", "E2E 套件"),
    ("make e2e", "端到端"),
    ("pytest", "整仓测试"),
])
def test_full_suite_command_in_phase_one_is_left_alone(command, title) -> None:
    """形态像全量门禁**不作为**改阶段的理由。

    2026-09-16："判断是 phase1 还是 phase2 应该在用模型生成任务
    列表的同时就去判断，不要用单纯的字符匹配去做这件事情。"

    第一版按命令/标题特征做"确定性提升"，on that plan把 9 条本该留在
    Phase 1 的 VP 误提升（裸 ``e2e`` 命中路径 ``tests/e2e/*.spec.ts``、
    标题 ``端到端`` 把方法词当范围词），Phase 2 从 2 条涨到 11 条。
    """
    from verification_phases import normalize_plan_phases, is_final_gate
    plan = _plan(_vp("VP-001", title, command))
    summary = normalize_plan_phases(plan)
    vp = plan["verification_points"][0]
    assert is_final_gate(vp) is False, "本模块不得改判阶段"
    assert "phase_promoted_by" not in vp
    assert summary["phase2"] == 0


def test_scoped_spec_file_in_path_is_not_a_full_suite() -> None:
    """回归：``tests/e2e/xxx.spec.ts`` 是路径，不是"全量 E2E 套件"。"""
    from verification_phases import normalize_plan_phases, is_final_gate
    plan = _plan(_vp(
        "VP-019", "Playwright 实测：/data-viewer tooltip",
        "cd frontend && npx playwright test tests/e2e/tooltip_0903.spec.ts",
    ))
    normalize_plan_phases(plan)
    assert is_final_gate(plan["verification_points"][0]) is False


def test_model_labelled_phase_two_is_honoured() -> None:
    """模型标了 Phase 2 就留在 Phase 2 —— 本模块只归一化，不改判。"""
    from verification_phases import normalize_plan_phases, is_final_gate
    plan = _plan(_vp("VP-001", "单元测试", "pytest tests/x.py"),
                 _vp("VP-002", "全量 Nightly CI", "scripts/ci_local.py",
                     verification_phase=2, phase_order=1))
    summary = normalize_plan_phases(plan)
    assert summary["phase2"] == 1
    assert is_final_gate(plan["verification_points"][1]) is True


def test_normalize_reports_phase_distribution_and_gates() -> None:
    """摘要要能回溯"哪几条是关卡、顺序是多少"（可审计）。"""
    from verification_phases import normalize_plan_phases
    plan = _plan(
        _vp("VP-001", "单元测试", "pytest tests/x.py"),
        _vp("VP-028", "全量 Nightly CI", "scripts/ci_local.py",
            verification_phase=2, phase_order=1),
        _vp("VP-029", "全量 E2E", "npx playwright test",
            verification_phase=2, phase_order=2),
    )
    summary = normalize_plan_phases(plan)
    assert (summary["phase1"], summary["phase2"]) == (1, 2)
    assert summary["gates"] == [
        {"id": "VP-028", "phase_order": 1},
        {"id": "VP-029", "phase_order": 2},
    ]
    assert summary["missing_phase_order"] == []


# ---------------------------------------------------------------------------
# phase_order 推断
# ---------------------------------------------------------------------------

def test_phase_order_comes_from_the_model() -> None:
    """顺序取模型写的 ``phase_order``：Nightly=1 先，E2E=2 后。

    这是一个**契约值**断言——提示词要求 Phase 2 的 Nightly CI 填 1、
    E2E 填 2。本模块不再靠标题/命令关键词猜谁先谁后。
    """
    from verification_phases import (
        normalize_plan_phases, split_by_phase, ORDER_NIGHTLY, ORDER_E2E,
    )
    assert (ORDER_NIGHTLY, ORDER_E2E) == (1, 2)
    plan = _plan(
        _vp("VP-001", "全量 E2E 测试", "npx playwright test",
            verification_phase=2, phase_order=ORDER_E2E),
        _vp("VP-002", "全量 Nightly CI", "bash scripts/ci_local.py",
            verification_phase=2, phase_order=ORDER_NIGHTLY),
    )
    summary = normalize_plan_phases(plan)
    assert summary["missing_phase_order"] == []
    _core, gates = split_by_phase(plan["verification_points"])
    assert [vp["id"] for vp in gates] == ["VP-002", "VP-001"]   # nightly → e2e


def test_missing_phase_order_falls_back_to_plan_order() -> None:
    """模型漏标 ``phase_order``：退回计划顺序，并记进审计摘要。"""
    from verification_phases import normalize_plan_phases, split_by_phase
    plan = _plan(
        _vp("VP-028", "全量 Nightly CI", "bash scripts/ci_local.py",
            verification_phase=2),
        _vp("VP-029", "全量 E2E", "npx playwright test",
            verification_phase=2),
    )
    summary = normalize_plan_phases(plan)
    assert summary["missing_phase_order"] == ["VP-028", "VP-029"]
    assert [g["phase_order"] for g in summary["gates"]] == [1, 2]
    _core, gates = split_by_phase(plan["verification_points"])
    assert [vp["id"] for vp in gates] == ["VP-028", "VP-029"]


def test_explicit_phase_order_wins() -> None:
    from verification_phases import normalize_plan_phases
    plan = _plan(_vp("VP-001", "Nightly CI", "bash scripts/ci_local.py",
                     verification_phase=2, phase_order=5))
    normalize_plan_phases(plan)
    assert plan["verification_points"][0]["phase_order"] == 5
    assert plan["verification_points"][0]["verification_phase"] == 2


# ---------------------------------------------------------------------------
# split_by_phase
# ---------------------------------------------------------------------------

def test_split_by_phase_orders_gates_by_phase_order() -> None:
    from verification_phases import split_by_phase, normalize_plan_phases
    plan = _plan(
        _vp("VP-001", "单元测试", "pytest tests/x.py"),
        _vp("VP-002", "全量 E2E", "npx playwright test",
            verification_phase=2, phase_order=2),
        _vp("VP-003", "Nightly CI", "bash scripts/ci_local.py",
            verification_phase=2, phase_order=1),
        _vp("VP-004", "另一个单元测试", "pytest tests/y.py"),
    )
    normalize_plan_phases(plan)
    core, gates = split_by_phase(plan["verification_points"])
    assert [vp["id"] for vp in core] == ["VP-001", "VP-004"]
    assert [vp["id"] for vp in gates] == ["VP-003", "VP-002"]  # 按 phase_order


def test_normalize_returns_empty_summary_for_garbage() -> None:
    from verification_phases import normalize_plan_phases
    assert normalize_plan_phases({})["total"] == 0
    assert normalize_plan_phases({"verification_points": "nope"})["total"] == 0


# ---------------------------------------------------------------------------
# 护栏豁免（Phase 2 不受有界命令要求约束）
# ---------------------------------------------------------------------------

def test_command_guard_exempts_phase_two(tmp_path: Path) -> None:
    from verification_command_guard import annotate_plan
    plan = _plan(
        _vp("VP-002", "Nightly CI", "pytest backend/tests/",
            verification_phase=2),
    )
    findings = annotate_plan(plan, tmp_path)
    assert findings == []
    assert "command_guard" not in plan["verification_points"][0]


def test_command_guard_still_flags_phase_one(tmp_path: Path) -> None:
    from verification_command_guard import annotate_plan
    plan = _plan(_vp("VP-001", "整仓测试", "pytest backend/tests/"))
    findings = annotate_plan(plan, tmp_path)
    assert findings, "Phase 1 的无界命令必须仍然被拦下"


def test_semantic_audit_exempts_phase_two() -> None:
    """Phase 2 不进语义审计候选集 —— 全量命令不该被按有界口径判。"""
    import inspect
    from verification_agent import VerificationAgent
    src = inspect.getsource(VerificationAgent._llm_audit_commands)
    assert "is_final_gate" in src


# ---------------------------------------------------------------------------
# 声明驱动的提升：Phase 2 专用方法写在 Phase 1 上（2026-09-18, D3）
# ---------------------------------------------------------------------------

def test_phase2_only_method_on_phase_one_is_promoted() -> None:
    """``full_ci`` 自己就声明了"我是整仓门禁"，写在 Phase 1 上是自相矛盾。"""
    from verification_phases import normalize_plan_phases
    plan = _plan({
        "id": "VP-017",
        "verification_method": "full_ci",
        "verification_phase": 1,
        "ci_entry": "pytest tests/",
    })
    summary = normalize_plan_phases(plan)

    vp = plan["verification_points"][0]
    assert vp["verification_phase"] == 2
    assert vp["phase_promoted_by"] == "method:full_ci"
    assert summary["phase2"] == 1
    assert summary["phase1"] == 0
    assert summary["phase_promoted"] == [{"id": "VP-017", "method": "full_ci"}]


def test_e2e_on_phase_one_is_promoted() -> None:
    from verification_phases import normalize_plan_phases
    plan = _plan({
        "id": "VP-018",
        "verification_method": "e2e",
        "verification_phase": 1,
        "target_url": "http://127.0.0.1:3000/x",
    })
    normalize_plan_phases(plan)
    assert plan["verification_points"][0]["verification_phase"] == 2
    assert plan["verification_points"][0]["phase_promoted_by"] == "method:e2e"


def test_promotion_survives_a_missing_phase_field() -> None:
    """没写 ``verification_phase`` 等于 Phase 1 —— 同样要提升。"""
    from verification_phases import normalize_plan_phases
    plan = _plan({"id": "VP-017", "verification_method": "full_ci",
                  "ci_entry": "pytest tests/"})
    normalize_plan_phases(plan)
    assert plan["verification_points"][0]["verification_phase"] == 2


def test_promoted_vp_gets_a_phase_order() -> None:
    from verification_phases import normalize_plan_phases
    plan = _plan({
        "id": "VP-017",
        "verification_method": "full_ci",
        "ci_entry": "pytest tests/",
    })
    summary = normalize_plan_phases(plan)
    assert plan["verification_points"][0]["phase_order"] == 1
    assert summary["gates"] == [{"id": "VP-017", "phase_order": 1}]


def test_phase_one_methods_are_never_promoted() -> None:
    """提升只认 ``e2e`` / ``full_ci`` 这两个声明 —— 不碰 api_test。"""
    from verification_phases import normalize_plan_phases
    plan = _plan({
        "id": "VP-001",
        "verification_method": "api_test",
        "verification_phase": 1,
        "request": {"method": "GET", "url": "http://127.0.0.1:1/x"},
        "assertions": [{"name": "ok", "status": 200}],
    })
    summary = normalize_plan_phases(plan)
    assert plan["verification_points"][0]["verification_phase"] == 1
    assert "phase_promoted_by" not in plan["verification_points"][0]
    assert summary["phase_promoted"] == []


def test_an_explicit_phase_two_is_not_double_promoted() -> None:
    from verification_phases import normalize_plan_phases
    plan = _plan({
        "id": "VP-017",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": "pytest tests/",
    })
    summary = normalize_plan_phases(plan)
    assert "phase_promoted_by" not in plan["verification_points"][0]
    assert summary["phase_promoted"] == []


def test_promotion_does_not_touch_neighbours() -> None:
    from verification_phases import normalize_plan_phases
    plan = _plan(
        {"id": "VP-001", "verification_method": "code_review",
         "verification_phase": 1},
        {"id": "VP-017", "verification_method": "full_ci",
         "ci_entry": "pytest tests/"},
        {"id": "VP-018", "verification_method": "ui_validation",
         "target_url": "http://127.0.0.1:3000/x"},
    )
    summary = normalize_plan_phases(plan)
    phases = [vp["verification_phase"] for vp in plan["verification_points"]]
    assert phases == [1, 2, 1]
    assert (summary["phase1"], summary["phase2"]) == (2, 1)


def test_unknown_method_is_not_promoted() -> None:
    """只有那两个声明的提升；未知方法留给 method 归一化去标 obsolete。"""
    from verification_phases import normalize_plan_phases
    plan = _plan({"id": "VP-001", "verification_method": "command_run"})
    summary = normalize_plan_phases(plan)
    assert plan["verification_points"][0]["verification_phase"] == 1
    assert summary["phase_promoted"] == []
