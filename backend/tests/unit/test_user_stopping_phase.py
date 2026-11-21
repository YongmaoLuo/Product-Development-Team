"""2026-11-20: ``user_stopping`` —— plan 级「正在停止」相位。

背景
----
架构决策 1：停止这件事**只有一个**计划级状态。execution / verification /
repair 三处的停止共用同一个相位，不按阶段各起一个名字 —— 否则「停」在词表
里就有三个名字，读路径要各判一次，判漏一处就是一个「显示已停、后台仍在
派发」的计划。

此前词表里只有 ``verification_stopping``（按阶段命名）。execution 的停止用
不了它：那个名字属于验证子机器，而且因为 ``verification`` 前缀落在**执行
门禁之后** —— 一个执行到一半被喊停的计划根本走不到那个相位。执行停止与验证
停止因此无法共用同一状态（违反 G1）。

本任务把词表换成 plan 级的 ``user_stopping``，并把旧值
``verification_stopping`` 作为历史别名归一化
（:func:`plan_state.normalize_phase`）。

钉住的东西
----------
* ``test_user_stopping_in_valid_phases`` —— 新名在词表里、旧名不在，且新名
  **不是**终态（停是中间态：停了还要能落到终态）；
* ``test_executing_can_enter_user_stopping`` —— 执行中的计划能进入它；
* ``test_legacy_phase_is_normalized`` —— 旧值归一化成新值；
* 三条来源边（execution / verification / repair）、落点，以及「未知相位仍然
  报错」。

两个容易写错的地方
------------------
1. **迁移表只有一张。** ``transition_to`` 取的是 ``PHASE_TRANSITIONS`` 与
   ``VERIFICATION_PHASE_TRANSITIONS`` 的**并集**（见
   ``plan_state.PlanState._transition_to``），不是某一张表独断。本文件里的
   ``_allowed_targets`` 复刻的正是这个并集 —— 断言边是否存在时不能用其中
   一张表去猜。
2. **目录名是 ``plan_routing`` 的键。** ``PlanState`` 落到 SQLite 的
   ``plan_routing`` 表里，键是**目录名**；同名目录会复用上一次写下的
   ``current_phase``，读到的根本不是这份 ``plan_state.json``。所以每个用例
   用自己的目录名。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import (  # noqa: E402
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    VALID_PHASES,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
    normalize_phase,
)

#: 「正在停止」这个相位的名字（plan 级，execution/verification/repair 共用）。
STOPPING_PHASE = "user_stopping"

#: 旧名（按阶段命名）。只作**输入别名**：既有数据行里存着它。
LEGACY_STOPPING_PHASE = "verification_stopping"


def _allowed_targets(source: str) -> set:
    """``transition_to`` 认的合法落点集合。

    复刻 ``plan_state.PlanState._transition_to`` 的判定：主表
    (:data:`PHASE_TRANSITIONS`) 与验证子表
    (:data:`VERIFICATION_PHASE_TRANSITIONS`) 的**并集**。断言「这条边存在」
    时必须按并集算 —— 只查其中一张表会把另一张表接住的边判成不存在。
    """
    return set(PHASE_TRANSITIONS.get(source, [])) | set(
        VERIFICATION_PHASE_TRANSITIONS.get(source, [])
    )


def _make_plan_dir(root: Path, name: str) -> Path:
    """建一个 plan 目录（每个用例自己的名字，见模块 docstring）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_plan_state(plan_dir: Path, phase: str) -> None:
    """把计划直接放在某个相位上。

    ``completed_phases`` 里带 ``execution``：验证子相位都在执行门禁**之后**
    （``plan_state._is_gated_verification_phase``），没有这条记录的话门禁会
    先于迁移表把转移挡下来 —— 那测的就不是迁移表了。
    """
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "current_phase": phase,
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {
                "status": "running" if phase.startswith("verification") else "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1) 词表
# ---------------------------------------------------------------------------


def test_user_stopping_in_valid_phases():
    """新名在词表里、旧名不在，且新名**不是**终态。

    旧名必须从词表里彻底消失：留着它，「停」在词表里就还是两个名字，读
    路径又要各判一次。

    新名不进 ``TERMINAL_PHASES``：它是**中间相位** —— 停了之后还要能落到
    终态（``stopped``），进终态集合就等于把它变成死路。
    """
    assert STOPPING_PHASE in VALID_PHASES, (
        f"{STOPPING_PHASE!r} must be in VALID_PHASES; "
        f"got {VALID_PHASES}"
    )
    assert LEGACY_STOPPING_PHASE not in VALID_PHASES, (
        f"{LEGACY_STOPPING_PHASE!r} is the legacy alias and must not stay in "
        f"the vocabulary; got {VALID_PHASES}"
    )
    assert STOPPING_PHASE not in TERMINAL_PHASES, (
        f"{STOPPING_PHASE!r} is an intermediate phase and must not be terminal; "
        f"got {TERMINAL_PHASES}"
    )


# ---------------------------------------------------------------------------
# 2) 来源边：execution / verification / repair 共用同一个相位
# ---------------------------------------------------------------------------


def test_executing_can_enter_user_stopping(tmp_path):
    """执行中的计划能进入「正在停止」。

    这是 G1 的核心：执行停止与验证停止**共用**同一个状态，所以这条边必须
    走得通 —— 而且它是**声明**的边（在迁移表里），不是碰巧能走。

    边必须是声明的：表里没有这条边、只是别处的并集接住了它，换一张表就断。
    """
    assert STOPPING_PHASE in _allowed_targets("executing"), (
        f"'executing' must declare a {STOPPING_PHASE!r} edge; it has "
        f"{sorted(_allowed_targets('executing'))}"
    )

    plan_dir = _make_plan_dir(tmp_path, "enter-executing")
    _write_plan_state(plan_dir, "executing")
    ps = PlanState(plan_dir)

    ps.transition_to(STOPPING_PHASE)

    assert ps.get_current_phase() == STOPPING_PHASE, (
        f"'executing' -> {STOPPING_PHASE!r} must land the plan in "
        f"{STOPPING_PHASE!r}; got {ps.get_current_phase()!r}"
    )


@pytest.mark.parametrize(
    "source",
    ["verification_running", "verification_repairing"],
)
def test_verification_sources_can_enter_user_stopping(tmp_path, source):
    """验证侧的两条来源边同样指向这个 plan 级相位。

    名字是 plan 级的，所以三条来源边指向**同一个**相位 —— 不是三个按阶段
    命名的相位。
    """
    assert STOPPING_PHASE in _allowed_targets(source), (
        f"{source!r} must declare a {STOPPING_PHASE!r} edge; it has "
        f"{sorted(_allowed_targets(source))}"
    )

    plan_dir = _make_plan_dir(tmp_path, f"enter-{source}")
    _write_plan_state(plan_dir, source)
    ps = PlanState(plan_dir)

    ps.transition_to(STOPPING_PHASE)

    assert ps.get_current_phase() == STOPPING_PHASE, (
        f"{source!r} -> {STOPPING_PHASE!r} must land the plan in "
        f"{STOPPING_PHASE!r}; got {ps.get_current_phase()!r}"
    )


def test_stopping_is_not_behind_the_execution_gate(tmp_path):
    """执行到一半被喊停的计划**不该**被执行门禁挡在门外。

    执行门禁（``_is_gated_verification_phase``）问的是「执行完成了吗」。
    执行没完成恰恰是「执行停止」发生的前提 —— 所以这个相位不能落在门禁
    之后。

    同一个判定还有第二处后果：门禁后的相位会**自动**给 ``completed_phases``
    补一条 ``execution``（「执行完成了」）。停到一半的计划补上这条，等于在
    相位历史里记下一件它没做过的事。
    """
    plan_dir = _make_plan_dir(tmp_path, "gate")
    _write_plan_state(plan_dir, "executing")
    # 去掉那条「执行已完成」的记录 —— 执行中的计划本来就没有它。
    state_file = plan_dir / "plan_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["completed_phases"] = []
    state_file.write_text(json.dumps(state), encoding="utf-8")
    ps = PlanState(plan_dir)

    ps.transition_to(STOPPING_PHASE)

    assert ps.get_current_phase() == STOPPING_PHASE
    assert "execution" not in ps.get_completed_phases(), (
        "stopping a running execution must not record 'execution' as "
        f"completed; got {ps.get_completed_phases()!r}"
    )


# ---------------------------------------------------------------------------
# 3) 旧值归一化
# ---------------------------------------------------------------------------


def test_legacy_phase_is_normalized():
    """旧值 ``verification_stopping`` 归一化成 ``user_stopping``。

    既有 ``plan_routing`` 行里存着旧值：读路径必须把它认成新相位，否则
    那些计划会停在一个词表里已经没有的相位上，没人能推进它。
    """
    assert normalize_phase(LEGACY_STOPPING_PHASE) == STOPPING_PHASE, (
        f"{LEGACY_STOPPING_PHASE!r} must normalize to {STOPPING_PHASE!r}; "
        f"got {normalize_phase(LEGACY_STOPPING_PHASE)!r}"
    )


def test_legacy_phase_reads_as_user_stopping_from_a_db_row():
    """数据库行里的旧值读出来就是新相位。

    这是「读得到旧值」的那条路：``plan_routing`` 里存着旧值的行，读出来
    必须是新相位。
    """
    state = PlanState._sqlite_row_to_state({
        "plan_id": "legacy-row",
        "current_phase": LEGACY_STOPPING_PHASE,
        "completed_phases": None,
        "review_rounds": None,
        "flags": None,
        "verification": None,
    })

    assert state["current_phase"] == STOPPING_PHASE, (
        f"a row holding {LEGACY_STOPPING_PHASE!r} must read as "
        f"{STOPPING_PHASE!r}; got {state['current_phase']!r}"
    )


def test_legacy_phase_reads_as_user_stopping_from_a_legacy_file(tmp_path):
    """旧版 ``plan_state.json`` 里的旧值读出来也是新相位。

    两条读路径（SQLite 行 / 旧文件）必须给出同一个答案 —— 一条归一化、
    另一条不归一化的话，同一个计划从哪条路读出来取决于它落在哪种存储上。
    """
    plan_dir = _make_plan_dir(tmp_path, "legacy-file")
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "current_phase": LEGACY_STOPPING_PHASE,
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {
                "status": "running", "round": 0, "max_rounds": 3,
                "stop_reason": None,
            },
        }),
        encoding="utf-8",
    )

    ps = PlanState(plan_dir)

    assert ps.get_current_phase() == STOPPING_PHASE, (
        f"a legacy file holding {LEGACY_STOPPING_PHASE!r} must read as "
        f"{STOPPING_PHASE!r}; got {ps.get_current_phase()!r}"
    )


