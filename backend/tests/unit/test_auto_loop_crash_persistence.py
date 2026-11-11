"""auto-loop 崩溃必须落成 ``plan_verification`` 的 failed 终态。

背景
----
``start_verification`` 的 auto-fix 分支（``auto_fix=True``，默认值）
在 verification 线程里跑 ``_server._run_auto_verification_loop``。
这条调用一旦抛异常，``except Exception`` 原来只做两件事：

  * ``logger.exception(...)``；
  * 改**内存** —— ``_verification_state[plan_id]`` 的
    ``verification_status`` / ``stop_reason`` / ``updated_at``。

内存是**进程内**的事实。线程一结束、进程一重启，它就没了 ——
而 ``plan_verification.verification_status`` 还停在 ``running``。
运维看到的就是一个「静默停止」的 plan：没有终态、没有失败原因、
没有卡片，只有一行永远转圈的进度。

契约
----
  * auto-loop 崩溃 → ``_persist_verification_terminal(plan_id, "failed",
    reason)`` 被调用**一次**，``reason`` 是 ``f"auto_loop_crash: {exc}"``；
  * 同一个 reason **逐字符**出现在内存 ``stop_reason`` 里 —— 落盘的
    和内存说的必须是同一件事（两套拼写会让事后复盘对不上）；
  * 落盘自己抛异常 → 被吞掉，函数不外抛（这段跑在 verification
    线程里，抛出去只会换一个地方炸，而内存里的 failed 已经是运维
    看得见的事实）；
  * auto-loop 正常返回 → 一次都不落盘（终态由 auto-loop 自己写，
    重复写会把已经写好的 reason 覆盖掉）；
  * 内存里没有这个 plan 的 state → **仍然**落盘（best-effort：
    落盘是线程结束后唯一还站得住的事实，内存缺不缺它都得写）。

另外钉住调用点：``start_verification`` 里**不得**再直接调用
``_server._run_auto_verification_loop`` —— 那个直接调用就是绕过
落盘的入口，留着它等于给「静默停止」留后门。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

# ``backend/`` 在 ``sys.path`` 上，``routes.verification`` 才 import 得到
# （与同目录其它用例同一套路）。
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` 必须先 import：``server`` 自己会 import ``routes.verification``，
# 反过来先 import 会撞成半个模块的循环 import。
import server as server_mod  # noqa: E402
import routes.verification as rv  # noqa: E402


_VERIFICATION_PY = _BACKEND_DIR / "routes" / "verification.py"


@pytest.fixture(autouse=True)
def clean_verification_state():
    """``_verification_state`` 是模块级全局：用例前后都清空。

    否则一个用例写进去的 ``failed`` 会漏进下一个用例 —— 那种失败
    看起来像产品 bug，其实是自己没擦桌子。
    """
    server_mod._verification_state.clear()
    yield
    server_mod._verification_state.clear()


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plan"
    d.mkdir()
    return d


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    d = tmp_path / "project"
    d.mkdir()
    return d


def _install_crash(monkeypatch, exc: Exception) -> Mock:
    """把 auto-loop 换成一颗会炸的雷，把终态落盘换成记录器。

    返回的 Mock 就是被断言的 ``persist``：调用参数必须能一字不差
    地读出来（``assert_called_once_with("p", "failed", reason)``）。
    """
    monkeypatch.setattr(
        server_mod,
        "_run_auto_verification_loop",
        Mock(side_effect=exc),
    )
    persist = Mock()
    monkeypatch.setattr(server_mod, "_persist_verification_terminal", persist)
    return persist


# ---------------------------------------------------------------------------
# 崩溃 → 落盘 failed
# ---------------------------------------------------------------------------

def test_crash_persists_failed_terminal(monkeypatch, plan_dir, project_dir):
    """auto-loop 抛异常 → ``failed`` 被落进 plan_verification（一次）。"""
    persist = _install_crash(monkeypatch, RuntimeError("boom"))
    server_mod._verification_state["p"] = {"verification_status": "running"}

    rv._run_auto_loop_with_crash_persistence("p", plan_dir, project_dir, 4, None, 1)

    persist.assert_called_once_with("p", "failed", "auto_loop_crash: boom")


def test_persisted_reason_matches_memory(monkeypatch, plan_dir, project_dir):
    """落盘的 reason 与内存的 ``stop_reason`` 逐字符相等。"""
    persist = _install_crash(monkeypatch, RuntimeError("boom"))
    server_mod._verification_state["p"] = {"verification_status": "running"}

    rv._run_auto_loop_with_crash_persistence("p", plan_dir, project_dir, 4, None, 1)

    persisted_reason = persist.call_args[0][2]
    memory_reason = server_mod._verification_state["p"]["stop_reason"]
    assert memory_reason == persisted_reason
    assert persisted_reason == "auto_loop_crash: boom"
    assert server_mod._verification_state["p"]["verification_status"] == "failed"


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------

def test_persist_failure_does_not_reraise(monkeypatch, plan_dir, project_dir):
    """落盘自己抛异常 → 被吞掉，函数正常返回（内存里的 failed 留下）。"""
    persist = _install_crash(monkeypatch, RuntimeError("boom"))
    persist.side_effect = RuntimeError("sqlite is gone")
    server_mod._verification_state["p"] = {"verification_status": "running"}

    # 不抛 —— 这一段跑在 verification 线程里，抛出去只是换个地方炸。
    rv._run_auto_loop_with_crash_persistence("p", plan_dir, project_dir, 4, None, 1)

    # 内存那一半仍然写进去了（先改内存、再落盘）。
    assert server_mod._verification_state["p"]["verification_status"] == "failed"
    assert server_mod._verification_state["p"]["stop_reason"] == "auto_loop_crash: boom"


def test_clean_run_does_not_persist(monkeypatch, plan_dir, project_dir):
    """auto-loop 正常返回 → 一次都不落盘（终态归 auto-loop 自己写）。"""
    monkeypatch.setattr(
        server_mod, "_run_auto_verification_loop", Mock(return_value=None)
    )
    persist = Mock()
    monkeypatch.setattr(server_mod, "_persist_verification_terminal", persist)
    server_mod._verification_state["p"] = {"verification_status": "running"}

    rv._run_auto_loop_with_crash_persistence("p", plan_dir, project_dir, 4, None, 1)

    persist.assert_not_called()


def test_missing_memory_state_still_persists(monkeypatch, plan_dir, project_dir):
    """内存里没有这个 plan → 仍然落盘（best-effort）。"""
    persist = _install_crash(monkeypatch, RuntimeError("boom"))
    server_mod._verification_state.pop("p", None)

    rv._run_auto_loop_with_crash_persistence("p", plan_dir, project_dir, 4, None, 1)

    persist.assert_called_once_with("p", "failed", "auto_loop_crash: boom")


def test_wrapper_forwards_arguments_to_auto_loop(monkeypatch, plan_dir, project_dir):
    """包装层是**透明**的：参数原样交给 auto-loop，不改语义。"""
    loop = Mock(return_value=None)
    monkeypatch.setattr(server_mod, "_run_auto_verification_loop", loop)
    persist = Mock()
    monkeypatch.setattr(server_mod, "_persist_verification_terminal", persist)

    rv._run_auto_loop_with_crash_persistence("p", plan_dir, project_dir, 4, None, 1)

    assert loop.call_count == 1
    args, kwargs = loop.call_args
    assert args[0] == "p"
    assert args[1] == plan_dir
    assert args[2] == project_dir
    assert kwargs["max_rounds"] == 4
    assert kwargs["tool"] is None
    assert kwargs["start_round"] == 1


# ---------------------------------------------------------------------------
# 调用点：start_verification 必须走包装层
# ---------------------------------------------------------------------------

def _called_names(func_def: ast.AST) -> set:
    names = set()
    for node in ast.walk(func_def):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_start_verification_delegates_to_crash_persistence_wrapper():
    """``start_verification`` 走包装层，且不再直接调 auto-loop。"""
    tree = ast.parse(_VERIFICATION_PY.read_text(encoding="utf-8"))
    start = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "start_verification"
    )
    called = _called_names(start)

    assert "_run_auto_loop_with_crash_persistence" in called
    # 直接调用 auto-loop 就是绕过落盘的入口 —— 留着它等于给
    # 「静默停止」留后门。
    assert "_run_auto_verification_loop" not in called
