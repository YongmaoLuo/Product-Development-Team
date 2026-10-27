"""TDD spec —— SQLite 与 legacy 两条存储路径的**终态等价**。

背景
----
A1′ 的删除（主题外验证点：先归档、后移除）落在**两条**存储路径上：

* **SQLite** —— ``verif_repo`` 已绑定，裁决写进 ``plan_verification`` 的
  ``verdicts`` 列；
* **legacy** —— ``verif_repo is None``，裁决写进计划目录里的状态文件。

两条路径必须**终态一致**：同一份计划、同一个假执行器，跑出来的裁决投影、
归档记录、发号账本必须逐字相同。差一处，就意味着「换一种部署方式，结论就
变了」—— 而移除是**不可逆**的：一条被移除的验证点不会因为存储后端不同而
多活一轮或少活一轮。

两条差分测试
------------
1. ``test_sqlite_and_legacy_terminal_state_equal`` —— 终态差分 **0 处**不
   一致（裁决投影 / 索引桶 / 归档记录 / 移除集 / 发号账本 / 活动集合 /
   重新装载后的投影）。
2. ``test_no_backend_branch`` —— 两路径的**决策轨迹**逐条相同。这条比终态
   比对更严：终态一样也可能是「走了不同的路、碰巧落到同一个结果」。

比较时排除的东西（以及为什么）
------------------------------
* ``ts`` / ``round`` —— 时间戳与轮次号。轮次号住在状态库的
  ``plan_verification.round`` 列上，legacy 路径没有这张表，于是它恒为 0。
  那是**存储事实**，不是判定结果。
* ``pending_vps`` —— legacy 独有的记账字段。SQLite 一侧对应的
  ``runtime_state`` 列恒为 null（执行器从不写它），而执行循环要跑的条目来自
  ``_build_items_from_plan()``，不来自 ``pending_vps``。比它等于比两个不同
  的产物，不是比两种行为。
* 原始存储字节 —— SQLite 那一列是「每条裁决一个 dict 的列表」，legacy 是
  「``{vp_id: verdict}`` 字典」；存储专有字段（``worker_id`` / ``seq``）
  也只有前者有。形状本来就不同，比它没有意义。

测试命令::

    pytest backend/tests/integration/verification_chain/test_storage_path_equivalence.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)
from verification.vp_removal_audit import (  # noqa: E402
    AUDIT_FILENAME,
    load_removed_vp_ids,
)
from verification_executor import (  # noqa: E402
    DEFAULT_STATE_FILENAME,
    VerificationExecutor,
)
from verification_plan_delta import (  # noqa: E402
    load_delta_state,
    save_delta_state,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

#: 本测试用的计划 id。
PLAN_ID = "storage-equivalence-plan"

#: 判据表（编号式）。主题判定拿它当「这个计划到底要什么」的权威。
CRITERIA: List[Dict[str, str]] = [
    {"id": "AC-001", "text": "用户可以用邮箱和密码登录"},
    {"id": "AC-002", "text": "用户可以导出 CSV 报表"},
]

#: 主题外的那条：它声明的 ``AC-NOT-IN-PRD`` 不在本计划的判据表里。
OFF_TOPIC_VP = "VP-900"

#: 孤儿的那条：计划里有它，但它没有声明任何判据。
ORPHAN_VP = "VP-902"

#: 计划没有定义的那条（上一轮遗留的裁决）—— 两条路径都要把它隔离掉。
UNDECLARED_VP = "VP-777"

#: 判失败的那条：失败与通过必须落在**不同的桶**里，桶错了也算终态不一致。
FAILED_VP = "VP-903"

#: 两条路径共用同一份发号账本 —— 计划里这四条验证点的号都是发出去的。
LEDGER: List[str] = ["VP-900", "VP-901", "VP-902", "VP-903"]

#: 两种存储后端。
MODES = ("sqlite", "legacy")


# ---------------------------------------------------------------------------
# 计划与执行器
# ---------------------------------------------------------------------------


def _normal_point(vp_id: str, criteria: List[str]) -> Dict[str, Any]:
    """一条正常的验证点：声明的判据在本计划的判据表里。"""
    return {
        "id": vp_id,
        "title": f"{vp_id} 验的是计划里的一条判据",
        "related_prd_criteria": list(criteria),
        "verification_method": "api_test",
        "verification_phase": 1,
        "priority": "medium",
        "expected_result": "这条验证点验的是本计划要的判据",
    }


def _plan() -> Dict[str, Any]:
    """四条验证点：主题外 1 + 正常 2 + 孤儿 1。"""
    return {
        "plan_id": PLAN_ID,
        "criteria": [dict(c) for c in CRITERIA],
        "verification_points": [
            {
                "id": OFF_TOPIC_VP,
                "title": "主题外验证点：引用的验收标准不在本计划的判据表里",
                "related_prd_criteria": ["AC-NOT-IN-PRD"],
                "verification_method": "api_test",
                "verification_phase": 1,
                "priority": "medium",
                "expected_result": "主题外的验证点不该被跑，也不该进报告",
            },
            _normal_point("VP-901", ["AC-001"]),
            {
                "id": ORPHAN_VP,
                "title": "孤儿验证点：没有任何判据支撑它",
                "related_prd_criteria": [],
                "verification_method": "api_test",
                "verification_phase": 1,
                "priority": "low",
                "expected_result": "没有判据支撑的验证点按「相关」处理",
            },
            _normal_point(FAILED_VP, ["AC-002"]),
        ],
    }


def _runner(ran: List[str]):
    """一个只记账的假执行器：跑过的验证点 id 记在 ``ran`` 里。

    其中一条判失败 —— 成功与失败必须落在不同的桶里，桶错了也算终态不一致。
    """

    async def _run(vp_node: Dict[str, Any]) -> Dict[str, Any]:
        ran.append(vp_node["id"])
        status = "FAILED" if vp_node["id"] == FAILED_VP else "PASSED"
        return {"status": status, "reasons": ["ok"], "evidence": {}}

    return _run


def _seed_orphan(plan_dir: Path, repo: Optional[VerificationRepository]) -> None:
    """在存储里放一条**计划没有定义**的裁决（上一轮的遗留）。

    SQLite 侧写进 ``verdicts`` 列，legacy 侧写进状态文件 —— 形状不同，
    位置不同，但**事实**相同：存储里有这么一条结论，计划里没有这条验证点。
    """
    payload = {
        "vp_id": UNDECLARED_VP,
        "status": "FAILED",
        "reasons": ["boom"],
        "evidence": {},
    }
    if repo is not None:
        repo.append_verdict(PLAN_ID, dict(payload))
        repo._conn.commit()  # noqa: SLF001 - 测试直接落库
        return
    (plan_dir / DEFAULT_STATE_FILENAME).write_text(
        json.dumps(
            {
                "plan_id": PLAN_ID,
                "pending_vps": [
                    str(vp["id"]) for vp in _plan()["verification_points"]
                ],
                "verdicts": {UNDECLARED_VP: dict(payload)},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _make_executor(
    plan_dir: Path,
    repo: Optional[VerificationRepository],
    ran: List[str],
    executor_cls=VerificationExecutor,
):
    """按给定存储后端造一个执行器。"""
    return executor_cls(
        verification_plan=_plan(),
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=_runner(ran),
        verif_repo=repo,
    )


# ---------------------------------------------------------------------------
# 快照：两条路径跑完之后各取一份可比对的快照
# ---------------------------------------------------------------------------


def _projection(executor) -> Dict[str, Any]:
    """裁决投影 —— 归一化到「这条验证点的结论是什么」。

    只留结论本身：``status`` / ``reasons`` / ``evidence`` / 定义指纹。
    存储专有字段（SQLite 那一列的 ``worker_id`` / ``seq``）不比 —— 那是两种
    **产物**，不是两种**行为**。
    """
    out: Dict[str, Any] = {}
    for entry in executor.collect_verdicts():
        out[str(entry["vp_id"])] = {
            "status": entry.get("status"),
            "reasons": list(entry.get("reasons") or []),
            "evidence": entry.get("evidence") or {},
            "definition_fingerprint": entry.get("definition_fingerprint", ""),
        }
    return out


def _audit_rows(plan_dir: Path) -> List[Dict[str, Any]]:
    """归档记录 —— 去掉时间戳与轮次号之后的内容。"""
    path = plan_dir / AUDIT_FILENAME
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in ("ts", "round")
                }
            )
    return rows


def _active_ids(executor) -> List[str]:
    """活动集合 —— 这一轮**会跑**的验证点（计划里的，不是已移除的）。"""
    return sorted(str(vp.get("id")) for vp in executor._build_items_from_plan())


def _snapshot(root: Path, mode: str, executor_cls=VerificationExecutor) -> Dict[str, Any]:
    """在 ``root`` 下按指定存储后端跑一轮，返回可比对的终态快照。"""
    plan_dir = root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    repo: Optional[VerificationRepository] = None
    if mode == "sqlite":
        conn = open_db(root / "state.db")
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(PLAN_ID, verification_status="running")
        conn.commit()

    # 发号账本：两条路径读同一份、写同一份。
    save_delta_state(
        plan_dir,
        {"seen_task_ids": [], "round": 1, "allocated_vp_ids": list(LEDGER)},
    )
    # 上一轮遗留的孤儿裁决：两条路径的存储里各有一份。
    _seed_orphan(plan_dir, repo)

    ran: List[str] = []
    executor = _make_executor(plan_dir, repo, ran, executor_cls)
    asyncio.run(executor.run(max_parallel=1))

    # 重新装载：同一个目录上造一个新的执行器，看**落盘的东西**能不能还原出
    # 同一份投影。这比「内存里的两份一样」更严 —— 它证明两种存储产物是
    # 可以互换的。
    rehydrated = _make_executor(plan_dir, repo, [], executor_cls)

    return {
        "verdicts": _projection(executor),
        "rehydrated": _projection(rehydrated),
        "buckets": {
            "completed": sorted(executor.completed_vps),
            "failed": sorted(executor.failed_vps),
            "skipped": sorted(executor.skipped_vps),
        },
        "audit": _audit_rows(plan_dir),
        "removed": sorted(load_removed_vp_ids(plan_dir)),
        "allocated_vp_ids": list(
            load_delta_state(plan_dir).get("allocated_vp_ids") or []
        ),
        "executed": sorted(ran),
        "active": _active_ids(executor),
    }


def _diff(left: Any, right: Any, path: str = "") -> List[str]:
    """两份快照的逐项差异（可读）。"""
    if isinstance(left, dict) and isinstance(right, dict):
        out: List[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left:
                out.append(f"{child}: 仅右侧有 {right[key]!r}")
            elif key not in right:
                out.append(f"{child}: 仅左侧有 {left[key]!r}")
            else:
                out.extend(_diff(left[key], right[key], child))
        return out
    if left != right:
        return [f"{path}: {left!r} != {right!r}"]
    return []


# ---------------------------------------------------------------------------
# 1. 终态等价
# ---------------------------------------------------------------------------


def test_sqlite_and_legacy_terminal_state_equal(tmp_path: Path, reclaim_workers):
    """两条存储路径的终态差分 **0 处**不一致。

    跑的是同一份计划、同一个假执行器，唯一不同的是``verif_repo`` 绑没绑。
    落盘位置不同是允许的；**落出来的结论**必须一样：

    * 裁决投影 —— 哪些验证点有结论、结论是什么；
    * 索引桶 —— 通过 / 失败 / 跳过各是哪些；
    * 归档记录 —— 移除了谁、依据是什么（去掉时间戳与轮次号）；
    * 移除集 —— 谁被移除了；
    * 发号账本 —— 哪些号已经发出去过；
    * 活动集合 —— 下一轮还会跑哪些；
    * 重新装载后的投影 —— 两种存储产物能不能还原出同一份结论。
    """
    try:
        sqlite_state = _snapshot(tmp_path / "sqlite", "sqlite")
        legacy_state = _snapshot(tmp_path / "legacy", "legacy")
    finally:
        alive = reclaim_workers()

    assert not alive, f"后台 worker 没收干净: {alive!r}"

    differences = _diff(sqlite_state, legacy_state)
    assert not differences, (
        "两条存储路径的终态不一致（sqlite vs legacy）:\n"
        + "\n".join(differences)
    )

    # 差分 0 处还不够 —— 得确认这份快照**不是空的**。空快照与空快照永远相等。
    assert sqlite_state["verdicts"], sqlite_state
    assert sqlite_state["removed"] == [OFF_TOPIC_VP], sqlite_state["removed"]
    assert sqlite_state["audit"], sqlite_state["audit"]
    assert sqlite_state["executed"] == ["VP-901", "VP-902", "VP-903"], (
        sqlite_state["executed"]
    )
    assert sqlite_state["buckets"]["failed"] == [FAILED_VP], sqlite_state["buckets"]
    assert UNDECLARED_VP not in sqlite_state["verdicts"], sqlite_state["verdicts"]


# ---------------------------------------------------------------------------
# 2. 没有按后端分叉的行为
# ---------------------------------------------------------------------------


class _TracingExecutor(VerificationExecutor):
    """记录**判定**轨迹的执行器。

    记的是「决定了什么」（装载了哪些结论、隔离了哪些孤儿、移除了哪些验证点、
    这一轮会跑哪些、产出了哪些裁决），不是「存在哪里」—— 后者两条路径本来
    就不同，比它没有意义。

    轨迹里的每一条都必须**逐条相同**：只要有一条不同，就说明有一条判定被
    ``verif_repo`` 分叉了。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.trace: List[Tuple[Any, ...]] = []
        super().__init__(*args, **kwargs)

    def _hydrate_verdicts(self) -> None:
        super()._hydrate_verdicts()
        self.trace.append(("hydrate", tuple(sorted(self._verdicts))))

    def _quarantine_orphan_verdicts(self) -> None:
        before = set(self._verdicts)
        super()._quarantine_orphan_verdicts()
        dropped = tuple(sorted(before - set(self._verdicts)))
        # 只记**效果**，不记调用次数：两条路径装载状态的时机不同（SQLite 侧
        # 总是有一行，legacy 侧可能还没有状态文件），但效果必须一样。
        if dropped:
            self.trace.append(("quarantine", dropped))

    def _apply_topic_relevance(self):
        removed = super()._apply_topic_relevance()
        self.trace.append(("removed", tuple(sorted(removed))))
        return removed

    def _build_items_from_plan(self):
        items = super()._build_items_from_plan()
        self.trace.append(
            ("active", tuple(str(vp.get("id")) for vp in items))
        )
        return items

    def _record_result(self, item_id: str, result: Dict[str, Any]) -> None:
        self.trace.append(("verdict", str(item_id), str(result.get("status"))))
        super()._record_result(item_id, result)


