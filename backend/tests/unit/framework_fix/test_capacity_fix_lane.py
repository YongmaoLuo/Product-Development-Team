"""TDD spec —— 框架修复 lane 的容量判定：全局上限、每仓上限、超卖槽、等合并槽。

这个模块要解决什么
------------------
架构决策点 5 钉死了三个数字：**全局活跃修复 plan ≤ 2**、**同一框架仓 ≤ 2**、
**全局超卖 1 槽**。合并串行则是注册表上的**仓库级租约**（架构决策点 5：租约
留在注册表上，不塞进容量账本），但「这个仓的槽被别人占着」这件事必须变成
调度器能画出来的原因词 —— ``waiting_merge_slot``。

于是这里给一个判定::

    CapacityLedger.admit(kind=..., repo=..., running_global=...)

答案是一个三元组：``admit``（准不准）、``reason``（原因词）、``oversubscribed``
（是不是走了超卖槽）。

三个上限
--------
``framework_fix.max_active``（全局，默认 2）、``framework_fix.max_per_repo``
（每仓，默认 2）、``framework_fix.oversubscribe``（超卖槽，默认 1）。它们是
**运行期设置**：写在 ``scheduler_setting`` 表里，所以改了之后重启仍然生效。

超卖槽
------
全局上限是 2，**超卖槽不延长这条上限** —— 它只是 lane 自己在常规容量之外的
那一个槽位，仍算在同一个 ≤2 的额度里（PRD 对同仓的写法「1 个正常槽位 + 1 个
超额槽位」就是这个构成）。

超卖槽是**租约**：同一时刻至多一个修复 plan 持有它，持有者记在
``scheduler_setting`` 的 ``framework_fix.oversubscribe_lease`` 上。别人持有时
只能等（``blocked_by_capacity``），自己持有不算冲突，持有者停下来租约就释放。
普通计划不持有这个租约 —— 它是 lane 自己的保留槽位，普通计划把机器撑到超容量
也不该把修复 plan 挡在门外。

数谁
----
只数 ``COUNTED_PHASES`` 里的计划 —— 一个排队等着的修复 plan 不占额度，它
没在干活。

TDD contract —— 4 cases
-----------------------
1. ``test_global_cap`` —— 全局上限 2，第 3 个修复 plan 没有额度。
2. ``test_per_repo_cap`` —— 同仓第 3 个拒绝。
3. ``test_waiting_lease`` —— 租约冲突 -> ``waiting_merge_slot``。
4. ``test_settings_persisted`` —— 改上限后重启仍生效。

同文件覆盖的边界条件：

* 没有常规空位时，第 2 个修复 plan 占超卖槽（``oversubscribed`` 是 1）；
* 超卖槽的租约：持有者被记下来 / 别人持有时被拒 / 自己持有不是冲突 /
  持有者停止后释放；
* 普通计划不占这个租约；
* 只有 ``COUNTED_PHASES`` 里的计划被计入额度；
* 原因词都在调度器的原因闭集 ``REASONS`` 里；
* 三个上限键在 ``scheduler_setting`` 上的读写。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _path in (REPO_ROOT, BACKEND_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from plan_scheduler.capacity import (  # noqa: E402
    DEFAULT_FRAMEWORK_FIX_MAX_ACTIVE,
    DEFAULT_FRAMEWORK_FIX_MAX_PER_REPO,
    DEFAULT_FRAMEWORK_FIX_OVERSUBSCRIBE,
    FRAMEWORK_FIX_MAX_ACTIVE_KEY,
    FRAMEWORK_FIX_MAX_PER_REPO_KEY,
    FRAMEWORK_FIX_OVERSUBSCRIBE_KEY,
    CapacityLedger,
)
from plan_scheduler.facts import PlanFactsReader  # noqa: E402
from plan_scheduler.policy import COUNTED_PHASES, REASONS  # noqa: E402
from plan_scheduler.store import SqliteSchedulerStore  # noqa: E402

#: 一个固定的时刻，让每个用例读同一个时钟。
BASE_EPOCH = datetime(2026, 11, 24, 3, 0, 0, tzinfo=timezone.utc).timestamp()

#: 修复计划的种类词。
FIX_KIND = "framework_fix"


def _utc_iso(epoch: float) -> str:
    """返回那一刻，格式与状态库写时间戳时一样。"""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class _Clock:
    """用例自己动的时钟 —— 账本与事实读取器都问它「现在几点」。"""

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """返回当前时刻（epoch 秒）。"""
        return self._now


class _World:
    """这个用例的世界：一个数据库、一个账本、一个时钟。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ledger: CapacityLedger,
        clock: _Clock,
        store: SqliteSchedulerStore,
    ) -> None:
        self.conn = conn
        self.ledger = ledger
        self.clock = clock
        self.store = store

    # -- 计划 -------------------------------------------------------------

    def plan(
        self,
        plan_id: str,
        *,
        kind: str = "normal",
        phase: str = "verify_first_pass",
        repo: str = "",
    ) -> None:
        """写下这个计划的路由行：种类、相位、框架修复块。

        框架修复块里带着这个计划修的仓（``repo``）—— 那是「这个修复属于
        哪个仓」的事实，每仓上限就靠它。
        """
        block = json.dumps(
            {"repo": repo, "fingerprint": "sha256:" + plan_id},
            ensure_ascii=False,
        )
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at, "
            "plan_kind, framework_fix) VALUES (?, ?, ?, ?, ?)",
            (plan_id, phase, _utc_iso(self.clock()), kind, block),
        )
        self.conn.commit()

    def working(
        self,
        plan_id: str,
        *,
        kind: str = "normal",
        phase: str = "verify_first_pass",
        repo: str = "",
    ) -> None:
        """让这个计划**占着算力**，并说清它是什么计划、属于哪个仓。

        走的是验证侧（一个正在跑的验证轮次），所以用例不需要真的起子进程。
        """
        self.plan(plan_id, kind=kind, phase=phase, repo=repo)
        self.conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "stop_reason, task_progress, next_run_at, card_state, flags, "
            "exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (plan_id, _utc_iso(self.clock()), _utc_iso(self.clock())),
        )
        self.conn.execute(
            "INSERT INTO plan_verification (plan_id, verification_status, "
            "round, max_rounds, updated_at) VALUES (?, 'running', 0, 3, ?)",
            (plan_id, _utc_iso(self.clock())),
        )
        self.conn.commit()

    def working_but_queued(self, plan_id: str, *, kind: str = "normal", repo: str = "") -> None:
        """一个**占着算力**但相位不在 ``COUNTED_PHASES`` 里的计划。

        执行侧活着（本进程的 pid），相位却是 ``queued`` —— 它在等，不在
        干活，所以不占额度。
        """
        self.plan(plan_id, kind=kind, phase="queued", repo=repo)
        self.conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "stop_reason, task_progress, next_run_at, card_state, flags, "
            "exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, 0, NULL, NULL, NULL, NULL, NULL, NULL, ?, 'running', ?, ?)",
            (plan_id, os.getpid(), _utc_iso(self.clock()), _utc_iso(self.clock())),
        )
        self.conn.commit()

    # -- 合并槽 -----------------------------------------------------------

    def lease(self, repo: str, fix_plan_id: str) -> None:
        """让某个修复计划占住这个仓的合并槽。"""
        self.conn.execute(
            "INSERT INTO framework_fix_merge_lease "
            "(repo, fix_plan_id, pr_url, acquired_at) VALUES (?, ?, NULL, ?)",
            (repo, fix_plan_id, _utc_iso(self.clock())),
        )
        self.conn.commit()


