"""挂起（hold）与重试预算冻结 —— 命中框架缺陷后原计划停下来，但预算不烧。

这个文件钉住什么
----------------
* ``test_hold_freezes`` —— 挂起**不动**重试计数：挂起本身不消耗预算；
* ``test_hold_idempotent`` —— 同一个 (指纹, 计划, 验证点) 挂两次只有一条挂接；
* ``test_release_restores`` —— 释放后计数**从挂起那一刻继续**：挂起期间
  发生的失败不补扣预算；
* ``test_unknown_fp`` —— 挂一个没人登记过的指纹 → ``KeyError``，不静默创建；
* ``test_hold_failure_rolls_back`` —— 挂起写到一半失败 → 计划状态不变，
  挂接行也不留；
* ``test_multi_vp_one_block`` —— 同一计划多个验证点 → 多条挂接、**一个**挂起块；
* ``test_release_keeps_other_holds`` —— 释放一个缺陷不解除另一个缺陷的挂起；
* ``test_hold_after_release_reopens`` —— 放开之后再挂，算**新的一次挂起**；
* ``test_module_level_hold`` —— 模块级 ``hold()`` 与类方法走同一条路。

**部分阻塞 vs 全部阻塞**（架构决策点 4）—— 下面四个用例钉住相位那条分叉：

* ``test_partial_blocking_keeps_the_phase`` —— 只挂住一个验证点 → 相位**不动**，
  计划剩下的无关工作继续做；
* ``test_total_blocking_enters_the_blocked_phase`` —— 全部剩余工作被挡住 →
  相位变成 ``blocked_on_framework_fix``（让出执行/验证槽位）；
* ``test_blocked_phase_writes_no_verdict`` —— 进阻塞相位**不写失败裁决**，
  也**不消耗**重试预算；
* ``test_release_returns_the_blocked_plan_to_the_queue`` —— 释放挂起 → 计划从
  阻塞相位回调度队列（``queued``）。

为什么这是集成测试
------------------
挂起这件事横跨三份**跨进程的事实**：注册表里的挂接行、计划状态里的
``framework_fix`` 块、以及 ``plan_tasks`` 里那个验证点的重试计数。三样都住在
状态库里，替身（double）证明不了「读的就是生产那张库」—— 迁移是真的、查询是
真的、块是真的。所以每个用例都对着 ``state_db`` fixture 给的那份真实 SQLite
文件跑。

边界条件
--------
* 重复 hold → 幂等（一条挂接，预算不重新冻结）；
* 未知指纹 → ``KeyError``，**不**静默创建注册表行；
* hold 失败 → 事务回滚，计划状态不变；
* 同一计划多 VP → 多条 attach，单一 hold 块；
* 释放后计数从 hold 时点继续（挂起期间不补扣）。

「还挂着吗」由**审计链**回答，不是由挂接行的状态回答：挂接行是历史
（「谁曾经等过这个修复」），注册表的 ``status`` 词表是它自己的闭集，生命周期
不往里写第三个词。所以释放留下的是一条 ``released`` 事件。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
import time
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework_fix.lifecycle import FrameworkFixLifecycle  # noqa: E402
from framework_fix.registry import FrameworkFixRegistry  # noqa: E402
from plan_state import PlanState  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


#: 生命周期写进审计链的事件类型。
_LIFECYCLE_EVENTS = ("held", "released", "hold_rolled_back")


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """计划自己的目录 —— 目录名就是计划号。"""
    directory = tmp_path / "plans" / "p1"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def registry(state_db: Path) -> FrameworkFixRegistry:
    """一台注册表，指向 ``state_db`` fixture 给的那份库。"""
    return FrameworkFixRegistry(db_path=state_db)


@pytest.fixture
def lifecycle(
    registry: FrameworkFixRegistry, plan_dir: Path
) -> FrameworkFixLifecycle:
    """一台生命周期 —— 注册表是真的，计划状态也是真的。"""
    return FrameworkFixLifecycle(
        registry=registry,
        plan_state=PlanState(plan_dir),
    )


class _UnwritablePlanState:
    """一个写不进去的计划状态。

    ``enter_block`` 抛错 —— 这模拟「挂起写到一半失败」（库忙、磁盘满、
    迁移没跑）。真实写路径失败时，调用方必须看到失败，而不是一个「挂上了」
    的假象。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def enter_block(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the plan state could not be written")

    def exit_block(self, *args: Any, **kwargs: Any) -> None:
        self._inner.exit_block(*args, **kwargs)

    def get_framework_fix(self) -> Dict[str, Any]:
        return self._inner.get_framework_fix()


def _register(
    registry: FrameworkFixRegistry,
    plan_id: str = "p1",
    vp_id: str = "VP-3",
    blocking: bool = True,
    **overrides: Any,
) -> str:
    """登记一次框架缺陷命中，返回它的指纹。

    走生产写路径（``FrameworkFixRegistry.register_hit``），库是
    ``state_db`` fixture 给的那份 —— 迁移是真的，表是真的。
    """
    hit: Dict[str, Any] = {
        "plan_id": plan_id,
        "vp_id": vp_id,
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
        "blocking": blocking,
    }
    hit.update(overrides)
    return registry.register_hit(hit)


def _seed_retry(
    state_db: Path,
    plan_id: str,
    vp_id: str,
    attempt: int,
    task_id: str = "R1-1",
) -> None:
    """给这个验证点造一条修复任务行，并把重试计数写成 ``attempt``。

    ``plan_tasks.attempt`` 就是那个重试计数：验证点失败一次，修复任务被
    复活一次，计数就 +1。挂起冻结的正是它。
    """
    conn = open_db(state_db)
    try:
        repo = PlanTaskRepository(conn)
        repo.add_task(
            plan_id,
            {"id": task_id, "failed_vp_id": vp_id, "task_group": "repair"},
        )
        repo.update_task(plan_id, task_id, {"attempt": attempt})
        conn.commit()
    finally:
        conn.close()


def _attempts(state_db: Path, plan_id: str) -> Dict[str, int]:
    """这个计划每条任务的重试计数。"""
    conn = sqlite3.connect(str(state_db))
    try:
        rows = conn.execute(
            "SELECT task_id, attempt FROM plan_tasks WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: int(row[1] or 0) for row in rows}


def _attach_rows(state_db: Path) -> List[Dict[str, Any]]:
    """每一条挂接行，最早的在前。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT attach_id, fingerprint, plan_id, vp_id, round, source, "
            "status, held_since, resolved_at FROM framework_fix_attach "
            "ORDER BY held_since, attach_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _events(state_db: Path) -> List[Dict[str, Any]]:
    """审计链上的生命周期事件，按写入顺序。

    按 ``rowid`` 排 —— 审计链只追加，插入顺序就是事件顺序（时间戳会撞，
    ``event_id`` 是随机的，按它排等于随机排历史）。
    """
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_type, payload_json, created_at "
            "FROM framework_fix_event WHERE event_type IN (?, ?, ?) "
            "ORDER BY rowid",
            _LIFECYCLE_EVENTS,
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "event_type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _block(state_db: Path, plan_id: str) -> Dict[str, Any]:
    """计划状态里的 ``framework_fix`` 块（没有行就是空块）。"""
    conn = sqlite3.connect(str(state_db))
    try:
        row = conn.execute(
            "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row[0]:
        return {}
    block = json.loads(row[0])
    return block if isinstance(block, dict) else {}


def _hold(
    lifecycle: FrameworkFixLifecycle,
    fingerprint: str,
    plan_id: str = "p1",
    vp_id: str = "VP-3",
    round_no: int = 2,
) -> Any:
    """挂起一次 —— 走生命周期自己的入口。"""
    return lifecycle.hold(
        plan_id=plan_id,
        vp_id=vp_id,
        fingerprint=fingerprint,
        round_no=round_no,
        source="clean_main_repro",
    )


# ---------------------------------------------------------------------------
# 阻塞相位的辅助工具
#
# 「这条计划还剩多少工作」是从三处**真实事实**里读出来的：
#
#   * ``plan_tasks`` 里未完成的任务行（``failed_vp_id`` 说它属于哪个验证点）；
#   * 计划目录里的 ``verification_plan.json``（这条计划声明了哪些验证点）；
#   * ``plan_verification_result`` 里已判定但还没过的验证点。
#
# 三处都住在真实库里 / 真实目录里，替身证明不了「读到的就是生产看到的那份」。
# ---------------------------------------------------------------------------
def _seed_phase(plan_dir: Path, phase: str) -> None:
    """把计划停在一个相位上 —— 走生产写路径。"""
    PlanState(plan_dir).force_set_phase(phase)


def _phase(plan_dir: Path) -> str:
    """这条计划此刻的相位（读的是库里那一列）。"""
    return PlanState(plan_dir).get_current_phase()


def _seed_verification_plan(plan_dir: Path, vp_ids: List[str]) -> None:
    """写下这条计划声明的验证点清单。"""
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"verification_points": [{"id": vp} for vp in vp_ids]}),
        encoding="utf-8",
    )


def _verification_results(state_db: Path, plan_id: str) -> List[Dict[str, Any]]:
    """这个计划的验证结果行 —— 「有没有失败裁决」由它回答。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT vp_id, verdict FROM plan_verification_result "
            "WHERE plan_id = ? ORDER BY rowid",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_hold_freezes(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """挂起不动重试计数 —— 挂起本身不消耗预算。"""
    fingerprint = _register(registry)
    _seed_retry(state_db, "p1", "VP-3", attempt=2)

    receipt = _hold(lifecycle, fingerprint)

    assert _attempts(state_db, "p1")["R1-1"] == 2, (
        "挂起不消耗重试预算：挂起前是 2，挂起后还得是 2；实际是 "
        f"{_attempts(state_db, 'p1')!r}"
    )
    assert receipt["frozen_budget"] == 2, (
        "挂起时必须把当时的重试计数记下来 —— 释放时要从这个数继续；实际是 "
        f"{receipt['frozen_budget']!r}"
    )
    assert receipt["attach_id"] == _attach_rows(state_db)[0]["attach_id"], (
        "凭据上的挂接 id 就是库里那一行的 id"
    )


def test_hold_idempotent(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """同一个 (指纹, 计划, 验证点) 挂两次只有一条挂接。"""
    fingerprint = _register(registry)
    _seed_retry(state_db, "p1", "VP-3", attempt=2)

    first = _hold(lifecycle, fingerprint)
    second = _hold(lifecycle, fingerprint)

    assert first == second, (
        "重复挂起必须返回同一个挂接 id：挂起表达的是「谁在等这个修复」，"
        f"不是「等了几次」；{first!r} != {second!r}"
    )
    rows = _attach_rows(state_db)
    assert len(rows) == 1, f"期望恰好一条挂接行，实际 {rows!r}"
    assert rows[0]["attach_id"] == first
    assert second["frozen_budget"] == 2, (
        "重复挂起不能重新冻结预算 —— 冻结的是第一次挂起时的那一笔"
    )


def test_release_restores(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """释放后计数从挂起那一刻继续 —— 挂起期间不补扣。"""
    fingerprint = _register(registry)
    _seed_retry(state_db, "p1", "VP-3", attempt=2)

    _hold(lifecycle, fingerprint)

    # 挂起期间又失败了一次（有人在挂起窗口里动了这个计数）。
    _seed_retry(state_db, "p1", "VP-3", attempt=3, task_id="R1-1")

    released = lifecycle.release_hits(fingerprint)

    assert released == 1, f"应当释放一条挂接，实际 {released!r}"
    assert _attempts(state_db, "p1")["R1-1"] == 2, (
        "释放后计数从挂起那一刻继续：挂起时是 2，挂起期间的失败不补扣；"
        f"实际是 {_attempts(state_db, 'p1')!r}"
    )
    rows = _attach_rows(state_db)
    assert len(rows) == 1, (
        f"挂接行是历史 —— 释放不抹掉「谁等过这个修复」；实际 {rows!r}"
    )
    events = _events(state_db)
    release_events = [e for e in events if e["event_type"] == "released"]
    assert len(release_events) == 1, (
        f"释放要在审计链上留一条 released 事件；实际 {events!r}"
    )
    assert release_events[0]["created_at"], "释放要留下释放时间"
    assert release_events[0]["payload"]["frozen_budget"] == 2, (
        "释放事件要带着当时冻结的那个计数 —— 释放之后从那个数继续"
    )
    assert _block(state_db, "p1") == {}, "被释放的计划不再挂着"


def test_unknown_fp(
    lifecycle: FrameworkFixLifecycle,
    state_db: Path,
) -> None:
    """挂一个没人登记过的指纹 → KeyError，不静默创建。"""
    with pytest.raises(KeyError):
        _hold(lifecycle, "sha256:" + "0" * 64)

    assert _attach_rows(state_db) == [], (
        "挂一个不存在的缺陷等于什么都没挂住 —— 不能留下挂接行"
    )
    assert _block(state_db, "p1") == {}, (
        "挂起失败时计划状态不变"
    )


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------
def test_hold_failure_rolls_back(
    registry: FrameworkFixRegistry,
    plan_dir: Path,
    state_db: Path,
) -> None:
    """挂起写到一半失败 → 计划状态不变，挂接行也不留。"""
    lifecycle = FrameworkFixLifecycle(
        registry=registry,
        plan_state=_UnwritablePlanState(PlanState(plan_dir)),
    )
    fingerprint = _register(registry)

    with pytest.raises(RuntimeError):
        _hold(lifecycle, fingerprint)

    assert _attach_rows(state_db) == [], (
        "挂起失败必须回滚 —— 留下半截的挂接行，下一次挂起会以为已经挂上了"
    )
    assert _block(state_db, "p1") == {}, (
        "挂起失败时计划状态不变"
    )


def test_multi_vp_one_block(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """同一计划多个验证点 → 多条挂接、一个挂起块。"""
    fingerprint = _register(registry)

    _hold(lifecycle, fingerprint, vp_id="VP-3")
    _hold(lifecycle, fingerprint, vp_id="VP-4")

    rows = _attach_rows(state_db)
    assert len(rows) == 2, f"每个验证点一条挂接，实际 {rows!r}"
    assert sorted(row["vp_id"] for row in rows) == ["VP-3", "VP-4"]
    assert {row["plan_id"] for row in rows} == {"p1"}

    block = _block(state_db, "p1")
    assert block.get("hold") is True, "计划确实挂起了"
    assert set(block) == {
        "hold",
        "fingerprint",
        "fix_plan_id",
        "unclassified_rounds",
    }, (
        "一个计划只有一个挂起块 —— 块里不该长出一个验证点一个条目的结构；"
        f"实际 {block!r}"
    )


def test_release_keeps_other_holds(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """释放一个缺陷，不解除另一个缺陷的挂起。"""
    first = _register(registry, plan_id="p1", vp_id="VP-3")
    second = _register(
        registry,
        plan_id="p2",
        vp_id="VP-1",
        error_class="KeyError",
        first_error_line="KeyError: 'missing'",
    )

    _hold(lifecycle, first, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, second, plan_id="p2", vp_id="VP-1")

    released = lifecycle.release_hits(first)

    assert released == 1, f"应当只释放一条挂接，实际 {released!r}"
    assert _block(state_db, "p1") == {}, "被释放的计划不再挂着"
    assert _block(state_db, "p2").get("hold") is True, (
        "另一个缺陷还挂着 —— 它的计划不能被顺带解除"
    )


def test_release_unknown_fingerprint(
    lifecycle: FrameworkFixLifecycle,
    state_db: Path,
) -> None:
    """释放一个没人登记过的指纹 → 什么都没释放。"""
    assert lifecycle.release_hits("sha256:" + "0" * 64) == 0
    assert _attach_rows(state_db) == []


def test_hold_after_release_reopens(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """放开之后再挂 —— 算**新的一次挂起**。

    放开一次之后，这个验证点又可以被同一个缺陷挂住 —— 缺陷可能复发。而它
    算**新的一次**挂起：冻结的是这次的计数，不是上一次那个已经作废的数。
    """
    fingerprint = _register(registry)
    _seed_retry(state_db, "p1", "VP-3", attempt=2)

    _hold(lifecycle, fingerprint)
    lifecycle.release_hits(fingerprint)

    # 放开之后又消耗了一次重试。
    _seed_retry(state_db, "p1", "VP-3", attempt=3, task_id="R1-1")
    receipt = _hold(lifecycle, fingerprint)

    assert receipt["frozen_budget"] == 3, (
        "新的一次挂起冻结的是这次的计数 —— 上一次那个已经作废；实际是 "
        f"{receipt['frozen_budget']!r}"
    )
    assert _block(state_db, "p1").get("hold") is True, "计划重新挂住"
    rows = _attach_rows(state_db)
    assert len(rows) == 1, (
        f"同一个 (指纹, 计划, 验证点) 永远只有一行挂接；实际 {rows!r}"
    )
    events = _events(state_db)
    assert [e["event_type"] for e in events] == [
        "held",
        "released",
        "held",
    ], f"审计链上要能读出「挂起 → 放开 → 再挂起」；实际 {events!r}"


def test_module_level_hold(
    registry: FrameworkFixRegistry,
    plan_dir: Path,
    state_db: Path,
) -> None:
    """模块级 ``hold()`` 与类方法走同一条路。"""
    from framework_fix import lifecycle as lifecycle_module

    fingerprint = _register(registry)
    _seed_retry(state_db, "p1", "VP-3", attempt=2)

    receipt = lifecycle_module.hold(
        plan_id="p1",
        vp_id="VP-3",
        fingerprint=fingerprint,
        round_no=2,
        source="clean_main_repro",
        registry=registry,
        plan_state=PlanState(plan_dir),
    )

    assert isinstance(receipt, str), "挂接 id 本身就是一个字符串"
    assert receipt == _attach_rows(state_db)[0]["attach_id"]
    assert dict(receipt) == {
        "attach_id": receipt,
        "frozen_budget": 2,
    }, "凭据同时带着这次冻结下来的预算"
    assert _attempts(state_db, "p1")["R1-1"] == 2, "挂起不动重试计数"


# ---------------------------------------------------------------------------
# TDD 规格：部分阻塞 vs 全部阻塞（架构决策点 4）
#
# 架构决策点 4：只有当一条计划的**全部剩余工作**都被框架缺陷挡住时，它才进
# ``blocked_on_framework_fix`` 相位 —— 让出执行/验证槽位，回调度队列排队。
# 部分阻塞时相位**不动**：计划剩下的无关工作继续做。
#
# 「剩余工作」由三处事实回答（见上面的辅助工具）：未完成的 ``plan_tasks``
# 行、计划声明的验证点清单、已判定但还没过的验证点。被挡住 = 能归到某个
# 被挂住的验证点上；归不上的（没有验证点归属的普通任务、没被挂住的其他
# 验证点）就是**还有活要干**。
# ---------------------------------------------------------------------------
def test_partial_blocking_keeps_the_phase(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    plan_dir: Path,
) -> None:
    """只挂住一个验证点 → 相位不动，计划剩下的工作继续。"""
    fingerprint = _register(registry)
    _seed_phase(plan_dir, "verification_repairing")
    _seed_verification_plan(plan_dir, ["VP-3", "VP-4"])
    _seed_retry(state_db, "p1", "VP-3", attempt=2, task_id="R1-1")
    _seed_retry(state_db, "p1", "VP-4", attempt=1, task_id="R1-2")

    _hold(lifecycle, fingerprint, vp_id="VP-3")

    assert _phase(plan_dir) == "verification_repairing", (
        "部分阻塞：VP-4 还没被挡住，那份工作还在 —— 相位必须留在原处；"
        f"实际是 {_phase(plan_dir)!r}"
    )
    assert _block(state_db, "p1").get("hold") is True, "挂起块照写不误"


def test_total_blocking_enters_the_blocked_phase(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    plan_dir: Path,
) -> None:
    """全部剩余工作被挡住 → 相位变成 ``blocked_on_framework_fix``。"""
    fingerprint = _register(registry)
    _seed_phase(plan_dir, "verification_repairing")
    _seed_verification_plan(plan_dir, ["VP-3", "VP-4"])
    _seed_retry(state_db, "p1", "VP-3", attempt=2, task_id="R1-1")
    _seed_retry(state_db, "p1", "VP-4", attempt=1, task_id="R1-2")

    _hold(lifecycle, fingerprint, vp_id="VP-3")
    assert _phase(plan_dir) == "verification_repairing", (
        "只挂住一个验证点时相位不动 —— 这是部分阻塞那一侧的分叉；"
        f"实际是 {_phase(plan_dir)!r}"
    )

    _hold(lifecycle, fingerprint, vp_id="VP-4")

    assert _phase(plan_dir) == "blocked_on_framework_fix", (
        "全部剩余工作都被挡住 → 计划让出执行/验证槽位，停在这个相位上；"
        f"实际是 {_phase(plan_dir)!r}"
    )


def test_blocked_phase_writes_no_verdict(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    plan_dir: Path,
) -> None:
    """进阻塞相位不写失败裁决，也不消耗重试预算。"""
    fingerprint = _register(registry)
    _seed_phase(plan_dir, "verification_repairing")
    _seed_verification_plan(plan_dir, ["VP-3"])
    _seed_retry(state_db, "p1", "VP-3", attempt=2, task_id="R1-1")

    _hold(lifecycle, fingerprint, vp_id="VP-3")

    assert _phase(plan_dir) == "blocked_on_framework_fix"
    assert _verification_results(state_db, "p1") == [], (
        "被框架缺陷挡住不是失败裁决 —— 不写任何验证结果行"
    )
    assert _attempts(state_db, "p1")["R1-1"] == 2, (
        "被挡住不消耗重试预算：挂起前是 2，挂起后还得是 2；实际是 "
        f"{_attempts(state_db, 'p1')!r}"
    )


def test_release_returns_the_blocked_plan_to_the_queue(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    plan_dir: Path,
) -> None:
    """释放挂起 → 计划从阻塞相位回调度队列。"""
    fingerprint = _register(registry)
    _seed_phase(plan_dir, "verification_repairing")
    _seed_verification_plan(plan_dir, ["VP-3"])
    _seed_retry(state_db, "p1", "VP-3", attempt=2, task_id="R1-1")

    _hold(lifecycle, fingerprint, vp_id="VP-3")
    assert _phase(plan_dir) == "blocked_on_framework_fix"

    lifecycle.release_hits(fingerprint)

    assert _phase(plan_dir) == "queued", (
        "挂起被释放 → 计划回到调度队列（让它重新被调度一次）；"
        f"实际是 {_phase(plan_dir)!r}"
    )


def test_exhausted_attachment_no_longer_blocks_the_plan(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """耗尽之后，这条挂接不再阻塞那个计划 —— 但它还在。

    「阻塞计数」是调度器算的：``status='blocked'`` 的挂接有几条，那个计划就
    有几件东西挂着。耗尽是终态 —— 等人拍板，不再自动重试 —— 所以这条挂接
    必须**不再**算进阻塞计数，否则那个计划会被一件已经不再等修复的事永久
    拖着。

    记录本身留着：挂接行是历史（「谁曾经等过这个修复」），耗尽不擦掉历史。
    """
    fingerprint = _register(registry)
    _hold(lifecycle, fingerprint, vp_id="VP-3")

    assert [row["status"] for row in _attach_rows(state_db)] == ["blocked"]

    lifecycle.exhaust(fingerprint)

    rows = _attach_rows(state_db)
    assert len(rows) == 1, f"挂接记录是历史，耗尽不删它；实际 {rows!r}"
    assert rows[0]["status"] == "exhausted", (
        f"耗尽的挂接不再阻塞那个计划；实际 {rows!r}"
    )
    blocked = [
        row
        for row in rows
        if row["status"] == "blocked"
    ]
    assert blocked == [], (
        f"耗尽之后阻塞计数必须归零；实际还有 {blocked!r}"
    )


def test_exhausted_block_write_refuses_to_fabricate_a_routing_row(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """块写不进去就报错 —— 它不自己造一行计划状态。

    ``framework_fix`` 块住在 ``plan_routing`` 行里，而那一行带着相位。这条
    路曾经自己 ``INSERT OR IGNORE`` 一行（相位写死 ``'interview'``）——
    那是**造一行**：造出来的行带着一个编出来的相位，而 ``current_phase`` 是
    唯一的工作流状态列，写它的只该有路由仓库的相位 CAS。

    现在这一行只 ``UPDATE``：行不在 → 报错。写不进计划状态却报告成功，比写
    失败更糟 —— 读模型会按「没挂起」渲染，而计划其实停在被阻塞的状态上。
    """
    fingerprint = _register(registry)
    _hold(lifecycle, fingerprint, vp_id="VP-3")

    # 路由行没了 —— 计划没进过状态机，或者被人删了。
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute("DELETE FROM plan_routing WHERE plan_id = ?", ("p1",))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError):
        lifecycle.exhaust(fingerprint)

    conn = sqlite3.connect(str(state_db))
    try:
        rows = conn.execute(
            "SELECT count(*) FROM plan_routing WHERE plan_id = ?", ("p1",)
        ).fetchall()
    finally:
        conn.close()
    assert rows[0][0] == 0, (
        "写失败不许顺手造一行计划状态 —— 那一行的相位是编的；实际有 "
        f"{rows[0][0]} 行"
    )


# ---------------------------------------------------------------------------
# 发现通知 —— 「发现了，要修」这句话对同一个人说一次
# ---------------------------------------------------------------------------
#: 「已经通知过发现」这件事在审计链上的标记行。它活得比这次进程长：
#: 进程重启之后不该再喊一遍，而静默窗口吞掉的那一次要在窗口过去之后补上。
DISCOVERED_MARKER = "notify_discovered_sent"


def _outlet(plan_dir: Path, out: Path) -> None:
    """给计划目录装一个出口 —— 送出去的消息落在磁盘上。

    于是「通知到了没有」不是一个断言，而是磁盘上一个文件在不在。
    """
    script = plan_dir / "record.py"
    script.write_text(
        "import sys\n"
        "with open(sys.argv[1], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(sys.stdin.read().strip() + '\\n')\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{out}"'
    (plan_dir / "hitl_config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": 1800,
                "hooks": [{"kind": "cmd", "command": command, "timeout_sec": 10}],
            }
        ),
        encoding="utf-8",
    )


def _bodies(out: Path) -> List[Dict[str, Any]]:
    """出口收到的那几份 body。"""
    if not out.exists():
        return []
    return [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for_bodies(out: Path, timeout: float = 10.0) -> List[Dict[str, Any]]:
    """等出口把 body 送过来。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bodies = _bodies(out)
        if bodies:
            return bodies
        time.sleep(0.05)
    return _bodies(out)


def _discovered_bodies(out: Path) -> List[Dict[str, Any]]:
    """出口收到的「发现了」那几条。"""
    return [
        body
        for body in _wait_for_bodies(out)
        if body.get("event") == "framework_fix_discovered"
    ]


def _notify_markers(state_db: Path) -> List[Dict[str, Any]]:
    """审计链上「已经通知过发现」的标记行。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_type, fix_plan_id, fingerprint, payload_json "
            "FROM framework_fix_event WHERE event_type = ? ORDER BY rowid",
            (DISCOVERED_MARKER,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@pytest.fixture
def daytime(monkeypatch: pytest.MonkeyPatch) -> None:
    """白天才通知 —— 静默窗口由用例自己决定，不由它跑的时刻决定。

    通知窗口是 22:00–08:00：一条在夜里跑的用例若不钉住这一刻，它可能在
    白天绿、在夜里红 —— 而它绿红的原因与它要验证的那件事毫无关系。
    """
    monkeypatch.setattr(
        "framework_fix.notify.in_quiet_hours", lambda moment: False
    )


def test_the_discovery_is_notified_once(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    plan_dir: Path,
    state_db: Path,
    tmp_path: Path,
    daytime: None,
) -> None:
    """N 个验证点挂到同一个修复计划上 -> 只有一条通知。

    挂接行是**关系**（谁在等这个修复），通知是**事件**（这件事发生了）。
    两个验证点挂到同一个修复计划上是两行挂接、一条通知 —— 否则同一个人
    会收到两条一样的消息，而「消息太多」正是人学会忽略通知的方式。
    """
    out = tmp_path / "delivered.jsonl"
    _outlet(plan_dir, out)
    fingerprint = _register(registry, fix_plan_id="fx1")

    _hold(lifecycle, fingerprint, vp_id="VP-3")
    _hold(lifecycle, fingerprint, vp_id="VP-4")

    assert len(_attach_rows(state_db)) == 2, "两个验证点 -> 两条挂接行"

    discovered = _discovered_bodies(out)
    assert len(discovered) == 1, (
        f"同一个修复计划只通知一次；实际 {discovered!r}"
    )
    assert discovered[0]["fix_plan_id"] == "fx1", (
        f"通知里要说得清是哪个修复计划；实际 {discovered[0]!r}"
    )
    assert discovered[0]["plan_id"] == "p1"
    assert discovered[0]["fingerprint"] == fingerprint


def test_the_discovery_is_not_notified_again(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    plan_dir: Path,
    state_db: Path,
    tmp_path: Path,
    daytime: None,
) -> None:
    """通知过了就不再通知 —— 标记在库里，不在进程的内存里。

    进程重启之后不该再喊一遍：那条消息已经送出去了。标记写在审计链上，
    所以「通知过了」这件事活得比这次进程长。
    """
    out = tmp_path / "delivered.jsonl"
    _outlet(plan_dir, out)
    fingerprint = _register(registry, fix_plan_id="fx1")

    _hold(lifecycle, fingerprint, vp_id="VP-3")

    # 另一个进程 —— 同一份库，一台新的生命周期。
    restarted = FrameworkFixLifecycle(
        registry=registry, plan_state=PlanState(plan_dir)
    )
    _hold(restarted, fingerprint, vp_id="VP-4")

    discovered = _discovered_bodies(out)
    assert len(discovered) == 1, (
        f"已经通知过的不再通知；实际 {discovered!r}"
    )
    markers = _notify_markers(state_db)
    assert len(markers) == 1, f"标记行只有一条；实际 {markers!r}"
    assert markers[0]["fix_plan_id"] == "fx1"


def test_a_discovery_that_was_not_delivered_is_retried(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    plan_dir: Path,
    state_db: Path,
    tmp_path: Path,
    daytime: None,
) -> None:
    """送不出去的那一次不算「通知过了」。

    静默窗口吞掉的那一次、出口挂掉的那一次 —— 都不算通知过了：窗口过去
    之后要补上，人总得知道有这么个缺陷要修。
    """
    out = tmp_path / "delivered.jsonl"
    fingerprint = _register(registry, fix_plan_id="fx1")

    # 没有出口 —— 送不出去。
    _hold(lifecycle, fingerprint, vp_id="VP-3")
    assert _notify_markers(state_db) == [], (
        "送不出去就不能写「已经通知过了」—— 那会让这条通知永远丢掉"
    )

    # 出口装上了 —— 这一次送得出去。
    _outlet(plan_dir, out)
    _hold(lifecycle, fingerprint, vp_id="VP-4")

    discovered = _discovered_bodies(out)
    assert len(discovered) == 1, f"补发一条；实际 {discovered!r}"
    assert _notify_markers(state_db), "送到了就要留下标记"


def test_a_failing_notification_does_not_break_the_hold(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    plan_dir: Path,
    state_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知坏了，挂起不能跟着坏。

    通知不是主流程：出口挂了、数据库打不开，这些都不该把修复流程带崩 ——
    挂起成立，挂接行在，缺陷照样有人修。
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the outlet is down")

    monkeypatch.setattr("hooks.dispatcher.dispatch", _boom)
    fingerprint = _register(registry, fix_plan_id="fx1")

    receipt = _hold(lifecycle, fingerprint, vp_id="VP-3")

    assert receipt["attach_id"], "挂起本身要成立"
    assert len(_attach_rows(state_db)) == 1, (
        f"挂接行要在；实际 {_attach_rows(state_db)!r}"
    )
    assert _notify_markers(state_db) == [], "没送出去就不能写标记"
