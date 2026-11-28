"""修复合入之后的重跑与结果追加 —— 只重跑挂接的验证点，结果只追加不覆盖。

这个文件钉住什么
----------------
* ``test_rerun_only_attached`` —— 只重跑**挂接**的那些 (计划, 验证点)，
  没挂接的验证点不碰，整轮也不重跑；
* ``test_append_only`` —— 旧结果行永远不被更新；同一棵树、同一个验证点重复
  跑 → 追加新行，不改旧行；
* ``test_still_failing_reopens`` —— 重跑仍失败 → 缺陷重新打开（``reopened``），
  挂起被**恢复**；
* ``test_exhaust_marks`` —— 连续失败到上限 → ``exhaust``，挂接的计划转成
  ``framework_fix_exhausted``（等人工决策，不再自动重试同一签名）；
* ``test_no_recursive_spawn`` —— 修复计划自己再撞上框架问题时，同签名且范围
  可合并就并入当前修复计划、签名不同或扩大范围就升级 HITL；**任何情况下都
  不派生第二个修复计划**（spawn 只被调用一次）。

为什么这是集成测试
------------------
这件事横跨四份**跨进程的事实**：结果行落在 ``plan_verification_result``（只
追加）、挂接关系落在 ``framework_fix_attach``、审计链落在 ``framework_fix_event``、
计划的挂起块落在 ``plan_routing``。四样都住在状态库里，替身（double）证明不了
「读的就是生产那张库」—— 迁移是真的、查询是真的、块是真的。所以每个用例都对着
``state_db`` fixture 给的那份真实 SQLite 文件跑。

「还挂着吗」由**审计链**回答，不是由挂接行的状态回答
-----------------------------------------------------
挂接行是**历史**（「谁曾经等过这个修复」），注册表的 ``status`` 词表是它自己的
闭集（``open`` / ``fixed`` / ``reopened``），生命周期不往里写第三个词。所以
「重跑通过」与「重跑失败」落在审计链上（``rerun_passed`` / ``rerun_failed``），
「重跑仍失败」把注册表行改回 ``reopened`` —— 那**是**闭集里的词。

边界条件
--------
* 写结果失败 → 记录下来并继续，**不影响重跑调度**；
* 没挂接的验证点 → 不重跑、不写结果行；
* 耗尽 → 不再挂起（等人工决策），而不是继续占着挂起块。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework_fix.classifier import FailureHit  # noqa: E402
from framework_fix.fingerprint import compute_fingerprint  # noqa: E402
from framework_fix.lifecycle import FrameworkFixLifecycle  # noqa: E402
from framework_fix.registry import FrameworkFixRegistry  # noqa: E402
from framework_fix.repro import ReproAttempt, ReproductionResult  # noqa: E402
from framework_fix.spawner import build_envelope, spawn  # noqa: E402
from plan_state import PlanState  # noqa: E402
import verification_persistence  # noqa: E402


#: 生命周期在重跑这条链上写进审计链的事件类型。
_RERUN_EVENTS = ("rerun_passed", "rerun_failed", "reopened", "exhausted")


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """计划自己的目录 —— 目录名就是计划号。"""
    directory = tmp_path / "plans" / "p1"
    directory.mkdir(parents=True, exist_ok=True)
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


def _register(
    registry: FrameworkFixRegistry,
    *,
    plan_id: str = "p1",
    vp_id: str = "VP-3",
    fix_plan_id: str = "fx1",
    repo: str = "repo-a",
    **overrides: Any,
) -> str:
    """登记一次框架缺陷命中，返回它的指纹。

    走生产写路径（``FrameworkFixRegistry.register_hit``）—— 库是 ``state_db``
    fixture 给的那份，迁移是真的，表是真的。
    """
    hit: Dict[str, Any] = {
        "plan_id": plan_id,
        "vp_id": vp_id,
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
        "blocking": True,
        "classification": "framework_deterministic",
        "repo": repo,
        "fix_plan_id": fix_plan_id,
    }
    hit.update(overrides)
    return registry.register_hit(hit)


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


def _block(state_db: Path, plan_id: str = "p1") -> Dict[str, Any]:
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


def _events(state_db: Path) -> List[Dict[str, Any]]:
    """审计链上重跑这条链的事件，按写入顺序。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_type, payload_json FROM framework_fix_event "
            "WHERE event_type IN (?, ?, ?, ?) ORDER BY rowid",
            _RERUN_EVENTS,
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "event_type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def _attach_rows(state_db: Path) -> List[Dict[str, Any]]:
    """每一条挂接行。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT attach_id, fingerprint, plan_id, vp_id, round, source, "
            "status FROM framework_fix_attach ORDER BY held_since, attach_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_rerun_only_attached(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """只重跑挂接的那些 (计划, 验证点) —— 不跑整轮。"""
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    calls: List[Any] = []

    def rerun(plan_id: str, vp_id: str) -> str:
        calls.append((plan_id, vp_id))
        return "passed"

    outcomes = lifecycle.rerun_after_merge(
        "fx1", rerun=rerun, worktree=fixture_repo["clone"]
    )

    assert calls == [("p1", "VP-3")], (
        "只重跑挂接的那些 (计划, 验证点) —— 没挂接的验证点不碰，整轮也不重跑；"
        f"实际跑了 {calls!r}"
    )
    assert len(outcomes) == 1, f"只该有一个重跑结论，实际 {outcomes!r}"
    outcome = outcomes[0]
    assert outcome["plan_id"] == "p1"
    assert outcome["vp_id"] == "VP-3"
    assert outcome["new_status"] == "passed"
    assert str(outcome["tree_fingerprint"]).startswith("sha256:"), (
        "结论必须绑在树指纹上 —— 没有树就没法说「这棵树过了」；实际是 "
        f"{outcome['tree_fingerprint']!r}"
    )

    # 结果行只落在被重跑的那一个 (计划, 验证点) 上。
    rows = verification_persistence.read_verification_results(db_path=state_db)
    assert [(row["plan_id"], row["vp_id"]) for row in rows] == [
        ("p1", "VP-3")
    ], f"没挂接的验证点不该有结果行；实际 {rows!r}"


def test_append_only(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """旧结果行永不更新；同一棵树、同一个验证点重复跑 → 追加新行。"""
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    old_id = verification_persistence.append_verification_result(
        plan_id="p1",
        vp_id="VP-3",
        tree_fingerprint="sha256:" + "0" * 64,
        verdict="failed",
        round_no=1,
        source="verification_loop",
        db_path=state_db,
    )

    first = lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "passed", worktree=fixture_repo["clone"]
    )
    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "passed", worktree=fixture_repo["clone"]
    )

    rows = verification_persistence.read_verification_results(db_path=state_db)
    assert len(rows) == 3, (
        f"两次重跑追加两行 —— 加上原来那一行共三行；实际 {rows!r}"
    )

    old = [row for row in rows if row["result_id"] == old_id]
    assert len(old) == 1, f"旧行必须还在；实际 {rows!r}"
    assert old[0]["verdict"] == "failed", (
        f"旧结果行永远不被更新；实际 {old[0]!r}"
    )
    assert old[0]["tree_fingerprint"] == "sha256:" + "0" * 64, (
        f"旧行绑的是旧树，别被改写；实际 {old[0]!r}"
    )

    added = [row for row in rows if row["result_id"] != old_id]
    assert all(row["verdict"] == "passed" for row in added), (
        f"新行的结论是这次重跑的结果；实际 {added!r}"
    )
    assert added[0]["tree_fingerprint"] == added[1]["tree_fingerprint"], (
        "同一棵树、同一个验证点重复跑 → 追加两行，不改旧行；实际 "
        f"{added!r}"
    )
    assert added[0]["tree_fingerprint"] == first[0]["tree_fingerprint"], (
        "结论绑的树指纹就是这次重跑的那棵树；实际 "
        f"{added[0]['tree_fingerprint']!r} != {first[0]['tree_fingerprint']!r}"
    )


def test_still_failing_reopens(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """重跑仍失败 → 缺陷重新打开，挂起被恢复。"""
    from framework_fix.read_model import project_display_state

    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    # 修复合入 → 挂起被放开，计划继续往前走。
    assert lifecycle.release_hits(fingerprint) == 1
    assert not _block(state_db, "p1").get("hold"), "合入之后计划不再挂着"

    outcomes = lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )

    assert outcomes[0]["new_status"] == "failed"

    row = registry.lookup(fingerprint)
    assert row is not None
    assert row["status"] == "reopened", (
        f"重跑仍失败 → 缺陷重新打开；实际 {row['status']!r}"
    )

    block = _block(state_db, "p1")
    assert block.get("hold") is True, (
        f"重跑仍失败 → 恢复挂起；实际 {block!r}"
    )
    assert block.get("fingerprint") == fingerprint

    assert project_display_state({"framework_fix": block}, row) == "reopened", (
        "对外可见的状态是 reopened —— 同一个计划继续跑下一轮"
    )

    events = [event["event_type"] for event in _events(state_db)]
    assert "reopened" in events, f"审计链上要留下 reopened；实际 {events!r}"
    assert "rerun_failed" in events, f"审计链上要留下 rerun_failed；实际 {events!r}"


def test_exhaust_marks(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """连续失败到上限 → exhaust，挂接的计划转 framework_fix_exhausted。"""
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    first = lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )
    assert first[0]["new_status"] == "failed"
    block = _block(state_db, "p1")
    assert block.get("hold") is True, (
        f"还没到上限 → 恢复挂起，等下一轮修；实际 {block!r}"
    )
    assert block.get("status") != "framework_fix_exhausted", (
        f"还没到上限就不该说耗尽；实际 {block!r}"
    )

    second = lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )
    assert second[0]["new_status"] == "failed"

    block = _block(state_db, "p1")
    assert block.get("status") == "framework_fix_exhausted", (
        "连续失败到上限 → 挂接的计划转 framework_fix_exhausted；实际 "
        f"{block!r}"
    )
    assert not block.get("hold"), (
        f"耗尽之后不再占着挂起块 —— 等人决策；实际 {block!r}"
    )

    events = [event["event_type"] for event in _events(state_db)]
    assert events.count("rerun_failed") == 2, f"两次失败各留一条；实际 {events!r}"
    assert events.count("exhausted") == 1, f"耗尽留一条 exhausted；实际 {events!r}"

    from framework_fix.read_model import project_display_state

    row = registry.lookup(fingerprint)
    assert project_display_state({"framework_fix": block}, row) == (
        "framework_fix_exhausted"
    ), "对外可见的状态是耗尽 —— 需人工决策，不再自动重试同一签名"


def test_no_recursive_spawn(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """修复计划自己撞框架问题时并入或升级 HITL —— spawn 只被调用一次。"""
    from framework_fix.classifier import FailureHit
    from framework_fix.repro import ReproAttempt, ReproductionResult
    from framework_fix.spawner import build_envelope, spawn

    fingerprint = "sha256:" + "c" * 64
    hit = FailureHit(
        vp_id="VP-3",
        plan_id="p1",
        error_class="AssertionError",
        first_error_line="AssertionError: expected 1, got 2",
        framework_files=["backend/framework_fix/registry.py"],
        acceptance_critical=True,
        out_of_scope_change=False,
    )
    repro = ReproductionResult(
        classification="framework_deterministic",
        attempts=(
            ReproAttempt(
                run=1,
                exit_code=1,
                timed_out=False,
                first_error_line="AssertionError: expected 1, got 2",
                duration_s=1.5,
            ),
            ReproAttempt(
                run=2,
                exit_code=1,
                timed_out=False,
                first_error_line="AssertionError: expected 1, got 2",
                duration_s=1.4,
            ),
        ),
        clean_commit="a" * 40,
        tree_fingerprint="sha256:" + "b" * 64,
    )
    envelope = build_envelope(
        fingerprint=fingerprint, hit=hit, repro=repro, repo="repo-a"
    )

    calls: List[Any] = []

    def counting_spawn(env: Dict[str, Any]) -> str:
        calls.append(env)
        return spawn(env)

    # 第一次：普通计划命中 → 派生一个修复计划（这是唯一的一次 spawn）。
    first = lifecycle.handle_fix_plan_hit(
        hit_plan_id="p1",
        fingerprint=fingerprint,
        envelope=envelope,
        repo="repo-a",
        spawn=counting_spawn,
    )
    assert first["action"] == "spawned", f"普通计划命中 → 派生修复计划；实际 {first!r}"
    assert len(calls) == 1
    fix_plan_id = str(first["fix_plan_id"])
    assert fix_plan_id

    # 第二次：修复计划自己又撞上**同一个签名**。同仓、范围可合并 → 并入当前
    # 修复计划（revise 增补任务），**不派生第二个**。
    merged: List[Any] = []
    second = lifecycle.handle_fix_plan_hit(
        hit_plan_id=fix_plan_id,
        fingerprint=fingerprint,
        repo="repo-a",
        scope_mergeable=True,
        spawn=counting_spawn,
        merge=merged.append,
    )
    assert second["action"] == "merged", (
        f"同签名、同仓且范围可合并 → 并入当前修复计划；实际 {second!r}"
    )
    assert len(merged) == 1, "并入要真的发生一次（revise 增补任务）"
    assert len(calls) == 1, (
        "修复计划自己撞框架问题时不得派生第二个修复计划；实际调用了 "
        f"{len(calls)} 次"
    )

    # 第三次：**不同签名** → 升级 HITL。不同签名的问题超出当前修复计划的
    # 验收边界（PRD 决策点 7），调用方说「范围可合并」也不行 —— 那样会把
    # 另一个缺陷悄悄塞进这个修复计划。同样不派生。
    escalated: List[Any] = []
    third = lifecycle.handle_fix_plan_hit(
        hit_plan_id=fix_plan_id,
        fingerprint="sha256:" + "d" * 64,
        repo="repo-a",
        scope_mergeable=True,
        spawn=counting_spawn,
        hitl=escalated.append,
    )
    assert third["action"] == "hitl", (
        f"不同签名 → 升级 HITL；实际 {third!r}"
    )
    assert len(escalated) == 1, "升级 HITL 要真的发生一次"
    assert len(calls) == 1, (
        "任何情况下都不得派生第二个修复计划；实际调用了 "
        f"{len(calls)} 次"
    )

    # 第四次：扩大了修复范围 → 升级 HITL，同样不派生。
    widened: List[Any] = []
    fourth = lifecycle.handle_fix_plan_hit(
        hit_plan_id=fix_plan_id,
        fingerprint=fingerprint,
        repo="repo-a",
        scope_expanded=True,
        scope_mergeable=True,
        spawn=counting_spawn,
        hitl=widened.append,
    )
    assert fourth["action"] == "hitl", (
        f"扩大范围 → 升级 HITL；实际 {fourth!r}"
    )
    assert len(widened) == 1, "扩大范围要真的升级一次"
    assert len(calls) == 1, (
        "任何情况下都不得派生第二个修复计划；实际调用了 "
        f"{len(calls)} 次"
    )


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------
def test_result_write_failure_does_not_stop_the_rerun(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写结果失败 → 记录下来并继续，不影响重跑调度。"""
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("the state database refused the row")

    monkeypatch.setattr(
        "framework_fix.lifecycle.append_verification_result", _boom
    )

    outcomes = lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "passed", worktree=fixture_repo["clone"]
    )

    assert len(outcomes) == 1, (
        f"写结果失败不能吞掉这次重跑；实际 {outcomes!r}"
    )
    assert outcomes[0]["new_status"] == "passed"


