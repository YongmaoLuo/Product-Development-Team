"""预算分层：执行器的停止预算必须严格小于服务端给它的 grace 窗口。

背景
----
服务端把 ``PDT_STOP_GRACE_SECONDS`` 当成「SIGTERM 之后到 SIGKILL 之间」
的那个窗口（``server._kill_process_tree``），执行器以前却把**同一个值**
直接当成自己的停止预算（``cli._stop_grace_seconds``）。两层预算相等，
级联稍微慢一点，整组进程就被服务端硬杀 —— 而硬杀正是这套协作式停机
存在的理由所要避免的结果。

R3 要求：服务端的 grace 必须**严格大于**执行器的预算，而且这个不等式
只能由**同一个函数**导出 —— 两处各写一份、今天碰巧相等，明天就会漂移。

这里钉住的是那条契约：

* 执行器的预算是服务端 grace 的**一半**；
* grace 低于下限时先抬到下限，再取一半；
* 预算有上限，胖手指的 grace 不能把停机路径停上几分钟；
* grace 不可用时回落到默认值，而不是「不等待」；
* CLI 那个读环境变量的入口就是共享推导本身，不是它的第二份拷贝。

测试不碰真实进程：算的都是纯函数。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cli
import coding_tool


#: 环境变量里可能出现的每一种形态：缺失、空串、非数字、非正、NaN、
#: inf，以及各档正常值。每一档都必须推导出同一个「预算 < grace」。
_RAW_GRACES = (
    None,
    "",
    "  ",
    "abc",
    "0",
    "-5",
    "nan",
    "inf",
    "1",
    "3",
    "4",
    "6",
    "10",
    "120",
    "600",
    "10000",
)


def _effective_grace(raw) -> float:
    """服务端实际等待的窗口：解析 raw，无效时用默认值，再抬到下限。

    这是**契约**的另一次陈述（不是实现的转抄）：下限
    ``MIN_SERVER_STOP_GRACE_SECONDS`` 是契约的一部分，所以预算要跟
    「执行器被承诺的那个窗口」比，而不是跟一个荒唐的原始值比。
    """
    if raw is None:
        grace = coding_tool.DEFAULT_SERVER_STOP_GRACE_SECONDS
    else:
        try:
            grace = float(raw)
        except (TypeError, ValueError):
            grace = coding_tool.DEFAULT_SERVER_STOP_GRACE_SECONDS
        if not math.isfinite(grace) or grace <= 0:
            grace = coding_tool.DEFAULT_SERVER_STOP_GRACE_SECONDS
    return max(grace, coding_tool.MIN_SERVER_STOP_GRACE_SECONDS)


def test_budget_is_half_the_grace():
    """预算就是 grace 的一半 —— 不是 grace 本身。"""
    assert coding_tool.executor_stop_budget(10.0) == 5.0


def test_below_floor_uses_the_floor():
    """grace 低于下限时先抬到下限：1s → 4s → 预算 2s。"""
    assert coding_tool.executor_stop_budget(1.0) == 2.0


def test_ceiling_clamps():
    """预算有上限：胖手指的 grace 不能把停机路径停上几分钟。"""
    assert coding_tool.executor_stop_budget(1000.0) == 60.0


def test_invalid_values_use_default():
    """不可用的 grace 回落默认值（10s → 预算 5s），而不是「不等待」。"""
    for bad in ("abc", "0", "nan"):
        assert coding_tool.executor_stop_budget(bad) == 5.0, bad


def test_env_three_gives_two(monkeypatch):
    """env="3"：3s 的 grace 低于下限，抬到 4s 后预算 2s。

    改前 ``cli._stop_grace_seconds()`` 直接返回 3.0（等于 grace 本身），
    这条断言就是为了钉住新契约而存在的。
    """
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "3")
    assert cli._stop_grace_seconds() == 2.0


def test_cli_delegates_to_the_shared_derivation(monkeypatch):
    """CLI 那个读环境变量的入口就是共享推导本身，不是第二份拷贝。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "10")
    assert cli._stop_grace_seconds() == coding_tool.executor_stop_budget(10.0)


def test_handler_wait_exceeds_the_whole_sequence():
    """处理器等序列的时间要**超过**序列自己用掉的总时间。

    否则处理器会在序列还在跑的时候就把 executor 掐掉 —— 序列没跑完，
    子进程就留成了孤儿。

    而且这两段（序列本身 + 处理器等它）都还得落在服务端的窗口之内：
    预算已经是 grace 的一半，这里再留出余量。
    """
    whole_sequence = (
        coding_tool._STOP_STAGE_CLOSE_FRACTION
        + coding_tool._STOP_STAGE_TERM_FRACTION
        + coding_tool._STOP_STAGE_KILL_FRACTION
    )
    assert coding_tool._STOP_STAGE_CLOSE_FRACTION == 0.35
    assert coding_tool._STOP_STAGE_TERM_FRACTION == 0.25
    assert coding_tool._STOP_STAGE_KILL_FRACTION == 0.10
    assert cli._STOP_SEQUENCE_WAIT_FRACTION > whole_sequence

    grace = 10.0
    budget = coding_tool.executor_stop_budget(grace)
    assert budget * whole_sequence < grace
    assert budget * cli._STOP_SEQUENCE_WAIT_FRACTION < grace


def test_min_server_stop_grace_is_derived_not_written_down():
    """下限是从预算下限推导出来的，不是手写的一个数字。"""
    assert coding_tool.MIN_SERVER_STOP_GRACE_SECONDS == 4.0
    assert (
        coding_tool.MIN_SERVER_STOP_GRACE_SECONDS
        == coding_tool._STOP_BUDGET_MIN_SECONDS / coding_tool.EXECUTOR_STOP_BUDGET_FRACTION
    )


def test_budget_is_always_below_the_effective_grace():
    """R3 的不等式本身：任何 env 取值，预算都严格小于服务端的窗口。"""
    for raw in _RAW_GRACES:
        budget = coding_tool.executor_stop_budget(raw)
        grace = _effective_grace(raw)
        assert budget < grace, raw


def test_budget_stays_inside_the_budget_bounds():
    """预算落在 [2, 60] 里 —— 下限来自 grace 的下限，上限来自预算上限。"""
    for raw in _RAW_GRACES:
        budget = coding_tool.executor_stop_budget(raw)
        assert coding_tool._STOP_BUDGET_MIN_SECONDS <= budget, raw
        assert budget <= coding_tool._STOP_BUDGET_MAX_SECONDS, raw


def test_cli_matches_the_shared_derivation_for_every_shape(monkeypatch):
    """每一档 env 取值，CLI 的入口都等于共享推导的结果。"""
    for raw in _RAW_GRACES:
        if raw is None:
            monkeypatch.delenv("PDT_STOP_GRACE_SECONDS", raising=False)
        else:
            monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", raw)
        assert cli._stop_grace_seconds() == coding_tool.executor_stop_budget(raw), raw


def test_handler_wait_fits_inside_the_server_window(monkeypatch):
    """处理器等待序列的那段时间，整体仍落在服务端的窗口之内。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "10")

    grace = 10.0
    budget = coding_tool.executor_stop_budget(grace)
    handler_wait = budget * cli._STOP_SEQUENCE_WAIT_FRACTION

    assert {"grace": grace, "budget": budget, "handler_wait": handler_wait} == {
        "grace": 10.0,
        "budget": 5.0,
        "handler_wait": 4.5,
    }
    assert handler_wait < grace
