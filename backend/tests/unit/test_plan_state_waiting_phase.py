"""等待相位统一为 ``queued``（``ready`` 只作输入别名）。

背景
----
「任务已生成、等人（或调度器）决定何时开始」这件事，历史上叫 ``ready``，
现在叫 ``queued``。两套词汇说的是同一件事，于是读路径上每处都得各判一次
「这是不是 ready」——判漏一处，就是一个卡在旧相位里出不来、也没人认领的
计划。

R10 把 ``queued`` 定为**唯一**的等待相位：``ready`` 降级为**输入别名**。
写路径不再产生它，但仍然接受它（并留一条弃用日志），因为既有数据库行里
还存着它。

钉死的行为
----------
* ``transition_to('ready')`` 落到 ``queued`` —— 不再落回旧词；
* ``executing`` / ``failed`` / ``stopped`` 都能走到 ``queued``；
* ``verification_running → queued`` 仍然非法：验证中要停下来，必须先经
  ``stopped``；
* 产物检测器在只有 ``tasks.json`` 时给出 ``queued``；
* ``PHASE_LABELS['queued']`` 是中文标签（列表接口据此起显式桶）。

边界
----
* 归一化后与当前相位相同 → 幂等 no-op，不抛异常；
* 命中别名要留痕：一条带 plan_id 与来源的 warning，否则旧数据永远清不掉
  而没人知道它还在；
* ``_load_state_from_legacy_file`` 的读取语义**不变**：旧文件里写着的
  ``ready`` 仍然读成 ``ready`` —— 迁移由写路径完成，不是读路径。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import (  # noqa: E402
    PHASE_LABELS,
    PHASE_TRANSITIONS,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
)


def _tasks_only_plan_dir(tmp_path, plan_id: str) -> Path:
    """一个只有 ``tasks.json`` 的 plan 目录 —— 走产物检测器那条路。"""
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text("{}", encoding="utf-8")
    return plan_dir


# ---------------------------------------------------------------------------
# 1) 写路径：别名归一化
# ---------------------------------------------------------------------------


def test_transition_to_ready_lands_queued(sample_plan_factory):
    """``transition_to('ready')`` 必须落到 ``queued``。

    ``ready`` 是**输入别名**：接受它，但不再写它。
    """
    plan_dir = sample_plan_factory(plan_id="waiting-alias", phase="ready")
    ps = PlanState(plan_dir)

    ps.transition_to("ready")

    assert ps.get_current_phase() == "queued", (
        f"ready 是 queued 的输入别名，写路径必须落 queued；实际 {ps.get_current_phase()!r}"
    )


def test_transition_to_ready_is_idempotent_when_already_queued(sample_plan_factory):
    """已经在 ``queued`` 时再 ``transition_to('ready')``：幂等 no-op，不抛。"""
    plan_dir = sample_plan_factory(plan_id="waiting-idempotent", phase="queued")
    ps = PlanState(plan_dir)

    ps.transition_to("ready")  # 不抛即为通过

    assert ps.get_current_phase() == "queued", (
        "归一化后等于当前相位，必须是 no-op"
    )


def test_transition_to_ready_leaves_a_deprecation_trace(sample_plan_factory, caplog):
    """命中别名要留痕：warning 必须带 plan_id 与来源。"""
    plan_dir = sample_plan_factory(plan_id="waiting-deprecated", phase="ready")
    ps = PlanState(plan_dir)

    with caplog.at_level(logging.WARNING, logger="plan_state"):
        ps.transition_to("ready")

    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "waiting-deprecated" in text, (
        f"弃用日志必须带 plan_id；实际记录：{text!r}"
    )
    assert "plan_state" in text, (
        f"弃用日志必须带来源；实际记录：{text!r}"
    )


# ---------------------------------------------------------------------------
# 2) 转移边
# ---------------------------------------------------------------------------


def test_executing_to_queued_is_legal(sample_plan_factory):
    """``executing → queued`` 必须合法（执行完回到等待）。"""
    plan_dir = sample_plan_factory(plan_id="exec-to-queued", phase="executing")
    ps = PlanState(plan_dir)

    ps.transition_to("queued")

    assert ps.get_current_phase() == "queued", (
        f"executing → queued 必须合法；实际 {ps.get_current_phase()!r}"
    )


def test_verification_running_to_queued_is_illegal(sample_plan_factory):
    """``verification_running → queued`` 仍然非法：必须先经 ``stopped``。

    验证跑到一半直接回到等待相位，等于把「验证还在跑」这件事从相位里抹掉
    —— 后台那条链还在跑，而相位说它停了。
    """
    plan_dir = sample_plan_factory(plan_id="ver-to-queued", phase="verification_running")
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError):
        ps.transition_to("queued")

    # 必须先经 stopped 才能停。
    ps.transition_to("stopped")
    ps.transition_to("queued")
    assert ps.get_current_phase() == "queued", (
        "stopped 之后必须能停到 queued"
    )


def test_waiting_phase_rows_declare_queued():
    """``executing`` / ``failed`` / ``stopped`` 三行都要声明 ``queued``。"""
    for table, name in (
        (PHASE_TRANSITIONS, "PHASE_TRANSITIONS"),
        (VERIFICATION_PHASE_TRANSITIONS, "VERIFICATION_PHASE_TRANSITIONS"),
    ):
        for src in ("executing", "failed", "stopped"):
            assert "queued" in table.get(src, []), (
                f"{name}[{src!r}] 必须包含 'queued'；实际 {table.get(src)!r}"
            )


def test_ready_stays_a_table_key():
    """``ready`` 的**表键**保留：磁盘上的旧行仍然停在 ``ready``。"""
    assert "ready" in PHASE_TRANSITIONS, (
        "ready 必须继续作为表键存在，否则停在旧相位的计划哪儿也去不了"
    )
    assert "ready" in VERIFICATION_PHASE_TRANSITIONS, (
        "ready 必须继续作为表键存在，否则停在旧相位的计划哪儿也去不了"
    )


# ---------------------------------------------------------------------------
# 3) 读路径：产物检测器
# ---------------------------------------------------------------------------


def test_detect_phase_returns_queued_for_tasks_branch(tmp_path):
    """只有 ``tasks.json`` 时，检测器给出 ``queued``（不是 ``ready``）。"""
    plan_dir = _tasks_only_plan_dir(tmp_path, "detect-queued")

    assert PlanState(plan_dir).get_current_phase() == "queued", (
        "只有 tasks.json 时必须推断为 queued"
    )


# ---------------------------------------------------------------------------
# 4) 展示面
# ---------------------------------------------------------------------------


def test_phase_label_for_queued_is_chinese():
    """``queued`` 必须有中文稳定标签。"""
    assert PHASE_LABELS["queued"] == "排队中", (
        f"queued 的标签必须是「排队中」；实际 {PHASE_LABELS.get('queued')!r}"
    )


# ---------------------------------------------------------------------------
# 5) 回退
# ---------------------------------------------------------------------------


def test_reset_to_queued_is_legal(sample_plan_factory):
    """``reset_to('queued')`` 必须合法（回退到等待相位）。"""
    plan_dir = sample_plan_factory(plan_id="reset-to-queued", phase="executing")
    ps = PlanState(plan_dir)

    ps.reset_to("queued")

    assert ps.get_current_phase() == "queued", (
        f"reset_to('queued') 必须合法；实际 {ps.get_current_phase()!r}"
    )
