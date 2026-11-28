"""调度 tick 的框架修复准入、抢占与死锁升级。

这个文件钉住什么
----------------
* ``test_tick_starts_fix`` —— 有空槽时，框架修复计划**立刻**取得执行路径；
* ``test_tick_preempts`` —— 没有空槽时，抢占一个正在运行的普通计划
  （被抢占的是**优先值最高的那一个**）；
* ``test_no_victim_waits`` —— 既没有空槽也没有可抢占的人时**问账本**
  （``test_tick_asks_the_ledger_for_an_oversubscribed_slot``）：账本给了
  超额槽位就启动，不给就**等**（``waiting_for_slot``），**不自己长容量**；
* ``test_deadlock_escalates`` —— 等过了十分钟 → 原因词
  ``scheduler_deadlock_suspected``，并发一条通知事件；
* ``test_deadlock_escalates_to_hitl`` —— 同一刻还要**升级 HITL**：光贴一条
  墙上的消息不算升级，决定才需要人回答。

边界条件
--------
* 同一仓已经有一个框架修复计划在跑 → 后来的那个等；
* 框架修复计划**永不被选为受害者** —— 两个修复计划互相抢占会活锁；
* 账本说不行 → 等，理由词是账本给的那个；
* 空闲 tick（没有框架修复计划）→ 与改动前一致：什么都不启动、不抢占、不通知；
* tick 说出的每一个原因词都在调度器的原因闭集 ``plan_scheduler.policy.REASONS``
  里 —— 面板、dry run 与 API 画的都是同一批词。

为什么这是集成测试
------------------
tick 的判据要跨三样东西：调度器的原因闭集、框架修复 lane 的抢占顺序
（``framework_fix.scheduling``）与 tick 自己的接线（``plan_scheduler.engine``）。
三样各写一份、各测一份，就有可能各说各话 —— 替身证明不了「三样说的是同一件
事」，所以每个用例都对着真的模块跑。

时钟是**注入**的，不是 monkeypatch 出来的：``time.time`` 属于整个进程，一个
用例挪了它就把它挪给了同进程里的每一个线程。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework_fix.scheduling import (  # noqa: E402
    DEADLOCK_THRESHOLD_SECONDS,
    detect_deadlock,
)
from hitl.service import list_decisions  # noqa: E402
from plan_scheduler.capacity import CapacityLedger  # noqa: E402
from plan_scheduler.commands import CommandExecutor  # noqa: E402
from plan_scheduler.config import SchedulerConfig  # noqa: E402
from plan_scheduler.engine import PlanSchedulerEngine, tick  # noqa: E402
from plan_scheduler.facts import PlanFactsReader  # noqa: E402
from plan_scheduler.policy import REASONS  # noqa: E402
from plan_scheduler.store import SqliteSchedulerStore  # noqa: E402


#: 每一个用例都从这一刻开始 —— 时钟是注入的，答案不该取决于用例什么时候跑。
BASE_EPOCH = 7000.0

#: 队列里的框架修复计划修的仓。
REPO = "app"

#: 死锁的阈值 —— 等了这么久还没有执行路径就算死锁。
DEADLOCK_AFTER = DEADLOCK_THRESHOLD_SECONDS


# ---------------------------------------------------------------------------
# 入参
# ---------------------------------------------------------------------------
def _fix(plan_id: str = "fx1", **overrides: Any) -> Dict[str, Any]:
    """队列里的一个框架修复计划。"""
    entry: Dict[str, Any] = {
        "plan_id": plan_id,
        "plan_kind": "framework_fix",
        "repo": REPO,
    }
    entry.update(overrides)
    return entry


def _ordinary(plan_id: str, **overrides: Any) -> Dict[str, Any]:
    """一个正在运行的普通计划。"""
    entry: Dict[str, Any] = {
        "plan_id": plan_id,
        "plan_kind": "normal",
        "elapsed_s": 100.0,
    }
    entry.update(overrides)
    return entry


def _collector() -> Any:
    """一个通知出口 —— 把发出去的事件收下来。"""
    events: List[Any] = []

    def _notify(event: str, payload: Dict[str, Any]) -> None:
        events.append((event, payload))

    _notify.events = events  # type: ignore[attr-defined]
    return _notify


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_tick_starts_fix() -> None:
    """有空槽时，框架修复计划立刻取得执行路径。

    框架修复是硬需求：修复计划创建后**立即**取得执行路径，不排在普通计划
    后面。有一个空槽时它就进去 —— 不抢占谁，也不等下一 tick。
    """
    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1")],
        running=[],
        capacity=2,
        used=0,
    )

    assert outcome["started"] == ["fx1"], (
        "有空槽时修复计划必须立刻取得执行路径；实际 "
        f"{outcome['started']!r}"
    )
    assert outcome["preempted"] == [], (
        "有空槽就不抢谁 —— 抢占是容量不够时的手段，不是常规手段；实际 "
        f"{outcome['preempted']!r}"
    )
    assert outcome["reasons"] == {}, (
        "拿到了执行路径的计划没有被暂停，所以它没有暂停原因；实际 "
        f"{outcome['reasons']!r}"
    )


def test_tick_preempts() -> None:
    """没有空槽时，抢占一个正在运行的普通计划。

    被抢占者是**优先值最高的那一个** —— 队列按优先值升序站队，数字最大的
    那个站在队尾，是调度器最捨得打断的（``framework_fix.scheduling`` 的顺序：
    优先值降序 → 挂起的阻塞工作数升序 → 已跑时长升序 → 计划号升序）。
    """
    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1")],
        running=[
            _ordinary("p1", priority=5),
            _ordinary("p2", priority=1),
        ],
        capacity=2,
        used=2,
    )

    assert outcome["started"] == ["fx1"], (
        "抢占是为了给修复计划腾出位置，腾出来它就得进去；实际 "
        f"{outcome['started']!r}"
    )
    assert outcome["preempted"] == ["p1"], (
        "被抢占的是优先值最大的那个（p1 的优先值 5，p2 的是 1）；实际 "
        f"{outcome['preempted']!r}"
    )
    assert outcome["reasons"] == {"p1": "preempted_by_framework_fix"}, (
        "被抢占的计划带着理由词「为框架修复让路」；实际 "
        f"{outcome['reasons']!r}"
    )


def test_no_victim_waits() -> None:
    """既没有空槽也没有可抢占的人 → 等，不扩容量。

    跑着的那个是**另一个框架修复计划** —— 它受保护（两个修复计划互相抢占会
    活锁），所以没有受害者。没有受害者时的答案是**等待**，不是扩容量：多开
    一个槽就是多占一份机器。
    """
    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1")],
        running=[_fix("fx2")],
        capacity=1,
        used=1,
    )

    assert outcome["started"] == [], (
        "没有位置就没有执行路径 —— 修复计划也不能凭空多要一个槽；实际 "
        f"{outcome['started']!r}"
    )
    assert outcome["preempted"] == [], (
        "受保护的修复计划不能被抢占 —— 两个修复计划互相抢占会活锁；实际 "
        f"{outcome['preempted']!r}"
    )
    assert outcome["reasons"] == {"fx1": "waiting_for_slot"}, (
        "等着的理由词是「等一个空槽」；实际 {0!r}".format(outcome["reasons"])
    )


def test_tick_asks_the_ledger_for_an_oversubscribed_slot() -> None:
    """容量满、又没有人可抢 → 问账本要那个**超额槽位**。

    跑着的是**另一个仓**的框架修复计划 —— 它受保护（两个修复计划互相抢占会
    活锁），所以没有受害者；它也不是同一个仓（同一个仓的修复计划在跑时，
    后来的那个要等的是那个仓，不是槽位）。这时 tick 不自己长容量：上限是
    **容量账本**的生意，所以它去问账本，账本说「可以超额启动」
    （``oversubscribed``）就启动。
    """
    calls: List[Dict[str, Any]] = []

    def _admit(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        return {
            "admit": True,
            "reason": "framework_fix_priority",
            "oversubscribed": 1,
        }

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1")],
        running=[_fix("fx2", repo="other")],
        capacity=1,
        used=1,
        admit=_admit,
    )

    assert calls, "容量满又没有受害者时必须问账本要超额槽位"
    assert calls[0].get("kind") == "framework_fix", (
        f"问账本时说的是 lane 的种类：{calls[0]!r}"
    )
    assert outcome["started"] == ["fx1"], (
        "账本给了超额槽位就必须用它 —— 修复计划等的是执行路径，不是运力；"
        f"实际 {outcome['started']!r}"
    )
    assert outcome["reasons"].get("fx1") == "framework_fix_priority", (
        "超额启动的理由词是 lane 的那个；实际 {0!r}".format(outcome["reasons"])
    )


def test_tick_waits_when_the_ledger_refuses_the_oversubscribed_slot() -> None:
    """账本不给超额槽位 → 还是等。

    账本是上限的权威：它说不给，tick 就不给 —— 自己长容量就是多占一份机器。
    """
    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1")],
        running=[_fix("fx2", repo="other")],
        capacity=1,
        used=1,
        admit=lambda **kwargs: {
            "admit": False,
            "reason": "blocked_by_capacity",
            "oversubscribed": 0,
        },
    )

    assert outcome["started"] == [], (
        f"账本说不行就不启动：{outcome['started']!r}"
    )
    assert outcome["reasons"] == {"fx1": "blocked_by_capacity"}, (
        "理由词是账本给的那个：{0!r}".format(outcome["reasons"])
    )


def test_deadlock_escalates_to_hitl() -> None:
    """等过了十分钟 → 事件、通知，**并且**升级 HITL。

    十分钟没有执行路径是死锁：没有人会被叫醒，计划会一直等下去。这件事要
    有人**决定**怎么办 —— 所以除了写事件、发通知，还要在 HITL 的决定表里落
    一条问题。只贴一条墙上的消息不算升级：消息不需要人回答，决定需要。
    """
    escalated: List[Dict[str, Any]] = []
    notify = _collector()

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[
            _fix("fx1", stalled_since=BASE_EPOCH - DEADLOCK_AFTER - 1),
        ],
        running=[_fix("fx2")],
        capacity=1,
        used=1,
        notify=notify,
        escalate=lambda payload: escalated.append(payload),
    )

    assert outcome["reasons"].get("fx1") == "scheduler_deadlock_suspected", (
        "等过了阈值，原因词就是死锁疑似；实际 {0!r}".format(outcome["reasons"])
    )
    assert len(escalated) == 1, (
        "死锁要升级且只升级一次 —— 每一 tick 升级一次会把人吵死；实际 "
        f"{escalated!r}"
    )
    assert escalated[0].get("plan_id") == "fx1", (
        f"升级要说得清是哪个计划：{escalated[0]!r}"
    )
    assert notify.events, "升级之外，通知事件照发"


def test_the_deadlock_escalation_asks_a_human(state_db: Path) -> None:
    """默认的升级口真的在 **HITL 的决定表**里落了一条待回答的问题。

    上一个用例证明 tick 会叫升级口；这个证明**默认的**升级口就是
    ``hitl.service`` 的人工决定入口 —— 决定表里真的多了一条问题。

    这条断言是「不能只贴 outlet 消息」的钉子：通知事件是一条墙上的消息，
    没有人必须回答它；决定表里的一条问题是**等人回答**的，问的是「这件事
    怎么办」。所以「升级」不是多发一条消息，是多落一条决定。
    """
    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[
            _fix("fx1", stalled_since=BASE_EPOCH - DEADLOCK_AFTER - 1),
        ],
        running=[_fix("fx2", repo="other")],
        capacity=1,
        used=1,
        notify=_collector(),
    )

    assert outcome["reasons"].get("fx1") == "scheduler_deadlock_suspected", (
        "等过了阈值，原因词就是死锁疑似；实际 {0!r}".format(outcome["reasons"])
    )
    decisions = list_decisions("fx1")
    assert decisions, "死锁必须在决定表里落一条问题 —— 只发通知不算升级"
    question = decisions[-1]
    assert question.source == "framework_fix_deadlock", (
        f"问题的来源词是 lane 的死锁那个：{question.source!r}"
    )
    assert question.status == "pending", (
        f"问题得是待回答的：{question.status!r}"
    )
    assert question.task_id is None, (
        "问题问的是这个计划，不是它的一个任务 —— 否则计划的整个调度会卡在"
        f"这个问题后面：{question.task_id!r}"
    )
    assert question.mode == "strict", (
        "这道题必须等人回答：不能让时钟替人回答；实际 " f"{question.mode!r}"
    )


def test_the_deadlock_escalation_is_asked_once(state_db: Path) -> None:
    """同一个计划的同一道死锁题只问一次。

    tick 每几秒跑一次；一个一直死锁的计划若每一 tick 都问一次，人会被
    吵死 —— 而「已经问过了」这件事，决定表里查得到。
    """
    stalled = [
        _fix("fx1", stalled_since=BASE_EPOCH - DEADLOCK_AFTER - 1),
    ]
    running = [_fix("fx2", repo="other")]

    for _ in range(3):
        tick(
            now=BASE_EPOCH,
            framework_queue=stalled,
            running=running,
            capacity=1,
            used=1,
            notify=_collector(),
        )

    decisions = list_decisions("fx1")
    assert len(decisions) == 1, (
        "同一道死锁题只问一次；实际 {0!r}".format(decisions)
    )


def test_deadlock_escalates() -> None:
    """等过了十分钟 → 死锁原因词 + 一条通知事件。

    十分钟没有执行路径不是「还在排队」，是**死锁**：没有任何人会被叫醒，
    计划会一直等下去。这件事要有人知道，所以升级的时候既写原因词，也发通知。
    """
    notify = _collector()

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[
            _fix("fx1", stalled_since=BASE_EPOCH - DEADLOCK_AFTER - 1),
        ],
        running=[_fix("fx2")],
        capacity=1,
        used=1,
        notify=notify,
        escalate=lambda payload: None,
    )

    assert outcome["reasons"].get("fx1") == "scheduler_deadlock_suspected", (
        "等过了阈值，原因词就是死锁疑似；实际 {0!r}".format(outcome["reasons"])
    )
    events = notify.events  # type: ignore[attr-defined]
    assert len(events) == 1, (
        "死锁要发且只发一条通知事件 —— 每一 tick 都发一条会把人吵死；实际 "
        f"{events!r}"
    )
    event, payload = events[0]
    assert event == "scheduler_deadlock_suspected", (
        f"通知事件的类型词错了：{event!r}"
    )
    assert payload.get("plan_id") == "fx1", (
        f"通知要说得清是哪个计划：{payload!r}"
    )


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------
def test_idle_tick_is_unchanged() -> None:
    """空闲 tick（没有框架修复计划）→ 与改动前一致。

    没有修复计划的那一 tick，什么都不启动、不抢占、不通知 —— 这一 tick 的
    行为与接线之前一模一样。
    """
    notify = _collector()

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[],
        running=[_ordinary("p1")],
        capacity=1,
        used=1,
        notify=notify,
    )

    assert outcome == {"started": [], "preempted": [], "reasons": {}}, (
        f"空闲 tick 不该有任何动作：{outcome!r}"
    )
    assert notify.events == [], (  # type: ignore[attr-defined]
        "空闲 tick 不该发通知"
    )


def test_same_repo_waits() -> None:
    """同一仓已经有一个修复计划在跑 → 后来的那个等。

    同一个仓的两个修复计划同时改一份代码会互相打架 —— 后来的那个等。
    """
    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1", repo=REPO)],
        running=[_fix("fx2", repo=REPO)],
        capacity=2,
        used=1,
    )

    assert outcome["started"] == [], (
        "同一个仓的第二个修复计划要等；实际 {0!r}".format(outcome["started"])
    )
    assert outcome["reasons"] == {"fx1": "waiting_for_slot"}, (
        "等着的理由词是「等一个空槽」；实际 {0!r}".format(outcome["reasons"])
    )


def test_other_repo_starts() -> None:
    """别的仓有修复计划在跑 → 不影响这个仓的那个。"""
    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1", repo="other")],
        running=[_fix("fx2", repo=REPO)],
        capacity=2,
        used=1,
    )

    assert outcome["started"] == ["fx1"], (
        "同仓才排队 —— 别的仓的修复计划不挡路；实际 {0!r}".format(
            outcome["started"]
        )
    )


def test_tick_asks_the_ledger() -> None:
    """账本说不行 → 等，理由词是账本给的那个。

    账本管的是 lane 自己的上限（合并槽、每仓上限、全局上限）。它说不行，
    tick 就不动 —— 哪怕此刻确实有空槽。
    """
    calls: List[Dict[str, Any]] = []

    def _admit(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        return {"admit": False, "reason": "waiting_merge_slot", "oversubscribed": 0}

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[_fix("fx1")],
        running=[],
        capacity=2,
        used=0,
        admit=_admit,
    )

    assert calls, "准入要先问账本"
    assert calls[0].get("kind") == "framework_fix", (
        f"问账本时说的是 lane 的种类：{calls[0]!r}"
    )
    assert calls[0].get("plan_id") == "fx1", (
        f"问账本时说的是哪个计划：{calls[0]!r}"
    )
    assert outcome["started"] == [], (
        f"账本说不行就不启动：{outcome['started']!r}"
    )
    assert outcome["reasons"] == {"fx1": "waiting_merge_slot"}, (
        f"理由词是账本给的那个：{outcome['reasons']!r}"
    )


# ---------------------------------------------------------------------------
# 容量上界 —— 5 个不同仓的修复 plan 一起进来
# ---------------------------------------------------------------------------
def test_five_repair_plans_converge_within_the_lane_ceilings(
    state_db: Path,
) -> None:
    """5 个不同仓的修复 plan 一起提交：全局活跃 ≤2，超额槽位至多一个持有者。

    这是计划自带的那个场景（test-design.md:293）：5 个不同仓的修复 plan 一起
    进来，tick 收敛之后全局活跃的修复 plan 不超过 2 个，超额槽位任何时刻只
    被 1 个修复 plan 持有。

    判据是**差分**的：修好之前这一 tick 会启动 3 个（两个常规槽位 + 一个超额
    槽位），全局活跃 3 > 2。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    clock = lambda: BASE_EPOCH  # noqa: E731 —— 时钟是注入的
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=clock)
    ledger = CapacityLedger(store, facts, clock)

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[
            _fix(f"fx-{index}", repo=f"repo-{index}") for index in range(1, 6)
        ],
        running=[],
        capacity=2,
        used=0,
        admit=ledger.admit,
    )

    started = list(outcome["started"])
    assert len(started) <= 2, (
        "全局活跃的修复 plan 不超过 2；实际 {0!r}".format(started)
    )
    for plan_id, reason in outcome["reasons"].items():
        assert reason in REASONS, "{0}: {1!r} 不在闭集里".format(plan_id, reason)
    on_the_slot = [
        plan_id
        for plan_id, reason in outcome["reasons"].items()
        if reason == "framework_fix_priority"
    ]
    assert len(on_the_slot) <= 1, (
        "超额槽位任一时刻只被 1 个修复 plan 持有；实际 {0!r}".format(on_the_slot)
    )
    conn.close()


