"""启动期的等待相位迁移（``ready`` → ``queued``）—— 后端启动钩子这一侧。

被钉住的行为
------------
1. **启动即迁移**：lifespan 的启动阶段把 ``plan_routing`` 里停在 ``ready``
   的行改写成 ``queued``。观察口是 ``/api/plan/{id}/state-from-db`` ——
   它直接读那一行，不看磁盘上的计划目录，所以它读到的是迁移后的值。
2. **幂等**：第二次启动是空操作 —— ``rows_migrated=0``、
   ``state_files_migrated=0``，数据不再被改动。
3. **迁移失败不挡启动**：迁移抛异常时被记进日志，后端照常起来
   （``/api/plans`` 仍然 200）。迁移是一次数据清扫，失败只说明这一轮没迁
   干净，下一轮启动会按当前值再补一遍 —— 它没有理由把后端带走。
4. **不在 import 期执行**：迁移挂在 lifespan 上。import ``server`` 本身不
   碰数据 —— 否则一次普通的 import 就会改写操作员的数据库。

每一轮启动打一行 ``[phase_migration] rows_migrated=N
state_files_migrated=M`` —— 那是这一轮的账，第二次启动必须是 ``0 0``。
"""

from __future__ import annotations

import ast
import logging
import sqlite3
from pathlib import Path
from typing import List, Sequence, Tuple

import pytest
from fastapi.testclient import TestClient

from state_machine.db.schema import migrate as migrate_schema


#: 迁移计数行的前缀。每一轮启动都必须打出以它开头的一行，否则运维看不到
#: 这一轮迁了多少 —— 迁移是静默的数据改写，没有这一行就无从判断它跑没跑。
_MIGRATION_LOG_PREFIX = "[phase_migration] rows_migrated="


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_state_db(db_path: Path, rows: Sequence[Tuple[str, str]] = ()) -> None:
    """建一个带**生产 schema** 的 state.db，并插入若干 ``plan_routing`` 行。

    用真实的 ``migrate()`` 建表，而不是手写 DDL —— 列名与约束跟生产一致，
    测试不会在「测试里的表」和「真正的表」之间漂移。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        migrate_schema(conn)
        for plan_id, phase in rows:
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES (?, ?, ?)",
                (plan_id, phase, "2026-10-25T00:00:00Z"),
            )
    finally:
        conn.close()


def _routing_phase(db_path: Path, plan_id: str) -> str:
    """读回 ``plan_routing`` 里某行的 ``current_phase``。"""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no plan_routing row for {plan_id}"
    return row[0]


def _migration_log_lines(caplog: pytest.LogCaptureFixture) -> List[str]:
    """从 caplog 里挑出迁移的计数行（已经渲染成最终文本）。"""
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(_MIGRATION_LOG_PREFIX)
    ]


def _callee_name(call: ast.Call) -> str:
    """被调用者的名字：``f()`` 与 ``mod.f()`` 都算 ``f``。"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _module_level_calls(tree: ast.AST, name: str) -> List[ast.Call]:
    """模块体里（不含函数 / 类体内部）对 *name* 的调用点。

    「import 期执行」就是「模块体执行时执行」—— 函数体里的调用要等到那个
    函数被调用才发生，不算 import 期。
    """
    calls: List[ast.Call] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                # 函数 / 类体里的调用不在这条路上执行。
                continue
            if isinstance(child, ast.Call) and _callee_name(child) == name:
                calls.append(child)
            visit(child)

    visit(tree)
    return calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    """本测试的 ``state.db``。

    conftest 的 autouse ``isolated_plans_dir`` 已经把
    ``server._state_db_path`` 指到 ``tmp_path / state.db``（同一个
    ``tmp_path``），所以这里返回的就是应用会读的那一个。
    """
    return tmp_path / "state.db"