def test_no_attach_nothing_to_rerun(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """没有挂接 → 不重跑任何东西，也不写结果行。"""
    _register(registry, plan_id="p1", vp_id="VP-3")

    calls: List[Any] = []

    def rerun(plan_id: str, vp_id: str) -> str:
        calls.append((plan_id, vp_id))
        return "passed"

    outcomes = lifecycle.rerun_after_merge(
        "fx1", rerun=rerun, worktree=fixture_repo["clone"]
    )

    assert calls == [], f"没有挂接就没有要重跑的验证点；实际跑了 {calls!r}"
    assert outcomes == [], f"没有挂接就没有结论；实际 {outcomes!r}"
    assert verification_persistence.read_verification_results(db_path=state_db) == []


def test_loop_writes_result_rows(
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """验证轮自己的结论也按树指纹追加到同一张表。"""
    import verification_loop as vl

    vl._record_round_tree_results(
        "p1",
        3,
        fixture_repo["clone"],
        [
            {"id": "VP-3", "status": "PASSED"},
            {"id": "VP-4", "status": "FAILED"},
        ],
    )

    rows = verification_persistence.read_verification_results(db_path=state_db)
    assert [(row["vp_id"], row["verdict"]) for row in rows] == [
        ("VP-3", "passed"),
        ("VP-4", "failed"),
    ], f"每个验证点一行，结论归一；实际 {rows!r}"
    assert all(row["source"] == "verification_loop" for row in rows)
    assert all(row["round"] == 3 for row in rows)
    assert str(rows[0]["tree_fingerprint"]).startswith("sha256:"), (
        f"结论必须绑在树指纹上；实际 {rows[0]!r}"
    )
    assert rows[0]["base_main_commit"], "结论要记下基准 commit"


def test_pass_resets_the_failure_count(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """通过之后重新开始数 —— 复发是新的一次，不是上一段的延续。"""
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )
    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "passed", worktree=fixture_repo["clone"]
    )
    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )

    block = _block(state_db, "p1")
    assert block.get("status") != "framework_fix_exhausted", (
        f"通过之后重新开始数 —— 复发是新的一次；实际 {block!r}"
    )
    assert block.get("hold") is True, f"仍然失败 → 挂起；实际 {block!r}"


