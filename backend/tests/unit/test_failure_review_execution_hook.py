"""执行终态失败 → 旁路宣告一次失败复盘。

What this pins
--------------
1. **四处终态失败分支都会宣告。** ``backend/routes/execution.py`` 有四处
   把执行状态写成终态 ``failed``；每一处之后都要宣告一次失败复盘
   （arch 决策点 R2：执行终态 failed 也触发）。
2. **宣告在失败分支内、状态改写之后。** 宣告不能被挪到函数入口 ——
   非失败分支（``completed`` / ``stopped``）不得产生任何记录
   （test 决策点 R8：负控）。
3. **顶层 import 不引入循环依赖。** ``failure_review`` 不 import
   ``server`` / ``routes.*``，所以路由可以在模块层 import 它。

Test command::

    pytest backend/tests/unit/test_failure_review_execution_hook.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List

#: The route module under test.  Resolved from this file so the test does
#: not depend on the working directory the suite happens to run in.
BACKEND_DIR = Path(__file__).resolve().parents[2]
EXECUTION_ROUTE = BACKEND_DIR / "routes" / "execution.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_source() -> str:
    """The source of the route module, as it is on disk right now."""
    return EXECUTION_ROUTE.read_text(encoding="utf-8")


def _route_tree() -> ast.Module:
    """The parsed route module."""
    return ast.parse(_route_source())


def _announce_calls(tree: ast.Module) -> List[ast.Call]:
    """Every call to ``announce_failure`` in the route module.

    Both spellings count: ``failure_review.announce_failure(...)`` (an
    attribute of a module) and a bare ``announce_failure(...)`` (a name
    imported into the module's namespace).
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "announce_failure"
            or isinstance(node.func, ast.Name)
            and node.func.id == "announce_failure"
        )
    ]


def _failed_status_lines(tree: ast.Module) -> List[int]:
    """The lines that write ``state["status"] = "failed"``."""
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "state"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "status"
                and isinstance(node.value, ast.Constant)
                and node.value.value == "failed"
            ):
                lines.append(node.lineno)
    return lines


# ---------------------------------------------------------------------------
# 1. Every terminal-failure branch announces
# ---------------------------------------------------------------------------


def test_execution_route_calls_announce_failure_four_times() -> None:
    """执行路由至少在四处宣告失败复盘（四处终态 failed 分支各一处）。"""
    tree = _route_tree()
    calls = _announce_calls(tree)
    assert len(calls) >= 4, (
        "backend/routes/execution.py 必须有至少四处 announce_failure 调用，"
        f"实际 {len(calls)} 处："
        f"{sorted(call.lineno for call in calls)}"
    )


# ---------------------------------------------------------------------------
# 2. The announcement sits in the failed branch, after the status write
# ---------------------------------------------------------------------------


def test_each_call_is_a_failed_branch() -> None:
    """每一处宣告都在同一失败分支内、状态改写之后（20 行以内）。"""
    tree = _route_tree()
    calls = _announce_calls(tree)
    failed_lines = _failed_status_lines(tree)
    assert calls, "路由里没有 announce_failure 调用"
    assert failed_lines, "路由里没有 state['status'] = 'failed' 的状态改写"

    for call in calls:
        line = call.lineno
        nearby = [failed for failed in failed_lines if 0 < line - failed <= 20]
        assert nearby, (
            f"第 {line} 行的 announce_failure 不在任何失败分支内 —— "
            f"它之前 20 行内没有 state['status'] = 'failed'；"
            f"路由里的状态改写在第 {sorted(failed_lines)} 行"
        )


def test_no_announce_call_outside_a_failed_branch() -> None:
    """负控：非失败分支不得宣告（completed / stopped 不得产生记录）。"""
    tree = _route_tree()
    calls = _announce_calls(tree)
    failed_lines = _failed_status_lines(tree)

    orphans = [
        call.lineno
        for call in calls
        if not [failed for failed in failed_lines if 0 < call.lineno - failed <= 20]
    ]
    assert not orphans, (
        f"第 {orphans} 行的 announce_failure 不在任何失败分支内 —— "
        "非失败分支不得产生失败复盘记录"
    )


