"""TDD spec — 框架修复 lane 的抢占顺序（受害者选择）与展示装饰。

这个模块要解决什么
------------------
PRD 决策点 5 要求：框架修复 plan 创建后**立即**取得执行路径，容量不足时由
调度器**抢占**一个正在运行的普通 plan。被抢占者是谁，必须是确定的 ——
否则两个 tick 会挑中不同的 plan，损失既不可复算也不可度量。

修复 plan 自身**不可被抢占**：两个修复 plan 互相抢占会形成活锁（架构决策
点 5：``preemptable`` 恒为 false）。

于是本模块给两个纯函数：

    select_victim(running: list[dict]) -> dict | None
    decorate_plans(plans, *, registry_rows, leases) -> list[dict]

``select_victim`` 在运行中的计划里挑出一个**可抢占**的受害者，顺序是固定的：

    priority 降序 → attach_blocked_count 升序 → elapsed 升序 → plan_id 升序

* ``priority`` 降序 —— 优先值**大**的先被抢占。队列按优先值升序站队，数字
  最大的那个站在队尾，也就是调度器最捨得打断的那一个（PRD：被抢占者顺序为
  「优先级最低」）；
* ``attach_blocked_count`` 升序 —— 挂在这个计划上的阻塞工作**少**的先被抢占
  （牵连面越小，越先让路）；
* ``elapsed`` 升序 —— 运行得短的先被抢占（已完成的工作最少）；
* ``plan_id`` 升序 —— 兜底位。

最后一位是 ``plan_id``，所以这是一个**全序**：同一份输入永远给出同一个答案，
与输入的排列无关（稳定排序）。

``select_victim`` **不读** ``min_runtime_seconds``：那个保护窗口 ``M`` 属于
operator 触发的驱逐与超容量收敛；框架修复的抢占是硬需求（PRD：修复 plan
创建后立即取得执行路径），不受它约束 —— 所以一个刚跑起来的普通 plan 照样
可以被抢占。

没有可抢占的普通 plan 时答案是 ``None``：调用方**等待**，不扩容量。

``decorate_plans`` 给计划条目补上框架修复的事实：五状态展示投影（读模型，
架构决策点 6）、是否受保护、以及本仓的合并槽是持有还是等待。

TDD contract — 5 cases
-----------------------
1. ``test_order_priority`` —— 优先值大的（最不重要的）先被抢占。
2. ``test_tie_attach_count`` —— 同优先级时 attach 阻塞数少的先被抢占。
3. ``test_framework_protected`` —— 框架修复 plan 永不被选。
4. ``test_ignore_min_runtime`` —— 不受运行时长保护窗口约束。
5. ``test_empty_returns_none`` —— 没有可抢占的普通 plan -> ``None``。

同文件覆盖的边界条件：

* 顺序全序（``plan_id`` 兜底）：同一批计划的两种排列给出同一个答案；
* ``PROTECTED_REASONS`` 恰好是 ``protected_framework_fix`` 一个词，且该词在
  调度器的原因闭集 ``REASONS`` 里；
* ``decorate_plans`` 给出展示状态、保护位与合并槽状态。

纯函数：只读入参，不读时钟、不读文件、不发请求。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _path in (REPO_ROOT, BACKEND_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from backend.framework_fix.scheduling import (  # noqa: E402
    PROTECTED_REASONS,
    decorate_plans,
    select_victim,
)
from plan_scheduler.policy import (  # noqa: E402
    REASONS,
    is_protected,
    protection_reason,
)
from plan_scheduler.policy import select_victim as policy_select_victim  # noqa: E402

#: 被抢占时挂上的原因词 —— 在调度器的原因闭集里。
PREEMPTED_REASON = "preempted_by_framework_fix"


# ---------------------------------------------------------------------------
# 1. 顺序 —— 优先值大的（最不重要的）先被抢占
# ---------------------------------------------------------------------------
def test_order_priority():
    """优先值**大**的先被抢占 —— 那是优先级最低的那一个。

    顺序的第一位是 ``priority`` 降序。队列按优先值升序站队，数字最大的那个
    站在队尾，也就是调度器最捨得打断的那一个（PRD：被抢占者顺序为「优先级
    最低」）。

    这里的两个计划**只**在 priority 上有差别 —— attach 阻塞数与运行时长都
    指向另一个 —— 所以这条断言只能靠 priority 这一位成立。
    """
    running = [
        {"plan_id": "b", "priority": 3, "attach_blocked_count": 0, "elapsed_s": 100},
        {"plan_id": "a", "priority": 5, "attach_blocked_count": 9, "elapsed_s": 5},
    ]

    assert select_victim(running) == {
        "plan_id": "a",
        "reason": PREEMPTED_REASON,
    }


def test_order_priority_matches_the_prd_example():
    """PRD 的输入输出例子，逐字成立。

    PRD 的例子：``a``（priority 5 / attach 0 / elapsed 100）与 ``b``
    （priority 3 / attach 2 / elapsed 50），答案是 ``a`` —— 优先值大的那个
    （优先级最低的那个）先被抢占。
    """
    running = [
        {"plan_id": "a", "priority": 5, "attach_blocked_count": 0, "elapsed_s": 100},
        {"plan_id": "b", "priority": 3, "attach_blocked_count": 2, "elapsed_s": 50},
    ]

    assert select_victim(running) == {
        "plan_id": "a",
        "reason": PREEMPTED_REASON,
    }


# ---------------------------------------------------------------------------
# 2. 顺序 —— 同优先级时 attach 阻塞数少的先被抢占
# ---------------------------------------------------------------------------
def test_tie_attach_count():
    """同优先级时，attach 阻塞数**少**的先被抢占。

    顺序的第二位是 ``attach_blocked_count`` 升序：挂在这个 plan 上的阻塞
    工作越少，抢占它牵连的面越小。

    这里的两个计划**只**在 attach 阻塞数上有差别（运行时长指向另一个），
    所以这条断言只能靠 attach 这一位成立。
    """
    running = [
        {"plan_id": "a", "priority": 3, "attach_blocked_count": 1, "elapsed_s": 100},
        {"plan_id": "b", "priority": 3, "attach_blocked_count": 4, "elapsed_s": 10},
    ]

    assert select_victim(running) == {
        "plan_id": "a",
        "reason": PREEMPTED_REASON,
    }


def test_order_elapsed_then_plan_id():
    """attach 阻塞数也相同 -> 运行时长短的先被抢占；再相同 -> plan_id 小的。

    顺序的第三位是 ``elapsed`` 升序（运行得短的丢失最少），第四位是
    ``plan_id`` 升序（把顺序变成全序，答案与输入排列无关）。
    """
    same_attach = [
        {"plan_id": "a", "priority": 3, "attach_blocked_count": 2, "elapsed_s": 100},
        {"plan_id": "b", "priority": 3, "attach_blocked_count": 2, "elapsed_s": 10},
    ]
    assert select_victim(same_attach)["plan_id"] == "b"

    identical = [
        {"plan_id": "b", "priority": 3, "attach_blocked_count": 2, "elapsed_s": 10},
        {"plan_id": "a", "priority": 3, "attach_blocked_count": 2, "elapsed_s": 10},
    ]
    assert select_victim(identical)["plan_id"] == "a"


def test_order_is_stable_across_input_permutations():
    """同一批计划的两种排列给出同一个答案。

    顺序是全序（末位是 ``plan_id``），所以答案与输入的排列无关 —— 这是
    「被抢占者是谁」可复算的前提。
    """
    rows = [
        {"plan_id": "a", "priority": 3, "attach_blocked_count": 1, "elapsed_s": 30},
        {"plan_id": "b", "priority": 3, "attach_blocked_count": 1, "elapsed_s": 30},
        {"plan_id": "c", "priority": 1, "attach_blocked_count": 2, "elapsed_s": 70},
    ]

    first = select_victim(rows)
    second = select_victim(list(reversed(rows)))

    assert first == second
    assert first["plan_id"] == "a", (
        "优先值最大的先被抢占（``a`` 与 ``b`` 都是 3，``c`` 是 1）；实际 "
        f"{first!r}"
    )


# ---------------------------------------------------------------------------
# 3. 保护 —— 框架修复 plan 永不被选
# ---------------------------------------------------------------------------
def test_framework_protected():
    """框架修复 plan 永不被选为受害者。

    这一条断言不靠它在队列里的位置：这里的修复计划拿着**最大的**优先值
    —— 没有了保护，它就是排序的第一个（被抢占者顺序是优先值降序），正是
    这条断言要钉住的地方。

    两个修复 plan 互相抢占会形成活锁，所以保护是结构性的，不是「尽量」。
    """
    running = [
        {
            "plan_id": "fx-1",
            "priority": 9,
            "attach_blocked_count": 9,
            "elapsed_s": 1,
            "plan_kind": "framework_fix",
        },
        {
            "plan_id": "ordinary",
            "priority": 5,
            "attach_blocked_count": 0,
            "elapsed_s": 100,
        },
    ]

    assert select_victim(running) == {
        "plan_id": "ordinary",
        "reason": PREEMPTED_REASON,
    }


def test_only_framework_plans_returns_none():
    """只有框架修复 plan 时没有受害者 —— 等待，不抢占自己的同类。"""
    running = [
        {
            "plan_id": "fx-1",
            "priority": 0,
            "attach_blocked_count": 3,
            "elapsed_s": 10,
            "plan_kind": "framework_fix",
        },
        {
            "plan_id": "fx-2",
            "priority": 0,
            "attach_blocked_count": 1,
            "elapsed_s": 10,
            "plan_kind": "framework_fix",
        },
    ]

    assert select_victim(running) is None


def test_protected_reasons_is_one_word():
    """受保护的原因词闭集恰好一个词，且在调度器的原因闭集里。

    ``protected_framework_fix`` 是 ``REASONS`` 里的词，调度器的原因词表是
    闭集 —— 一个闭集外的词，面板画不出来。
    """
    assert PROTECTED_REASONS == frozenset({"protected_framework_fix"})
    assert PROTECTED_REASONS <= REASONS


# ---------------------------------------------------------------------------
# 4. 保护窗口 —— 抢占不受 min_runtime_seconds 约束
# ---------------------------------------------------------------------------
def test_ignore_min_runtime():
    """刚跑起来的普通 plan 照样可以被抢占。

    ``min_runtime_seconds``（``M``）保护的是 **operator 触发的驱逐**与超容量
    收敛；框架修复的抢占是硬需求（PRD：修复 plan 创建后立即取得执行路径），
    不受它约束。所以 ``elapsed_s`` 只有 1 秒的计划照样是受害者。
    """
    running = [
        {
            "plan_id": "young",
            "priority": 2,
            "attach_blocked_count": 0,
            "elapsed_s": 1,
            "min_runtime_seconds": 600,
        },
    ]

    assert select_victim(running) == {
        "plan_id": "young",
        "reason": PREEMPTED_REASON,
    }


# ---------------------------------------------------------------------------
# 5. 没有受害者
# ---------------------------------------------------------------------------
def test_empty_returns_none():
    """没有运行中的计划 -> 没有受害者。"""
    assert select_victim([]) is None


# ---------------------------------------------------------------------------
# 6. 展示装饰
# ---------------------------------------------------------------------------
def test_decorate_marks_framework_plan_protected():
    """修复 plan 被装饰成「受保护」，并挂上 ``protected_framework_fix``。"""
    plans = [{"plan_id": "fx-1", "plan_kind": "framework_fix"}]
    registry_rows = [
        {
            "fingerprint": "sha256:aa",
            "fix_plan_id": "fx-1",
            "repo": "repo-a",
            "status": "active",
        }
    ]
    leases = [
        {"repo": "repo-a", "fix_plan_id": "fx-1", "pr_url": "https://pr/1"}
    ]

    decorated = decorate_plans(plans, registry_rows=registry_rows, leases=leases)

    assert len(decorated) == 1
    entry = decorated[0]
    assert entry["plan_id"] == "fx-1"
    assert entry["protected"] is True
    assert entry["protected_reason"] == "protected_framework_fix"
    assert entry["fingerprint"] == "sha256:aa"
    assert entry["repo"] == "repo-a"
    assert entry["merge_slot"] == "held"
    assert entry["merge_slot_reason"] is None
    assert entry["pr_url"] == "https://pr/1"


def test_decorate_projects_display_state():
    """展示状态来自读模型的五状态投影（架构决策点 6）。

    一个被框架缺陷挂起的普通 plan -> ``blocked_on_framework_fix``；没有框架
    修复状态的普通 plan -> ``None``（调用方沿用原来的相位）。
    """
    plans = [
        {
            "plan_id": "p-1",
            "plan_kind": "normal",
            "framework_fix": {"hold": True, "paused_by": {"fix_plan_id": "fx-1"}},
        },
        {"plan_id": "p-2", "plan_kind": "normal"},
    ]
    registry_rows = [
        {
            "fingerprint": "sha256:aa",
            "fix_plan_id": "fx-1",
            "repo": "repo-a",
            "status": "active",
        }
    ]

    decorated = decorate_plans(plans, registry_rows=registry_rows, leases=[])

    assert decorated[0]["display_state"] == "blocked_on_framework_fix"
    assert decorated[0]["protected"] is False
    assert decorated[0]["protected_reason"] is None
    assert decorated[1]["display_state"] is None
    assert decorated[1]["protected"] is False


def test_decorate_reports_merge_slot_waiting():
    """本仓的合并槽被**别人**持有时，修复 plan 挂在 ``waiting_merge_slot`` 上。

    合并串行与抢占正交：它限制的是修复 plan 之间的未合入 PR 数量。
    """
    plans = [{"plan_id": "fx-2", "plan_kind": "framework_fix"}]
    registry_rows = [
        {
            "fingerprint": "sha256:bb",
            "fix_plan_id": "fx-2",
            "repo": "repo-a",
            "status": "active",
        }
    ]
    leases = [{"repo": "repo-a", "fix_plan_id": "fx-1", "pr_url": "https://pr/1"}]

    decorated = decorate_plans(plans, registry_rows=registry_rows, leases=leases)

    entry = decorated[0]
    assert entry["merge_slot"] == "waiting"
    assert entry["merge_slot_reason"] == "waiting_merge_slot"
    # 合并槽在别人手里，本 plan 没有自己的 PR 链接。
    assert entry["pr_url"] is None


def test_decorate_does_not_mutate_the_input():
    """装饰不改输入 —— 调用方拿到的还是它自己那份列表。"""
    plans = [{"plan_id": "fx-1", "plan_kind": "framework_fix"}]

    decorated = decorate_plans(plans, registry_rows=[], leases=[])

    assert plans == [{"plan_id": "fx-1", "plan_kind": "framework_fix"}]
    assert decorated is not plans
    assert decorated[0]["plan_id"] == "fx-1"


# ---------------------------------------------------------------------------
# 7. 调度器策略里的保护位（policy.py 的使用点）
# ---------------------------------------------------------------------------
def test_policy_never_evicts_a_framework_fix():
    """调度器的驱逐也不碰框架修复计划。

    operator 触发的驱逐与超容量收敛走的是调度器自己的 ``select_victim``，
    它也必须跳过受保护的计划 —— 修复计划在**任何**路径下都不能被抢走。
    """
    snapshot = {
        "running": [
            {
                "plan_id": "fx-1",
                "elapsed": 900,
                "priority": 0,
                "plan_kind": "framework_fix",
            },
            {"plan_id": "ordinary", "elapsed": 900, "priority": 5},
        ]
    }

    assert policy_select_victim(snapshot, 600) == "ordinary"


def test_policy_protection_reason():
    """受保护的理由词是 ``protected_framework_fix``，普通计划没有理由。"""
    framework_fix = {"plan_id": "fx-1", "plan_kind": "framework_fix"}
    ordinary = {"plan_id": "p-1", "plan_kind": "normal"}

    assert is_protected(framework_fix) is True
    assert protection_reason(framework_fix) == "protected_framework_fix"
    assert is_protected(ordinary) is False
    assert protection_reason(ordinary) is None
    assert protection_reason(framework_fix) in REASONS