@pytest.fixture()
def world(tmp_path: Path):
    """一个账本，架在带真实 schema 的内存库上。"""
    from state_machine.db.schema import migrate

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    clock = _Clock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, tmp_path / "plans", clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    try:
        yield _World(conn, ledger, clock, store)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 全局上限 —— 第 3 个走超卖槽
# ---------------------------------------------------------------------------
def test_global_cap(world: _World) -> None:
    """全局上限是 2：第 3 个修复 plan 没有额度。

    超卖槽**不**延长这条上限 —— 它是 lane 自己在常规容量之外的那一个槽位，
    仍算在同一个 ≤2 的额度里。所以两个修复 plan 在跑的时候，第 3 个拿不到
    任何槽位，理由词是 ``blocked_by_capacity``。
    """
    world.working("fix-1", kind=FIX_KIND, repo="repo-a")
    world.working("fix-2", kind=FIX_KIND, repo="repo-b")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-c", running_global=2, plan_id="fix-3"
    )

    assert verdict == {
        "admit": False,
        "reason": "blocked_by_capacity",
        "oversubscribed": 0,
    }


def test_global_cap_blocks_the_fourth(world: _World) -> None:
    """第 4 个修复 plan 没有槽位可走。

    三个修复 plan 在跑，全局上限 2 早已满 —— 第 4 个既没有常规额度，也没有
    超卖槽，于是被 ``blocked_by_capacity`` 拒绝。

    这一条靠**账本自己数**成立：调用方没给 ``running_global``，账本从事实里
    数出三个在跑的修复 plan。
    """
    world.working("fix-1", kind=FIX_KIND, repo="repo-a")
    world.working("fix-2", kind=FIX_KIND, repo="repo-b")
    world.working("fix-3", kind=FIX_KIND, repo="repo-c")

    verdict = world.ledger.admit(kind=FIX_KIND, repo="repo-d", plan_id="fix-4")

    assert verdict == {
        "admit": False,
        "reason": "blocked_by_capacity",
        "oversubscribed": 0,
    }