def test_three_repair_plans_of_one_repository_stay_within_the_ceilings(
    state_db: Path,
) -> None:
    """同一个仓的 3 个修复 plan：至多 2 个在跑，其余等。

    每仓上限是 2 —— 同一个仓的修复 plan 不许超过两个在跑。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    clock = lambda: BASE_EPOCH  # noqa: E731 —— 时钟是注入的
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=clock)
    ledger = CapacityLedger(store, facts, clock)

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[
            _fix(f"fx-same-{index}", repo="repo-a") for index in range(1, 4)
        ],
        running=[],
        capacity=2,
        used=0,
        admit=ledger.admit,
    )

    started = list(outcome["started"])
    assert len(started) <= 2, (
        "同一个仓的修复 plan 至多两个在跑；实际 {0!r}".format(started)
    )
    for plan_id, reason in outcome["reasons"].items():
        assert reason in REASONS, "{0}: {1!r} 不在闭集里".format(plan_id, reason)
    conn.close()


def test_a_preempted_plan_frees_its_slot_for_the_repair_plan(
    state_db: Path,
) -> None:
    """被抢占的普通 plan 空出来的槽位归修复 plan —— 超额槽位不动。

    机器满着的时候，tick 会把一个普通 plan 推 aside，修复 plan 就用它空出来的
    那个槽位。这时候机器上还是 2 个 plan —— **没有人跑到容量之外**，所以 lane
    自己的超额槽位不该被这次启动拿走：拿走了，占着它的 plan 并不在那个槽位
    上，真正的第二个修复 plan 反而被挡住。

    判据是**差分**的：改坏之前这个 tick 只启动 1 个修复 plan（第二个被
    ``blocked_by_capacity`` 挡住），而且超额槽位的持有者是一个并不在槽位上的
    plan。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    clock = lambda: BASE_EPOCH  # noqa: E731 —— 时钟是注入的
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=clock)
    ledger = CapacityLedger(store, facts, clock)

    outcome = tick(
        now=BASE_EPOCH,
        framework_queue=[
            _fix("fx-1", repo="repo-1"),
            _fix("fx-2", repo="repo-2"),
        ],
        running=[_ordinary("p1"), _ordinary("p2")],
        capacity=2,
        used=2,
        admit=ledger.admit,
    )

    started = list(outcome["started"])
    assert started == ["fx-1", "fx-2"], (
        "被抢占者空出来的槽位归修复 plan；实际启动 {0!r}".format(started)
    )
    assert store.get_oversubscribe_lease(conn) is None, (
        "没有人跑到容量之外，超额槽位就不该被拿走；实际持有者 {0!r}".format(
            store.get_oversubscribe_lease(conn)
        )
    )
    conn.close()


