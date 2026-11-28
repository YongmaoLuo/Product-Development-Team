"""抢占暂停的落盘与重启恢复。

这个文件钉住什么
----------------
* ``test_pause_persisted`` —— 写入后读回同一个理由；
* ``test_restart_restores`` —— **新实例**读回同一个理由，时间戳不丢；
* ``test_resume_clears`` —— 手动 resume 清空原因；
* ``test_bad_reason_rejected`` —— 闭集外的理由词被拒绝，一个字节都不落盘。

边界条件
--------
* 命令层写下的正是持久层读到的：``pause`` 命令带原因 → 落盘 → 读回；
* 来源分得清：操作员按的暂停是 ``operator``，框架抢占的是
  ``framework_fix_preemption``；
* 已恢复的计划不再被重复暂停 —— resume 之后没有残留的意图；
* 暂停意图的时间戳在重启之后不丢：读回来的 ``paused_at`` 与写下去的一样。

为什么这是集成测试
------------------
判据要跨两样东西：命令层（``plan_scheduler.commands``）与持久层
（``pause_state``）。只测其中一样，证明不了「命令写下的就是重启读到的」
—— 而那正是这条修复要保证的事：被抢占的计划重启之后仍然是被暂停的，
不会因为内存里的东西没了就自己跑起来。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import pause_state  # noqa: E402
from plan_scheduler.capacity import CapacityLedger  # noqa: E402
from plan_scheduler.commands import APPLIED, CommandExecutor  # noqa: E402
from plan_scheduler.facts import PlanFactsReader  # noqa: E402
from plan_scheduler.store import SqliteSchedulerStore  # noqa: E402


#: 每一个用例都从这一刻开始 —— 时钟是注入的，答案不该取决于用例什么时候跑。
BASE_EPOCH = 7000.0

#: 被抢占的那个计划。
PLAN_ID = "p2"

#: 框架抢占的理由词 —— 调度器的原因闭集里的那一个。
PREEMPTED = "preempted_by_framework_fix"

#: 操作员按的暂停 —— 闭集里的另一个词。
OPERATOR = "paused"


# ---------------------------------------------------------------------------
# 入参
# ---------------------------------------------------------------------------
class _Result:
    """生命周期门面的答案 —— 这一组用例只关心「成没成」。"""

    def __init__(self, ok: bool = True, detail: str = "ok") -> None:
        self.ok = ok
        self.detail = detail


class _Lifecycle:
    """命令执行器要的生命周期门面。

    真的门面会去动相位；这一组用例要证明的是**暂停意图的落盘与恢复**，
    相位怎么动是别的用例的事。所以这里只把请求记下来，并按请求给出答案。
    """

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def pause_plan(self, plan_id: str) -> _Result:
        """停一个计划。"""
        self.calls.append(("pause", plan_id))
        return _Result(True, "paused")

    def requeue_plan(self, plan_id: str) -> _Result:
        """把一个停住的计划放回等待相位。"""
        self.calls.append(("requeue", plan_id))
        return _Result(True, "requeued")


def _plan_dir(plans_root: Path, plan_id: str) -> Path:
    """造一个最小的计划目录 —— 事实读得到它，它才算一个计划。"""
    directory = plans_root / plan_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "prd.json").write_text(
        json.dumps({"prd": "framework fix"}), encoding="utf-8"
    )
    (directory / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    return directory


def _route(conn: sqlite3.Connection, plan_id: str, phase: str = "queued") -> None:
    """写下路由行：这个计划此刻在排队，调度器认得它。"""
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, plan_kind, framework_fix, updated_at) "
        "VALUES (?, ?, 'framework_fix', ?, ?)",
        (
            plan_id,
            phase,
            json.dumps({"repo": "app"}),
            "2026-11-24T00:00:00Z",
        ),
    )
    conn.commit()


def _open(path: Path) -> sqlite3.Connection:
    """打开这份状态库。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _executor(
    conn: sqlite3.Connection, plans_root: Path, lifecycle: _Lifecycle
) -> CommandExecutor:
    """造一个真的命令执行器 —— 真的库、真的事实、真的账本。"""
    clock = lambda: BASE_EPOCH  # noqa: E731 —— 时钟是注入的
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    return CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=2,
        min_runtime_seconds=600.0,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_pause_persisted() -> None:
    """写入后可读回 —— 写下去的理由就是读回来的理由。

    这是落盘本身：读回来的不是内存里的那份，是文件里的那份。
    """
    written = pause_state.write_pause(PLAN_ID, PREEMPTED)

    assert set(written) == {"plan_id", "reason", "persisted"}, (
        f"写下去的答案就是这三个键：{written!r}"
    )
    assert written["plan_id"] == PLAN_ID, (
        f"写下去的是哪个计划：{written!r}"
    )
    assert written["reason"] == PREEMPTED, (
        f"写下去的理由词：{written!r}"
    )
    assert written["persisted"] is True, (
        f"写下去的东西是落盘的：{written!r}"
    )

    read = pause_state.read_pause(PLAN_ID)
    assert read is not None, "刚写下的暂停意图读不回来"
    assert read["reason"] == PREEMPTED, (
        f"读回来的理由词与写下去的不一样：{read!r}"
    )
    assert read["plan_id"] == PLAN_ID, (
        f"读回来的计划与写下去的不一样：{read!r}"
    )


