"""验证分支的阶段推断、转移边、前缀排除与稳定标签。

背景
----
计划书（``verification-plan.md``）生成之后，这条 plan 必须**停在审核阶段**等人
逐条 accept / skip，而不是掉回 ``prd_review`` 或 ``ready``。阶段推断因此要认这
份产物 —— 而且只认它：机器格式的 VP 清单（``verification_plan.json``）是计划书
翻译出来的产物，不是审核对象。

四个容易写反的地方
------------------
1. **判定顺序**：``tasks.json`` 先于计划书；两者同时存在是**冲突**（框架不替人
   猜意图），必须抛 :class:`plan_artifacts.ArtifactConflictError`。
2. **前缀排除**：``transition_to`` 的执行门禁判据是 ``phase.startswith("verification")``。
   新阶段的名字以 ``verification`` 开头，不排除就会被自己拦下 ——
   ``tasks_generation → verification_plan_review`` 在「计划书刚写完」这一步就抛
   ``ValueError``。
3. **VP 清单不是计划书**：只有 ``verification_plan.json`` 时不得推断为审核阶段。
4. **既有推断链逐字不变**：``test-design.md`` / ``arch-design.md`` / ``prd.json`` /
   ``interview.json`` 的判定与改动前完全一致，空目录的结果也一模一样。

钉住的东西
----------
* ``test_infer_phase_order`` —— 四类目录的推断结果（ready / 审核阶段 / 原结果），
  以及两类产物同时存在时的冲突报错；
* ``test_verification_plan_json_is_not_doc`` —— VP 清单不触发审核阶段；
* ``test_prefix_exclusion_allows_review_phase`` —— 前缀排除只排除新阶段；
* ``test_phase_label_stable`` —— 新阶段的稳定标签是「计划书待审」。

每个目录用**独立的子目录**：``PlanState`` 落到 ``plan_routing`` 表里，键是目录
名。同一个目录名在一个测试里先后放不同的产物，读到的会是第一次写下的那一行，
推断根本不会重跑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_artifacts import ArtifactConflictError  # noqa: E402
from plan_state import (  # noqa: E402
    PHASE_LABELS,
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    VALID_PHASES,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
)

#: 审核阶段的名字。计划书生成后停在这里，等人逐条审。
REVIEW_PHASE = "verification_plan_review"


def _make_plan_dir(root: Path, name: str) -> Path:
    """建一个空的 plan 目录。

    每个用例用**自己的**目录名：``plan_routing`` 的键就是目录名，同名目录会
    复用上一次写下的 ``current_phase``，阶段推断根本不会重跑。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_plan_state(
    plan_dir: Path,
    phase: str,
    completed_phases: list | None = None,
) -> None:
    """写一份旧的 ``plan_state.json``，把计划直接放在某个阶段上。"""
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "current_phase": phase,
            "completed_phases": list(completed_phases or []),
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )


def _plan_doc(plan_dir: Path) -> None:
    """放一份验证计划书（人工审核对象）。"""
    (plan_dir / "verification-plan.md").write_text(
        "# 验证计划书\n\n本计划书是人工审核对象。\n",
        encoding="utf-8",
    )


def _vp_manifest(plan_dir: Path) -> None:
    """放一份机器格式的 VP 清单（计划书的翻译结果，不是审核对象）。"""
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"services": [], "verification_points": []}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1) 推断顺序
# ---------------------------------------------------------------------------


def test_infer_phase_order(tmp_path):
    """四类目录 → ready / 审核阶段 / 原结果；两类产物同时存在 → 冲突。"""
    # (1) 只有 ``tasks.json`` —— 开发分支，既有判定不变。
    dev_dir = _make_plan_dir(tmp_path, "dev-only")
    (dev_dir / "tasks.json").write_text("{}", encoding="utf-8")
    assert PlanState(dev_dir).get_current_phase() == "ready", (
        "只有 tasks.json 时必须推断为 ready"
    )

    # (2) 只有计划书 —— 验证分支，停在审核阶段。
    doc_dir = _make_plan_dir(tmp_path, "doc-only")
    _plan_doc(doc_dir)
    assert PlanState(doc_dir).get_current_phase() == REVIEW_PHASE, (
        "只有 verification-plan.md 时必须推断为 verification_plan_review"
    )

    # (3) 只有 VP 清单 —— 不是计划书，沿用改动前的推断结果。
    manifest_dir = _make_plan_dir(tmp_path, "manifest-only")
    _vp_manifest(manifest_dir)
    assert PlanState(manifest_dir).get_current_phase() == "interview", (
        "verification_plan.json 不是计划书，不能推断为审核阶段"
    )

    # (4) 空目录 —— 结果与改动前逐字一致。
    empty_dir = _make_plan_dir(tmp_path, "empty")
    assert PlanState(empty_dir).get_current_phase() == "interview", (
        "空目录的推断结果必须与改动前逐字一致"
    )

    # (5) 两类产物同时存在 —— 冲突，拒绝猜测意图。
    conflict_dir = _make_plan_dir(tmp_path, "conflict")
    (conflict_dir / "tasks.json").write_text("{}", encoding="utf-8")
    _plan_doc(conflict_dir)
    with pytest.raises(ArtifactConflictError):
        PlanState(conflict_dir)