def test_every_reason_is_in_the_closed_set() -> None:
    """tick 说出的每一个原因词都在调度器的原因闭集里。

    面板、dry run 与 API 画的都是同一批词 —— 闭集外的词画不出来。
    """
    reasons: List[str] = []

    scenarios = [
        # 启动了：没有暂停原因。
        dict(framework_queue=[_fix("fx1")], running=[], capacity=2, used=0),
        # 抢占了：被抢占者带理由。
        dict(
            framework_queue=[_fix("fx1")],
            running=[_ordinary("p1")],
            capacity=1,
            used=1,
        ),
        # 等待：等一个空槽。
        dict(
            framework_queue=[_fix("fx1")],
            running=[_fix("fx2")],
            capacity=1,
            used=1,
        ),
        # 死锁：等过了阈值。
        dict(
            framework_queue=[_fix("fx1", stalled_since=BASE_EPOCH - 10000)],
            running=[_fix("fx2")],
            capacity=1,
            used=1,
        ),
        # 账本说的那个理由。
        dict(
            framework_queue=[_fix("fx1")],
            running=[],
            capacity=2,
            used=0,
            admit=lambda **kwargs: {
                "admit": False,
                "reason": "blocked_by_capacity",
                "oversubscribed": 0,
            },
        ),
    ]

    for scenario in scenarios:
        outcome = tick(now=BASE_EPOCH, **scenario)  # type: ignore[arg-type]
        reasons.extend(str(reason) for reason in outcome["reasons"].values())

    assert reasons, "这一组用例至少要产出几个理由词"
    outside = sorted({reason for reason in reasons if reason not in REASONS})
    assert outside == [], (
        f"闭集外的理由词画不出来：{outside!r}"
    )