# ---------------------------------------------------------------------------
# 3. The module-level import
# ---------------------------------------------------------------------------


def test_routes_execution_imports_failure_review() -> None:
    """路由在模块层 import failure_review（不引入循环依赖）。"""
    tree = _route_tree()
    module_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import)
        and any(alias.name == "failure_review" for alias in node.names)
    ]
    assert module_imports, (
        "backend/routes/execution.py 顶层缺少 `import failure_review`"
    )


def test_route_module_resolves_the_real_failure_review_module() -> None:
    """顶层 import 解析到真正的 failure_review 模块，且不产生循环依赖。"""
    import server  # noqa: F401  — server 先 wiring，routes.execution 才能 import

    import failure_review
    from routes import execution as routes_execution

    assert routes_execution.failure_review is failure_review


# ---------------------------------------------------------------------------
# 4. The hook, driven for real
# ---------------------------------------------------------------------------


class _AnnounceRecorder:
    """Stands in for the ``failure_review`` module in the route."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def announce_failure(self, plan_id: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append((plan_id, args, kwargs))
        return "decision-under-test"


def test_failed_verdict_announces_the_failure_review(monkeypatch) -> None:
    """终态失败分支：状态写成 failed 之后宣告一次，带上 plan_id。"""
    import server  # noqa: F401  — 先 wiring，routes.execution 才能 import
    import verification_loop
    from routes import execution as routes_execution

    recorder = _AnnounceRecorder()
    monkeypatch.setattr(routes_execution, "failure_review", recorder)
    monkeypatch.setattr(
        verification_loop, "_reap_orphaned_in_progress_tasks", lambda plan_id: None
    )

    state: Dict[str, Any] = {"status": "running", "pid": 4242}
    routes_execution._mark_failed_with_reason("plan-xyz", "boom", state)

    assert state["status"] == "failed"
    assert state["stop_reason"] == "boom"
    assert recorder.calls, "终态失败分支没有宣告失败复盘"
    assert recorder.calls[0][0] == "plan-xyz"


def test_a_run_that_is_not_running_announces_nothing(monkeypatch) -> None:
    """负控：状态不是 running 的执行不宣告（不产生任何记录）。"""
    import server  # noqa: F401  — 先 wiring，routes.execution 才能 import
    import verification_loop
    from routes import execution as routes_execution

    recorder = _AnnounceRecorder()
    monkeypatch.setattr(routes_execution, "failure_review", recorder)
    monkeypatch.setattr(
        verification_loop, "_reap_orphaned_in_progress_tasks", lambda plan_id: None
    )

    for status in ("completed", "stopped", "failed"):
        state: Dict[str, Any] = {"status": status, "pid": 4242}
        routes_execution._mark_failed_with_reason("plan-abc", "boom", state)

    assert not recorder.calls, (
        f"非 running 的执行宣告了失败复盘：{recorder.calls}"
    )


def test_the_announcement_leaves_a_sidecar_and_a_pending_decision(
    isolated_plans_dir, monkeypatch
) -> None:
    """真的走一遍：失败复盘落 ``failure_review.json``，决策是 pending。"""
    import server  # noqa: F401  — 先 wiring，routes.execution 才能 import
    import verification_loop
    from routes import execution as routes_execution

    from config_paths import resolve_state_db_path
    from hitl.models import DecisionStatus
    from hitl.store import HitlStore

    monkeypatch.setattr(
        verification_loop, "_reap_orphaned_in_progress_tasks", lambda plan_id: None
    )

    plan_id = "plan-e2e"
    state: Dict[str, Any] = {"status": "running", "pid": 4242}
    routes_execution._mark_failed_with_reason(plan_id, "boom", state)

    sidecar = Path(isolated_plans_dir) / plan_id / "failure_review.json"
    assert sidecar.exists(), f"失败复盘没有落盘：{sidecar}"

    with HitlStore(resolve_state_db_path()) as store:
        decisions = [
            decision
            for decision in store.list_by_plan(plan_id)
            if decision.source == "failure_review"
        ]
    assert len(decisions) == 1, decisions
    assert decisions[0].status == DecisionStatus.PENDING.value, decisions[0]
