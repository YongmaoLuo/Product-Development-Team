"""收尾判定点的门控回归测试：验证通过出口的那**一个**判定。

What this file witnesses
------------------------
验证通过的出口只有一个判定点：``has_postprocess_spec(plan_dir)``。
有要求书 → 进收尾阶段（``post_processing``），收尾阶段拥有终态出口；
没有要求书 → 直接 ``completed``，终态记一次。

这个文件钉住两件事：

1. **有要求书**：相位写成 ``post_processing``，收尾编排器被叫起来恰好
   一次，而且**一次终态都不记** —— 计划还没收完，发「计划关闭」是错的。
2. **没有要求书**：相位写成 ``completed``，终态以 ``passed`` 记一次，
   收尾编排器一次都没被叫起来。

外加一条**静态合同**：修复完成后的通过出口（``_on_repair_complete`` 的
``passed`` 分支）必须走同一个判定点，不许自己写终态。

为什么用真的 ``PlanState``
--------------------------
相位写入的合法性是状态机的事实，不是测试替身的事实。所以这里用
:mod:`plan_state` 的真表：替身会藏起生产状态机会拒绝的相位写入。
计划目录与状态库都指向本次测试的临时目录（会话级 hermetic 环境）。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402


#: The plan id every case uses — a plain, valid leaf.
PLAN_ID = "p-postprocess-gate-regression"

#: The file the requirement book lives in.
SPEC_FILENAME = "postprocess.json"

#: A minimal but legal requirement book: one entry, nothing external, no
#: waiting, and both criteria spelled in the closed vocabulary.
VALID_BOOK: Dict[str, Any] = {
    "items": [
        {
            "id": "publish-notes",
            "action": "发布说明",
            "acceptance_predicate": {"kind": "exit_code"},
            "evidence_requirement": {"kind": "exit_code"},
            "external": False,
            "required": True,
            "wait": {"type": "none"},
            "command": ["echo", "ok"],
        },
    ],
}


class _FakeRouting:
    """The routing repository, reduced to the CAS the passed round does."""

    def __init__(self) -> None:
        self.cases: List[Tuple[Any, ...]] = []

    def try_mark_phase(
        self, plan_id: str, expected: Any, phase: str
    ) -> None:
        """Record the phase CAS the round attempted."""
        self.cases.append((plan_id, expected, phase))


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Arm the module: a plan root, a fake routing CAS, two recorders.

    The plan root is redirected at the server module's ``PLANS_DIR`` so
    the round resolves *this* test's plan directory — and nothing of the
    round touches a real state database: the routing CAS and the
    terminal write are both faked.

    Attributes:
        plan_dir: The plan's directory.
        routing: The fake routing repository.
        terminals: Every terminal record the round wrote.
        started: Every plan id the finishing stage was asked to run.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(srv, "PLANS_DIR", plans_root)

    routing = _FakeRouting()
    monkeypatch.setattr(
        srv, "_open_verification_state", lambda: (None, routing, None)
    )

    terminals: List[Tuple[Any, ...]] = []

    def _record_terminal(
        plan_id: str, status: str, stop_reason: Optional[str], **kwargs: Any
    ) -> None:
        terminals.append((plan_id, status, stop_reason, kwargs))

    monkeypatch.setattr(srv, "_record_verification_terminal", _record_terminal)

    started: List[str] = []
    monkeypatch.setattr(
        vl, "_start_postprocess", lambda plan_id: started.append(plan_id)
    )

    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    return SimpleNamespace(
        plan_dir=plan_dir,
        routing=routing,
        terminals=terminals,
        started=started,
    )


def _write_book(plan_dir: Path) -> Path:
    """Write the legal requirement book into the plan directory."""
    path = plan_dir / SPEC_FILENAME
    path.write_text(
        json.dumps(VALID_BOOK, ensure_ascii=False), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------
# The gated path — a plan that declares a requirement book
# ---------------------------------------------------------------------


def test_spec_path_enters_the_finishing_stage(armed: SimpleNamespace):
    """有要求书 → 相位是 ``post_processing``，收尾编排器被叫起来一次。"""
    _write_book(armed.plan_dir)
    ps = srv.PlanState(armed.plan_dir)

    vl._handle_passed_round(PLAN_ID, 2, ps)

    assert ps.get_current_phase() == "post_processing"
    assert armed.started == [PLAN_ID]


def test_spec_path_records_no_terminal_while_the_stage_runs(
    armed: SimpleNamespace,
):
    """有要求书 → 一次终态都不记：关闭计划是收尾阶段的活。"""
    _write_book(armed.plan_dir)
    ps = srv.PlanState(armed.plan_dir)

    vl._handle_passed_round(PLAN_ID, 2, ps)

    assert armed.terminals == []


# ---------------------------------------------------------------------
# The zero-intrusion baseline — a plan that declares nothing
# ---------------------------------------------------------------------


def test_no_spec_path_completes_and_records_passed(armed: SimpleNamespace):
    """没有要求书 → 相位是 ``completed``，终态以 ``passed`` 记一次。"""
    ps = srv.PlanState(armed.plan_dir)

    vl._handle_passed_round(PLAN_ID, 2, ps)

    assert ps.get_current_phase() == "completed"
    assert len(armed.terminals) == 1
    plan_id, status, stop_reason, _kwargs = armed.terminals[0]
    assert plan_id == PLAN_ID
    assert status == "passed"
    assert stop_reason is None


def test_no_spec_path_never_starts_the_finishing_stage(
    armed: SimpleNamespace,
):
    """没有要求书 → 收尾编排器一次都没被叫起来。"""
    ps = srv.PlanState(armed.plan_dir)

    vl._handle_passed_round(PLAN_ID, 2, ps)

    assert armed.started == []


# ---------------------------------------------------------------------
# The static contract — the repair exit walks the same gate
# ---------------------------------------------------------------------


def _function_named(tree: ast.Module, name: str) -> ast.FunctionDef:
    """The function ``name`` in ``tree`` — the one under test."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def test_repair_passed_exit_calls_the_one_gate():
    """修复完成后的通过出口必须走同一个判定点，不许自己写终态。

    这条静态合同是这次修复的**行为**见证：``_on_repair_complete`` 的
    ``passed`` 分支以前直接 ``_record_terminal("passed", ...)``，那是一条
    绕过判定点的出口 —— 有要求书的计划被直接写成 ``completed``，收尾阶段
    从不启动。
    """
    source = (_BACKEND_DIR / "verification_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    factory = _function_named(tree, "_make_repair_on_complete")
    callback = _function_named(factory, "_on_repair_complete")

    gate_calls = [
        node
        for node in ast.walk(callback)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_handle_passed_round"
    ]
    assert len(gate_calls) == 1, (
        "the repair callback's passed exit must call "
        "_handle_passed_round exactly once"
    )