def _trace(root: Path, mode: str) -> List[Tuple[Any, ...]]:
    """跑一轮，返回判定轨迹。"""
    plan_dir = root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    repo: Optional[VerificationRepository] = None
    if mode == "sqlite":
        conn = open_db(root / "state.db")
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(PLAN_ID, verification_status="running")
        conn.commit()

    save_delta_state(
        plan_dir,
        {"seen_task_ids": [], "round": 1, "allocated_vp_ids": list(LEDGER)},
    )
    _seed_orphan(plan_dir, repo)

    ran: List[str] = []
    executor = _make_executor(plan_dir, repo, ran, _TracingExecutor)
    asyncio.run(executor.run(max_parallel=1))
    return list(executor.trace)


def test_no_backend_branch(tmp_path: Path, reclaim_workers):
    """两路径的判定轨迹逐条相同 —— 没有按 ``verif_repo`` 分叉的行为。

    终态比对（上一条）有一个盲区：两条路径可能**走了不同的路**、碰巧落到
    同一个结果。这条测试堵住那个盲区 —— 它比的是**每一步的判定**：

    * 装载了哪些结论；
    * 隔离了哪些孤儿；
    * 移除了哪些验证点；
    * 这一轮会跑哪些验证点；
    * 产出了哪些裁决、什么状态。

    只要有一条判定被 ``verif_repo`` 分叉了，轨迹就会差一条 —— 而差一条就
    足以让两条路径在某一天给出不同的结论。
    """
    try:
        sqlite_trace = _trace(tmp_path / "sqlite", "sqlite")
        legacy_trace = _trace(tmp_path / "legacy", "legacy")
    finally:
        alive = reclaim_workers()

    assert not alive, f"后台 worker 没收干净: {alive!r}"

    assert sqlite_trace == legacy_trace, (
        "两条存储路径的判定轨迹不一致（sqlite vs legacy）:\n"
        f"sqlite: {sqlite_trace!r}\n"
        f"legacy: {legacy_trace!r}"
    )

    # 轨迹非空，且钉住了关键判定 —— 空轨迹与空轨迹永远相等。
    assert sqlite_trace, "判定轨迹是空的 —— 这条测试什么也没钉住"
    assert ("quarantine", (UNDECLARED_VP,)) in sqlite_trace, sqlite_trace
    assert ("removed", (OFF_TOPIC_VP,)) in sqlite_trace, sqlite_trace
    assert (
        "active",
        ("VP-901", "VP-902", "VP-903"),
    ) in sqlite_trace, sqlite_trace
    assert ("verdict", FAILED_VP, "FAILED") in sqlite_trace, sqlite_trace
    assert ("verdict", "VP-901", "PASSED") in sqlite_trace, sqlite_trace