# ---------------------------------------------------------------------------
# detect_deadlock —— 纯函数
# ---------------------------------------------------------------------------
def test_detect_deadlock_below_threshold() -> None:
    """还没到阈值 → 不是死锁。"""
    assert detect_deadlock(
        BASE_EPOCH, stalled_since=BASE_EPOCH - DEADLOCK_AFTER + 1
    ) is False


def test_detect_deadlock_at_threshold() -> None:
    """到了阈值 → 是死锁。

    阈值是「十分钟没有执行路径」—— 正好十分钟也算。
    """
    assert detect_deadlock(
        BASE_EPOCH, stalled_since=BASE_EPOCH - DEADLOCK_AFTER
    ) is True


def test_detect_deadlock_without_a_start() -> None:
    """不知道从什么时候开始等 → 说不准，就不算死锁。

    没有起点就没有「等了多久」这回事 —— 猜一个比说「不知道」更糟。
    """
    assert detect_deadlock(BASE_EPOCH, stalled_since=None) is False


def test_detect_deadlock_without_a_clock() -> None:
    """没有此刻 → 不算死锁。"""
    assert detect_deadlock(None, stalled_since=BASE_EPOCH) is False


def test_detect_deadlock_clock_went_backwards() -> None:
    """时钟往回走了 → 不算死锁。

    负的等待时长不是「等了负十分钟」，是读错了。
    """
    assert detect_deadlock(
        BASE_EPOCH, stalled_since=BASE_EPOCH + DEADLOCK_AFTER
    ) is False