def test_unknown_phase_is_not_normalized():
    """未知名原样返回 —— 归一化是兼容层，不是校验器。

    未知相位必须在**写**路径上照样报错（见
    ``test_unknown_phase_still_raises``）；归一化函数本身不吞掉它。
    """
    assert normalize_phase("not_a_phase") == "not_a_phase"
    assert normalize_phase(None) is None


# ---------------------------------------------------------------------------
# 4) 落点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("landing", ["stopped", "verification", "verification_repairing"])
def test_stopping_settles(tmp_path, landing):
    """落点：终态 ``stopped``，以及验证侧的两个安顿相位。

    ``stopped`` 必须在列表里：执行停止这条路**永远**过不了执行门禁
    （执行没完成才需要停），所以验证侧那两个落点在执行停止路径上是死路 ——
    没有 ``stopped`` 这条边，「执行到一半被喊停」会把计划关在这个相位里。
    """
    assert landing in _allowed_targets(STOPPING_PHASE), (
        f"{STOPPING_PHASE!r} must be able to settle on {landing!r}; it has "
        f"{sorted(_allowed_targets(STOPPING_PHASE))}"
    )

    plan_dir = _make_plan_dir(tmp_path, f"settle-{landing}")
    _write_plan_state(plan_dir, STOPPING_PHASE)
    ps = PlanState(plan_dir)

    ps.transition_to(landing)

    assert ps.get_current_phase() == landing, (
        f"{STOPPING_PHASE!r} -> {landing!r} must be allowed; got "
        f"{ps.get_current_phase()!r}"
    )