def test_first_fix_takes_a_normal_slot(world: _World) -> None:
    """额度没满时走常规额度，``oversubscribed`` 是 0。"""
    verdict = world.ledger.admit(kind=FIX_KIND, repo="repo-a", running_global=0)

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 0,
    }


def test_the_second_fix_plan_takes_the_oversubscribe_slot(world: _World) -> None:
    """没有常规空位时，第 2 个修复 plan 占超卖槽。

    全局额度是 2：第 2 个修复 plan 还在额度里，但机器已经被普通计划占满了
    —— 没有常规空位可走，它走的是 lane 自己的超卖槽，答案里的
    ``oversubscribed`` 是 1。
    """
    world.working("busy-1")
    world.working("busy-2")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", running_global=0, plan_id="fix-3", capacity=2
    )

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 1,
    }


# ---------------------------------------------------------------------------
# 2. 每仓上限 —— 同仓第 3 个拒绝
# ---------------------------------------------------------------------------
def test_per_repo_cap(world: _World) -> None:
    """同一个仓的第 3 个修复 plan 被拒绝。

    每仓上限是 2：仓里已经有 2 个修复 plan 在跑，第 3 个就超出了 —— 即便
    全局还有位置（这里全局一个都没跑，全局额度是空的）。
    """
    world.working("fix-1", kind=FIX_KIND, repo="repo-a")
    world.working("fix-2", kind=FIX_KIND, repo="repo-a")

    verdict = world.ledger.admit(kind=FIX_KIND, repo="repo-a")

    assert verdict == {
        "admit": False,
        "reason": "blocked_by_capacity",
        "oversubscribed": 0,
    }


def test_per_repo_cap_does_not_reach_across_repositories(world: _World) -> None:
    """别的仓不受这个仓的上限约束。

    每仓上限调成 1：仓 A 已经有 1 个修复 plan 在跑（它满了），仓 A 的下一个
    修复 plan 被拒；仓 B 的修复 plan 照样能跑 —— 每仓上限是**按仓**数的。
    """
    world.store.set_framework_fix_limits(world.conn, max_per_repo=1)
    world.conn.commit()
    world.working("fix-1", kind=FIX_KIND, repo="repo-a")

    blocked = world.ledger.admit(kind=FIX_KIND, repo="repo-a", plan_id="fix-2")

    assert blocked == {
        "admit": False,
        "reason": "blocked_by_capacity",
        "oversubscribed": 0,
    }

    admitted = world.ledger.admit(kind=FIX_KIND, repo="repo-b", plan_id="fix-3")

    assert admitted == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 0,
    }