def test_detect_deadlock_threshold_is_configurable() -> None:
    """阈值是参数 —— 别的调用方可以自己给。"""
    assert detect_deadlock(
        BASE_EPOCH, stalled_since=BASE_EPOCH - 30, threshold_s=10
    ) is True


# ---------------------------------------------------------------------------
# 引擎的接线 —— 真引擎、真库、真事实
# ---------------------------------------------------------------------------
class _RecordingSink:
    """把这一 tick 的决定收下来 —— 引擎自己不碰计划。"""

    def __init__(self) -> None:
        self.decisions: List[Dict[str, Any]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """收下这一批决定，并说它们都成了。"""
        recorded = [dict(decision) for decision in decisions]
        self.decisions.extend(recorded)
        return [dict(decision, ok=True) for decision in recorded]

    def actions(self) -> List[Dict[str, Any]]:
        """收下来的决定。"""
        return list(self.decisions)


class _Lifecycle:
    """命令执行器要的生命周期门面 —— 这一 tick 不碰它。"""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def start_plan(self, plan_id: str, spec: Any = None) -> None:
        """起一个计划。"""
        self.calls.append(("start", plan_id))

    def stop_plan(self, plan_id: str, *, requeue: bool = False) -> None:
        """停一个计划。"""
        self.calls.append(("stop", plan_id, requeue))


class _StaticConfig:
    """一份永远是同一份的配置。"""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """这一份配置。"""
        return self.config


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


def _route_framework_fix(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    repo: str = REPO,
    phase: str = "queued",
) -> None:
    """写下路由行：这个计划是一个框架修复计划，此刻在排队。"""
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, plan_kind, framework_fix, updated_at) "
        "VALUES (?, ?, 'framework_fix', ?, ?)",
        (
            plan_id,
            phase,
            json.dumps({"repo": repo, "fingerprint": "sha256:" + "0" * 64}),
            "2026-11-24T00:00:00Z",
        ),
    )
    conn.commit()


def _enqueue(conn: sqlite3.Connection, plan_id: str, seq: int = 1) -> None:
    """写下队列行：这个计划站在队伍里的哪个位置。"""
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, 0, NULL, ?, ?)",
        (plan_id, seq, "2026-11-24T00:00:00Z", "2026-11-24T00:00:00Z"),
    )
    conn.commit()