def test_module_level_entry_points(
    registry: FrameworkFixRegistry,
    plan_dir: Path,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """模块级入口与类方法走同一条路。"""
    from framework_fix import lifecycle as lifecycle_module

    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    lifecycle_module.hold(
        plan_id="p1",
        vp_id="VP-3",
        fingerprint=fingerprint,
        round_no=2,
        source="clean_main_repro",
        registry=registry,
        plan_state=PlanState(plan_dir),
    )

    outcomes = lifecycle_module.rerun_after_merge(
        "fx1",
        rerun=lambda p, v: "passed",
        worktree=fixture_repo["clone"],
        registry=registry,
        plan_state=PlanState(plan_dir),
    )

    assert len(outcomes) == 1
    assert outcomes[0]["vp_id"] == "VP-3"
    assert len(verification_persistence.read_verification_results(db_path=state_db)) == 1

def _decisions(state_db: Path) -> List[Dict[str, Any]]:
    """状态库里那些「问人」的问题。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT decision_id, plan_id, source, question, status "
            "FROM plan_decisions ORDER BY created_at, decision_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def test_the_merged_repair_marks_the_registry_fixed(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """修复合入 → 注册表那一行写 ``fixed``。

    「这个缺陷修好了没有」这个问题的答案在注册表里 —— 而合入就是「修好了」
    这个事实被观测到的那一刻。不写它，重跑失败时就分不清「修复合入了但没修
    好」与「还没合入」。
    """
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "passed", worktree=fixture_repo["clone"]
    )

    row = registry.lookup(fingerprint)
    assert row is not None
    assert row["status"] == "fixed", (
        f"修复合入 → 注册表写 fixed；实际 {row['status']!r}"
    )


def test_a_failed_rerun_leaves_the_registry_reopened(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """重跑仍失败且未达上限 → 注册表写 ``reopened``，同一个修复计划继续。"""
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )

    row = registry.lookup(fingerprint)
    assert row is not None
    assert row["status"] == "reopened", (
        f"重跑仍失败 → 同一个修复计划继续（reopened）；实际 {row['status']!r}"
    )


def test_exhaust_marks_the_attachments_and_asks_a_human(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """耗尽：挂接不再阻塞（记录留着），并且有人被问到。

    耗尽是终态 —— 同一个签名不再自动重试，等人拍板。所以两件事必须同时成立：
    挂接**不再持有**原计划的执行（阻塞计数归零），以及**有一个问题**等着人
    回答。只有前者 = 缺陷被悄悄忘掉；只有后者 = 计划还被挂住。
    """
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")

    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )
    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )

    rows = _attach_rows(state_db)
    assert [row["status"] for row in rows] == ["exhausted"], (
        f"耗尽之后挂接不再阻塞（记录留着）；实际 {rows!r}"
    )

    decisions = _decisions(state_db)
    assert len(decisions) == 1, f"耗尽要问人一次；实际 {decisions!r}"
    assert decisions[0]["plan_id"] == "fx1", (
        f"问题问在修复计划上（它拥有这个签名）；实际 {decisions!r}"
    )


def test_a_late_hit_reruns_on_the_fixed_tree(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """已经修好的签名又被撞上 → 不新建修复计划，在含修复的树上重跑。

    延迟命中走的是同一条路径：挂接到已修复的记录，在含修复的树上重跑，只追加
    结果。旧裁决原样保留 —— 新结论是**新的一行**，不是对旧行的覆盖。
    """
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")
    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "passed", worktree=fixture_repo["clone"]
    )

    outcome = lifecycle.handle_late_hit(
        hit_plan_id="p2",
        fingerprint=fingerprint,
        vp_id="VP-9",
        rerun=lambda p, v: "passed",
        worktree=fixture_repo["clone"],
    )

    assert outcome["action"] == "late_hit_rerun", (
        f"已修复的签名再被命中 → 走延迟命中路径；实际 {outcome!r}"
    )
    assert outcome["spawned"] is False, (
        f"延迟命中不新建修复计划；实际 {outcome!r}"
    )

    rows = verification_persistence.read_verification_results(db_path=state_db)
    late = [row for row in rows if row["source"] == "late_hit_rerun"]
    assert [(row["plan_id"], row["vp_id"]) for row in late] == [("p2", "VP-9")], (
        f"延迟命中的重跑结论另起一行（source=late_hit_rerun）；实际 {rows!r}"
    )
    assert len(rows) == 2, (
        f"旧裁决原样保留，只追加；实际 {rows!r}"
    )


def test_a_late_hit_on_an_open_signature_is_not_a_late_hit(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
) -> None:
    """还没修好的签名被命中 → 不走延迟命中路径（那条路要修好的树）。"""
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")

    outcome = lifecycle.handle_late_hit(
        hit_plan_id="p2",
        fingerprint=fingerprint,
        vp_id="VP-9",
        rerun=lambda p, v: "passed",
    )

    assert outcome["action"] == "not_fixed", (
        f"没修好的签名照常派生修复计划；实际 {outcome!r}"
    )


def test_a_production_module_reruns_after_the_merge() -> None:
    """重跑这条链在生产里有接线 —— 不是只有测试在调用。

    生命周期模块必须被一个**生产**模块 import，而且那个模块必须调用
    ``rerun_after_merge``。否则这条链停在「修复合入了」：没有人把受影响的
    验证点放到含修复的树上重跑一遍。
    """
    import re

    backend = Path(__file__).resolve().parents[3]
    pattern = re.compile(
        r"^\s*(?:from\s+framework_fix\.lifecycle\s+import|"
        r"from\s+framework_fix\s+import\s+[^\n]*\blifecycle\b|"
        r"import\s+framework_fix\.lifecycle)",
        re.MULTILINE,
    )
    importers: List[str] = []
    calls = False
    for path in sorted(backend.rglob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if not pattern.search(text):
            continue
        importers.append(path.relative_to(backend).as_posix())
        if "rerun_after_merge" in text:
            calls = True
    assert importers, (
        "没有生产模块 import framework_fix.lifecycle —— 重跑这条链没有生产入口"
    )
    assert calls, (
        "生产模块 import 了生命周期却没有调用 rerun_after_merge —— 链没接上；"
        f"importers: {importers!r}"
    )


# ---------------------------------------------------------------------------
# 入口：延迟命中与这条链的第一行
# ---------------------------------------------------------------------------
def _fingerprint() -> str:
    """这份材料算出来的指纹 —— 注册表与信封用的是同一份材料。"""
    return compute_fingerprint(
        framework_files=["backend/framework_fix/registry.py"],
        error_class="AssertionError",
        first_error_line="AssertionError: expected 1, got 2",
    )


def _envelope(
    *,
    plan_id: str = "p1",
    vp_id: str = "VP-3",
    repo: str = "",
) -> Dict[str, Any]:
    """一份信封 —— 走生产写路径（``build_envelope``）。"""
    hit = FailureHit(
        vp_id=vp_id,
        plan_id=plan_id,
        error_class="AssertionError",
        first_error_line="AssertionError: expected 1, got 2",
        framework_files=["backend/framework_fix/registry.py"],
        acceptance_critical=True,
        out_of_scope_change=False,
    )
    repro = ReproductionResult(
        classification="framework_deterministic",
        attempts=(
            ReproAttempt(
                run=1,
                exit_code=1,
                timed_out=False,
                first_error_line="AssertionError: expected 1, got 2",
                duration_s=1.5,
            ),
            ReproAttempt(
                run=2,
                exit_code=1,
                timed_out=False,
                first_error_line="AssertionError: expected 1, got 2",
                duration_s=1.4,
            ),
        ),
        clean_commit="a" * 40,
        tree_fingerprint="sha256:" + "b" * 64,
    )
    return build_envelope(
        fingerprint=_fingerprint(), hit=hit, repro=repro, repo=repo
    )


def test_a_late_hit_does_not_build_a_second_repair_plan(
    registry: FrameworkFixRegistry,
    lifecycle: FrameworkFixLifecycle,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """已经修好的签名又被撞上 → 不新建修复计划，在含修复的树上重跑。

    延迟命中走的是**同一条路径**：同一个签名、同一个修复计划。这个签名已经
    被修好过一次 —— 再建一个修复计划，等于同一个缺陷有两条修复线，两条线的
    结论互相矛盾。
    """
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-3")
    _hold(lifecycle, fingerprint, plan_id="p1", vp_id="VP-3")
    lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "passed", worktree=fixture_repo["clone"]
    )

    plan_id = spawn(_envelope(plan_id="p2", vp_id="VP-9"))

    assert plan_id == "fx1", (
        f"延迟命中不新建修复计划，返回原来那个；实际 {plan_id!r}"
    )

    rows = verification_persistence.read_verification_results(db_path=state_db)
    late = [row for row in rows if row["source"] == "late_hit_rerun"]
    assert [(row["plan_id"], row["vp_id"]) for row in late] == [("p2", "VP-9")], (
        f"延迟命中的重跑结论另起一行（source=late_hit_rerun）；实际 {rows!r}"
    )


def test_the_first_result_of_the_chain_is_the_initial_one(
    lifecycle: FrameworkFixLifecycle,
    state_db: Path,
) -> None:
    """这条链的第一行结果是 ``initial`` —— 后面的每一次重跑都拿它比。

    没有基线，「修了之后是好了还是换了个样子」就没有可比的起点。基线写在
    **链路开始的那一步**（普通计划命中 → 派生修复计划），落在撞上这个缺陷的
    那个验证点上 —— 那正是后面会被重跑的那一行。
    """
    outcome = lifecycle.handle_fix_plan_hit(
        hit_plan_id="p1",
        fingerprint=_fingerprint(),
        envelope=_envelope(plan_id="p1", vp_id="VP-3"),
        spawn=spawn,
    )
    assert outcome["action"] == "spawned", (
        f"普通计划命中 → 派生修复计划；实际 {outcome!r}"
    )

    rows = verification_persistence.read_verification_results(db_path=state_db)
    initial = [row for row in rows if row["source"] == "initial"]
    assert [(row["plan_id"], row["vp_id"]) for row in initial] == [
        ("p1", "VP-3")
    ], (
        f"这条链的第一行结果落在撞上它的那个验证点上；实际 {rows!r}"
    )


def test_spawning_a_repair_plan_writes_no_verdict(state_db: Path) -> None:
    """派生修复计划这一步不写裁决 —— 它只建计划。

    写裁决是**链路**的事：基线（``initial``）由链路开始的那一步落下，重跑
    结论由合入之后的那一步落下。派生这一步要是顺手写一行「失败」，链上就会
    多出一条没人比对的结论 —— 而且写在了谁都没打算重跑的那个验证点上。
    """
    spawn(_envelope(plan_id="p1", vp_id="VP-3"))

    rows = verification_persistence.read_verification_results(db_path=state_db)
    assert rows == [], (
        f"派生修复计划不写结果行 —— 基线由链路开始的那一步写；实际 {rows!r}"
    )


# ---------------------------------------------------------------------------
# 入口：调度器的那一拍
# ---------------------------------------------------------------------------
class _StaticConfig:
    """一个永远给同一份快照的配置读取器。"""

    def __init__(self, **overrides: Any) -> None:
        from plan_scheduler.config import SchedulerConfig

        self._config = SchedulerConfig(**overrides)

    def current(self) -> Any:
        """返回那份快照。"""
        return self._config


class _RecordingSink:
    """一个只记录、不执行的决策去处。"""

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """记下这一批决策。"""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return batch


def _engine_with_a_merged_repair(state_db: Path, tmp_path: Path) -> Any:
    """一台调度器 —— 库里有一个已经合入的修复计划。

    「合入」在这里就是**修复计划走到终相位**：调度器在那一拍上看见它，
    于是把受影响的验证点放回含修复的树上重跑一遍。返回（引擎, 连接）
    —— 连接归调用方管。
    """
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore
    from state_machine.db.connection import open as open_db

    conn = open_db(state_db)
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, plan_kind, updated_at) VALUES (?, ?, ?, ?)",
        ("fx1", "completed", "framework_fix", "2026-11-25T00:00:00+00:00"),
    )

    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)

    def _clock() -> float:
        return 1_700_000_000.0

    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=_clock)
    ledger = CapacityLedger(store, facts, _clock)
    commands = CommandExecutor(
        store, ledger, object(), facts=facts, conn=conn, clock=_clock
    )
    engine = PlanSchedulerEngine(
        _StaticConfig(
            enabled=True,
            capacity=1,
            min_runtime_seconds=0.0,
            tick_seconds=0.01,
            command_timeout_seconds=10.0,
        ),
        store,
        facts,
        ledger,
        commands,
        _RecordingSink(),
        _clock,
        conn=conn,
    )
    return engine, conn


@pytest.mark.parametrize("patch_target", ["lifecycle", "engine"])
def test_the_merge_path_calls_the_rerun_entry(
    patch_target: str,
    state_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复合入被观测到 → 那一拍上重跑入口被调用了。

    这条链在生产里的入口是**调度器的一拍**：修复计划走到终相位，调度器
    看见它合入了，就把受影响的验证点放回含修复的树上重跑一遍。探针把
    入口换掉，看那一拍上它到底被调用了没有 —— 换在哪一头都算数：生命
    周期模块上的那个名字，和调度器模块上的那个名字。入口在**调用时**才
    解析，所以哪一头被换掉都算数。
    """
    import framework_fix.lifecycle as lifecycle_module
    import plan_scheduler.engine as engine_module

    calls: List[Any] = []

    def _probe(fix_plan_id: str, **kwargs: Any) -> List[Any]:
        calls.append((fix_plan_id, kwargs))
        return []

    if patch_target == "lifecycle":
        monkeypatch.setattr(lifecycle_module, "rerun_after_merge", _probe)
    else:
        monkeypatch.setattr(engine_module, "rerun_after_merge", _probe)

    engine, conn = _engine_with_a_merged_repair(state_db, tmp_path)
    try:
        engine._run_tick()
    finally:
        conn.close()

    assert [plan_id for plan_id, _ in calls] == ["fx1"], (
        f"修复合入的那一拍上没有调用重跑入口；实际 {calls!r}"
    )


def test_the_two_entry_points_record_the_same_baseline_row(
    lifecycle: FrameworkFixLifecycle,
    state_db: Path,
) -> None:
    """两个入口写下的**同一件事**必须长得一样。

    这条链的第一行结果有两个入口：方法（生产路径走它）与模块级入口。它们
    写的是同一行 —— 基线。复现按协议跑在基准 commit 的一份**干净**检出上，
    写库的那一层默认也是干净的，所以 ``clean`` 是 1。模块级入口曾经默认 0：
    同一件事，两个入口给出两个答案，而读的人分不出哪一行是哪条路径写的。
    """
    from framework_fix.lifecycle import record_initial_result

    lifecycle.record_initial_result(
        plan_id="p1",
        vp_id="VP-3",
        fingerprint=_fingerprint(),
        tree_fingerprint="sha256:" + "a" * 64,
    )
    record_initial_result(
        plan_id="p1",
        vp_id="VP-4",
        fingerprint=_fingerprint(),
        tree_fingerprint="sha256:" + "a" * 64,
    )

    rows = verification_persistence.read_verification_results(db_path=state_db)
    initial = [row for row in rows if row["source"] == "initial"]
    assert len(initial) == 2, f"两个入口各写一行基线；实际 {rows!r}"
    assert [row["clean"] for row in initial] == [1, 1], (
        "同一件事，两个入口必须给出同一个答案（复现跑在干净检出上）；"
        f"实际 {initial!r}"
    )


# ---------------------------------------------------------------------------
# 边界条件：挂接多个验证点时，「一轮」还是一轮
# ---------------------------------------------------------------------------
def test_many_attachments_still_get_two_rounds(
    lifecycle: FrameworkFixLifecycle,
    registry: FrameworkFixRegistry,
    state_db: Path,
    fixture_repo: Dict[str, Any],
) -> None:
    """挂接 5 个验证点时，第 1 轮失败之后还有第 2 轮。

    「一轮」是**整轮**的结论 —— 这个指纹挂接的验证点各重跑一次，合起来才
    算一轮。按验证点各写一条失败事件时，挂接 5 个验证点 → 一轮写 5 条 →
    「连续失败轮数」在第一个验证点判完时就顶到上限，规格要求的第 2 轮永远
    轮不到（PRD 验收标准第 4 条与第 8 条：同一签名最多 2 轮，第 2 轮仍失败
    才算耗尽）。
    """
    fingerprint = _register(registry, plan_id="p1", vp_id="VP-0")
    attached = 5
    for index in range(attached):
        _hold(lifecycle, fingerprint, plan_id="p1", vp_id=f"VP-{index}")

    # 合入 → 挂起被放开，计划继续往前走。
    assert lifecycle.release_hits(fingerprint) == attached, (
        "合入放开的是**全部**挂接的验证点"
    )

    first = lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )
    assert len(first) == attached, f"挂接的几个都重跑；实际 {first!r}"
    assert all(row["new_status"] == "failed" for row in first), (
        f"这一轮全没过；实际 {first!r}"
    )

    block = _block(state_db, "p1")
    assert block.get("status") != "framework_fix_exhausted", (
        f"第 1 轮失败只是第 1 轮 —— 上限是 2 轮，挂接几个都一样；实际 {block!r}"
    )
    assert block.get("hold") is True, f"第 1 轮之后重新挂起；实际 {block!r}"

    row = registry.lookup(fingerprint)
    assert row is not None and row["status"] == "reopened", (
        f"第 1 轮失败 → 注册表置 reopened（同一个 plan 继续跑第 2 轮）；实际 {row!r}"
    )

    rows = _attach_rows(state_db)
    assert not [item for item in rows if item["status"] == "exhausted"], (
        f"第 1 轮不该耗尽任何挂接；实际 {rows!r}"
    )

    events = [event["event_type"] for event in _events(state_db)]
    assert events.count("rerun_failed") == 1, (
        f"一轮一条失败事件 —— 挂接几个都算同一轮；实际 {events!r}"
    )

    # 第 2 轮仍失败 → 达到上限 → 耗尽，并升级 HITL。
    second = lifecycle.rerun_after_merge(
        "fx1", rerun=lambda p, v: "failed", worktree=fixture_repo["clone"]
    )
    assert len(second) == attached, f"第 2 轮同样重跑挂接的几个；实际 {second!r}"

    block = _block(state_db, "p1")
    assert block.get("status") == "framework_fix_exhausted", (
        f"第 2 轮仍失败 → 耗尽；实际 {block!r}"
    )
    assert not block.get("hold"), f"耗尽之后不再占着挂起块；实际 {block!r}"

    events = [event["event_type"] for event in _events(state_db)]
    assert events.count("rerun_failed") == 2, (
        f"两轮各留一条；实际 {events!r}"
    )
    assert events.count("exhausted") == 1, (
        f"耗尽只发生一次 —— 挂接几个都不能重复升级 HITL；实际 {events!r}"
    )

    decisions = _decisions(state_db)
    assert len(decisions) == 1, f"耗尽只问人一次；实际 {decisions!r}"