# ---------------------------------------------------------------------------
# 3. 合并槽 —— 租约被别人占着
# ---------------------------------------------------------------------------
def test_waiting_lease(world: _World) -> None:
    """别人占着这个仓的合并槽 -> 等合并槽。

    一个仓同时只允许一个未合并的修复 PR（任务 12 的租约）。槽在**别人**
    手里时，这个修复 plan 只能等，理由词是 ``waiting_merge_slot``。
    """
    world.lease("repo-a", "other-fix")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", plan_id="fix-3"
    )

    assert verdict == {
        "admit": False,
        "reason": "waiting_merge_slot",
        "oversubscribed": 0,
    }


def test_own_lease_is_not_a_conflict(world: _World) -> None:
    """占着槽的就是自己时，不是冲突。

    租约的互斥是**按仓**的：自己已经占着这个仓的槽，再问一次仍然是自己，
    不能把自己挡在门外。
    """
    world.lease("repo-a", "fix-3")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", plan_id="fix-3"
    )

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 0,
    }


def test_lease_of_another_repository_does_not_block(world: _World) -> None:
    """别的仓的槽不挡这个仓。"""
    world.lease("repo-z", "other-fix")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", plan_id="fix-3"
    )

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 0,
    }


# ---------------------------------------------------------------------------
# 4. 超卖槽 —— 没有常规空位时，修复 plan 占它
# ---------------------------------------------------------------------------
def test_an_ordinary_plan_does_not_hold_the_oversubscribe_slot(world: _World) -> None:
    """普通计划不占 lane 的超卖槽。

    超卖槽是 lane 自己的保留槽位，租约记在 ``scheduler_setting`` 上，只有
    修复 plan 会写它。所以机器被普通计划撑到超容量（3 个在跑，常规容量 2）
    时，修复 plan 照样能拿到这个槽位 —— 普通计划不把它挡在门外。
    """
    world.working("busy-1")
    world.working("busy-2")
    world.working("busy-3")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", running_global=0, plan_id="fix-3", capacity=2
    )

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 1,
    }


def test_the_oversubscribe_slot_is_not_needed_below_the_capacity(
    world: _World,
) -> None:
    """没到常规容量时，超卖槽用不上，修复 plan 走常规槽位。

    机器上还有一个空位（用了 1 个，容量 2），修复 plan 就走那个空位 ——
    ``oversubscribed`` 是 0，超卖槽的租约也仍然空着（没有持有者）。
    """
    world.working("busy-1")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", running_global=0, plan_id="fix-3", capacity=2
    )

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 0,
    }
    assert world.store.get_oversubscribe_lease(world.conn) is None


# ---------------------------------------------------------------------------
# 5. 超卖槽的租约 —— 持有者被记下来
# ---------------------------------------------------------------------------
def test_the_oversubscribe_lease_records_its_holder(world: _World) -> None:
    """谁占着超卖槽，租约上写着谁。

    超卖槽是租约，不是「数出来的共享位」：占用与否由**持有者**决定，而不是
    由「在跑的数比容量多几个」推出来。所以问「现在谁占着超卖槽」有答案。
    """
    world.working("busy-1")
    world.working("busy-2")

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", running_global=0, plan_id="fix-3", capacity=2
    )

    assert verdict["oversubscribed"] == 1
    assert world.store.get_oversubscribe_lease(world.conn) == "fix-3"