def test_engine_tick_admits_the_framework_fix(state_db: Path) -> None:
    """引擎的那一 tick 真的把修复计划放进了执行路径。

    这是接线本身：不是纯函数答对了，而是**引擎**读到了队列里的修复计划、
    问过账本、把它放了进去 —— 而且放它进去的理由词是 lane 的那个
    （``framework_fix_priority``），不是普通路径的 ``running``。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    _plan_dir(plans_root, "fx1")
    conn = _open(state_db)
    _route_framework_fix(conn, "fx1")
    _enqueue(conn, "fx1")

    clock = lambda: BASE_EPOCH  # noqa: E731 —— 时钟是注入的
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    config_loader = _StaticConfig(
        enabled=True,
        capacity=2,
        min_runtime_seconds=600.0,
        tick_seconds=0.01,
        command_timeout_seconds=10.0,
    )
    commands = CommandExecutor(
        store,
        ledger,
        _Lifecycle(),
        facts=facts,
        conn=conn,
        capacity=2,
        min_runtime_seconds=600.0,
        clock=clock,
        config_provider=config_loader,
    )
    sink = _RecordingSink()
    engine = PlanSchedulerEngine(
        config_loader=config_loader,
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=sink,
        clock=clock,
        conn=conn,
    )

    engine._run_tick()

    admits = [
        decision
        for decision in sink.actions()
        if decision.get("action") == "admit"
    ]
    assert [decision.get("plan_id") for decision in admits] == ["fx1"], (
        "引擎的那一 tick 应当把框架修复计划放进去；实际 {0!r}".format(
            sink.actions()
        )
    )
    assert admits[0].get("reason") == "framework_fix_priority", (
        "放它进去的理由词是 lane 的那个，不是普通路径的 running；实际 "
        "{0!r}".format(admits[0])
    )
    conn.close()


def test_a_running_repair_plan_survives_the_over_capacity_convergence(
    state_db: Path,
) -> None:
    """超容量收敛不打断正在运行的框架修复计划。

    修复计划**不可被抢占** —— 两个修复计划互相让路会活锁。lane 自己的抢占
    已经保护了它；**普通**的超容量收敛是另一条会打断计划的路，它也得看得见
    「这一条是修复计划」。

    两条计划站在同样的位置（同样的优先值、同样的运行时长），所以没有保护的话
    被选中的是**字典序在前**的那个 —— ``fx1``。它被选中，就是这条保护没生效。
    """
    conn = _open(state_db)
    _route_framework_fix(conn, "fx1", phase="running")
    engine = PlanSchedulerEngine(
        config_loader=_StaticConfig(enabled=True, capacity=1),
        store=SqliteSchedulerStore(),
        facts=object(),
        ledger=object(),
        commands=object(),
        sink=_RecordingSink(),
        conn=conn,
    )
    state = {
        "capacity": 1,
        "used": 2,
        "min_runtime_seconds": 0,
        "running": [
            {
                "plan_id": "fx1",
                "created_at": 1000.0,
                "acquired_at": 1000.0,
                "elapsed": 900.0,
            },
            {
                "plan_id": "p1",
                "created_at": 1000.0,
                "acquired_at": 1000.0,
                "elapsed": 900.0,
            },
        ],
    }

    victims = engine._converge_over_capacity(state)

    assert "fx1" not in victims, (
        "正在运行的框架修复计划不得被超容量收敛打断；实际 {0!r}".format(victims)
    )
    assert victims == ["p1"], (
        "被打断的应当是那条普通计划；实际 {0!r}".format(victims)
    )
    conn.close()


def _attach(
    conn: sqlite3.Connection,
    attach_id: str,
    plan_id: str,
    *,
    status: str = "blocked",
) -> None:
    """挂一条阻塞关系 —— 谁在等这个缺陷的修复。"""
    conn.execute(
        "INSERT INTO framework_fix_attach ("
        "  attach_id, fingerprint, plan_id, vp_id, round, source, status,"
        "  held_since, resolved_at, result_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
        (
            attach_id,
            "sha256:" + attach_id.ljust(64, "0"),
            plan_id,
            "VP-001",
            1,
            "clean_main_repro",
            status,
            "2026-11-24T00:00:00Z",
        ),
    )
    conn.commit()


def test_running_rows_carry_the_blocked_attachment_count(
    state_db: Path,
) -> None:
    """运行中的计划带着它挂着的**阻塞工作数**。

    抢占顺序的第二位是「挂起的阻塞工作数少的先被抢占」—— 这个数只有从
    ``framework_fix_attach`` 里数得出来。引擎构造运行行的时候不带上它，
    这一位在生产里就恒为 0，等于没有第二位。
    """
    conn = _open(state_db)
    _attach(conn, "a1", "p1")
    _attach(conn, "a2", "p1")
    _attach(conn, "a3", "p2", status="registered")

    engine = PlanSchedulerEngine(
        config_loader=_StaticConfig(enabled=True, capacity=2),
        store=SqliteSchedulerStore(),
        facts=object(),
        ledger=object(),
        commands=object(),
        sink=_RecordingSink(),
        conn=conn,
    )
    state = {
        "capacity": 2,
        "used": 2,
        "min_runtime_seconds": 0,
        "running": [
            {
                "plan_id": "p1",
                "created_at": 1000.0,
                "acquired_at": 1000.0,
                "elapsed": 900.0,
            },
            {
                "plan_id": "p2",
                "created_at": 1000.0,
                "acquired_at": 1000.0,
                "elapsed": 900.0,
            },
        ],
    }

    rows = engine._framework_fix_running(state)
    counts = {row["plan_id"]: row.get("attach_blocked_count") for row in rows}

    assert counts == {"p1": 2, "p2": 0}, (
        "运行行要带着挂着的阻塞工作数（只有 blocked 的算）；实际 "
        f"{counts!r}"
    )
    conn.close()


def _open(path: Path) -> sqlite3.Connection:
    """打开这份状态库。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 合并审批 —— CI 通过后加急 HITL、30 分钟重发、2 小时升级、夜间静默
# ---------------------------------------------------------------------------

#: PR 地址 —— 问句里带着它，人看了才知道批的是哪个 PR。
PR_URL = "https://github.invalid/acme/framework/pull/42"


class _Clock:
    """一只可以拨动的钟 —— 「过了 30 分钟」是拨出来的，不是等出来的。"""

    def __init__(self, moment: float) -> None:
        self.now = moment

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """把钟往前拨。"""
        self.now += seconds


def _noon() -> float:
    """一个白天的时刻（本地时间正午）—— 不在静默窗口里。"""
    return datetime(2026, 11, 24, 12, 0).timestamp()


def _night() -> float:
    """一个夜里的时刻（本地时间 23:30）—— 落在静默窗口里。"""
    return datetime(2026, 11, 24, 23, 30).timestamp()


def _open_pr_report(
    tree: Path,
    *,
    pr_url: str = PR_URL,
    ci_conclusion: str = "passed",
) -> Path:
    """写下开 PR 任务的那份报告 —— 两行机器可读字段。"""
    report = tree / ".pdt" / "framework_fix" / "open_pr.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "# 开 PR\n\n"
        "分支：fix/framework-x\n\n"
        f"pr_url: {pr_url}\n"
        f"ci_conclusion: {ci_conclusion}\n",
        encoding="utf-8",
    )
    return report


def _worked_tree(tmp_path: Path, plan_id: str, **report: Any) -> Path:
    """这个修复计划干活的那棵树 —— 报告就在它里面。"""
    tree = tmp_path / "trees" / plan_id
    tree.mkdir(parents=True, exist_ok=True)
    _open_pr_report(tree, **report)
    return tree


