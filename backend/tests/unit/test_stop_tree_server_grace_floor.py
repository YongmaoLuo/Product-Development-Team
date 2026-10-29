"""服务端执行停机路径的 grace 必须有下界（arch 决策点 3 / test 决策点 4）。

背景
----
任务 4 把执行器的停止预算改成「服务端 grace 的一半」，并且给 grace 加了下限
``coding_tool.MIN_SERVER_STOP_GRACE_SECONDS``（4s）：grace 低于 4s 时先抬到 4s，
预算再取一半（2s）。这条下限是为了让「预算 < 窗口」这个不等式在任何 env 取值下
都成立。

但服务端自己那条解析路径 ``server._resolve_stop_grace_seconds`` 仍然把
``PDT_STOP_GRACE_SECONDS`` 原样返回：env=1 → 服务端窗口 1.0s，而执行器预算被抬到
2.0s —— **预算 > 窗口**。级联稍微慢一点，整组进程就被服务端 SIGKILL，而硬杀正是
这套协作式停机存在的理由所要避免的结果。

所以执行停机路径改走一个新的入口 ``server._execution_stop_grace_seconds()``：它把
解析出来的值抬到下限。而 ``_resolve_stop_grace_seconds(0)`` 的既有契约（显式 0 =
不等待）原样保留 —— 下界只加在**停机路径**上，不改显式调用者。

测试不碰真实进程：算的都是纯函数；停机路径用 monkeypatch 记录 killer 收到的
grace，因此断言落在**实际传下去的那个值**上，而不是函数内部的计数器上。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool
import server


#: 环境变量里可能出现的每一档：缺失、空串、非正、非数字、低于下限、等于下限、
#: 正常值、远大于下限。每一档都必须推出同一个「执行器预算 < 服务端窗口」。
_RAW_GRACES = (
    None,
    "",
    "0",
    "-5",
    "abc",
    "1",
    "3",
    "4",
    "10",
    "60",
    "600",
)


class _FakeProc:
    """``subprocess.Popen`` 的最小替身：停机路径只看它是不是还活着。"""

    pid = 424242

    def poll(self):
        return None


def _set_env(monkeypatch, raw) -> None:
    """把 ``PDT_STOP_GRACE_SECONDS`` 设成这一档（``None`` = 不设）。"""
    if raw is None:
        monkeypatch.delenv("PDT_STOP_GRACE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", raw)


def test_floor_applied_below_four(monkeypatch):
    """env=1：低于下限 → 抬到 4s（执行器那 2s 的预算才有地方放）。"""
    _set_env(monkeypatch, "1")

    assert server._execution_stop_grace_seconds() == 4.0


def test_env_above_floor_kept(monkeypatch):
    """env=20：高于下限 → 原样保留（运维配的窗口说了算）。"""
    _set_env(monkeypatch, "20")

    assert server._execution_stop_grace_seconds() == 20.0


def test_invalid_env_uses_default(monkeypatch):
    """env=0/-5/abc → 默认 10s：「不等」不是「不等待」。"""
    for raw in ("0", "-5", "abc"):
        _set_env(monkeypatch, raw)

        assert server._execution_stop_grace_seconds() == 10.0, raw


def test_invariant_holds_for_every_value(monkeypatch):
    """R3 的不等式本身：任何 env 取值，执行器预算都严格小于服务端窗口。

    这条断言在改之前是**假**的：env=1 时执行器预算 2.0 > 服务端窗口 1.0。
    """
    for raw in _RAW_GRACES:
        _set_env(monkeypatch, raw)
        budget = coding_tool.executor_stop_budget(raw)
        grace = server._execution_stop_grace_seconds()

        assert budget < grace, raw


def test_stop_path_uses_the_floored_grace(monkeypatch):
    """停机路径把**带下界**的 grace 交给 killer：env=1 时收到 4.0，不是 1.0。"""
    _set_env(monkeypatch, "1")
    recorded = []

    def _record(proc, grace_seconds=None):
        recorded.append(grace_seconds)
        return False

    monkeypatch.setattr(server, "_kill_process_tree", _record)
    monkeypatch.setitem(
        server._execution_state, "plan-grace-floor", {"process": _FakeProc()}
    )

    server._shutdown_all_executions()

    assert recorded, "the shutdown path never reached the killer"
    assert set(recorded) == {4.0}


def test_explicit_zero_grace_still_honoured():
    """显式 0 仍然是 0：下界只加在停机路径上，不改显式调用者的契约。

    这条契约原是 ``tests/unit/test_kill_process_tree_grace.py`` 的
    ``test_zero_grace_is_honoured``；它改前改后都为真。
    """
    assert server._resolve_stop_grace_seconds(0) == 0.0