def test_the_oversubscribe_lease_holds_one_plan_at_a_time(world: _World) -> None:
    """同一时刻至多一个修复 plan 持有超卖槽。

    别的修复 plan 看到租约有人拿着，就只能等 —— 理由词是
    ``blocked_by_capacity``，租约的持有者也不变。
    """
    world.working("busy-1")
    world.working("busy-2")

    world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", running_global=0, plan_id="fix-3", capacity=2
    )

    blocked = world.ledger.admit(
        kind=FIX_KIND, repo="repo-b", running_global=1, plan_id="fix-4", capacity=2
    )

    assert blocked == {
        "admit": False,
        "reason": "blocked_by_capacity",
        "oversubscribed": 0,
    }
    assert world.store.get_oversubscribe_lease(world.conn) == "fix-3"


def test_own_oversubscribe_lease_is_not_a_conflict(world: _World) -> None:
    """占着超卖槽的就是自己时，不是冲突。

    与合并槽的口径一致：自己占着不算冲突，不能把自己挡在门外。
    """
    world.working("busy-1")
    world.working("busy-2")
    world.store.set_oversubscribe_lease(world.conn, "fix-3")
    world.conn.commit()

    verdict = world.ledger.admit(
        kind=FIX_KIND, repo="repo-a", running_global=0, plan_id="fix-3", capacity=2
    )

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 1,
    }


def test_the_oversubscribe_lease_is_released_when_the_holder_stops(
    world: _World,
) -> None:
    """持有者停下来，租约就释放。

    租约跟着持有者走：持有者不再占着算力（事实里没有它了），下一个修复
    plan 就能拿到这个槽位。释放走的是**对账**那条路 —— 账本每 tick 都让槽位
    行与事实对齐，租约也一样。
    """
    world.working("fix-3", kind=FIX_KIND, repo="repo-a")
    world.store.set_oversubscribe_lease(world.conn, "fix-3")
    world.conn.commit()

    assert world.store.get_oversubscribe_lease(world.conn) == "fix-3"

    # 持有者停下来：它不再占着算力。
    world.conn.execute("DELETE FROM plan_execution WHERE plan_id = ?", ("fix-3",))
    world.conn.execute("DELETE FROM plan_verification WHERE plan_id = ?", ("fix-3",))
    world.conn.commit()

    world.ledger.reconcile()

    assert world.store.get_oversubscribe_lease(world.conn) is None


# ---------------------------------------------------------------------------
# 6. 数谁 —— 只有 COUNTED_PHASES 里的计划占额度
# ---------------------------------------------------------------------------
def test_only_counted_phases_count(world: _World) -> None:
    """排队等着的修复 plan 不占额度。

    这两个计划**占着算力**（执行侧活着），但相位是 ``queued`` —— 它们在
    等，不在干活，所以不计入额度。若它们被计入，账本看到的全局数就是 2，
    第三个修复 plan 会被推到超卖槽上（``oversubscribed`` 变成 1）；不计入
    的话，它走的是常规额度（``oversubscribed`` 是 0）。

    这条断言只能靠相位过滤成立。
    """
    assert "queued" not in COUNTED_PHASES

    world.working_but_queued("fix-queued-1", kind=FIX_KIND, repo="repo-a")
    world.working_but_queued("fix-queued-2", kind=FIX_KIND, repo="repo-b")

    verdict = world.ledger.admit(kind=FIX_KIND, repo="repo-c")

    assert verdict == {
        "admit": True,
        "reason": "framework_fix_priority",
        "oversubscribed": 0,
    }