@pytest.mark.parametrize(
    "forbidden",
    [
        "verification_running",
        "verify_first_pass",
        "verify_recheck",
        "verification_passed",
        "verification_failed",
        "verification_loop_stopped",
        "executing",
    ],
)
def test_stopping_refuses_every_unlisted_landing(tmp_path, forbidden):
    """未列出的相位一律拒绝 —— 拒绝必须是抛异常，不是静默停在原地。

    尤其 ``verification_running``：停了还能重新开跑，停就没停。
    """
    plan_dir = _make_plan_dir(tmp_path, f"refuse-{forbidden}")
    _write_plan_state(plan_dir, STOPPING_PHASE)
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError):
        ps.transition_to(forbidden)

    assert ps.get_current_phase() == STOPPING_PHASE, (
        f"a rejected transition must not move the plan; got "
        f"{ps.get_current_phase()!r}"
    )


def test_unknown_phase_still_raises(tmp_path):
    """未知相位在写路径上照样报错 —— 消息与之前一致。

    词表换名不改变「非法相位」的判定口径：不在 ``VALID_PHASES`` 里的值
    一律 ``ValueError``。
    """
    plan_dir = _make_plan_dir(tmp_path, "unknown")
    _write_plan_state(plan_dir, "executing")
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError) as excinfo:
        ps.transition_to("not_a_phase")

    assert "Invalid phase: not_a_phase" in str(excinfo.value), (
        f"the invalid-phase error must keep its wording; got "
        f"{str(excinfo.value)!r}"
    )
    assert ps.get_current_phase() == "executing", (
        "a rejected transition must not move the plan; got "
        f"{ps.get_current_phase()!r}"
    )