def _worked_in(conn: sqlite3.Connection, plan_id: str, tree: Path) -> None:
    """写下执行行 —— 这个计划是在哪棵树里干的活。"""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, project_dir, updated_at) VALUES (?, ?, ?)",
        (plan_id, str(tree), "2026-11-24T00:00:00Z"),
    )
    conn.commit()


def _outlet(plan_dir: Path, out: Path) -> None:
    """给计划目录装一个出口 —— 送出去的消息落在磁盘上。

    出口是 ``strict`` 的：合并审批要的是一个人的答复，不是「到期按默认值
    办」。每一条 body 单独一行，于是「送出去了没有」不是一个断言，而是
    磁盘上一个文件在不在。
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
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
                "mode": "strict",
                "timeout_sec": 1800,
                "hooks": [{"kind": "cmd", "command": command, "timeout_sec": 10}],
            }
        ),
        encoding="utf-8",
    )


def _merge_engine(
    conn: sqlite3.Connection, plans_root: Path, clock: Any
) -> PlanSchedulerEngine:
    """一台真的引擎 —— 走真的 tick，不是直接调那个方法。"""
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    config_loader = _StaticConfig(
        enabled=True,
        capacity=2,
        min_runtime_seconds=600.0,
        tick_seconds=0.01,
        command_timeout_seconds=10.0,
    )
    commands = CommandExecutor(
        store,
        ledger,
        _Lifecycle(),
        facts=facts,
        conn=conn,
        capacity=2,
        min_runtime_seconds=600.0,
        clock=clock,
        config_provider=config_loader,
    )
    return PlanSchedulerEngine(
        config_loader=config_loader,
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=_RecordingSink(),
        clock=clock,
        conn=conn,
        plans_dir=plans_root,
    )


def _block_of(conn: sqlite3.Connection, plan_id: str) -> Dict[str, Any]:
    """这个计划的框架修复块。"""
    row = conn.execute(
        "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if row is None or not row[0]:
        return {}
    block = json.loads(str(row[0]))
    return block if isinstance(block, dict) else {}


def _bodies(out: Path) -> List[Dict[str, Any]]:
    """出口收到的那几份 body。"""
    if not out.exists():
        return []
    return [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _merge_ready_plan(
    conn: sqlite3.Connection,
    plans_root: Path,
    tmp_path: Path,
    *,
    ci_conclusion: str = "passed",
) -> None:
    """一个 PR 已开、CI 已绿的修复计划。"""
    _plan_dir(plans_root, "fx1")
    _outlet(plans_root / "fx1", tmp_path / "delivered.jsonl")
    _route_framework_fix(conn, "fx1", phase="running")
    _worked_in(conn, "fx1", _worked_tree(tmp_path, "fx1", ci_conclusion=ci_conclusion))


def test_merge_ready_asks_the_operator(state_db: Path, tmp_path: Path) -> None:
    """CI 绿了 -> 一条等人批准的决定：PR 号、CI 结论、approve 指引、默认拒绝。

    合入是人的决定（PRD 决策点 7）：没有批准就不合入。所以「CI 通过」不是
    一条贴在墙上的消息，它是一条**问句** —— 落在 HITL 的决定表里，默认答案
    是 reject（没有答复不等于批准）。
    """
    from framework_fix.notify import clear_records

    clear_records()
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merge_ready_plan(conn, plans_root, tmp_path)

    engine = _merge_engine(conn, plans_root, _Clock(_noon()))
    engine._run_tick()

    decisions = list_decisions("fx1")
    assert len(decisions) == 1, (
        "CI 通过后应当问一次；实际 {0!r}".format(decisions)
    )
    decision = decisions[0]
    assert PR_URL in decision.question, decision.question
    assert "passed" in decision.question, decision.question
    assert "approve" in decision.question, decision.question
    assert decision.source == "framework_fix", decision.source
    assert decision.options == ["approve", "reject"]
    assert decision.default_option == "reject", "没有答复不等于批准"
    assert decision.mode == "strict", decision.mode

    block = _block_of(conn, "fx1")
    assert block.get("merge_ready_at"), (
        "就绪的时刻要写进块里；实际 {0!r}".format(block)
    )
    assert block.get("pr_url") == PR_URL
    assert block.get("ci_conclusion") == "passed"
    conn.close()


def test_merge_ready_is_asked_once(state_db: Path, tmp_path: Path) -> None:
    """第二个 tick 不再问一遍 —— 同一件事只问一次。

    ``merge_ready_at`` 就是那个幂等标记：它在了，就说明问题已经问过了。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merge_ready_plan(conn, plans_root, tmp_path)

    engine = _merge_engine(conn, plans_root, _Clock(_noon()))
    engine._run_tick()
    engine._run_tick()

    assert len(list_decisions("fx1")) == 1, (
        "同一件事只问一次；实际 {0!r}".format(list_decisions("fx1"))
    )
    conn.close()


def test_merge_reminder_goes_out_after_the_threshold(
    state_db: Path, tmp_path: Path
) -> None:
    """就绪满 30 分钟没人答 -> 再喊一遍（阈值取自配置）。"""
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merge_ready_plan(conn, plans_root, tmp_path)

    clock = _Clock(_noon())
    engine = _merge_engine(conn, plans_root, clock)
    engine._run_tick()

    events = [body.get("event") for body in _bodies(tmp_path / "delivered.jsonl")]
    assert "framework_fix_merge_reminder_30m" not in events, (
        "刚就绪的时候还没有到重发的点；实际 {0!r}".format(events)
    )

    clock.advance(31 * 60)
    engine._run_tick()

    events = [body.get("event") for body in _bodies(tmp_path / "delivered.jsonl")]
    assert "framework_fix_merge_reminder_30m" in events, (
        "过了阈值要再喊一遍；实际 {0!r}".format(events)
    )
    block = _block_of(conn, "fx1")
    assert block.get("merge_reminder_30m_at"), (
        "提醒过了要留下标记，同一条不再重复；实际 {0!r}".format(block)
    )
    conn.close()


