"""收敛 —— 五个计划同因缺陷：一份修复工作量、五个挂接、零份本地修复。

这个文件钉住什么
----------------
* ``test_converges_to_one_fix`` —— 五个计划（**五个不同的仓**）撞上**同一个
  签名** → 只派生**一个**修复计划；注册表上也只有**一行**。仓不同不影响收敛：
  签名（框架文件集 + 错误类别 + 首错行）才是身份的来源；
* ``test_all_attached`` —— 五个命中方**全部**挂接到那一个修复计划上，每一个
  都带着「我挂在哪个指纹、哪个修复计划上」的块；
* ``test_no_repair_tasks`` —— 五个计划**一份本地修复任务都不生成**。守卫是
  **问模型之前**的短路：模型根本没被问过。两份修复会改同一批文件、互相打架，
  所以「恒为 1 份」的第一半就是这份零；
* ``test_rerun_after_merge`` —— 修复合入之后，每个计划**只重跑命中的那个验证
  点**（不是整轮、不是别的验证点），结果行按树指纹追加，且**没有任何一个计划
  进入耗尽**。

为什么这是集成测试
------------------
收敛这件事横跨四份**跨进程的事实**：注册表行（``framework_fix_registry``）、
挂接行（``framework_fix_attach``）、审计链（``framework_fix_event``）、计划
状态里的挂起块（``plan_routing.framework_fix``），外加修复计划目录本身与修复
生成器读到的计划块。四样都住在状态库里，替身（double）证明不了「读的就是生产
那张库」—— 迁移是真的、查询是真的、块是真的。所以每个用例都对着 ``state_db``
fixture 给的那份真实 SQLite 文件跑。

这个文件证明的到底是什么
------------------------
「收敛」不是一个计数器的自述，而是一条**端到端**的链：五个计划各自撞上同一个
缺陷 → 五次派生请求只落成一个修复计划 → 五个计划全部挂接 → 五个计划都不再本地
修 → 修复合入 → 五个计划各自只重跑自己那一个验证点 → 没人耗尽。链上任何一环
松掉，这四条判据里至少有一条会红。

那一圈账
--------
``_summary`` 把这一轮收敛算成四个数（``fix_plans`` / ``attach`` /
``repair_tasks`` / ``preemptions_after``）—— 验收要的就是这四个数：

* ``fix_plans`` —— 派生出来的修复计划个数（1，不是 5）；
* ``attach`` —— 挂接到它上面的计划数（5）；
* ``repair_tasks`` —— 五个等待计划自己产出的本地修复任务数（0）；
* ``preemptions_after`` —— 修复合入并重跑之后**还停着**的计划数（0）：收敛不
  放大，也不把五个计划永久停在那里。

边界条件
--------
* 五个计划在**五个不同的仓**里 → 仍然只有一个修复计划（签名相同）；
* 修复合入后，五个计划**各只重跑 1 个**验证点 —— 命中那一个；
* 任何一个计划都**不**进入 ``framework_fix_exhausted``。
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
from framework_fix.lifecycle import FrameworkFixLifecycle  # noqa: E402
from framework_fix.registry import FrameworkFixRegistry  # noqa: E402
from framework_fix.repro import ReproAttempt, ReproductionResult  # noqa: E402
from framework_fix.spawner import build_envelope, spawn  # noqa: E402
from repair_generator import RepairTaskGenerator  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)
import verification_persistence  # noqa: E402


#: 撞上同一个缺陷的计划个数。
_PLAN_COUNT = 5

#: 撞上框架缺陷的那个验证点。
_HIT_VP = "VP-1"

#: 同一个计划里**没有**撞上这个缺陷的另一个验证点。它存在的意义是：重跑的范围
#: 必须是「挂接的那一个」，而不是「这条计划的全部验证点」。
_OTHER_VP = "VP-2"

#: 五个计划各自的仓 —— **互不相同**。仓是运营字段（复现排在哪、合并串行到哪），
#: 签名才是身份，所以换仓不换签名就该收敛到同一个修复计划。
_REPOS = [f"repo-{chr(ord('a') + i)}" for i in range(_PLAN_COUNT)]

#: 签名��材料 —— 五个计划**完全一样**。
_FRAMEWORK_FILE = "backend/framework_fix/registry.py"
_ERROR_CLASS = "AssertionError"
_FIRST_ERROR_LINE = "AssertionError: expected 1, got 2"

#: 修复生成器给出的跳过原因 —— 与展示词表里的 ``blocked_on_framework_fix``
#: 同一个词：那是这件事在计划侧**看得见**的样子。
_BLOCKED = "blocked_on_framework_fix"

#: 耗尽之后挂接的计划在 ``framework_fix`` 块里写的状态。
_EXHAUSTED = "framework_fix_exhausted"


class _NoModelCodingTool:
    """一个拒绝回答的 coding tool。

    守卫是**问模型之前**做的判断：一个让模型先答了再断言结果的测试，证明不了
    守卫真的短路了。所以这里的替身一旦被调用就让测试失败。
    """

    def query(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the framework-fix guard must not call the model")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture
def registry(state_db: Path) -> FrameworkFixRegistry:
    """一台注册表，指向 ``state_db`` fixture 给的那份库。"""
    return FrameworkFixRegistry(db_path=state_db)


@pytest.fixture
def lifecycle(registry: FrameworkFixRegistry) -> FrameworkFixLifecycle:
    """一台生命周期 —— 注册表是真的，计划状态按生产路径自己造。

    ``plan_state`` 不给：给了就只有**一个**计划状态，而这里有五个计划各自的
    挂起块。不给的时候，生命周期按 ``<plans>/<plan_id>`` 找计划状态 —— 那正是
    生产路径。
    """
    return FrameworkFixLifecycle(registry=registry)


@pytest.fixture
def converged(
    state_db: Path,
    registry: FrameworkFixRegistry,
    lifecycle: FrameworkFixLifecycle,
    isolated_plans_dir: Path,
) -> Dict[str, Any]:
    """跑一遍「五个计划同因缺陷」的收敛场景，把这一轮的事实交出来。

    顺序就是生产顺序：**依次**由五个计划命中同一个签名 —— 第一个派生修复计划，
    后四个只写挂接。五个计划在五个不同的仓里。
    """
    plan_ids = _plan_ids()
    for plan_id in plan_ids:
        (isolated_plans_dir / plan_id).mkdir(parents=True, exist_ok=True)

    fingerprint = ""
    fingerprints: List[str] = []
    fix_plan_ids: List[str] = []
    envelopes: List[Dict[str, Any]] = []
    for plan_id, repo in zip(plan_ids, _REPOS):
        found = _register(registry, plan_id, repo)
        fingerprints.append(found)
        if not fingerprint:
            fingerprint = found

        # 每个计划都请求一次派生。五次请求、五个不同的仓、**同一个签名**。
        envelope = build_envelope(
            fingerprint=found,
            hit=_hit(plan_id),
            repro=_repro(),
            repo=repo,
        )
        envelopes.append(envelope)
        fix_plan_ids.append(spawn(envelope))

        # 派生之后挂起：这个计划在等那个修复计划。挂的是**它自己撞上的**那个
        # 缺陷 —— 收敛成立时，那就是同一个缺陷。
        lifecycle.hold(
            plan_id=plan_id,
            vp_id=_HIT_VP,
            fingerprint=found,
            round_no=1,
            source="clean_main_repro",
        )

    return {
        "fingerprint": fingerprint,
        "fingerprints": fingerprints,
        "fix_plan_ids": fix_plan_ids,
        "plan_ids": plan_ids,
        "repos": list(_REPOS),
        "envelopes": envelopes,
    }


# ---------------------------------------------------------------------------
# 小助手
# ---------------------------------------------------------------------------
def _plan_ids() -> List[str]:
    """等待修复的那几个计划。"""
    return [f"p{i}" for i in range(_PLAN_COUNT)]


def _hit(plan_id: str) -> FailureHit:
    """一次阻塞命中 —— 五个计划的那次命中**只有计划号不同**。"""
    return FailureHit(
        vp_id=_HIT_VP,
        plan_id=plan_id,
        error_class=_ERROR_CLASS,
        first_error_line=_FIRST_ERROR_LINE,
        framework_files=[_FRAMEWORK_FILE],
        acceptance_critical=True,
        out_of_scope_change=False,
    )


def _repro() -> ReproductionResult:
    """干净 main 上的复现结论：两次、同样地失败。"""
    return ReproductionResult(
        classification="framework_deterministic",
        attempts=(
            ReproAttempt(
                run=1,
                exit_code=1,
                timed_out=False,
                first_error_line=_FIRST_ERROR_LINE,
                duration_s=1.5,
            ),
            ReproAttempt(
                run=2,
                exit_code=1,
                timed_out=False,
                first_error_line=_FIRST_ERROR_LINE,
                duration_s=1.4,
            ),
        ),
        clean_commit="a" * 40,
        tree_fingerprint="sha256:" + "b" * 64,
    )


def _register(
    registry: FrameworkFixRegistry, plan_id: str, repo: str
) -> str:
    """登记一次框架缺陷命中，返回它的指纹。

    走生产写路径（``FrameworkFixRegistry.register_hit``），库是 ``state_db``
    fixture 给的那份 —— 迁移是真的，表是真的。``disposition`` 是
    ``fix_plan``：这是一个**阻塞**命中，它归框架修复车道修，不是「只登记一下
    签名」。
    """
    hit: Dict[str, Any] = {
        "plan_id": plan_id,
        "vp_id": _HIT_VP,
        "framework_files": [_FRAMEWORK_FILE],
        "error_class": _ERROR_CLASS,
        "first_error_line": _FIRST_ERROR_LINE,
        "blocking": True,
        "classification": "framework_deterministic",
        "disposition": "fix_plan",
        "repo": repo,
    }
    return registry.register_hit(hit)


def _query(state_db: Path, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    """查一张表，返回字典行。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _registry_rows(state_db: Path, fingerprint: str) -> List[Dict[str, Any]]:
    """签名表里这个指纹的那一行。"""
    return _query(
        state_db,
        "SELECT fingerprint, fix_plan_id, repo, status FROM framework_fix_registry "
        "WHERE fingerprint = ?",
        (fingerprint,),
    )


