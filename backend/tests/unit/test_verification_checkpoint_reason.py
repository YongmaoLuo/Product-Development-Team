"""暂停检查点的 ``reason`` 必须来自调用方 —— 不能写死 ``user_stopped``。

背景
----
``_write_verification_checkpoint()``（``backend/routes/verification.py``）
把 ``reason="user_stopped"`` 写死在函数体里，于是**任何**调用方都被记成
"用户按了停止"。调用方真正的原因（base 漂移、冲突、其它）被改写成一句
谎话，而 ``pause_state.json`` 的 ``reason`` 正是后来 resume / 运维读的那个
字段 —— 一句谎话会一路传下去。

契约
----
  * 调用方给了 reason → **原样落盘**（``base_drift_conflicts`` 就写
    ``base_drift_conflicts``，绝不被改写成 ``user_stopped``）；
  * 调用方没给 reason → 落盘为 ``null``，并记 ``pause_reason_unavailable``
    日志说明缺失。**不猜**：猜出来的 reason 和真 reason 在盘上无法区分；
  * ``interrupted_tasks`` 每条的 reason 与传入值一致 —— 任务级 reason 与
    检查点级 reason 说的是同一个事实，两者不能互相矛盾。

另外钉住调用点：``/stop`` **显式**传 ``reason="user_stopped"`` —— 这一次
是调用方真的在说"用户停了"，而不是函数替它编的。

上游对应：arch 决策点 3（暂停检查点使用调用方真实原因）、test 决策点 4
（不再写死 user_stopped）。
"""

from __future__ import annotations