# ---------------------------------------------------------------------------
# 7. 上限可配置且持久化
# ---------------------------------------------------------------------------
def test_settings_persisted(tmp_path: Path) -> None:
    """改了上限，重启之后仍然生效。

    上限是运行期设置：它写在状态库的 ``scheduler_setting`` 表里，而不是
    只活在内存里。所以这里关掉连接、重新连上、重新造一个账本（一次
    「重启」），答案不变。
    """
    from state_machine.db.schema import migrate

    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    migrate(conn)
    SqliteSchedulerStore().set_framework_fix_limits(
        conn, max_active=1, oversubscribe=0
    )
    conn.commit()
    conn.close()

    # 重启：新的连接、新的 store、新的账本 —— 同一个数据库文件。
    conn = sqlite3.connect(path)
    try:
        clock = _Clock()
        facts = PlanFactsReader(conn, tmp_path / "plans", clock=clock)
        ledger = CapacityLedger(SqliteSchedulerStore(), facts, clock)

        blocked = ledger.admit(
            kind=FIX_KIND, repo="repo-a", running_global=1
        )
        assert blocked == {
            "admit": False,
            "reason": "blocked_by_capacity",
            "oversubscribed": 0,
        }, "上限改了，重启之后没有生效"

        # 上限是 1，所以**第一个**修复 plan 仍然走常规额度。
        first = ledger.admit(kind=FIX_KIND, repo="repo-a", running_global=0)
        assert first == {
            "admit": True,
            "reason": "framework_fix_priority",
            "oversubscribed": 0,
        }
    finally:
        conn.close()


def test_store_reads_and_writes_the_three_limits(world: _World) -> None:
    """三个上限键在运行期设置上的读写。

    键名是 ``framework_fix.max_active`` / ``framework_fix.max_per_repo`` /
    ``framework_fix.oversubscribe``；没写过的键读回来是 ``None``（“用配置
    里的值”），不是空串。
    """
    assert world.store.get_framework_fix_limits(world.conn) == {
        FRAMEWORK_FIX_MAX_ACTIVE_KEY: None,
        FRAMEWORK_FIX_MAX_PER_REPO_KEY: None,
        FRAMEWORK_FIX_OVERSUBSCRIBE_KEY: None,
    }

    world.store.set_framework_fix_limits(
        world.conn, max_active=3, max_per_repo=4, oversubscribe=2
    )

    assert world.store.get_framework_fix_limits(world.conn) == {
        FRAMEWORK_FIX_MAX_ACTIVE_KEY: "3",
        FRAMEWORK_FIX_MAX_PER_REPO_KEY: "4",
        FRAMEWORK_FIX_OVERSUBSCRIBE_KEY: "2",
    }

    # 只写一个键，另外两个不动。
    world.store.set_framework_fix_limits(world.conn, max_active=5)
    assert world.store.get_framework_fix_limits(world.conn) == {
        FRAMEWORK_FIX_MAX_ACTIVE_KEY: "5",
        FRAMEWORK_FIX_MAX_PER_REPO_KEY: "4",
        FRAMEWORK_FIX_OVERSUBSCRIBE_KEY: "2",
    }


def test_limits_default_to_the_pinned_numbers(world: _World) -> None:
    """没人改过时，上限就是架构决策点 5 钉住的那三个数字。"""
    assert DEFAULT_FRAMEWORK_FIX_MAX_ACTIVE == 2
    assert DEFAULT_FRAMEWORK_FIX_MAX_PER_REPO == 2
    assert DEFAULT_FRAMEWORK_FIX_OVERSUBSCRIBE == 1

    assert world.ledger.framework_fix_limits() == {
        "max_active": 2,
        "max_per_repo": 2,
        "oversubscribe": 1,
    }


def test_limit_setting_wins_over_the_default(world: _World) -> None:
    """运行期设置压过默认值。"""
    world.store.set_framework_fix_limits(world.conn, max_per_repo=1)
    world.conn.commit()

    assert world.ledger.framework_fix_limits()["max_per_repo"] == 1


def test_unusable_limit_setting_is_ignored(world: _World) -> None:
    """读不成数字的设置行被忽略，而不是被照办。

    一个负数不是上限（上限不能是负的），一个不是数字的字符串也不是。
    照办一个读不出来的上限，比用默认值更糟。
    """
    world.store.set_setting(world.conn, FRAMEWORK_FIX_MAX_ACTIVE_KEY, "not a number")
    world.store.set_setting(world.conn, FRAMEWORK_FIX_MAX_PER_REPO_KEY, "-1")
    world.conn.commit()

    limits = world.ledger.framework_fix_limits()

    assert limits["max_active"] == DEFAULT_FRAMEWORK_FIX_MAX_ACTIVE
    assert limits["max_per_repo"] == DEFAULT_FRAMEWORK_FIX_MAX_PER_REPO