def test_restart_restores() -> None:
    """新实例读回原因 —— 重启之后计划仍然是被暂停的。

    「重启」在这里是**真的**：一个全新的存储层实例，读的是同一棵目录，
    里面没有任何内存里的东西。读回来的理由与时间戳都与写下去的一样。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    pause_state.write_pause(PLAN_ID, PREEMPTED)

    # 重启之前先读一次：时间戳在这一刻就被钉住了。
    before = pause_state.read_pause(PLAN_ID)
    assert before is not None, "刚写下的暂停意图读不回来"

    # 一个新实例 —— 与写下它的那一个毫无关系的对象。
    restarted = pause_state.PauseIntentStore(plans_root)
    read = restarted.read_pause(PLAN_ID)

    assert read is not None, (
        "重启之后读不回暂停意图 —— 计划会自己跑起来，与调度判断不一致"
    )
    assert read["reason"] == PREEMPTED, (
        f"重启之后读回来的理由词变了：{read!r}"
    )
    assert read["paused_at"] == before["paused_at"], (
        "重启之后时间戳丢了 —— 暂停了多久就再也说不清了；"
        f"写的是 {before['paused_at']!r}，读的是 {read.get('paused_at')!r}"
    )
    assert read["paused_at"], "暂停意图必须带着自己的时间戳"


def test_resume_clears(state_db: Path) -> None:
    """resume 清空 —— 恢复过的计划不再带着暂停的意图。

    意图留着，重启恢复扫描就会把这个已经恢复的计划再暂停一次。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    _plan_dir(plans_root, PLAN_ID)
    conn = _open(state_db)
    _route(conn, PLAN_ID)
    executor = _executor(conn, plans_root, _Lifecycle())

    paused = executor.pause(PLAN_ID, reason=PREEMPTED)
    assert paused.outcome == APPLIED, (
        f"暂停应当被执行：{paused!r}"
    )
    assert pause_state.read_pause(PLAN_ID) is not None, (
        "暂停之后读不回来意图 —— 落盘这一步没发生"
    )

    resumed = executor.resume(PLAN_ID)
    assert resumed.outcome == APPLIED, (
        f"恢复应当被执行：{resumed!r}"
    )
    assert pause_state.read_pause(PLAN_ID) is None, (
        "恢复之后意图还在 —— 已恢复的计划会被再暂停一次"
    )
    conn.close()


