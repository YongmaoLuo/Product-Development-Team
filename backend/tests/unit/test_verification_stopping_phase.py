"""2026-11-20: ``verification_stopping`` —— 已退休的旧相位名（只作输入别名）。

背景
----
旧名是按**阶段**命名的（「验证正在停止」）。架构决策 1 要求停止这件事只有
**一个** plan 级状态：``user_stopping``（execution / verification / repair
共用），不按阶段各起一个名字 —— 否则「停」在词表里就有两个名字，读路径要
各判一次，判漏一处就是一个「显示已停、后台仍在派发」的计划。

旧名因此从词表里**消失**：留着它，词表里就有两个「正在停止」，迁移表也得
为它多留一行。

但既有 ``plan_routing`` 行里存着旧值 —— 读路径必须把它归一化成新相位，
否则那些计划停在一个词表里已经没有的相位上，没人能推进它，也没人认领它。

本文件钉住的是**旧名退役**这件事：

* 旧名不在词表里（也不再是任何迁移表的源或落点）；
* 旧名归一化成新名；
* 停在旧名上的计划（DB 行 / 旧版 ``plan_state.json``）读出来就是新名。

新相位本身的契约（词表 / 来源边 / 落点）在
``tests/unit/test_user_stopping_phase.py``。
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

#: 旧名（按阶段命名）。只作**输入别名**：既有数据行里存着它。
LEGACY_STOPPING_PHASE = "verification_stopping"

#: 新名（plan 级，execution / verification / repair 共用）。
STOPPING_PHASE = "user_stopping"


def _make_plan_dir(root: Path, name: str) -> Path:
    """建一个 plan 目录（每个用例自己的名字：目录名是 ``plan_routing`` 的键）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 1) 旧名彻底退休
# ---------------------------------------------------------------------------


def test_legacy_phase_is_not_in_the_vocabulary():
    """旧名不在词表里 —— 词表里只有一个「正在停止」。

    留着旧名，词表里就有两个同义的相位，读路径要各判一次。
    """
    assert LEGACY_STOPPING_PHASE not in VALID_PHASES, (
        f"{LEGACY_STOPPING_PHASE!r} must be out of the vocabulary; got "
        f"{VALID_PHASES}"
    )
    assert LEGACY_STOPPING_PHASE not in TERMINAL_PHASES, (
        f"{LEGACY_STOPPING_PHASE!r} must be out of the terminal set; got "
        f"{TERMINAL_PHASES}"
    )


def test_no_transition_edge_mentions_the_legacy_name():
    """两张迁移表里都不再出现旧名 —— 它连一条边都没有。

    留着边就等于旧名还活着：那条边可以走到的落点会绕过新相位的规则。
    """
    for table_name, table in (
        ("PHASE_TRANSITIONS", PHASE_TRANSITIONS),
        ("VERIFICATION_PHASE_TRANSITIONS", VERIFICATION_PHASE_TRANSITIONS),
    ):
        assert LEGACY_STOPPING_PHASE not in table, (
            f"{table_name} must not have a {LEGACY_STOPPING_PHASE!r} row; got "
            f"{table.get(LEGACY_STOPPING_PHASE)!r}"
        )
        offenders = {
            src: dsts
            for src, dsts in table.items()
            if LEGACY_STOPPING_PHASE in dsts
        }
        assert not offenders, (
            f"{table_name} must not declare {LEGACY_STOPPING_PHASE!r} as a "
            f"target; got {offenders}"
        )


# ---------------------------------------------------------------------------
# 2) 旧名归一化
# ---------------------------------------------------------------------------


def test_legacy_phase_normalizes_to_user_stopping():
    """旧值归一化成新值。

    既有 ``plan_routing`` 行里存着旧值：读路径必须把它认成新相位，否则
    那些计划会停在一个词表里已经没有的相位上。
    """
    assert normalize_phase(LEGACY_STOPPING_PHASE) == STOPPING_PHASE, (
        f"{LEGACY_STOPPING_PHASE!r} must normalize to {STOPPING_PHASE!r}; got "
        f"{normalize_phase(LEGACY_STOPPING_PHASE)!r}"
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
    ``tests/unit/test_user_stopping_phase.py::test_unknown_phase_still_raises``）；
    归一化函数本身不吞掉它。
    """
    assert normalize_phase("not_a_phase") == "not_a_phase"
    assert normalize_phase(None) is None


@pytest.mark.parametrize("unknown", ["not_a_phase", "verification_stoppng"])
def test_writing_an_unknown_phase_still_raises(tmp_path, unknown):
    """写路径上未知相位仍然报错 —— 换名不改变判定口径。"""
    plan_dir = _make_plan_dir(tmp_path, f"unknown-{unknown}")
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "current_phase": "executing",
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {
                "status": "pending", "round": 0, "max_rounds": 3,
                "stop_reason": None,
            },
        }),
        encoding="utf-8",
    )
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError) as excinfo:
        ps.transition_to(unknown)

    assert f"Invalid phase: {unknown}" in str(excinfo.value), (
        f"the invalid-phase error must keep its wording; got "
        f"{str(excinfo.value)!r}"
    )
    assert ps.get_current_phase() == "executing", (
        f"a rejected transition must not move the plan; got "
        f"{ps.get_current_phase()!r}"
    )