# ---------------------------------------------------------------------------
# 8. 原因词都在闭集里
# ---------------------------------------------------------------------------
def test_reason_words_are_in_the_closed_set(world: _World) -> None:
    """账本给出的每个原因词都在调度器的原因闭集 ``REASONS`` 里。

    面板、dry run 与 API 画的都是同一批词；闭集外的词画不出来。
    """
    world.working("fix-1", kind=FIX_KIND, repo="repo-a")
    world.working("fix-2", kind=FIX_KIND, repo="repo-a")
    world.lease("repo-z", "other-fix")

    verdicts = [
        world.ledger.admit(kind=FIX_KIND, repo="repo-a"),
        world.ledger.admit(kind=FIX_KIND, repo="repo-b", running_global=2),
        world.ledger.admit(kind=FIX_KIND, repo="repo-z", plan_id="fix-9"),
        world.ledger.admit(kind="normal", repo="repo-a"),
    ]

    for verdict in verdicts:
        assert verdict["reason"] in REASONS, verdict
        assert set(verdict) == {"admit", "reason", "oversubscribed"}, verdict


# ---------------------------------------------------------------------------
# 9. 普通计划
# ---------------------------------------------------------------------------
def test_ordinary_plan_is_not_blocked_by_the_lane_ceilings(world: _World) -> None:
    """普通计划不受这条 lane 的上限约束。

    这三个上限是 lane 自己的：普通计划由调度器自己的容量决定，账本在
    这里只回答「有没有空槽」。
    """
    world.working("fix-1", kind=FIX_KIND, repo="repo-a")
    world.working("fix-2", kind=FIX_KIND, repo="repo-a")

    verdict = world.ledger.admit(kind="normal", repo="repo-a")

    assert verdict["reason"] in REASONS
    assert verdict["oversubscribed"] == 0


# ---------------------------------------------------------------------------
# 10. 候选行上的保护事实
# ---------------------------------------------------------------------------
def test_the_candidate_row_carries_the_protection_facts(world: _World) -> None:
    """被抢占候选行自带「我是修复计划、不可被抢占」。

    抢占规则读的是**候选行**上的 ``plan_kind`` 与 ``preemptable``。这
    两个键以前没有写入方：账本交给策略的每一行只有 plan_id / 时间戳 /
    elapsed / priority，于是策略答不出「这个受保护吗」—— 键不在行里，
    它就答「不受保护」，于是一条正在跑的修复计划在 operator 触发
    驱逐时被停掉。

    保护事实产在它唯一该在的地方 —— 账本自己的候选行：修复计划是
    ``framework_fix`` 且 ``preemptable`` 恒为 ``False``；普通计划是
    ``normal`` 且 ``preemptable`` 为 ``True``。

    这是**差分**的：两个计划只有种类不同，别的都一样。
    """
    world.working("fix-1", kind=FIX_KIND, repo="repo-a")
    world.working("normal-1", kind="normal")
    world.store.upsert_slot(world.conn, "fix-1", now=_utc_iso(BASE_EPOCH))
    world.store.upsert_slot(world.conn, "normal-1", now=_utc_iso(BASE_EPOCH))
    world.conn.commit()

    candidates = {
        candidate["plan_id"]: candidate
        for candidate in world.ledger.evictable(world.clock(), 0)
    }

    assert set(candidates) == {"fix-1", "normal-1"}, sorted(candidates)
    assert candidates["fix-1"]["plan_kind"] == FIX_KIND, candidates["fix-1"]
    assert candidates["fix-1"]["preemptable"] is False, candidates["fix-1"]
    assert candidates["normal-1"]["plan_kind"] == "normal", candidates["normal-1"]
    assert candidates["normal-1"]["preemptable"] is True, candidates["normal-1"]