import ast
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ``backend/`` 在 ``sys.path`` 上，``routes.verification`` 才 import 得到
# （与同目录其它用例同一套路）。
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` 必须先 import：``server`` 自己会 import ``routes.verification``，
# 反过来先 import 会撞成半个模块的循环 import。
import server  # noqa: E402,F401
from routes.verification import _write_verification_checkpoint  # noqa: E402


_VERIFICATION_PY = _BACKEND_DIR / "routes" / "verification.py"

#: 被停止的 VP —— 每一行都会带着"为什么被打断"落进检查点。
_INTERRUPTED = [
    {
        "task_id": "VP-003",
        "status": "interrupted",
        "phase": "verification",
        "session_id": None,
        "strategy": "verification_round_resume",
        "reason": "user_stopped",
    },
]


def _read_checkpoint(plan_dir: Path) -> Dict[str, Any]:
    """检查点文件读回来 —— 断言的是**盘上的东西**，不是返回值。"""
    return json.loads((plan_dir / "pause_state.json").read_text(encoding="utf-8"))


def _write(plan_dir: Path, **kwargs: Any) -> Dict[str, Any]:
    """用"调用方"的身份写一次检查点。"""
    return _write_verification_checkpoint(
        plan_dir,
        project_dir=None,
        round_n=2,
        verdicts=[],
        interrupted_tasks=[],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1) 调用方给了原因 → 原样落盘
# ---------------------------------------------------------------------------


def test_checkpoint_uses_caller_reason(tmp_path: Path):
    """调用方说什么，检查点就写什么 —— 绝不改写成 user_stopped。"""
    plan_dir = tmp_path / "plan"

    _write(plan_dir, reason="base_drift_conflicts")

    assert _read_checkpoint(plan_dir)["reason"] == "base_drift_conflicts"


# ---------------------------------------------------------------------------
# 2) 调用方没给原因 → null（不猜）
# ---------------------------------------------------------------------------


def test_checkpoint_reason_none_is_preserved(tmp_path: Path):
    """省略 reason → 落盘为 null，而不是替调用方编一个 user_stopped。"""
    plan_dir = tmp_path / "plan"

    _write(plan_dir)

    assert _read_checkpoint(plan_dir)["reason"] is None


# ---------------------------------------------------------------------------
# 3) 原因缺失 → 记 pause_reason_unavailable
# ---------------------------------------------------------------------------


def test_checkpoint_logs_when_reason_missing(tmp_path: Path, caplog):
    """缺失必须**说得出来**：盘上是 null，日志里也要有一条。"""
    plan_dir = tmp_path / "plan"

    with caplog.at_level(logging.WARNING):
        _write(plan_dir)

    assert "pause_reason_unavailable" in caplog.text


def test_checkpoint_does_not_log_when_reason_present(
    tmp_path: Path, caplog
):
    """有原因时不多事 —— 否则这条日志就没人信了。"""
    plan_dir = tmp_path / "plan"

    with caplog.at_level(logging.WARNING):
        _write(plan_dir, reason="base_drift_conflicts")

    assert "pause_reason_unavailable" not in caplog.text


# ---------------------------------------------------------------------------
# 4) interrupted_tasks 的 reason 与传入值一致
# ---------------------------------------------------------------------------


def test_interrupted_tasks_carry_the_caller_reason(tmp_path: Path):
    """任务级 reason 与检查点级 reason 是同一个事实，不能互相矛盾。"""
    plan_dir = tmp_path / "plan"

    _write_verification_checkpoint(
        plan_dir,
        project_dir=None,
        round_n=2,
        verdicts=[],
        interrupted_tasks=[dict(entry) for entry in _INTERRUPTED],
        reason="base_drift_conflicts",
    )

    rows: List[Dict[str, Any]] = _read_checkpoint(plan_dir)["interrupted_tasks"]
    assert [row["task_id"] for row in rows] == ["VP-003"]
    assert [row["reason"] for row in rows] == ["base_drift_conflicts"]


def test_interrupted_tasks_carry_no_reason_when_missing(tmp_path: Path):
    """调用方没给原因 → 每条任务也是 null，不是 user_stopped。"""
    plan_dir = tmp_path / "plan"

    _write_verification_checkpoint(
        plan_dir,
        project_dir=None,
        round_n=2,
        verdicts=[],
        interrupted_tasks=[dict(entry) for entry in _INTERRUPTED],
    )

    rows: List[Dict[str, Any]] = _read_checkpoint(plan_dir)["interrupted_tasks"]
    assert [row["reason"] for row in rows] == [None]


# ---------------------------------------------------------------------------
# 5) /stop 的调用点显式传 user_stopped
# ---------------------------------------------------------------------------


def _stop_checkpoint_call() -> ast.Call:
    """``stop_verification`` 里对 ``_write_verification_checkpoint`` 的调用。"""
    tree = ast.parse(_VERIFICATION_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "stop_verification"):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_write_verification_checkpoint"
            ):
                return inner
    raise AssertionError(
        "stop_verification does not call _write_verification_checkpoint"
    )


def test_stop_call_site_passes_user_stopped():
    """``/stop`` 是**用户**按的停止 —— 它自己说出 user_stopped。"""
    call = _stop_checkpoint_call()

    reasons = [kw.value for kw in call.keywords if kw.arg == "reason"]
    assert reasons, "stop_verification must pass reason=... explicitly"
    assert isinstance(reasons[0], ast.Constant), (
        "the reason passed by /stop must be a literal, not a computed value"
    )
    assert reasons[0].value == "user_stopped"


def test_stop_interrupted_tasks_say_user_stopped():
    """``/stop`` 写下的被中断任务，理由也是 user_stopped。"""
    call = _stop_checkpoint_call()

    interrupted = next(
        kw.value
        for kw in call.keywords
        if kw.arg == "interrupted_tasks"
    )
    reasons = [
        element.value
        for row in ast.walk(interrupted)
        if isinstance(row, ast.Dict)
        for key, element in zip(row.keys, row.values)
        if isinstance(key, ast.Constant) and key.value == "reason"
    ]
    assert reasons, "the interrupted tasks of /stop must state a reason"
    assert set(reasons) == {"user_stopped"}


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))