def test_bad_reason_rejected() -> None:
    """闭集外的理由词被拒绝 —— 一个字节都不落盘。

    理由词是闭集里的词：面板、dry run 与 API 画的都是同一批词。一个闭集外
    的词写进去，读的人就画不出来 —— 所以它连落盘都不该落。
    """
    with pytest.raises(pause_state.PauseReasonError):
        pause_state.write_pause(PLAN_ID, "因为我想让它停")

    assert pause_state.read_pause(PLAN_ID) is None, (
        "被拒绝的理由词还是落了盘 —— 校验失败就是什么都没发生"
    )


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------
def test_pause_command_persists_the_reason(state_db: Path) -> None:
    """命令层写下的正是持久层读到的。

    抢占的理由词是 tick 给的（任务 26 的 tick 抢占），命令把它落盘，
    落盘的那一份就是重启之后读得到的那一份。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    _plan_dir(plans_root, PLAN_ID)
    conn = _open(state_db)
    _route(conn, PLAN_ID)
    executor = _executor(conn, plans_root, _Lifecycle())

    result = executor.pause(PLAN_ID, reason=PREEMPTED)
    assert result.outcome == APPLIED, (
        f"暂停应当被执行：{result!r}"
    )

    read = pause_state.read_pause(PLAN_ID)
    assert read is not None, "命令暂停了计划，却什么也没落盘"
    assert read["reason"] == PREEMPTED, (
        f"落盘的理由词与命令给的不一样：{read!r}"
    )
    assert read["source"] == pause_state.FRAMEWORK_FIX_SOURCE, (
        f"框架抢占的来源要说得清：{read!r}"
    )
    conn.close()


def test_operator_pause_records_the_operator_source(state_db: Path) -> None:
    """操作员按的暂停，来源是操作员 —— 不是框架抢占。"""
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    _plan_dir(plans_root, PLAN_ID)
    conn = _open(state_db)
    _route(conn, PLAN_ID)
    executor = _executor(conn, plans_root, _Lifecycle())

    result = executor.pause(PLAN_ID)
    assert result.outcome == APPLIED, (
        f"暂停应当被执行：{result!r}"
    )

    read = pause_state.read_pause(PLAN_ID)
    assert read is not None, "命令暂停了计划，却什么也没落盘"
    assert read["reason"] == OPERATOR, (
        f"没有给理由词的暂停用的是闭集里的「暂停」：{read!r}"
    )
    assert read["source"] == pause_state.OPERATOR_SOURCE, (
        f"来源是操作员：{read!r}"
    )
    conn.close()


def test_bad_reason_is_refused_by_the_command(state_db: Path) -> None:
    """命令层也拒绝闭集外的理由词 —— 计划都不停，什么都不写。

    一个写不下去的暂停意图等于没有暂停意图：重启之后计划会跑起来，而
    调度器以为它停着。所以校验不过的命令整个不做。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    _plan_dir(plans_root, PLAN_ID)
    conn = _open(state_db)
    _route(conn, PLAN_ID)
    lifecycle = _Lifecycle()
    executor = _executor(conn, plans_root, lifecycle)

    result = executor.pause(PLAN_ID, reason="because_i_said_so")
    assert result.outcome != APPLIED, (
        f"闭集外的理由词不该被执行：{result!r}"
    )
    assert pause_state.read_pause(PLAN_ID) is None, (
        "被拒绝的命令还是落了盘"
    )
    assert lifecycle.calls == [], (
        f"被拒绝的命令不该去动计划：{lifecycle.calls!r}"
    )
    conn.close()


def test_a_resumed_plan_can_be_paused_again(state_db: Path) -> None:
    """已恢复的计划不再被重复暂停 —— 但它还能被重新暂停一次。

    「不再被重复暂停」说的是**残留的意图**不能再去暂停一个已经恢复的
    计划；它不是「这个计划以后不能被暂停」。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    _plan_dir(plans_root, PLAN_ID)
    conn = _open(state_db)
    _route(conn, PLAN_ID)
    executor = _executor(conn, plans_root, _Lifecycle())

    executor.pause(PLAN_ID, reason=PREEMPTED)
    executor.resume(PLAN_ID)
    assert pause_state.read_pause(PLAN_ID) is None, "恢复之后意图还在"

    again = executor.pause(PLAN_ID, reason=PREEMPTED)
    assert again.outcome == APPLIED, (
        f"重新暂停应当被执行：{again!r}"
    )
    read = pause_state.read_pause(PLAN_ID)
    assert read is not None and read["reason"] == PREEMPTED, (
        f"重新暂停之后读不回意图：{read!r}"
    )
    conn.close()


def test_the_intent_is_found_by_the_restart_scan() -> None:
    """重启恢复扫描能找到这份意图 —— 它不是只有自己知道的秘密。"""
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    _plan_dir(plans_root, PLAN_ID)
    pause_state.write_pause(PLAN_ID, PREEMPTED)

    found = pause_state.scan_pauses(root=plans_root)
    mine = [entry for entry in found if entry.get("plan_id") == PLAN_ID]

    assert len(mine) == 1, (
        f"重启恢复扫描应当找到这一条意图：{found!r}"
    )
    assert mine[0]["reason"] == PREEMPTED, (
        f"扫出来的意图带着别的理由：{mine[0]!r}"
    )