def test_verification_plan_json_is_not_doc(tmp_path):
    """VP 清单（``verification_plan.json``）不触发审核阶段。

    两者只差一个连字符，但一个是**人工审核对象**，一个是它的机器翻译结果。
    认错文件的后果是：计划书还没审，阶段已经跳到「待审」之外的地方。
    """
    plan_dir = _make_plan_dir(tmp_path, "manifest-only")
    _vp_manifest(plan_dir)

    phase = PlanState(plan_dir).get_current_phase()

    assert phase != REVIEW_PHASE, (
        f"verification_plan.json 不是计划书，推断结果不应是 {REVIEW_PHASE!r}；"
        f"实际得到 {phase!r}"
    )
    assert phase == "interview", (
        f"只有 VP 清单时沿用改动前的推断结果 'interview'；实际得到 {phase!r}"
    )


def test_infer_phase_keeps_legacy_chain(tmp_path):
    """既有推断链逐字不变：计划书不挤掉 ``test-design.md`` 的判定。

    计划书的判定排在 ``tasks.json`` 之后、``test-design.md`` 之前；两者同时存在
    时按顺序取先命中的那个。
    """
    plan_dir = _make_plan_dir(tmp_path, "doc-and-test-design")
    _plan_doc(plan_dir)
    (plan_dir / "test-design.md").write_text("# 测试设计\n", encoding="utf-8")

    assert PlanState(plan_dir).get_current_phase() == REVIEW_PHASE, (
        "计划书的判定排在 test-design.md 之前"
    )


# ---------------------------------------------------------------------------
# 2) 前缀排除
# ---------------------------------------------------------------------------


def test_prefix_exclusion_allows_review_phase(tmp_path):
    """``tasks_generation → verification_plan_review`` 不被前缀门禁拦下。

    执行门禁的判据是 ``phase.startswith("verification")``。新阶段的名字正好以
    ``verification`` 开头，不显式排除就会在「计划书刚写完」这一步自己把自己
    拦下，抛 ``ValueError``。
    """
    plan_dir = _make_plan_dir(tmp_path, "plan")
    # ``completed_phases`` 里没有 ``execution`` —— 执行门禁是这条边上唯一的
    # 拦路虎，排除生效时它不该再拦。
    _write_plan_state(plan_dir, "tasks_generation", completed_phases=[])
    ps = PlanState(plan_dir)
    ps.reload()

    ps.transition_to(REVIEW_PHASE)

    assert ps.get_current_phase() == REVIEW_PHASE, (
        f"tasks_generation → {REVIEW_PHASE} 必须合法；"
        f"实际停在 {ps.get_current_phase()!r}"
    )


def test_prefix_exclusion_does_not_loosen_other_verification_phases(tmp_path):
    """排除只针对新阶段：其余验证阶段仍受执行门禁约束。

    负控。判据写成 ``phase.startswith("verification") and phase != "..."`` 时，
    真正的风险是**排除得太宽** —— 把执行门禁对所有验证阶段一起关掉。开发分支
    上「没有 execution 就不能进验证」的原判定必须逐字生效。
    """
    plan_dir = _make_plan_dir(tmp_path, "plan")
    _write_plan_state(plan_dir, "ready", completed_phases=[])
    ps = PlanState(plan_dir)
    ps.reload()

    with pytest.raises(ValueError, match="without 'execution' in completed_phases"):
        ps.transition_to("verification")


# ---------------------------------------------------------------------------
# 3) 转移边
# ---------------------------------------------------------------------------


def test_review_phase_is_in_the_vocabulary():
    """新阶段进入阶段词汇，且不是终态。"""
    assert REVIEW_PHASE in VALID_PHASES
    assert REVIEW_PHASE not in TERMINAL_PHASES


def test_review_phase_declares_its_edges():
    """四条新边写进转移表，而不是碰巧可达。"""
    assert REVIEW_PHASE in PHASE_TRANSITIONS["tasks_generation"], (
        f"tasks_generation 必须声明一条到 {REVIEW_PHASE} 的边；"
        f"实际有 {PHASE_TRANSITIONS['tasks_generation']!r}"
    )
    # ``transition_to`` unions the two tables before it looks a target up,
    # so the contract is the *effective* legality set — which table holds
    # a row is not a behavioural difference.
    allowed = set(PHASE_TRANSITIONS.get(REVIEW_PHASE, []))
    allowed.update(VERIFICATION_PHASE_TRANSITIONS.get(REVIEW_PHASE, []))
    assert allowed == {"verification", "tasks_generation", "stopped"}, (
        f"{REVIEW_PHASE} 的出边必须是 verification / tasks_generation / stopped；"
        f"实际有 {sorted(allowed)!r}"
    )


def test_review_phase_can_go_back_and_stop(tmp_path):
    """审核阶段可以退回任务生成（重新生成计划书），也可以被停下。"""
    for target in ("tasks_generation", "stopped"):
        plan_dir = _make_plan_dir(tmp_path, f"plan-{target}")
        _write_plan_state(plan_dir, REVIEW_PHASE, completed_phases=[])
        ps = PlanState(plan_dir)
        ps.reload()

        ps.transition_to(target)

        assert ps.get_current_phase() == target, (
            f"{REVIEW_PHASE} → {target} 必须合法；"
            f"实际停在 {ps.get_current_phase()!r}"
        )


# ---------------------------------------------------------------------------
# 4) 稳定标签
# ---------------------------------------------------------------------------


def test_phase_label_stable():
    """新阶段的稳定标签是「计划书待审」。

    标签写在**一处**（``plan_state.PHASE_LABELS``），状态接口与列表接口共用它；
    展示面不再按文件存在性猜状态。
    """
    assert PHASE_LABELS[REVIEW_PHASE] == "计划书待审", (
        f"{REVIEW_PHASE} 的稳定标签必须是 '计划书待审'；"
        f"实际是 {PHASE_LABELS[REVIEW_PHASE]!r}"
    )