def _hit_rows(state_db: Path, fingerprint: str) -> List[Dict[str, Any]]:
    """命中表里这个指纹的每一次命中。"""
    return _query(
        state_db,
        "SELECT plan_id, vp_id, disposition, blocking, classification "
        "FROM framework_fix_hit WHERE fingerprint = ? ORDER BY plan_id",
        (fingerprint,),
    )


def _attach_rows(state_db: Path, fingerprint: str) -> List[Dict[str, Any]]:
    """挂接行 —— 「谁曾经等这个修复」。"""
    return _query(
        state_db,
        "SELECT attach_id, fingerprint, plan_id, vp_id, round, source, status "
        "FROM framework_fix_attach WHERE fingerprint = ? ORDER BY plan_id, vp_id",
        (fingerprint,),
    )


def _fix_plan_rows(state_db: Path) -> List[Dict[str, Any]]:
    """路由表里的框架修复计划。"""
    return _query(
        state_db,
        "SELECT plan_id, plan_kind FROM plan_routing "
        "WHERE plan_kind = 'framework_fix' ORDER BY plan_id",
    )


def _plan_kind(state_db: Path, plan_id: str) -> str:
    """这个计划是普通计划还是框架修复计划。"""
    rows = _query(
        state_db,
        "SELECT plan_kind FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    )
    if not rows or not rows[0].get("plan_kind"):
        return "normal"
    return str(rows[0]["plan_kind"])


def _block(state_db: Path, plan_id: str) -> Dict[str, Any]:
    """计划状态里的 ``framework_fix`` 块（没有行就是空块）。"""
    rows = _query(
        state_db,
        "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    )
    if not rows or not rows[0].get("framework_fix"):
        return {}
    block = json.loads(rows[0]["framework_fix"])
    return block if isinstance(block, dict) else {}


def _repair_task_rows(state_db: Path, plan_id: str) -> List[Dict[str, Any]]:
    """这个计划名下的**本地修复任务**（``task_group`` 以 ``repair`` 开头）。"""
    return _query(
        state_db,
        "SELECT task_id, task_group, failed_vp_id FROM plan_tasks "
        "WHERE plan_id = ? AND task_group LIKE 'repair%' ORDER BY task_id",
        (plan_id,),
    )


def _seed_other_vp(state_db: Path, plan_id: str) -> None:
    """给这个计划再造一个**没有**撞上框架缺陷的验证点。

    它用一条普通任务行表示：验证点是从计划的任务里读出来的，
    ``failed_vp_id`` 是任务与验证点的关联。它不是修复任务（``task_group``
    为空）—— 这个计划没有为它生成过任何修复。
    """
    conn = open_db(state_db)
    try:
        repo = PlanTaskRepository(conn)
        repo.add_task(
            plan_id,
            {"id": f"{plan_id}-task-2", "failed_vp_id": _OTHER_VP},
        )
        conn.commit()
    finally:
        conn.close()


def _still_held(state_db: Path, plan_ids: List[str]) -> List[str]:
    """还停着的计划 —— 挂起块里 ``hold`` 仍为真的那些。"""
    return [
        plan_id
        for plan_id in plan_ids
        if _block(state_db, plan_id).get("hold") is True
    ]


def _stub_content_call(
    monkeypatch: pytest.MonkeyPatch,
) -> List[Dict[str, Any]]:
    """把模型那一次内容调用换成记录器。

    返回记录器收集到的调用列表。一道把所有失败都挡住的守卫必须让这个列表保持
    空 —— 模型根本没被问。
    """
    calls: List[Dict[str, Any]] = []

    def fake(
        self: Any,
        failed_vps: List[Dict[str, Any]],
        round_number: int,
        *args: Any,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        calls.append(
            {"failed_vps": list(failed_vps), "round_number": round_number}
        )
        return [
            {
                "failed_vp_id": str(vp.get("id") or ""),
                "title": f"repair {vp.get('id')}",
                "description": "the guard let this failure through",
                "files_to_modify": ["backend/app.py"],
                "test_command": "pytest backend/tests/test_app.py",
            }
            for vp in failed_vps
        ]

    monkeypatch.setattr(
        RepairTaskGenerator, "generate_repair_contents", fake,
    )
    return calls


def _local_repair_tasks(
    isolated_plans_dir: Path,
    tmp_path: Path,
    plan_ids: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> List[Dict[str, Any]]:
    """让每个等待计划跑一次修复生成器，收回它们产出的本地修复任务。

    走生产写路径（``RepairTaskGenerator.generate``）。指纹不给 —— 让守卫从计划
    自己的挂起块里读，那才是生产里的读法。
    """
    calls = _stub_content_call(monkeypatch)
    produced: List[Dict[str, Any]] = []
    for plan_id in plan_ids:
        generator = RepairTaskGenerator(
            _NoModelCodingTool(), isolated_plans_dir / plan_id, tmp_path
        )
        result = generator.generate(
            plan_id=plan_id,
            failures=[{"vp_id": _HIT_VP}],
        )
        assert result["skipped_reason"] == _BLOCKED, (
            "守卫必须说得出跳过的是哪件事；实际 "
            f"{result['skipped_reason']!r}（plan {plan_id}）"
        )
        produced.extend(result["tasks"])
    assert calls == [], (
        "被挡住的那一轮不该问模型要修复内容 —— 守卫是问模型**之前**的判断；"
        f"实际问了 {len(calls)} 次"
    )
    return produced


def _summary(
    state_db: Path,
    fingerprint: str,
    plan_ids: List[str],
    local_repair_tasks: int,
) -> Dict[str, Any]:
    """这一轮收敛的账 —— 四个数就是验收要的那些数。"""
    return {
        "fix_plans": len(_fix_plan_rows(state_db)),
        "attach": len(_attach_rows(state_db, fingerprint)),
        "repair_tasks": local_repair_tasks,
        "preemptions_after": len(_still_held(state_db, plan_ids)),
    }


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_converges_to_one_fix(
    state_db: Path,
    converged: Dict[str, Any],
    isolated_plans_dir: Path,
) -> None:
    """五个计划（五个不同的仓）同因缺陷 → 只派生一个修复计划。"""
    fingerprint = converged["fingerprint"]
    fix_plan_ids = converged["fix_plan_ids"]
    plan_ids = converged["plan_ids"]
    envelopes = converged["envelopes"]

    # 五次派生请求 → 同一个计划号。
    assert len(set(fix_plan_ids)) == 1, (
        "五个计划撞上同一个签名 → 一次派生；实际派生了 "
        f"{sorted(set(fix_plan_ids))!r}"
    )

    # 路由表里只有一个框架修复计划。
    fix_plans = _fix_plan_rows(state_db)
    assert [row["plan_id"] for row in fix_plans] == fix_plan_ids[:1], (
        f"只该有一个框架修复计划；实际 {fix_plans!r}"
    )

    # 磁盘上也只有一个修复计划目录。
    fix_dirs = sorted(
        entry.name
        for entry in isolated_plans_dir.iterdir()
        if (entry / "framework_fix" / "envelope.json").exists()
    )
    assert fix_dirs == fix_plan_ids[:1], (
        f"只该有一个修复计划目录；实际 {fix_dirs!r}"
    )

    # 签名表上只有一行。
    registry_rows = _registry_rows(state_db, fingerprint)
    assert len(registry_rows) == 1, (
        f"同一个签名在签名表上只有一行；实际 {registry_rows!r}"
    )
    assert registry_rows[0]["fix_plan_id"] == fix_plan_ids[0], (
        "那一行必须指着那个修复计划；实际 "
        f"{registry_rows[0]['fix_plan_id']!r}"
    )

    # 五次命中都在。
    hits = _hit_rows(state_db, fingerprint)
    assert [row["plan_id"] for row in hits] == plan_ids, (
        f"五次命中都要在；实际 {hits!r}"
    )

    # **边界条件**：这五次派生的输入真的在五个不同的仓里 —— 信封是生产函数
    # 的产物，它的 ``repo`` 字段就是那五个不同的值；而五个信封带着**同一个**
    # 指纹。换仓不换签名，所以它们必须收敛到一个修复计划。
    assert len({env["repo"] for env in envelopes}) == _PLAN_COUNT, (
        "五个计划必须真的在五个不同的仓里 —— 否则这个用例证明不了「换仓也"
        f"收敛」；实际 {[env['repo'] for env in envelopes]!r}"
    )
    assert {env["repo"] for env in envelopes} == set(converged["repos"]), (
        "五个信封带着五个不同的仓；实际 "
        f"{[env['repo'] for env in envelopes]!r}"
    )
    assert set(converged["fingerprints"]) == {fingerprint}, (
        "五个不同的仓、同一份签名 → 同一个指纹 —— 仓是运营字段，不是身份；"
        f"实际 {sorted(set(converged['fingerprints']))!r}"
    )
    assert {env["fingerprint"] for env in envelopes} == {fingerprint}, (
        "五个信封必须带同一个指纹 —— 签名才是身份；实际 "
        f"{[env['fingerprint'] for env in envelopes]!r}"
    )
    assert sorted(env["source_plan_id"] for env in envelopes) == sorted(
        plan_ids
    ), (
        "五个信封来自五个不同的计划；实际 "
        f"{[env['source_plan_id'] for env in envelopes]!r}"
    )

    # 仓是**运营**字段：一份修复工作量排在一个仓里（第一次命中那个仓）。
    # 签名表上的那一行记着它 —— 说明它真的走完了生产写路径，而不是被丢掉。
    assert registry_rows[0]["repo"] == converged["repos"][0], (
        "签名表那一行的仓是运营字段（复现排在哪、合并串行到哪）；实际 "
        f"{registry_rows[0]['repo']!r}"
    )
    on_disk = json.loads(
        (
            isolated_plans_dir / fix_plan_ids[0] / "framework_fix" / "envelope.json"
        ).read_text()
    )
    assert on_disk["fingerprint"] == fingerprint, (
        f"落盘的信封带着那个指纹；实际 {on_disk.get('fingerprint')!r}"
    )
    assert on_disk["repo"] == converged["repos"][0], (
        "落盘的信封记着它排队的仓；实际 " f"{on_disk.get('repo')!r}"
    )

    # 等待修复的那五个计划**不是**修复计划 —— 它们是等待方。
    for plan_id in plan_ids:
        assert _plan_kind(state_db, plan_id) == "normal", (
            f"等待修复的计划不该是修复计划；实际 {plan_id} 是 "
            f"{_plan_kind(state_db, plan_id)!r}"
        )


def test_all_attached(
    state_db: Path,
    converged: Dict[str, Any],
) -> None:
    """五个命中方全部挂接到那一个修复计划上。"""
    fingerprint = converged["fingerprint"]
    fix_plan_ids = converged["fix_plan_ids"]
    plan_ids = converged["plan_ids"]

    rows = _attach_rows(state_db, fingerprint)
    assert len(rows) == _PLAN_COUNT, (
        f"五个命中方都要挂接；实际挂了 {len(rows)} 条：{rows!r}"
    )
    assert sorted(row["plan_id"] for row in rows) == sorted(plan_ids), (
        f"挂接的正是这五个计划；实际 {[row['plan_id'] for row in rows]!r}"
    )
    assert {row["vp_id"] for row in rows} == {_HIT_VP}, (
        "挂接的是撞上缺陷的那一个验证点；实际 "
        f"{[row['vp_id'] for row in rows]!r}"
    )
    assert {row["source"] for row in rows} == {"clean_main_repro"}, (
        "挂接来源是那次干净 main 上的复现；实际 "
        f"{[row['source'] for row in rows]!r}"
    )

    # 每个等待计划都带着自己的挂起块，指着同一个指纹、同一个修复计划。
    for plan_id in plan_ids:
        block = _block(state_db, plan_id)
        assert block.get("hold") is True, (
            f"{plan_id} 应当在等那个修复；实际 {block!r}"
        )
        assert block.get("fingerprint") == fingerprint, (
            f"{plan_id} 挂着的指纹不对；实际 {block!r}"
        )
        assert block.get("fix_plan_id") == fix_plan_ids[0], (
            f"{plan_id} 等的修复计划不对；实际 {block!r}"
        )


def test_no_repair_tasks(
    state_db: Path,
    converged: Dict[str, Any],
    isolated_plans_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """五个计划一份本地修复任务都不生成 —— 修复工作量恒为 1 份。"""
    fingerprint = converged["fingerprint"]
    plan_ids = converged["plan_ids"]

    produced = _local_repair_tasks(
        isolated_plans_dir, tmp_path, plan_ids, monkeypatch
    )
    assert produced == [], (
        "框架修复车道已经拥有这个缺陷 → 五个计划都不该再本地修一次；"
        f"实际产出 {produced!r}"
    )

    # 计划任务表里也没有落下的本地修复任务。
    for plan_id in plan_ids:
        rows = _repair_task_rows(state_db, plan_id)
        assert rows == [], (
            f"{plan_id} 名下不该有本地修复任务；实际 {rows!r}"
        )

    summary = _summary(state_db, fingerprint, plan_ids, len(produced))
    assert summary["fix_plans"] == 1, summary
    assert summary["attach"] == _PLAN_COUNT, summary
    assert summary["repair_tasks"] == 0, summary
    # 还在挂着 —— 修复还没合入，五个计划都停着。
    assert summary["preemptions_after"] == _PLAN_COUNT, summary


def test_rerun_after_merge(
    state_db: Path,
    converged: Dict[str, Any],
    lifecycle: FrameworkFixLifecycle,
    fixture_repo: Dict[str, Any],
) -> None:
    """修复合入后，每个计划只重跑命中的那一个验证点，且没人耗尽。"""
    fingerprint = converged["fingerprint"]
    fix_plan_ids = converged["fix_plan_ids"]
    plan_ids = converged["plan_ids"]
    fix_plan_id = fix_plan_ids[0]

    # 每个计划还有**另一个**验证点：它没撞上这个缺陷，所以不该被重跑。
    for plan_id in plan_ids:
        _seed_other_vp(state_db, plan_id)

    # 修复合入 → 挂起被放开，五个计划回到自己的路上。
    assert lifecycle.release_hits(fingerprint) == _PLAN_COUNT, (
        "合入之后五个挂接全部放开"
    )
    assert _still_held(state_db, plan_ids) == [], (
        "合入之后不该还有计划停着"
    )

    calls: List[Any] = []

    def rerun(plan_id: str, vp_id: str) -> str:
        calls.append((plan_id, vp_id))
        return "passed"

    outcomes = lifecycle.rerun_after_merge(
        fix_plan_id, rerun=rerun, worktree=fixture_repo["clone"]
    )

    # 每个计划**恰好**重跑一个验证点 —— 命中那一个。
    assert len(calls) == _PLAN_COUNT, (
        f"每个计划只重跑命中的那一个验证点；实际跑了 {calls!r}"
    )
    assert sorted(calls) == sorted(
        (plan_id, _HIT_VP) for plan_id in plan_ids
    ), (
        f"重跑的正是这五个 (计划, 命中验证点)；实际 {calls!r}"
    )
    assert _OTHER_VP not in {vp for _, vp in calls}, (
        f"没撞上这个缺陷的验证点不该被重跑；实际 {calls!r}"
    )

    assert len(outcomes) == _PLAN_COUNT, (
        f"每个被重跑的验证点一个结论；实际 {outcomes!r}"
    )
    assert all(outcome["new_status"] == "passed" for outcome in outcomes), (
        f"重跑通过；实际 {outcomes!r}"
    )
    assert all(
        str(outcome["tree_fingerprint"]).startswith("sha256:")
        for outcome in outcomes
    ), (
        f"结论必须绑在树指纹上；实际 {outcomes!r}"
    )

    # 结果行按树指纹追加：每个计划一行，正是那个验证点。
    rows = verification_persistence.read_verification_results(db_path=state_db)
    assert sorted((row["plan_id"], row["vp_id"]) for row in rows) == sorted(
        (plan_id, _HIT_VP) for plan_id in plan_ids
    ), (
        f"结果行只落在被重跑的那五个 (计划, 验证点) 上；实际 {rows!r}"
    )

    # 没有任何一个计划进入耗尽。
    for plan_id in plan_ids:
        block = _block(state_db, plan_id)
        assert block.get("status") != _EXHAUSTED, (
            f"{plan_id} 不该进入耗尽；实际 {block!r}"
        )
    events = _query(
        state_db,
        "SELECT event_type FROM framework_fix_event "
        "WHERE event_type = 'exhausted'",
    )
    assert events == [], f"审计链上不该有耗尽事件；实际 {events!r}"

    summary = _summary(state_db, fingerprint, plan_ids, 0)
    assert summary == {
        "fix_plans": 1,
        "attach": _PLAN_COUNT,
        "repair_tasks": 0,
        "preemptions_after": 0,
    }, summary