@pytest.fixture
def cc_switch_home(fake_cc_switch_home) -> None:
    """把 CC Switch 的家目录指向一个一次性副本。

    与迁移本身无关：lifespan 会解析 provider 兜底链，那一步要
    ``$HOME/.cc-switch/cc-switch.db``，而干净的 checkout 里没有这个文件 ——
    缺了它后端根本起不来，断言就变成了在测环境而不是在测迁移。

    迁移必须发生在**进入** TestClient 之前被观测到（数据要先摆好），所以
    这里只提供环境，不代替测试自己去启动应用。
    """
    return None


# ---------------------------------------------------------------------------
# 1. 启动即迁移
# ---------------------------------------------------------------------------


def test_startup_migrates_ready_row(
    state_db_path: Path,
    cc_switch_home: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """预置 ready 行 → 启动后 ``state-from-db`` 读到 queued。"""
    _create_state_db(state_db_path, rows=[("p1", "ready")])

    from server import app

    with caplog.at_level(logging.WARNING, logger="server"):
        with TestClient(app) as client:
            resp = client.get("/api/plan/p1/state-from-db")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "current_phase": "queued",
        "plan_id": "p1",
        "completed_phases": [],
    }
    # 这一轮的账：一行被迁走。
    assert _migration_log_lines(caplog) == [
        "[phase_migration] rows_migrated=1 state_files_migrated=0",
    ]
    # 读接口之外，数据库本身也确实是新词汇。
    assert _routing_phase(state_db_path, "p1") == "queued"


# ---------------------------------------------------------------------------
# 2. 幂等
# ---------------------------------------------------------------------------


def test_second_start_is_noop(
    state_db_path: Path,
    cc_switch_home: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """第二次启动是空操作：两个计数都是 0，数据保持 queued。"""
    _create_state_db(state_db_path, rows=[("p1", "ready")])

    from server import app

    with caplog.at_level(logging.WARNING, logger="server"):
        with TestClient(app) as client:
            first = client.get("/api/plan/p1/state-from-db")
        assert first.status_code == 200, first.text
        assert first.json()["current_phase"] == "queued"

        # 只看第二轮的账。
        caplog.clear()

        with TestClient(app) as client:
            second = client.get("/api/plan/p1/state-from-db")

    assert second.status_code == 200, second.text
    assert second.json()["current_phase"] == "queued"
    assert _migration_log_lines(caplog) == [
        "[phase_migration] rows_migrated=0 state_files_migrated=0",
    ]
    assert _routing_phase(state_db_path, "p1") == "queued"


# ---------------------------------------------------------------------------
# 3. 迁移失败不挡启动
# ---------------------------------------------------------------------------


def test_migration_failure_still_boots(
    state_db_path: Path,
    cc_switch_home: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """迁移抛异常 → 记 exception 日志，后端照常起来。"""
    _create_state_db(state_db_path, rows=[("p1", "ready")])

    import server
    from server import app

    def boom(*args, **kwargs):
        raise RuntimeError("phase migration exploded")

    monkeypatch.setattr(server, "migrate_ready_to_queued", boom)

    with caplog.at_level(logging.WARNING, logger="server"):
        with TestClient(app) as client:
            resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    # 异常被记下来了，而不是冒出去把后端带走。
    assert any(
        record.levelno >= logging.ERROR
        and "[phase_migration]" in record.getMessage()
        for record in caplog.records
    ), "the migration failure was not logged"


# ---------------------------------------------------------------------------
# 4. 不在 import 期执行
# ---------------------------------------------------------------------------


def test_migration_is_not_called_at_import_time() -> None:
    """模块体里没有迁移调用 —— import ``server`` 不碰数据。"""
    import server

    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    offenders = _module_level_calls(tree, "migrate_ready_to_queued")

    assert not offenders, (
        "the phase migration is called at module scope, so importing "
        "server rewrites the operator's database before the app is even "
        "running:\n  "
        + "\n  ".join(str(node.lineno) for node in offenders)
    )