def test_merge_escalates_to_waiting_on_user_merge(
    state_db: Path, tmp_path: Path
) -> None:
    """就绪满两小时没人答 -> 状态升级成 waiting_on_user_merge。"""
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merge_ready_plan(conn, plans_root, tmp_path)

    clock = _Clock(_noon())
    engine = _merge_engine(conn, plans_root, clock)
    engine._run_tick()

    clock.advance(2 * 60 * 60 + 60)
    engine._run_tick()

    block = _block_of(conn, "fx1")
    assert block.get("status") == "waiting_on_user_merge", (
        "两小时没人答就要升级；实际 {0!r}".format(block)
    )
    assert block.get("waiting_on_user_merge_at"), (
        "升级要留下标记；实际 {0!r}".format(block)
    )

    events = [body.get("event") for body in _bodies(tmp_path / "delivered.jsonl")]
    assert "framework_fix_waiting_on_user_merge" in events, (
        "升级要通知出去；实际 {0!r}".format(events)
    )
    conn.close()


def test_merge_approval_is_silent_at_night(state_db: Path, tmp_path: Path) -> None:
    """夜里不投递，但留痕 —— 静默是「现在不打扰」，不是「没发生」。

    夜里 CI 绿了也不问人（22:00–08:00 静默窗口），但记录里要看得见
    ``suppressed=quiet_hours``：第二天没人知道它被吞了，就没有人会去问。
    """
    from framework_fix.notify import clear_records, records

    clear_records()
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merge_ready_plan(conn, plans_root, tmp_path)

    engine = _merge_engine(conn, plans_root, _Clock(_night()))
    engine._run_tick()

    assert list_decisions("fx1") == [], "夜里不向人提问"

    swallowed = [
        record
        for record in records()
        if record.get("event") == "merge_ready"
    ]
    assert swallowed, "静默也要留痕；实际 {0!r}".format(records())
    assert swallowed[-1]["suppressed"] == "quiet_hours"
    assert swallowed[-1]["delivered"] is False
    conn.close()


# ---------------------------------------------------------------------------
# 合入通知 —— 「合了」这件事对人说一次
# ---------------------------------------------------------------------------
def _merged_plan(
    conn: sqlite3.Connection, plans_root: Path, tmp_path: Path
) -> None:
    """一个已经合入的修复计划。"""
    _plan_dir(plans_root, "fx1")
    _outlet(plans_root / "fx1", tmp_path / "delivered.jsonl")
    _route_framework_fix(conn, "fx1", phase="completed")


def _merged_bodies(out: Path) -> List[Dict[str, Any]]:
    """出口收到的「合入了」那几条。"""
    return [
        body
        for body in _bodies(out)
        if body.get("event") == "framework_fix_merged"
    ]


def test_a_landed_repair_is_notified_once(
    state_db: Path, tmp_path: Path
) -> None:
    """修复合入 -> 通知一次。

    合并审批那条链问的是「批不批」（``merge_ready`` -> 30 分钟重发 -> 两小时
    升级），没有人问过「合了没有」—— 消息发出去了，人批准了，PR 合进来了，
    而「这件事办完了」这句话始终没有说出来过。

    通知一次就够：同一个修复计划的合入通知只发一遍，第二次是噪音。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merged_plan(conn, plans_root, tmp_path)

    engine = _merge_engine(conn, plans_root, _Clock(_noon()))
    engine._run_tick()

    merged = _merged_bodies(tmp_path / "delivered.jsonl")
    assert len(merged) == 1, (
        "合入通知一次；实际 {0!r}".format(merged)
    )
    assert merged[0]["plan_id"] == "fx1"

    block = _block_of(conn, "fx1")
    assert block.get("merged_notified_at"), (
        "通知过了要留下标记；实际 {0!r}".format(block)
    )
    conn.close()


def test_a_landed_repair_is_not_notified_twice(
    state_db: Path, tmp_path: Path
) -> None:
    """第二个 tick 不再通知一遍 —— 同一件事只说一次。

    tick 每几秒跑一次；一条每个 tick 都发一次的通知就是一条没人看的通知。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merged_plan(conn, plans_root, tmp_path)

    engine = _merge_engine(conn, plans_root, _Clock(_noon()))
    engine._run_tick()
    engine._run_tick()

    merged = _merged_bodies(tmp_path / "delivered.jsonl")
    assert len(merged) == 1, (
        "同一件事只说一次；实际 {0!r}".format(merged)
    )
    conn.close()


def _set_phase(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """只改这个计划此刻的相位 —— 块里的东西一个字都不动。"""
    conn.execute(
        "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
        (phase, plan_id),
    )
    conn.commit()


def test_a_reopened_repair_is_notified_again(
    state_db: Path, tmp_path: Path
) -> None:
    """重开之后再合入 -> 再通知一次。

    同一个修复计划合入过两次是**两件事**：第一次的修复没修好，重开之后
    又修好了一次。通知只发一次的话，第二次合入就没人知道了。
    """
    plans_root = Path(os.environ["PDT_PLANS_DIR"])
    conn = _open(state_db)
    _merged_plan(conn, plans_root, tmp_path)

    engine = _merge_engine(conn, plans_root, _Clock(_noon()))
    engine._run_tick()
    assert len(_merged_bodies(tmp_path / "delivered.jsonl")) == 1

    # 重开 —— 这个计划不再处于「已合入」的状态。
    _set_phase(conn, "fx1", "running")
    engine._run_tick()

    block = _block_of(conn, "fx1")
    assert not block.get("merged_notified_at"), (
        "不再合入了就清掉标记；实际 {0!r}".format(block)
    )

    # 又合入了一次。
    _set_phase(conn, "fx1", "completed")
    engine._run_tick()

    merged = _merged_bodies(tmp_path / "delivered.jsonl")
    assert len(merged) == 2, (
        "第二次合入要再通知一次；实际 {0!r}".format(merged)
    )
    conn.close()
