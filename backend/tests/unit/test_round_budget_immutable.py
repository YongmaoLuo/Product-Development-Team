"""``max_rounds`` 是计划的预算，不可自增（2026-09-15）。

背景
----
补修后重入（``_on_repair_complete``）每跑一次就把上限 +1
（``min(_cur_max + 1, 10)``），理由是"让链条多一次迭代的余量"。实际效果是
**上限根本不约束任何东西**：``_next_round`` 按 ``round + 1`` 算，而上限同步
增长，于是轮轮失败的 plan 永远到不了 ``max_rounds_reached``。a production plan 上
留着指纹 —— ``plan_state.json`` 写 4、``state.db`` 写 5。

上限是预算，不随消耗一起抬高；每轮都自增等于没有上限。

这条与 ``reset_rounds`` 的语义（2026-09-14 决定：上限不可修改，只有
计数器在授权下可重置）必须一致。两处说法打架时，**没有授权的那一边**
（自动循环）在改预算，正是要禁掉的。

注意：只删自增是不够的 —— "仍有修复任务 → 再跑一轮修复"那条分支自身没有
任何上限判断，所以还必须在下一次重入前判死，否则链条会永远轮下去。
"""

from __future__ import annotations

import inspect

import pytest

import server


# ---------------------------------------------------------------------------
# _plan_next_round：判定的本体
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row,expected_next,expected_cap,expected_stop", [
    # 新计划：还没有行，从第 1 轮起，用默认预算（见
    # ``DEFAULT_MAX_VERIFICATION_ROUNDS``）。
    (None, 1, server.DEFAULT_MAX_VERIFICATION_ROUNDS, False),
    # 正常推进：cap 原样返回，**绝不 +1**。
    ({"round": 0, "max_rounds": 3}, 1, 3, False),
    ({"round": 1, "max_rounds": 3}, 2, 3, False),
    ({"round": 2, "max_rounds": 3}, 3, 3, False),
    # 撞顶：下一轮会超出预算 → 停。
    ({"round": 3, "max_rounds": 3}, 4, 3, True),
    # 五轮预算的计划。
    ({"round": 1, "max_rounds": 5}, 2, 5, False),
    ({"round": 4, "max_rounds": 5}, 5, 5, False),
    ({"round": 5, "max_rounds": 5}, 6, 5, True),
])
def test_plan_next_round(
    row, expected_next: int, expected_cap: int, expected_stop: bool,
) -> None:
    nxt, cap, stop = server._plan_next_round(row)
    assert (nxt, cap, stop) == (expected_next, expected_cap, expected_stop)


def test_the_cap_is_never_raised() -> None:
    """回归锁：无论当前第几轮，返回的上限都等于行里的上限。"""
    for cap in (1, 2, 3, 5, 10):
        for cur_round in range(0, cap + 3):
            _, returned_cap, _ = server._plan_next_round(
                {"round": cur_round, "max_rounds": cap}
            )
            assert returned_cap == cap, (
                f"cap 被改写了: round={cur_round} cap={cap} "
                f"-> {returned_cap}"
            )


def test_a_ten_round_plan_still_stops_at_ten() -> None:
    """旧的自增在 cap 到 10 时会把 round 推到 11、12… 永远不停。

    这里锁住：第 10 轮之后就必须停。
    """
    _, _, stop = server._plan_next_round({"round": 10, "max_rounds": 10})
    assert stop is True


def test_missing_fields_fall_back_to_the_documented_defaults() -> None:
    default = server.DEFAULT_MAX_VERIFICATION_ROUNDS
    assert server._plan_next_round({}) == (1, default, False)
    assert server._plan_next_round({"round": None, "max_rounds": None}) == (
        1, default, False,
    )


def test_default_round_budget_is_the_decided_policy() -> None:
    """把默认轮数预算的**数值**钉死。

    2026-09-19：3 → 4。同失败即停的收敛判定已经能把原地打转
    的循环在第 2 轮切断，所以上限不必再兼任主要收敛机制；放宽到 4 是给
    「失败集合在缩小」的计划多一轮修复的机会。

    套件里其余断言全都对着 ``DEFAULT_MAX_VERIFICATION_ROUNDS`` 比较，好让
    以后调默认值只改一处；这里是把数字说出口的唯一地方 —— 悄悄偏离既定
    策略会在这里响亮地失败，而不是无声地给所有计划重新定预算。
    """
    assert server.DEFAULT_MAX_VERIFICATION_ROUNDS == 4


# ---------------------------------------------------------------------------
# 结构性回归锁：闭包本身跑不动，只能锁源码形状
#
# 2026-09-18 C3：``server._run_auto_verification_loop`` 现在是一层
# try/finally 包装（工作流退出时回收计划声明的服务），循环本体改名成
# ``_run_auto_verification_loop_inner``。下面锁的是**本体**的源码形状，
# 所以 inspect 的是 inner。
# ---------------------------------------------------------------------------


def test_the_reentry_no_longer_bumps_the_cap() -> None:
    """``_on_repair_complete`` 是 ``_run_auto_verification_loop`` 内的闭包，
    要起一整条验证循环才能执行；所以这条锁源码。

    锁两件事：自增写法消失，且判定改走 ``_plan_next_round``。
    """
    src = inspect.getsource(server._run_auto_verification_loop_inner)

    # 断言的是**赋值写法**，不是裸字符串：解释这段历史的注释里也会提到
    # 那个表达式，锁裸字符串会把自己的说明文字判成回归。
    assert "_new_max = min(_cur_max + 1, 10)" not in src, (
        "自增写法又回来了 —— 上限会重新变成不约束任何东西"
    )
    assert "_plan_next_round(_cur)" in src, (
        "重入必须走 _plan_next_round 判定轮次/上限"
    )
    assert "_round_budget_exhausted" in src, (
        "撞顶后必须真的终止链条"
    )


def test_the_reentry_stops_with_max_rounds_reached() -> None:
    src = inspect.getsource(server._run_auto_verification_loop_inner)
    idx = src.index("if _round_budget_exhausted:")
    # 从判定点搜到下一条 `return`，覆盖 warning 文案 + 终态调用。
    tail = src[idx:src.index("return", idx) + 1]
    assert '"loop_stopped", "max_rounds_reached"' in tail, (
        "撞顶的终态必须是 loop_stopped / max_rounds_reached"
    )


def test_the_stale_self_extend_docstring_is_gone() -> None:
    """docstring 曾写"Bumps max_rounds by 1 (capped at 10)"。"""
    src = inspect.getsource(server._run_auto_verification_loop_inner)
    assert "Bumps ``max_rounds``" not in src, (
        "docstring 还在描述已被删除的自增行为"
    )


# ---------------------------------------------------------------------------
# 与 reset_rounds 的语义一致（两处说法不能打架）
# ---------------------------------------------------------------------------


def test_reset_and_the_reentry_agree_that_the_cap_is_immutable() -> None:
    """``reset_rounds`` 的 docstring 明确写了上限不可变。

    这条断言把两处的措辞绑在一起：任何一边改主意，另一边必须同步。
    """
    from server import ResetRoundsRequest  # noqa: F401  (存在性)
    doc = server.reset_rounds.__doc__ or ""
    assert "immutable" in doc.lower() or "不可" in doc, (
        "reset_rounds 的不可变语义不见了"
    )
    # 而自动循环那边也不再改它。
    _, cap, _ = server._plan_next_round({"round": 1, "max_rounds": 4})
    assert cap == 4
