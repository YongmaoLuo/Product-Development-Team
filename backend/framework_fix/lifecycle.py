"""框架缺陷的生命周期：挂起（hold）与重试预算冻结。

这个模块解决什么
----------------
命中一个框架缺陷之后，原计划必须**停下来**等修复 —— 但停下来不等于把预算
烧光。一个被框架缺陷挡住的验证点如果照常消耗重试预算，自动修复循环就会把它
反复烧掉：修复合入 → 重跑 → 还是同样的框架缺陷 → 再修一次……直到预算耗尽，
而每一次消耗的都是用户自己的重试额度。

于是「挂起」这件事有三样东西必须同时成立：

1. **计划真的停着** —— 计划状态里的 ``framework_fix`` 块被写上 ``hold``，
   原计划不再往下走（任务 17 的 ``enter_block``）；
2. **预算真的被冻住** —— 挂起那一刻的重试计数被记下来，释放后从那个数继续，
   挂起期间发生的失败不算数（:meth:`FrameworkFixLifecycle.hold` /
   :meth:`FrameworkFixLifecycle.release_hits`）；
3. **关系真的记下来了** —— 谁在等哪个缺陷的修复，落在注册表的挂接行里
   （任务 11 的 ``attach``）。

三条要一起成立：只有 1 没有 2，计划会被修复循环烧光；只有 2 没有 1，计划还
在跑，冻结的预算没有意义；只有 3 没有 1、2，注册表里多一行谁也看不见。

还有第四样，是给人看的
----------------------
挂起意味着「这个缺陷有人在修了」—— 而这件事得**说出来**。挂起时发一条
``discovered`` 通知（同一个修复计划只发一次，标记写在审计链上），否则修复合
入、重跑、失败、重开这一整串事都发生在没有人知道的地方。

谁回答「现在还挂着吗」
----------------------
**审计链**（``framework_fix_event``）。挂接行是**历史**（「谁曾经等过这个
修复」），所以「这个挂接现在还挂着吗」由审计链回答：这条挂接最后一条生命
周期事件是 ``held`` 就是还挂着，是 ``released`` 就是已经放开了。

挂接行的 ``status`` 说的是**另一件事**：这条挂接现在**占不占着**那个计划的
执行（``blocked`` 占着、``registered`` 不占、``exhausted`` 不占但等人拍板）。
状态词是注册表自己的闭集，由注册表写（``set_attach_status``）—— 别处用一条
裸 ``UPDATE`` 改它，审计链上就没有这次改动的记录。

为什么冻结值存在审计链里
------------------------
「挂起那一刻计数是多少」这件事必须**活得比这次进程长**：释放可能发生在另一个
进程里（调度 tick、验证子进程、运维手动操作）。审计链只追加，正好放这种事实。

重试计数是哪一列
----------------
``plan_tasks.attempt``。验证点失败一次，它名下的修复任务被复活一次，计数
+1 —— 那是这个验证点已经消耗掉的重试次数。挂起冻结的正是它：挂起时读一次
记下来，释放时把它按回去（挂起窗口里被谁动过都按回挂起那一刻的值）。

挂起块与挂接行的分工
--------------------
一个计划只有**一个**挂起块（计划状态里那一列 JSON），一个验证点一条挂接行。
同一个计划挂起三个验证点，是三条挂接行 + 一个块 —— 块说「这个计划停着」，
挂接行说「它停着等哪个缺陷的哪个验证点」。

包形态
------
与同包的 ``fingerprint.py`` / ``registry.py`` / ``read_model.py`` 一致：
``backend/framework_fix/`` 是 PEP 420 的隐式命名空间包，没有 ``__init__.py``。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from config_paths import resolve_plans_dir
except ImportError:  # pragma: no cover
    from backend.config_paths import resolve_plans_dir  # type: ignore

try:  # pragma: no cover
    from framework.clock import utcnow_iso
except ImportError:  # pragma: no cover
    from backend.framework.clock import utcnow_iso  # type: ignore

try:  # pragma: no cover
    from framework_fix.registry import (
        ATTACH_STATUS_EXHAUSTED,
        REGISTRY_STATUS_FIXED,
        FrameworkFixRegistry,
        _as_round_no,
        _as_vp_id,
        _require_text,
    )
except ImportError:  # pragma: no cover
    from backend.framework_fix.registry import (  # type: ignore
        ATTACH_STATUS_EXHAUSTED,
        REGISTRY_STATUS_FIXED,
        FrameworkFixRegistry,
        _as_round_no,
        _as_vp_id,
        _require_text,
    )

try:  # pragma: no cover
    from framework_fix.scheduling import FRAMEWORK_FIX_KIND
except ImportError:  # pragma: no cover
    from backend.framework_fix.scheduling import FRAMEWORK_FIX_KIND  # type: ignore

try:  # pragma: no cover
    from state_machine.db.connection import open as open_db
except ImportError:  # pragma: no cover
    from backend.state_machine.db.connection import open as open_db  # type: ignore

try:  # pragma: no cover
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
except ImportError:  # pragma: no cover
    from backend.state_machine.repositories.plan_task_repository import (  # type: ignore
        PlanTaskRepository,
    )

try:  # pragma: no cover
    from verification_persistence import (
        RESULT_SOURCE_FRAMEWORK_FIX_RERUN,
        RESULT_SOURCE_INITIAL,
        RESULT_SOURCE_LATE_HIT_RERUN,
        append_verification_result,
        normalize_result_verdict,
    )
except ImportError:  # pragma: no cover
    from backend.verification_persistence import (  # type: ignore
        RESULT_SOURCE_FRAMEWORK_FIX_RERUN,
        RESULT_SOURCE_INITIAL,
        RESULT_SOURCE_LATE_HIT_RERUN,
        append_verification_result,
        normalize_result_verdict,
    )

try:  # pragma: no cover
    from hitl.models import DecisionRequest
except ImportError:  # pragma: no cover
    from backend.hitl.models import DecisionRequest  # type: ignore

try:  # pragma: no cover
    from hitl.service import create_decision
except ImportError:  # pragma: no cover
    from backend.hitl.service import create_decision  # type: ignore

try:  # pragma: no cover
    from hitl.store import HitlStore
except ImportError:  # pragma: no cover
    from backend.hitl.store import HitlStore  # type: ignore

try:  # pragma: no cover
    from framework_fix.notify import emit as emit_notify
except ImportError:  # pragma: no cover
    from backend.framework_fix.notify import emit as emit_notify  # type: ignore


__all__ = [
    "DISCOVERED_NOTIFY_MARKER",
    "EVENT_EXHAUSTED",
    "EVENT_HELD",
    "EVENT_HOLD_ROLLED_BACK",
    "EVENT_LATE_HIT_RERUN",
    "EVENT_RELEASED",
    "EVENT_REOPENED",
    "EVENT_RERUN_FAILED",
    "EVENT_RERUN_PASSED",
    "MAX_FIX_ROUNDS",
    "FrameworkFixLifecycle",
    "HoldReceipt",
    "exhaust",
    "handle_fix_plan_hit",
    "handle_late_hit",
    "hit_material_of",
    "hold",
    "record_initial_result",
    "release_hits",
    "reopen",
    "rerun_after_merge",
]


# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------
#: 审计链上的事件类型。
#:
#: ``held`` —— 挂起了，载荷里带着这次冻结下来的重试计数；
#: ``released`` —— 放开了，载荷里带着当时冻结的那个计数；
#: ``hold_rolled_back`` —— 挂起写到一半失败了，之前那几步被撤销了。
EVENT_HELD = "held"
EVENT_RELEASED = "released"
EVENT_HOLD_ROLLED_BACK = "hold_rolled_back"

#: 修复合入后重跑通过了 —— 载荷里带着这棵树与结论。
EVENT_RERUN_PASSED = "rerun_passed"

#: 修复合入后重跑仍失败 —— 载荷里带着这棵树与结论。
EVENT_RERUN_FAILED = "rerun_failed"

#: 重跑仍失败 → 缺陷重新打开，同一个修复计划继续跑下一轮。
EVENT_REOPENED = "reopened"

#: 同一签名连续失败到上限 —— 需人工决策，不再自动重试。
EVENT_EXHAUSTED = "exhausted"

#: 已修复的签名又被撞上（延迟命中）→ 在含修复的树上重跑了一次。
#:
#: 它与 ``rerun_passed`` / ``rerun_failed`` 的区别不在**结论**（结论还是那
#: 两个词），而在**为什么会有这一轮**：这一轮是修复合入**之后**才撞上的。
EVENT_LATE_HIT_RERUN = "late_hit_rerun"

#: 生命周期自己写进审计链的事件类型 —— 读链时只看这些。
_LIFECYCLE_EVENTS = (EVENT_HELD, EVENT_RELEASED)

#: 重跑这条链上的事件类型。它们**不在** :data:`_LIFECYCLE_EVENTS` 里 ——
#: 「这条挂接现在还挂着吗」只由 ``held`` / ``released`` 回答，重跑的结论是
#: 另一件事：它说的是「重跑之后怎么样」，不是「挂起解没解」。
_RERUN_EVENTS = (
    EVENT_RERUN_PASSED,
    EVENT_RERUN_FAILED,
    EVENT_REOPENED,
    EVENT_EXHAUSTED,
)

#: 同一签名最多修几轮。超过就是耗尽 —— 转人工决策，不再自动重试。
MAX_FIX_ROUNDS = 2

#: 「已经通知过『发现了』」这件事在审计链上的标记行。
#:
#: 它必须是**持久**的：通知这件事本身是「一次性」的，但「只通知一次」这个
#: 约定必须活得比进程长 —— 进程内的列表一重启就失忆，于是每重启一次就再
#: 喊一遍。而静默窗口吞掉的那一次**不算**通知过（它根本没送出去），所以
#: 标记只在真的送到时才写。
DISCOVERED_NOTIFY_MARKER = "notify_discovered_sent"

#: 本模块的日志。
logger = logging.getLogger(__name__)

#: 任务行的「做完了」状态 —— 这些任务不再欠着什么。
#:
#: 别的状态（``pending`` / ``running`` / ``failed`` / 空）都算**还有活**：
#: 失败过的任务还要被复活修一次，而被复活的那次尝试会再 +1 预算。
_FINISHED_TASK_STATUSES = frozenset({"completed", "skipped"})

#: 验证裁决里「这条验证点不再欠着什么」的两个词。
#:
#: ``passed`` 是过了；``skipped`` 是这一条不适用（评审时被跳过）—— 两者都
#: 不再要求这条计划再跑一次。
_SETTLED_VERDICTS = frozenset({"passed", "skipped"})

#: 耗尽之后挂接的计划在 ``framework_fix`` 块里写的状态。读模型
#: （``framework_fix.read_model``）把这个词投影成对外可见的「耗尽」。
EXHAUSTED_BLOCK_STATUS = "framework_fix_exhausted"


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------
class HoldReceipt(str):
    """挂起凭据 —— 它**就是**挂接 id，同时带着这次冻结下来的重试预算。

    它是 :class:`str` 的子类：``attach_id = hold(...)`` 拿到的就是那个挂接
    id 本身，不需要解包。同时它按映射的方式读得出那份凭据 ——
    ``receipt["attach_id"]`` / ``receipt["frozen_budget"]`` /
    ``dict(receipt)`` —— 免得调用方为了看冻结了多少预算再读一次库。

    Args:
        attach_id: 挂接 id（``framework_fix_attach.attach_id``）。
        frozen_budget: 挂起那一刻的重试计数。

    Examples:
        >>> receipt = HoldReceipt("a1", 2)
        >>> receipt == "a1"
        True
        >>> receipt["frozen_budget"]
        2
        >>> dict(receipt) == {"attach_id": "a1", "frozen_budget": 2}
        True
    """

    def __new__(cls, attach_id: str, frozen_budget: int) -> "HoldReceipt":
        receipt = super().__new__(cls, attach_id)
        receipt.attach_id = attach_id
        receipt.frozen_budget = int(frozen_budget)
        return receipt

    def keys(self) -> tuple:
        """凭据有哪些键 —— 让 ``dict(receipt)`` 能用。"""
        return ("attach_id", "frozen_budget")

    def __getitem__(self, key: Any) -> Any:
        if key == "attach_id":
            return self.attach_id
        if key == "frozen_budget":
            return self.frozen_budget
        raise KeyError(key)

    def as_dict(self) -> Dict[str, Any]:
        """凭据的那份字典。"""
        return {"attach_id": self.attach_id, "frozen_budget": self.frozen_budget}

    def __repr__(self) -> str:
        return (
            f"HoldReceipt(attach_id={self.attach_id!r}, "
            f"frozen_budget={self.frozen_budget!r})"
        )


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
class FrameworkFixLifecycle:
    """框架缺陷的生命周期：挂起、冻结预算、释放。

    两个协作者：

    * ``registry`` —— 注册表（任务 11）。挂接关系与指纹都在它那里；
    * ``plan_state`` —— 计划状态（任务 17）。挂起块由它写。

    ``plan_state`` 可以不给：不给的时候按生产路径自己造一个（计划目录 =
    ``<plans>/<plan_id>``）。给了就用给的那个 —— 测试与多计划场景要靠它把
    挂起块写到正确的地方去。

    Examples:
        >>> lifecycle = FrameworkFixLifecycle()               # doctest: +SKIP
        >>> lifecycle.hold(                                  # doctest: +SKIP
        ...     plan_id="plan-a", vp_id="VP-3",
        ...     fingerprint="sha256:...", round_no=2,
        ...     source="clean_main_repro",
        ... )
        HoldReceipt(attach_id='a1', frozen_budget=2)
    """

    def __init__(
        self,
        registry: Optional[FrameworkFixRegistry] = None,
        *,
        plan_state: Optional[Any] = None,
    ) -> None:
        """装好这台生命周期。

        Args:
            registry: 注册表。``None`` 表示「问应用自己用哪个库」。
            plan_state: 计划状态。``None`` 表示按计划目录自己造一个。
        """
        self._registry = (
            registry if registry is not None else FrameworkFixRegistry()
        )
        self._plan_state = plan_state

    @property
    def registry(self) -> FrameworkFixRegistry:
        """这台生命周期用的是哪个注册表。"""
        return self._registry

    # -- 挂起 --------------------------------------------------------------
    def hold(
        self,
        *,
        plan_id: str,
        vp_id: str,
        fingerprint: str,
        round_no: int,
        source: str,
    ) -> HoldReceipt:
        """把一个验证点挂起，冻结它的重试预算。

        一次挂起做五件事，顺序是有讲究的：

        1. **查** —— 指纹必须登记过。没登记过的指纹抛 ``KeyError``：挂一个
           没人登记的缺陷等于什么都没挂住，而原计划会以为自己在等一个不会来
           的修复。
        2. **记关系** —— 注册表里落一条挂接行（任务 11 的 ``attach``）。同一
           个 ``(指纹, 计划, 验证点)`` 永远只有一行、一个 id。
        3. **记冻结值** —— 把挂起这一刻的重试计数写进审计链。释放时要靠它
           把计数按回去：挂起期间发生的失败不补扣预算。
        4. **挂计划** —— 计划状态里写上挂起块（任务 17 的 ``enter_block``）。
        5. **通知** —— 告诉人「这个缺陷有人修了」（``discovered``）。同一个
           修复计划只通知一次，标记写在审计链上。

        第 4 步写不进去 → 前面几步一起撤销（新落下的挂接行撤掉，审计链上留
        一条回滚事件），异常照原样抛出去。挂起失败绝不能留下「挂上了」的假
        象：计划状态没变，挂接关系也不该多出新的。

        第 5 步坏了**不撤销**挂起：通知不是主流程，出口挂了不该把修复流程
        带崩。没送出去的那一次不算通知过 —— 下一次挂起补上。

        幂等：同一个 ``(指纹, 计划, 验证点)`` **还挂着**的时候再挂一次，
        返回同一个挂接 id、**不重新冻结**预算 —— 冻结的是第一次挂起时的那一
        笔。挂过一次又放开了的，再挂算**新的一次挂起**：重新冻结当时的计数，
        审计链上多一条 ``held``。

        Args:
            plan_id: 被挂住的计划。
            vp_id: 被挂住的验证点。空字符串表示「整条计划挂起」。
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。
            round_no: 挂起发生在第几轮。
            source: 挂接来源（如 ``clean_main_repro``）。

        Returns:
            挂接 id —— :class:`HoldReceipt`，它**就是**那个字符串，同时带着
            冻结下来的预算（``receipt["frozen_budget"]``）。

        Raises:
            KeyError: 这个指纹没有登记过。
            ValueError: 参数不合法（空计划号、空来源、负轮次）。
        """
        plan = _require_text(plan_id, "plan_id")
        vp = _as_vp_id(vp_id)
        number = _as_round_no(round_no)
        origin = _require_text(source, "source")

        row = self._registry.lookup(fingerprint)
        if row is None:
            raise KeyError(
                f"no registered framework defect for fingerprint {fingerprint!r}: "
                "a hold on a defect nobody registered holds nothing"
            )
        canonical = str(row["fingerprint"])
        fix_plan_id = row.get("fix_plan_id")

        state = self._chain_state()
        existing = self._attach_for(canonical, plan, vp)
        if existing is not None and str(existing["attach_id"]) in _held(state):
            # 幂等：同一个 (指纹, 计划, 验证点) 只挂一次。预算冻结在**第一次**
            # 挂起时 —— 重新冻结会把挂起窗口里的变化算进预算里去。
            budget = self._frozen_budget(str(existing["attach_id"]), state)
            self._enter_block(plan, canonical, fix_plan_id)
            self._apply_blocking_phase(plan)
            # 通知在两条路上都发：上一次的通知被静默窗口吞掉了的话，这一次
            # 要把它补上。
            self._announce_discovery(canonical, fix_plan_id, plan)
            return HoldReceipt(str(existing["attach_id"]), budget)

        budget = self._retry_count(plan, vp)
        attach_id = self._registry.attach(canonical, plan, vp, number, origin)
        try:
            self._registry.append_event(
                EVENT_HELD,
                {
                    "attach_id": attach_id,
                    "plan_id": plan,
                    "vp_id": vp,
                    "round": number,
                    "source": origin,
                    "frozen_budget": budget,
                },
                fingerprint=canonical,
                fix_plan_id=fix_plan_id,
            )
            self._enter_block(plan, canonical, fix_plan_id)
        except BaseException:
            self._rollback_hold(
                attach_id, canonical, plan, vp, created=existing is None
            )
            raise
        # 相位在挂起块**之后**判定：挂起块是「这件事发生了」，相位是「这件事
        # 对计划意味着什么」。相位判定失败不回滚挂起 —— 挂起本身是成立的，
        # 挂起块已经写下去了。
        self._apply_blocking_phase(plan)
        # 通知在挂起**成立之后**发：挂起失败的那一次不算「发现了要修」——
        # 那个缺陷还在，但没有人被挂住。
        self._announce_discovery(canonical, fix_plan_id, plan)
        return HoldReceipt(attach_id, budget)

    # -- 通知 --------------------------------------------------------------
    def _announce_discovery(
        self, fingerprint: str, fix_plan_id: Any, plan_id: str
    ) -> None:
        """通知一次：这个人要修一个框架缺陷。

        「发现」这个事实对同一个人说一次就够：N 个验证点挂到同一个修复
        计划上，是 N 条挂接行，但只有一条通知 —— 否则同一个人会收到 N 条
        一样的消息，而「消息太多」正是人学会忽略通知的方式。

        标记写在审计链上（:data:`DISCOVERED_NOTIFY_MARKER`），不在进程的内存
        里：进程重启之后不该再喊一遍，而静默窗口吞掉的那一次要在窗口过去
        之后补上 —— 所以标记**只在真的送到时才写**。

        通知不是主流程：出口挂了、数据库打不开，这些都不该把挂起带崩。
        """
        fix_plan = str(fix_plan_id or "").strip()
        if not fix_plan:
            # 没有修复计划就没有「发现了并创建了修复 plan」这件事 —— 不发。
            return
        try:
            already = self._registry.has_event(
                DISCOVERED_NOTIFY_MARKER,
                fix_plan_id=fix_plan,
                fingerprint=fingerprint,
            )
        except Exception as exc:  # noqa: BLE001 — 通知坏了不能掀翻挂起
            logger.warning(
                "framework_fix_notify_discovered_lookup_failed: "
                "fix_plan=%s error=%s: %s",
                fix_plan,
                type(exc).__name__,
                exc,
            )
            return
        if already:
            return

        try:
            plan_dir = self._plan_dir_for(plan_id)
        except Exception as exc:  # noqa: BLE001 — 通知坏了不能掀翻挂起
            logger.warning(
                "framework_fix_notify_discovered_plan_dir_failed: fix_plan=%s "
                "plan_id=%s error=%s: %s",
                fix_plan,
                plan_id,
                type(exc).__name__,
                exc,
            )
            return
        if plan_dir is None or not Path(plan_dir).exists():
            # 没有地方可送 —— 不发，也不写标记：等有地方可送了再补。
            logger.info(
                "framework_fix_notify_discovered_skipped: fix_plan=%s: "
                "the plan directory of %s is not there",
                fix_plan,
                plan_id,
            )
            return

        payload: Dict[str, Any] = {
            "fix_plan_id": fix_plan,
            "fingerprint": fingerprint,
            "plan_id": plan_id,
            "plan_dir": str(plan_dir),
        }
        try:
            record = emit_notify("discovered", payload)
        except Exception as exc:  # noqa: BLE001 — 通知坏了不能掀翻挂起
            logger.warning(
                "framework_fix_notify_discovered_failed: fix_plan=%s "
                "error=%s: %s",
                fix_plan,
                type(exc).__name__,
                exc,
            )
            return
        if not isinstance(record, dict) or not record.get("delivered"):
            # 没送到（静默窗口吞了、出口挂了）—— 不写标记，窗口过去之后补。
            logger.info(
                "framework_fix_notify_discovered_not_delivered: fix_plan=%s: %s",
                fix_plan,
                (record or {}).get("suppressed") if isinstance(record, dict)
                else "no record",
            )
            return

        try:
            self._registry.append_event(
                DISCOVERED_NOTIFY_MARKER,
                {
                    "fix_plan_id": fix_plan,
                    "fingerprint": fingerprint,
                    "plan_id": plan_id,
                },
                fingerprint=fingerprint,
                fix_plan_id=fix_plan,
            )
        except Exception as exc:  # noqa: BLE001 — 标记写不进去也不能掀翻挂起
            logger.warning(
                "framework_fix_notify_discovered_mark_failed: fix_plan=%s "
                "error=%s: %s",
                fix_plan,
                type(exc).__name__,
                exc,
            )

    # -- 释放 --------------------------------------------------------------
    def release_hits(self, fingerprint: str) -> int:
        """释放这个缺陷挂住的所有验证点，返回释放了几条。

        释放做三件事：

        1. **按回计数** —— 把重试计数按回挂起那一刻的值。挂起期间发生的
           失败不补扣预算：释放之后，计数从挂起那一刻继续。
        2. **记审计** —— 审计链上留一条 ``released`` 事件。「现在还挂着吗」
           就是由这条事件回答的。
        3. **解挂** —— 这个计划名下**一条**挂接都不剩了，才解除它的挂起块。
           还挂着别的东西的计划不能被顺带解挂。
        4. **回队列** —— 停在阻塞相位上的计划（全部剩余工作都被挡住过）
           回到调度队列：挂起被释放意味着「挡路的东西没了」，计划该回去把
           那件事做完。耗尽（:meth:`exhaust`）不走这条路 —— 那条计划在等人
           决策，回队列等于让它再去撞一次同样的缺陷。

        挂接行本身**不动**：它是历史（「谁曾经等过这个修复」），注册表的
        ``status`` 词表是注册表自己的闭集，生命周期不往里写第三个词。

        释放一个没人登记过的指纹 → 释放 0 条（没有东西可释放，这不是错误）。

        Args:
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。

        Returns:
            释放了几条挂接。
        """
        canonical = self._canonical_or_none(fingerprint)
        if canonical is None:
            return 0
        return self._release_hits(canonical, requeue=True)

    def _release_hits(self, canonical: str, *, requeue: bool) -> int:
        """释放这个缺陷挂住的挂接（内部实现）。

        Args:
            canonical: 规范指纹。
            requeue: 挂起被释放之后，要不要把停在阻塞相位上的计划放回调度
                队列。**释放**要（修复合入了，计划该回去再跑一遍）；**耗尽**
                不要（那条计划在等人决策，回队列等于让它再去撞一次同样的
                缺陷）。
        """
        state = self._chain_state()
        held = _held(state)
        rows = [
            row
            for row in self._registry.list_attach(canonical)
            if str(row["attach_id"]) in held
        ]
        if not rows:
            return 0

        registry_row = self._registry.lookup(canonical)
        fix_plan_id = (
            registry_row.get("fix_plan_id") if registry_row is not None else None
        )

        released = 0
        for row in rows:
            attach_id = str(row["attach_id"])
            plan_id = str(row["plan_id"])
            vp_id = str(row["vp_id"])
            budget = self._frozen_budget(attach_id, state)
            # 顺序有讲究：计数先按回去，再落审计事件。审计事件落不下去，这
            # 一条就仍然算挂着（链上最后一条还是 held），调用方可以重试。
            self._restore_retry_count(plan_id, vp_id, budget)
            self._registry.append_event(
                EVENT_RELEASED,
                {
                    "attach_id": attach_id,
                    "plan_id": plan_id,
                    "vp_id": vp_id,
                    "frozen_budget": budget,
                },
                fingerprint=canonical,
                fix_plan_id=fix_plan_id,
            )
            released += 1

        # 链变了，重算一次：这个计划还挂着别的东西吗？
        held = _held(self._chain_state())
        for plan_id in sorted({str(row["plan_id"]) for row in rows}):
            if not self._plan_has_active_attach(plan_id, held):
                self._exit_block(plan_id)
                if requeue:
                    self._leave_blocked_phase(plan_id)
        return released

    # -- 重跑 --------------------------------------------------------------
    def rerun_after_merge(
        self,
        fix_plan_id: str,
        *,
        rerun: Optional[Callable[[str, str], Any]] = None,
        worktree: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        """修复合入后，只重跑**挂接**的那些验证点。

        重跑的范围是挂接关系，不是「整轮再跑一遍」：挂接行记着「谁曾经等这个
        修复」，那就是要重跑的全部。没挂接的验证点跟这个缺陷无关，重跑它们只
        是白烧预算。

        每一次重跑都往 :func:`verification_persistence.append_verification_result`
        追加一行 —— **只追加**。旧结果行原地不动：「这棵树过了吗」由这棵树的
        那几行回答，而不是由「最后一行」回答（后者会把一次失败抹掉）。

        一轮 = 这个签名**挂接的**那些验证点各重跑一次。结论按**整轮**写一条：
        全过 → 一条 ``rerun_passed``；有没过 → 一条 ``rerun_failed``。轮数数
        的是**轮**，不是验证点 —— 挂接几个都只算一轮。

        重跑仍失败 → 该指纹重新打开（``reopened``）并**恢复挂起**；同一签名
        连续失败到 :data:`MAX_FIX_ROUNDS` → 耗尽（见 :meth:`exhaust`）。

        **合入本身就是事实，先写下它**：这一轮被调用就意味着「修复合入被观测
        到了」，注册表那一行因此写成 ``fixed``。重跑仍失败时它再被改成
        ``reopened`` —— 两个词说的是两件事：「修复合入了」与「修复没修好」。

        **释放挂起不是这一步的事** —— 合并那一步已经放开了它（见
        :meth:`release_hits`）。这一步只在**失败**时把挂起恢复回来。

        写结果行失败**不影响重跑调度**：失败被记下来，剩下的验证点照跑。

        Args:
            fix_plan_id: 哪个修复计划合入了。
            rerun: 重跑的执行器 —— ``(计划号, 验证点)`` → 结论。``None`` 表示
                用默认执行器（从计划的验证报告里读结论）。
            worktree: 重跑发生的那棵树。``None`` 表示没有树可绑（结论里写成
                ``unknown``）。

        Returns:
            每个被重跑的验证点一个结论::

                [{"plan_id": "p1", "vp_id": "VP-3",
                  "new_status": "passed", "tree_fingerprint": "sha256:ab"}]
        """
        plan = _require_text(fix_plan_id, "fix_plan_id")
        runner = rerun if rerun is not None else _default_rerun

        fingerprints = self._fingerprints_for_fix_plan(plan)
        for fingerprint in fingerprints:
            # 合入被观测到了 —— 这是事实，先写下来。后面的重跑结论是在这个
            # 事实之上得出的；重跑仍失败时它再被改成 ``reopened``。
            self._registry.mark_fixed(fingerprint)

        outcomes: List[Dict[str, Any]] = []
        for fingerprint in fingerprints:
            round_outcomes = [
                self._rerun_one(
                    plan,
                    fingerprint,
                    plan_id,
                    vp_id,
                    round_no,
                    runner,
                    worktree,
                    source=RESULT_SOURCE_FRAMEWORK_FIX_RERUN,
                )
                for plan_id, vp_id, round_no in self._attached_pairs(fingerprint)
            ]
            outcomes.extend(round_outcomes)
            # 一轮一个结论 —— 挂接的验证点全过了，这一轮才算过了。
            self._record_round(plan, fingerprint, round_outcomes)
        return outcomes

    def _rerun_one(
        self,
        fix_plan_id: str,
        fingerprint: str,
        plan_id: str,
        vp_id: str,
        round_no: int,
        runner: Callable[[str, str], Any],
        worktree: Optional[Path],
        source: str = RESULT_SOURCE_FRAMEWORK_FIX_RERUN,
    ) -> Dict[str, Any]:
        """重跑一个验证点，落一行结果。

        这里只管**这个验证点**：结果行（只追加、绑树指纹、source 不变）与它
        的结论。「这一轮过了没有」是**整轮**的事 —— 由 :meth:`_record_round`
        写一条事件来说。

        为什么不按验证点各写一条事件：``_failure_count`` 数的是「连续失败了
        几轮」。按验证点写，挂接 N 个验证点时一轮就写 N 条 —— 第 1 轮就把
        「连续失败轮数」顶到上限，规格要求的第 2 轮永远轮不到。
        """
        error: Optional[str] = None
        try:
            verdict = normalize_result_verdict(runner(plan_id, vp_id))
        except Exception as exc:  # noqa: BLE001 - 重跑失败不能吞掉整个重跑
            verdict = "failed"
            error = f"the rerun did not produce a verdict: {exc}"

        tree, base_commit, clean, tree_error = self._tree_of(worktree)
        if tree_error and not error:
            error = tree_error

        payload: Dict[str, Any] = {
            "plan_id": plan_id,
            "vp_id": vp_id,
            "verdict": verdict,
            "round": round_no,
            "tree_fingerprint": tree,
            "fix_plan_id": fix_plan_id,
        }
        if error:
            payload["error"] = error

        # 结果行 —— 写不进去只记下来，不影响这次重跑本身。
        try:
            append_verification_result(
                plan_id=plan_id,
                vp_id=vp_id,
                tree_fingerprint=tree,
                verdict=verdict,
                round_no=self._next_result_round(plan_id, vp_id),
                base_main_commit=base_commit,
                clean=clean,
                source=source,
                fix_plan_id=fix_plan_id,
                db_path=self._registry.db_path,
            )
        except Exception as exc:  # noqa: BLE001 - 写结果失败不影响重跑调度
            payload["result_write_error"] = f"the result row was not written: {exc}"

        if source == RESULT_SOURCE_LATE_HIT_RERUN:
            # 延迟命中这一轮单独留一条 —— 「这一轮是修复合入之后才撞上的」是
            # 一件独立的事，混在普通的重跑事件里就看不出它为什么发生。
            self._registry.append_event(
                EVENT_LATE_HIT_RERUN,
                payload,
                fingerprint=fingerprint,
                fix_plan_id=fix_plan_id,
            )

        outcome: Dict[str, Any] = {
            "plan_id": plan_id,
            "vp_id": vp_id,
            "new_status": verdict,
            "tree_fingerprint": tree,
        }
        if error:
            outcome["error"] = error
        return outcome

    def _record_round(
        self,
        fix_plan_id: str,
        fingerprint: str,
        outcomes: List[Dict[str, Any]],
    ) -> Optional[str]:
        """把**一轮**重跑的结论写进审计链，并按这一轮的结论更新状态机。

        一轮 = 这个签名挂接的验证点各重跑一次。结论按**整轮**写一条：全过 →
        一条 ``rerun_passed``；有没过 → 一条 ``rerun_failed``（载荷里带着过
        与没过的那几个 (计划, 验证点)）。

        这一轮仍失败 → 缺陷重新打开（``reopened``）并恢复挂起；连续失败到
        :data:`MAX_FIX_ROUNDS` → 耗尽（见 :meth:`exhaust`）。事件的条数就是
        轮数 —— ``_failure_count`` 的计数口径不用改。

        这一轮什么都没重跑 → 不写事件：没有重跑就没有结论，也不能用一条
        「通过」把之前的失败计数清零。

        Args:
            fix_plan_id: 哪个修复计划合入了。
            fingerprint: 这一轮重跑的是哪个缺陷。
            outcomes: 这一轮每个验证点一个结论。

        Returns:
            写下的事件类型（这一轮什么都没重跑时是 ``None``）。
        """
        if not outcomes:
            return None

        passed = [
            item for item in outcomes if item.get("new_status") == "passed"
        ]
        failed = [
            item for item in outcomes if item.get("new_status") != "passed"
        ]
        payload: Dict[str, Any] = {
            "fingerprint": fingerprint,
            "fix_plan_id": fix_plan_id,
            "passed": [_round_entry(item) for item in passed],
            "failed": [_round_entry(item) for item in failed],
        }

        if not failed:
            self._registry.append_event(
                EVENT_RERUN_PASSED,
                payload,
                fingerprint=fingerprint,
                fix_plan_id=fix_plan_id,
            )
            return EVENT_RERUN_PASSED

        self._registry.append_event(
            EVENT_RERUN_FAILED,
            payload,
            fingerprint=fingerprint,
            fix_plan_id=fix_plan_id,
        )
        if self._failure_count(fingerprint) >= MAX_FIX_ROUNDS:
            self.exhaust(fingerprint)
        else:
            self.reopen(fingerprint)
        return EVENT_RERUN_FAILED

    # -- 重新打开 ----------------------------------------------------------
    def reopen(self, fingerprint: str) -> int:
        """这个缺陷重新打开，返回重新打开了几条挂接。

        「重新打开」做三件事：

        1. **注册表改回 ``reopened``** —— 那是注册表状态闭集里的词（``open`` /
           ``fixed`` / ``reopened``），不是新语义；
        2. **恢复挂起** —— 还没挂着的挂接重新挂上（挂起被放开过的情况下，
           重跑仍失败就意味着它还得等）；
        3. **审计链留一条 ``reopened``** —— 「发生过什么」写在链上。

        没人登记过的指纹 → 0 条（没有东西可重新打开，这不是错误）。

        Args:
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。

        Returns:
            重新打开了几条挂接。
        """
        canonical = self._canonical_or_none(fingerprint)
        if canonical is None:
            return 0

        # 注册表那一行的状态由注册表自己写（``reopen``）—— 状态是注册表自己
        # 的事实，别处用裸 UPDATE 改它，审计链上就没有这次改动的记录。
        self._registry.reopen(canonical)

        held = _held(self._chain_state())
        rows = self._registry.list_attach(canonical)
        reopened = 0
        for row in rows:
            attach_id = str(row["attach_id"])
            plan_id = str(row["plan_id"])
            vp_id = str(row["vp_id"])
            round_no = _as_int(row.get("round")) or 0
            if attach_id not in held:
                # 挂起被放开过 —— 恢复它，重跑仍失败就意味着还得等。
                self.hold(
                    plan_id=plan_id,
                    vp_id=vp_id,
                    fingerprint=canonical,
                    round_no=round_no,
                    source="framework_fix_rerun",
                )
            reopened += 1

        registry_row = self._registry.lookup(canonical)
        self._registry.append_event(
            EVENT_REOPENED,
            {
                "fingerprint": canonical,
                "reopened": reopened,
                "round": MAX_FIX_ROUNDS,
            },
            fingerprint=canonical,
            fix_plan_id=(
                registry_row.get("fix_plan_id") if registry_row is not None else None
            ),
        )
        return reopened

    # -- 耗尽 --------------------------------------------------------------
    def exhaust(self, fingerprint: str) -> int:
        """这个签名耗尽了，返回转成耗尽状态的计划数。

        耗尽是**终态**：同一签名不再自动重试，等人决策。做五件事：

        1. **解挂** —— 把挂起放开（重试预算按回冻结时的值）：耗尽的计划不
           再占着挂起块；
        2. **挂接的计划转 ``framework_fix_exhausted``** —— 写进计划状态的
           ``framework_fix`` 块，读模型把它投影成对外可见的「耗尽」；
        3. **挂接置 ``exhausted``** —— 记录**留着**，状态改掉：这条挂接不再
           占着那个计划的执行（阻塞计数因此归零），但「谁曾经等过这个修复」
           还在；
        4. **问人** —— 建一个 HITL 决策，等人拍板。没有这一步，耗尽就是
           「这件事被悄悄忘掉了」；
        5. **审计链留一条 ``exhausted``**。

        没人登记过的指纹 → 0 个（没有东西可耗尽，这不是错误）。

        Args:
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。

        Returns:
            转成耗尽状态的计划数。
        """
        canonical = self._canonical_or_none(fingerprint)
        if canonical is None:
            return 0

        # 先解挂：预算按回冻结时的值，块清空。
        #
        # **不回队列**：耗尽的计划在等人决策 —— 放回调度队列等于让它再去撞
        # 一次同样的缺陷。相位停在阻塞相位上（读模型把它投影成「轮次耗尽」，
        # 那一档的优先级高于相位）。
        self._release_hits(canonical, requeue=False)

        registry_row = self._registry.lookup(canonical)
        fix_plan_id = (
            registry_row.get("fix_plan_id") if registry_row is not None else None
        )
        rows = self._registry.list_attach(canonical)
        plans = sorted({str(row["plan_id"]) for row in rows})
        for plan_id in plans:
            self._write_framework_fix_block(
                plan_id,
                {
                    "hold": False,
                    "status": EXHAUSTED_BLOCK_STATUS,
                    "fingerprint": canonical,
                    "fix_plan_id": fix_plan_id,
                    "unclassified_rounds": 0,
                },
            )

        # 挂接不再占着那些计划的执行 —— 阻塞计数因此归零。记录留着：挂接行
        # 是历史（「谁曾经等过这个修复」），耗尽不擦掉历史。
        self._registry.set_attach_status(canonical, ATTACH_STATUS_EXHAUSTED)

        self._registry.append_event(
            EVENT_EXHAUSTED,
            {
                "fingerprint": canonical,
                "plans": plans,
                "max_rounds": MAX_FIX_ROUNDS,
            },
            fingerprint=canonical,
            fix_plan_id=fix_plan_id,
        )

        # 问人。耗尽本身已经写下（挂接已置 exhausted、审计链已留痕），所以
        # 问不到人**不掀翻耗尽** —— 它被记下来，不被吞掉。
        self._ask_a_human(canonical, fix_plan_id, plans)

        return len(plans)

    def _ask_a_human(
        self,
        fingerprint: str,
        fix_plan_id: Optional[str],
        plans: List[str],
    ) -> Optional[str]:
        """为耗尽建一个 HITL 决策，返回它的 id（建不成就是 ``None``）。

        问在**修复计划**上：它拥有这个签名，是这个缺陷的责任人。问在业务
        计划上会把同一个问题问好几遍 —— 而该拍板的是「这个签名还要不要
        继续修」。

        决策落在**注册表那本库**里：注册表与决策必须同库，否则读的人看到
        「缺陷耗尽了」却找不到该问谁。

        建决策失败**不掀翻耗尽** —— 耗尽是已经发生的事实，问不到人是另一件
        事，它被记下来，不被吞掉。
        """
        plan_id = str(fix_plan_id or "").strip()
        if not plan_id and plans:
            # 没有修复计划可问（缺陷没归谁管）—— 退一步问等它的那个计划。
            plan_id = plans[0]
        if not plan_id:
            logger.warning(
                "framework_fix_exhaustion_not_asked: fingerprint=%s error=%s",
                fingerprint,
                "no plan owns this defect, so there is nobody to ask",
            )
            return None

        plan_dir = self._plan_dir_for(plan_id)
        waiting = ", ".join(plans) if plans else "（无）"
        try:
            request = DecisionRequest(
                plan_id=plan_id,
                source="framework_fix",
                failure_class="framework_fix_exhausted",
                round=MAX_FIX_ROUNDS,
                question=(
                    f"框架缺陷 {fingerprint} 的修复轮次已耗尽"
                    f"（上限 {MAX_FIX_ROUNDS} 轮），需要人工决策："
                    "继续修、放宽修复范围，还是接受现状？"
                ),
                options=[
                    "继续修（人工接手）",
                    "放宽修复范围后继续",
                    "接受现状（不再自动重试同一签名）",
                ],
                default_option="接受现状（不再自动重试同一签名）",
                mode="strict",
                timeout_sec=None,
                answer_note=f"指纹 {fingerprint}；等它的计划：{waiting}",
            )
            with HitlStore(self._registry.db_path) as store:
                decision = create_decision(
                    request,
                    plan_dir,
                    store=store,
                    settle_unrouted=False,
                    require_answer=True,
                )
            return str(decision.decision_id)
        except Exception as exc:  # noqa: BLE001 - 问不到人不掀翻耗尽
            logger.warning(
                "framework_fix_exhaustion_not_asked: fingerprint=%s plan=%s "
                "error=%s",
                fingerprint,
                plan_id,
                exc,
            )
            return None

    # -- 归不了类 ----------------------------------------------------------
    def ask_for_unclassified_escalation(
        self,
        *,
        fingerprint: str,
        plan_id: str,
        vp_id: str = "",
        round_no: int = 0,
    ) -> Optional[str]:
        """连续多轮归不了类 —— 建一个 HITL 决策，问人。

        问的是**另一件事**，机制却与 :meth:`_ask_a_human` 是同一套
        （``DecisionRequest`` + ``HitlStore`` + :func:`hitl.service.
        create_decision`）：耗尽问的是「这个签名还要不要继续修」，这里问的
        是「这个失败连着几轮都归不了类，怎么处置它」。两件事都要人来拍板，
        所以两件事都走决策表 —— 而不是一条只有审计意义的事件。

        问在**命中所在的那条计划**上：那是被这件事挡住验收的计划。决策落在
        注册表那本库里 —— 缺陷的登记与它的决策必须同库，否则读的人看到
        「问过人了」却找不到该问谁。

        Args:
            fingerprint: 缺陷指纹。
            plan_id: 命中所在的那条计划。
            vp_id: 命中所在的验证点（可空）。
            round_no: 问人的那一轮。

        Returns:
            决策 id；建不成就是 ``None``。问不到人**不掀翻**已经发生的事 ——
            归不了类这件事已经被登记下来了，它被记下来，不被吞掉。
        """
        plan = _require_text(plan_id, "plan_id")
        try:
            request = DecisionRequest(
                plan_id=plan,
                source="framework_fix",
                vp_id=_as_vp_id(vp_id) or None,
                failure_class="framework_fix_unclassified",
                round=int(round_no or 0),
                question=(
                    f"框架缺陷 {fingerprint} 连着几轮都归不了类"
                    "（复现结论不明确），需要人工决策："
                    "按框架缺陷处理、按计划自身缺陷处理，还是继续复现？"
                ),
                options=[
                    "按框架缺陷处理（建修复计划）",
                    "按计划自身缺陷处理（本计划自己修）",
                    "继续复现（下一轮再判一次）",
                ],
                default_option="继续复现（下一轮再判一次）",
                mode="strict",
                timeout_sec=None,
                answer_note=f"指纹 {fingerprint}；命中所在的计划：{plan}",
            )
            with HitlStore(self._registry.db_path) as store:
                decision = create_decision(
                    request,
                    self._plan_dir_for(plan),
                    store=store,
                    settle_unrouted=False,
                    require_answer=True,
                )
            return str(decision.decision_id)
        except Exception as exc:  # noqa: BLE001 - 问不到人不掀翻已发生的事
            logger.warning(
                "framework_fix_unclassified_not_asked: fingerprint=%s plan=%s "
                "error=%s",
                fingerprint,
                plan,
                exc,
            )
            return None

    # -- 修复计划自己撞上框架问题 ------------------------------------------
    def handle_fix_plan_hit(
        self,
        *,
        hit_plan_id: str,
        fingerprint: str,
        envelope: Optional[Dict[str, Any]] = None,
        repo: str = "",
        scope_expanded: bool = False,
        scope_mergeable: bool = False,
        spawn: Optional[Callable[[Dict[str, Any]], str]] = None,
        merge: Optional[Callable[[Dict[str, Any]], Any]] = None,
        hitl: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        """一次框架缺陷命中该怎么处理 —— 派生修复计划，还是并入，还是升级。

        **永远不派生第二个修复计划。** 一个签名一个修复计划：修复计划自己再撞
        上框架问题时，处理办法只有两个 ——

        * **并入当前修复计划**（``revise`` 增补任务）：**同一个签名**、同一个
          仓、修复范围可合并、且没有扩大范围 —— 那还是同一件活，交给同一个
          计划接着做最省事；
        * **升级 HITL**：其余一切 —— **签名不同**（那是另一个缺陷，超出当前修复
          计划的验收边界，只能由人决定）、扩大了修复范围、不是同一个仓、或者
          范围不可合并。

        判定写死在一条规则里：**不同签名一律升级**，哪怕调用方说「范围可合并」
        —— 把另一个缺陷悄悄并进来，等于把没人批准过的改动混进这个修复计划。

        命中来自**普通**计划时，派生修复计划 —— 那一次 ``spawn`` 就是这个签名
        的**唯一一次**。

        Args:
            hit_plan_id: 命中的计划。
            fingerprint: 命中的缺陷指纹。
            envelope: 派生修复计划用的信封（普通计划命中时交给 ``spawn``）。
            repo: 命中排在哪个仓。
            scope_expanded: 这次命中是不是扩大了修复范围。
            scope_mergeable: 这次的修复范围能不能并进当前修复计划。
            spawn: 派生修复计划的执行器。
            merge: 并入当前修复计划的执行器（``revise`` 增补任务）。
            hitl: 升级 HITL 的执行器。

        Returns:
            ``{"action": "spawned" | "merged" | "hitl", ...}`` —— ``action`` 说
            这次命中被怎么处理了。
        """
        plan = _require_text(hit_plan_id, "hit_plan_id")

        if self._plan_kind(plan) != FRAMEWORK_FIX_KIND:
            # 普通计划命中 → 派生修复计划。这是这个签名的唯一一次 spawn。
            fix_plan_id = ""
            if spawn is not None and envelope is not None:
                fix_plan_id = str(spawn(envelope))
                # 链的第一行结果落在**链开始的那一步**：普通计划命中 → 派生
                # 修复计划。派生失败就没有链 —— 那时写下来的基线没人会拿它比。
                self._record_chain_first_result(plan, envelope, fix_plan_id)
            return {
                "action": "spawned",
                "hit_plan_id": plan,
                "fingerprint": fingerprint,
                "fix_plan_id": fix_plan_id,
                "spawned": True,
            }

        block = self._framework_fix_block(plan)
        fix_fingerprint = str(block.get("fingerprint") or "")
        fix_repo = str(block.get("repo") or "")
        same_signature = bool(fix_fingerprint) and fix_fingerprint == fingerprint
        same_repo = (not repo) or (not fix_repo) or repo == fix_repo

        payload: Dict[str, Any] = {
            "fix_plan_id": plan,
            "hit_plan_id": plan,
            "fingerprint": fingerprint,
            "repo": repo,
            "scope_expanded": bool(scope_expanded),
        }

        # 并入 or 升级 —— 判定只有这一条规则，每个条件都是「充分」的：
        #   并入 = 同签名 ∧ 同仓 ∧ 范围可合并 ∧ 没有扩大范围；
        #   其余 = 升级（不同签名 / 扩大范围 / 换仓 / 范围不可合并）。
        if (
            same_signature
            and same_repo
            and scope_mergeable
            and not scope_expanded
        ):
            action = "merged"
        else:
            action = "hitl"

        if action == "merged":
            if merge is not None:
                merge(payload)
        elif hitl is not None:
            reason = _hitl_reason(
                same_signature=same_signature,
                same_repo=same_repo,
                scope_expanded=bool(scope_expanded),
                scope_mergeable=bool(scope_mergeable),
            )
            hitl(dict(payload, reason=reason))

        return {
            "action": action,
            "hit_plan_id": plan,
            "fingerprint": fingerprint,
            "fix_plan_id": plan,
            "spawned": False,
        }

    # -- 延迟命中 ----------------------------------------------------------
    def handle_late_hit(
        self,
        *,
        hit_plan_id: str,
        fingerprint: str,
        vp_id: str,
        rerun: Optional[Callable[[str, str], Any]] = None,
        worktree: Optional[Path] = None,
        round_no: int = 1,
    ) -> Dict[str, Any]:
        """已修好的签名又被撞上 —— 挂到已修复的记录上，在含修复的树上重跑。

        **不新建修复计划。** 这个签名已经有主人了 —— 那个修复计划合入过了。
        再建一个，等于同一个缺陷有两条修复线，两条线的结论互相矛盾。

        走的是**同一条路径**：挂接 → 在含修复的树上重跑 → 只追加结果。旧裁决
        原样保留 —— 新结论是**新的一行**，不是对旧行的覆盖。

        只有**已经修好**（注册表那一行是 ``fixed``）的签名才算延迟命中。没修
        好的签名被撞上，那是一次普通的命中：照常派生修复计划（见
        :meth:`handle_fix_plan_hit`），不是这里的事。

        Args:
            hit_plan_id: 撞上这个签名的计划。
            fingerprint: 撞上的缺陷指纹。别名会被解析到规范指纹。
            vp_id: 撞上的验证点。
            rerun: 重跑的执行器 —— ``(计划号, 验证点)`` → 结论。``None`` 表示
                用默认执行器（从计划的验证报告里读结论）。
            worktree: 重跑发生的那棵树 —— 应当是**含修复**的那棵。
            round_no: 这一轮算第几轮。

        Returns:
            ``{"action": "late_hit_rerun" | "not_fixed", "spawned": False, ...}``
            —— ``action`` 说这次命中被怎么处理了。
        """
        plan = _require_text(hit_plan_id, "hit_plan_id")
        canonical = self._canonical_or_none(fingerprint)
        if canonical is None:
            return {
                "action": "not_fixed",
                "hit_plan_id": plan,
                "fingerprint": fingerprint,
                "spawned": False,
            }

        row = self._registry.lookup(canonical)
        if row is None or str(row.get("status") or "") != REGISTRY_STATUS_FIXED:
            # 还没修好 → 那不是延迟命中，是一次普通命中：照常派生修复计划。
            return {
                "action": "not_fixed",
                "hit_plan_id": plan,
                "fingerprint": canonical,
                "spawned": False,
            }

        fix_plan_id = str(row.get("fix_plan_id") or "").strip()
        vp = _as_vp_id(vp_id)

        # 这次命中记在案 —— 「修复合入之后又被撞上」是独立的一件事，不记下来
        # 就只剩一个结果行，看不出它为什么会有这一轮。
        self._record_late_hit(row, canonical, plan, vp, round_no)

        # 挂接：这条验证点从此等的是**这个**修复（它已经合入了，所以这次挂接
        # 不持有执行 —— 挂接状态由这次命中是不是阻塞性命中决定）。
        try:
            self._registry.attach(canonical, plan, vp, round_no, "late_hit")
        except Exception as exc:  # noqa: BLE001 - 挂不上也要重跑
            logger.warning(
                "framework_fix_late_hit_not_attached: fingerprint=%s plan=%s "
                "error=%s",
                canonical, plan, exc,
            )

        runner = rerun if rerun is not None else _default_rerun
        outcome = self._rerun_one(
            fix_plan_id,
            canonical,
            plan,
            vp,
            round_no,
            runner,
            worktree,
            source=RESULT_SOURCE_LATE_HIT_RERUN,
        )
        # 延迟命中也是**一轮**重跑：结论按整轮写一条，仍失败就重新打开
        # （连续失败到上限则耗尽）。写的是那个验证点 —— 这一轮只重跑了它。
        self._record_round(fix_plan_id, canonical, [outcome])

        return {
            "action": "late_hit_rerun",
            "hit_plan_id": plan,
            "fingerprint": canonical,
            "fix_plan_id": fix_plan_id,
            "spawned": False,
            "outcome": outcome,
        }

    def _record_late_hit(
        self,
        row: Dict[str, Any],
        canonical: str,
        hit_plan_id: str,
        vp_id: str,
        round_no: int,
    ) -> None:
        """把这次延迟命中记进命中表。

        命中行的身份由**材料**（框架文件 / 错误类别 / 首错行）算出来 ——
        那份材料已经存在注册表行里了（``key_json``），所以这一次登记落回
        **同一个**指纹：延迟命中是同一个缺陷再次被撞上，不是新缺陷。

        记不下来**不掀翻重跑**：重跑本身不依赖命中行，缺一条命中记录只是
        少一条旁证。
        """
        material = self._hit_material_of(row)
        if material is None:
            logger.warning(
                "framework_fix_late_hit_not_recorded: fingerprint=%s error=%s",
                canonical,
                "the registry row carries no fingerprint material",
            )
            return
        try:
            self._registry.register_hit(
                {
                    "plan_id": hit_plan_id,
                    "vp_id": vp_id,
                    "round": round_no,
                    "framework_files": material["framework_files"],
                    "error_class": material["error_class"],
                    "first_error_line": material["first_error_line"],
                    # 修复已经合入 —— 这次命中不持有原计划的执行，它等的是
                    # 「在含修复的树上重跑一遍」。
                    "blocking": False,
                    "disposition": "attach",
                    "repo": str(row.get("repo") or "") or "unknown",
                    "fix_plan_id": str(row.get("fix_plan_id") or "").strip(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 记不下来不掀翻重跑
            logger.warning(
                "framework_fix_late_hit_not_recorded: fingerprint=%s error=%s",
                canonical,
                exc,
            )

    @staticmethod
    def _hit_material_of(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """注册表行里的指纹材料 —— 缺了就返回 ``None``。

        ``key_json`` 存的就是 ``canonical_key(...)`` 的结果：三个跨 plan 稳定
        的字段。用它重算指纹，落回同一行。
        """
        return hit_material_of(row)

    # -- 这条链的第一行结果 ------------------------------------------------
    def record_initial_result(
        self,
        *,
        plan_id: str,
        vp_id: str,
        fingerprint: str,
        tree_fingerprint: str = "unknown",
        base_commit: str = "",
        round_no: int = 1,
        fix_plan_id: Optional[str] = None,
        clean: bool = True,
    ) -> Dict[str, Any]:
        """记下这条链的**第一行**结果（``source='initial'``）。

        这一行是这条链的基线：后来的每一次重跑都是拿它比的（「修了之后是好了
        还是换了个样子」）。没有它，就只剩一堆孤立的结论。

        只追加 —— 旧结果行原地不动。

        Args:
            plan_id: 撞上这个缺陷的计划。
            vp_id: 撞上的验证点。
            fingerprint: 缺陷指纹 —— 这一行是**哪个缺陷**的基线。
            tree_fingerprint: 复现时所在那棵树的指纹。
            base_commit: 复现时的基准 commit。
            round_no: 复现发生在第几轮。
            fix_plan_id: 为这个缺陷建的修复计划（还没有就留空）。
            clean: 复现时那棵树干不干净。复现按协议跑在基准 commit 的一份
                干净检出上，所以默认是干净的。

        Returns:
            落下的那一行结果。
        """
        plan = _require_text(plan_id, "plan_id")
        vp = _as_vp_id(vp_id)
        row: Dict[str, Any] = {
            "plan_id": plan,
            "vp_id": vp,
            "verdict": "failed",
            "round": round_no,
            "tree_fingerprint": tree_fingerprint or "unknown",
            "fix_plan_id": fix_plan_id,
            "source": RESULT_SOURCE_INITIAL,
            # 这一行是**哪个缺陷**的基线 —— 缺陷指纹本身住在注册表里，
            # 结果表不存它（那张表的每一行只说「在棵树上、哪个验证点、
            # 什么结论」）。
            "fingerprint": fingerprint,
        }
        append_verification_result(
            plan_id=plan,
            vp_id=vp,
            tree_fingerprint=tree_fingerprint or "unknown",
            verdict="failed",
            round_no=round_no,
            base_main_commit=base_commit,
            clean=clean,
            source=RESULT_SOURCE_INITIAL,
            fix_plan_id=fix_plan_id,
            db_path=self._registry.db_path,
        )
        return row

    # -- 这条链的第一行结果 ------------------------------------------------
    def _record_chain_first_result(
        self,
        plan_id: str,
        envelope: Dict[str, Any],
        fix_plan_id: str,
    ) -> None:
        """记下这条链的**第一行**结果（``source='initial'``）。

        基线落在撞上这个缺陷的那个验证点上 —— 那正是后面会被重跑的那一行。
        后来的每一次重跑都拿它比（「修了之后是好了还是换了个样子」）。

        写不进去也不拦着派生：基线缺一行是可查的缺口，派生失败是更大的坑。
        """
        source_plan_id = str(envelope.get("source_plan_id") or "").strip()
        source_vp_id = str(envelope.get("source_vp_id") or "").strip()
        if not source_plan_id or not source_vp_id:
            logger.warning(
                "framework_fix_initial_result_not_recorded: the hit on %s "
                "names no plan or no verification point",
                plan_id,
            )
            return
        reproduction = envelope.get("reproduction") or {}
        try:
            self.record_initial_result(
                plan_id=source_plan_id,
                vp_id=source_vp_id,
                fingerprint=str(envelope.get("fingerprint") or ""),
                tree_fingerprint=str(
                    reproduction.get("tree_fingerprint") or "unknown"
                ),
                base_commit=str(reproduction.get("base_commit") or ""),
                fix_plan_id=fix_plan_id or None,
            )
        except Exception as exc:  # noqa: BLE001 - 基线写不下去也要继续
            logger.warning(
                "framework_fix_initial_result_not_recorded: plan=%s vp=%s "
                "error=%s",
                source_plan_id,
                source_vp_id,
                exc,
            )

    # -- 内部：连接与只读查询 ----------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """一条新连接。

        和注册表一样**每次操作开一条新连接**：SQLite 的连接不是并发安全的，
        而生命周期会被调度 tick 与验证子进程同时调用。
        """
        conn = open_db(self._registry.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        """回滚 —— 失败时不能留下半截的写入。"""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # 没有活动事务（COMMIT 之后才失败的）：没什么可回滚的。
            pass

    def _canonical_or_none(self, fingerprint: str) -> Optional[str]:
        """规范指纹；没人登记过 → ``None``。"""
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("fingerprint must be a non-empty string")
        row = self._registry.lookup(fingerprint)
        if row is None:
            return None
        return str(row["fingerprint"])

    # -- 内部：审计链 ------------------------------------------------------
    def _chain_state(self) -> Dict[str, Dict[str, Any]]:
        """审计链上每条挂接的**最后**一条生命周期事件。

        按 ``rowid`` 排序 —— 审计链只追加，插入顺序就是事件顺序。时间戳会撞
        （同一微秒里两条事件），而 ``event_id`` 是随机的，按它排序等于随机排
        历史；``rowid`` 才是那条链真实的先后。

        Returns:
            ``attach_id`` → ``{"event_type": ..., "payload": ..., "created_at": ...}``。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_type, payload_json, created_at "
                "FROM framework_fix_event "
                "WHERE event_type IN (?, ?) ORDER BY rowid",
                _LIFECYCLE_EVENTS,
            ).fetchall()
        finally:
            conn.close()
        state: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            payload = _loads(row[1])
            if not isinstance(payload, dict):
                continue
            attach_id = payload.get("attach_id")
            if not isinstance(attach_id, str) or not attach_id:
                continue
            state[attach_id] = {
                "event_type": row[0],
                "payload": payload,
                "created_at": row[2],
            }
        return state

    def _frozen_budget(
        self, attach_id: str, state: Dict[str, Dict[str, Any]]
    ) -> int:
        """这条挂接冻结下来的重试计数。

        冻结值记在审计链上 —— 「挂起那一刻计数是多少」这件事必须**活得比这
        次进程长**：释放可能发生在另一个进程里。
        """
        info = state.get(attach_id)
        if info is None:
            return 0
        value = info["payload"].get("frozen_budget")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _attach_for(
        self, fingerprint: str, plan_id: str, vp_id: str
    ) -> Optional[Dict[str, Any]]:
        """这个 (计划, 验证点) 的挂接行 —— 不管它现在还挂不挂着。

        查的是**整个家族**（规范指纹 + 指向它的别名）：语义复核把两个签名
        合并之后，同一个验证点不该在合并前后各挂一次。
        """
        for row in self._registry.list_attach(fingerprint):
            if row.get("plan_id") == plan_id and row.get("vp_id") == vp_id:
                return row
        return None

    # -- 内部：重试预算 ----------------------------------------------------
    def _retry_count(self, plan_id: str, vp_id: str) -> int:
        """这个验证点已经消耗掉的重试次数。

        ``plan_tasks.attempt`` 就是那个计数：验证点失败一次，它名下的修复
        任务被复活一次，计数 +1。取最大值 —— 一个验证点可能有多条修复任务
        （每一轮一条），消耗掉的重试次数是它们里最大的那个。
        """
        conn = self._connect()
        try:
            rows = PlanTaskRepository(conn).load_all(plan_id)
        finally:
            conn.close()
        best = 0
        for row in rows.values():
            if not _matches_vp(row, vp_id):
                continue
            attempt = _as_int(row.get("attempt"))
            if attempt is not None and attempt > best:
                best = attempt
        return best

    def _restore_retry_count(
        self, plan_id: str, vp_id: str, budget: int
    ) -> None:
        """把计数按回挂起那一刻的值。

        挂起时没有任务超过冻结值（冻结值就是那时的最大值），所以「按回去」
        就是：**不许有任务比冻结值更高**。挂起窗口里被谁动过都按回去 ——
        挂起期间消耗的预算不算数。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                repo = PlanTaskRepository(conn)
                for task_id, row in repo.load_all(plan_id).items():
                    if not _matches_vp(row, vp_id):
                        continue
                    attempt = _as_int(row.get("attempt"))
                    if attempt is None or attempt <= budget:
                        continue
                    repo.update_task(plan_id, task_id, {"attempt": budget})
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    # -- 内部：挂起块 ------------------------------------------------------
    def _plan_state_for(self, plan_id: str) -> Any:
        """写挂起块用哪个计划状态。

        给了协作者就用给的；没给就按生产路径自己造一个（计划目录 =
        ``<plans>/<plan_id>``）。**不静默跳过** —— 挂起块是挂起的一半，
        跳过它等于报告了一个没有发生的挂起。
        """
        if self._plan_state is not None:
            return self._plan_state
        try:
            from plan_state import PlanState
        except ImportError:  # pragma: no cover - 两条路径等价
            from backend.plan_state import PlanState  # type: ignore
        return PlanState(resolve_plans_dir() / plan_id)

    def _enter_block(
        self, plan_id: str, fingerprint: str, fix_plan_id: Any
    ) -> None:
        """把计划挂进框架修复块（任务 17 的 ``enter_block``）。"""
        state = self._plan_state_for(plan_id)
        state.enter_block(
            plan_id, fingerprint=fingerprint, fix_plan_id=fix_plan_id
        )

    def _exit_block(self, plan_id: str) -> None:
        """把计划从框架修复块里放出来（任务 17 的 ``exit_block``）。"""
        state = self._plan_state_for(plan_id)
        state.exit_block(plan_id)

    def _plan_has_active_attach(self, plan_id: str, held: Set[str]) -> bool:
        """这个计划名下还有没有**挂着**的挂接。

        「挂着」由审计链回答（最后一条生命周期事件是 ``held``），不由挂接行
        的状态回答 —— 挂接行是历史，它一直都在。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT attach_id FROM framework_fix_attach WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        finally:
            conn.close()
        return any(str(row[0]) in held for row in rows)

    # -- 内部：阻塞相位 ----------------------------------------------------
    #
    # 架构决策点 4：只有**全部剩余工作**都被框架缺陷挡住时，计划才进
    # ``blocked_on_framework_fix`` 相位 —— 让出执行/验证槽位，回调度队列。
    # 部分阻塞时相位不动：计划剩下的无关工作继续做。
    #
    # 「剩余工作」由三处**真实事实**回答，缺一不可：
    #
    #   * ``plan_tasks`` 里未完成的任务行 —— ``failed_vp_id`` 说它属于哪个
    #     验证点。没有验证点归属的任务（普通开发任务）是**无关工作**；
    #   * 计划声明的验证点清单（``<plan_dir>/verification_plan.json``）——
    #     还没判定的验证点都还欠着；
    #   * ``plan_verification_result`` 里已判定但还没过的验证点。
    #
    # 判据（全部要成立）：
    #
    #   1. 这个计划至少有一个**挂着**的验证点（没有挂起就没有阻塞）；
    #   2. 每一条未完成的任务都能归到一个被挂住的验证点上；
    #   3. 每一个还没过的验证点都被挂住了。
    #
    # 归不上的（没被挂住的其他验证点、没有验证点归属的普通任务）就是**还有
    # 活要干** —— 那就是部分阻塞。
    def _apply_blocking_phase(self, plan_id: str) -> bool:
        """挂起之后判定部分阻塞还是全部阻塞 —— 全部阻塞才动相位。

        Returns:
            计划是否进了（已经在）阻塞相位。
        """
        if not self._is_fully_blocked(plan_id):
            return False
        return self._enter_blocked_phase(plan_id)

    def _is_fully_blocked(self, plan_id: str) -> bool:
        """这个计划的**全部**剩余工作是不是都被框架缺陷挡住了。"""
        held_vps, whole_plan = self._held_vps(plan_id)
        if not held_vps and not whole_plan:
            return False
        for vp_id in self._unfinished_task_vps(plan_id):
            if not whole_plan and vp_id not in held_vps:
                return False
        for vp_id in self._remaining_vps(plan_id):
            if not whole_plan and vp_id not in held_vps:
                return False
        return True

    def _held_vps(self, plan_id: str) -> Tuple[Set[str], bool]:
        """这个计划此刻被挂住的验证点，以及「整条计划被挂住」这个标志。

        空验证点的挂接表示「整条计划挂起」（见 ``_matches_vp``）—— 那样
        计划上的每一件事都被挡着。
        """
        held = _held(self._chain_state())
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT attach_id, vp_id FROM framework_fix_attach "
                "WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        finally:
            conn.close()
        vps: Set[str] = set()
        whole_plan = False
        for attach_id, vp_id in rows:
            if str(attach_id) not in held:
                continue
            name = str(vp_id or "").strip()
            if name:
                vps.add(name)
            else:
                whole_plan = True
        return vps, whole_plan

    def _unfinished_task_vps(self, plan_id: str) -> Set[str]:
        """这个计划未完成的任务各自属于哪个验证点。

        空字符串表示「这条任务没有验证点归属」—— 普通开发任务。它归不到
        被挂住的验证点上，所以它让计划保持部分阻塞（还有活要干）。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT failed_vp_id, status FROM plan_tasks WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        finally:
            conn.close()
        vps: Set[str] = set()
        for failed_vp_id, status in rows:
            if str(status or "").strip().lower() in _FINISHED_TASK_STATUSES:
                continue
            vps.add(str(failed_vp_id or "").strip())
        return vps

    def _remaining_vps(self, plan_id: str) -> Set[str]:
        """这个计划还没拿到「过了」结论的验证点。

        两个来源：计划声明的验证点清单（``verification_plan.json``）与
        ``plan_verification_result`` 里判过的那些。已判定为 ``passed`` /
        ``skipped`` 的验证点不再欠着什么。
        """
        judged = self._judged_vps(plan_id)
        remaining = {
            vp_id
            for vp_id, verdict in judged.items()
            if verdict not in _SETTLED_VERDICTS
        }
        for vp_id in self._declared_vps(plan_id):
            if judged.get(vp_id, "") not in _SETTLED_VERDICTS:
                remaining.add(vp_id)
        return remaining

    def _judged_vps(self, plan_id: str) -> Dict[str, str]:
        """这个计划每个验证点的**最新**裁决。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT vp_id, verdict FROM plan_verification_result "
                "WHERE plan_id = ? ORDER BY rowid",
                (plan_id,),
            ).fetchall()
        finally:
            conn.close()
        judged: Dict[str, str] = {}
        for vp_id, verdict in rows:
            judged[str(vp_id)] = str(verdict or "").strip().lower()
        return judged

    def _declared_vps(self, plan_id: str) -> Set[str]:
        """这条计划声明了哪些验证点（读它的 ``verification_plan.json``）。"""
        plan_dir = self._plan_dir_for(plan_id)
        if plan_dir is None:
            return set()
        path = Path(plan_dir) / "verification_plan.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        points = raw.get("verification_points") if isinstance(raw, dict) else None
        if not isinstance(points, list):
            return set()
        vps: Set[str] = set()
        for point in points:
            if isinstance(point, dict):
                name = str(point.get("id") or point.get("vp_id") or "").strip()
            else:
                name = str(point or "").strip()
            if name:
                vps.add(name)
        return vps

    def _plan_dir_for(self, plan_id: str) -> Optional[Path]:
        """这条计划的目录 —— 协作者是它自己的就用它，否则按生产路径找。"""
        state = self._plan_state_for(plan_id)
        plan_dir = getattr(state, "plan_dir", None)
        if plan_dir is not None and getattr(plan_dir, "name", "") == plan_id:
            return Path(plan_dir)
        return resolve_plans_dir() / plan_id

    def _state_for_plan(self, plan_id: str) -> Optional[Any]:
        """能代表这条计划的那个计划状态 —— 不是就返回 ``None``。

        相位是**这条计划自己**的属性，所以只有当手里的状态实例就是这条
        计划的状态时才动它。给错了实例会把**别的**计划推进相位 —— 那比不
        动更糟。
        """
        state = self._plan_state_for(plan_id)
        plan_dir = getattr(state, "plan_dir", None)
        if plan_dir is None:
            return None
        if getattr(plan_dir, "name", "") != plan_id:
            return None
        return state

    def _enter_blocked_phase(self, plan_id: str) -> bool:
        """把计划移进阻塞相位（全部剩余工作都被挡住时）。"""
        state = self._state_for_plan(plan_id)
        if state is None:
            return False
        enter = getattr(state, "enter_blocked_phase", None)
        if not callable(enter):
            return False
        try:
            return bool(enter())
        except Exception as exc:  # noqa: BLE001 - 相位写不进去不该掀翻挂起
            logger.warning(
                "framework_fix_blocked_phase_not_entered: plan=%s error=%s",
                plan_id, exc,
            )
            return False

    def _leave_blocked_phase(self, plan_id: str) -> bool:
        """把计划从阻塞相位带回调度队列（挂起被释放时）。"""
        state = self._state_for_plan(plan_id)
        if state is None:
            return False
        leave = getattr(state, "leave_blocked_phase", None)
        if not callable(leave):
            return False
        try:
            return bool(leave())
        except Exception as exc:  # noqa: BLE001 - 回不了队列也不该掀翻释放
            logger.warning(
                "framework_fix_blocked_phase_not_left: plan=%s error=%s",
                plan_id, exc,
            )
            return False

    # -- 内部：重跑这条链 --------------------------------------------------
    def _fingerprints_for_fix_plan(self, fix_plan_id: str) -> List[str]:
        """这个修复计划负责哪些缺陷。

        权威是注册表行上的 ``fix_plan_id`` —— 谁在修哪个缺陷，只有注册表说得
        清楚（计划目录看不见别的计划）。
        """
        fingerprints = [
            str(row["fingerprint"])
            for row in self._registry.list_registry()
            if str(row.get("fix_plan_id") or "") == fix_plan_id
        ]
        return sorted(fingerprints)

    def _attached_pairs(self, fingerprint: str) -> List[Tuple[str, str, int]]:
        """这个缺陷挂住了哪些 (计划, 验证点)。

        挂接行是**历史**（「谁曾经等过这个修复」）—— 正是「要重跑哪些」的答案。
        挂接关系在合并前后都不变，所以合入之后照样能读出同一份清单。
        """
        pairs: List[Tuple[str, str, int]] = []
        for row in self._registry.list_attach(fingerprint):
            pairs.append(
                (
                    str(row["plan_id"]),
                    str(row["vp_id"]),
                    _as_int(row.get("round")) or 0,
                )
            )
        return pairs

    def _next_result_round(self, plan_id: str, vp_id: str) -> int:
        """这个 (计划, 验证点) 的下一轮是第几轮。

        由已经落下的结果行数决定 —— 结果行只追加，所以「已经有多少行」就是
        「跑到第几轮了」，不用另找一份轮次账本。
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(round) FROM plan_verification_result "
                "WHERE plan_id = ? AND vp_id = ?",
                (plan_id, vp_id),
            ).fetchone()
        finally:
            conn.close()
        current = _as_int(row[0]) if row is not None else None
        return (current or 0) + 1

    def _failure_count(self, fingerprint: str) -> int:
        """这个签名**连续**重跑失败过几次。

        数的是审计链上的 ``rerun_failed`` —— 链只追加，所以这个数就是「发生过
        几次」。但数的是**连续**的那一段：一次 ``rerun_passed`` 之后重新开始
        数（缺陷修好了又复发，那是新的一次，不是上一段的延续）。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_type FROM framework_fix_event "
                "WHERE fingerprint = ? AND event_type IN (?, ?) ORDER BY rowid",
                (fingerprint, EVENT_RERUN_FAILED, EVENT_RERUN_PASSED),
            ).fetchall()
        finally:
            conn.close()
        count = 0
        for row in rows:
            if str(row[0]) == EVENT_RERUN_PASSED:
                count = 0
            else:
                count += 1
        return count

    def _tree_of(
        self, worktree: Optional[Path]
    ) -> Tuple[str, str, bool, Optional[str]]:
        """重跑发生在哪棵树上。

        Returns:
            ``(指纹, 基准 commit, 当时干不干净, 出错了没有)``。没有工作树 →
            指纹是 ``unknown``；工作树不是 git 工作树 → 同样 ``unknown``，并
            带上原因 —— 「没有树」要说出来，不能装作有。
        """
        if worktree is None:
            return ("unknown", "", False, None)
        try:
            from verification.tree_fingerprint import capture_tree_fingerprint
        except ImportError:  # pragma: no cover - 两条路径等价
            from backend.verification.tree_fingerprint import (  # type: ignore
                capture_tree_fingerprint,
            )
        try:
            captured = capture_tree_fingerprint(Path(worktree))
        except Exception as exc:  # noqa: BLE001 - 指纹拿不到不能吃掉整个重跑
            return ("unknown", "", False, f"the tree fingerprint failed: {exc}")
        return (
            str(captured["tree_fingerprint"]),
            str(captured["head_commit"]),
            not bool(captured["dirty"]),
            None,
        )

    def _write_framework_fix_block(
        self, plan_id: str, block: Dict[str, Any]
    ) -> None:
        """把 ``framework_fix`` 块写进计划状态。

        计划状态模块（``plan_state``）只提供「挂起」与「解挂」两个动作，而耗尽
        需要写第三种形状的块 —— 所以这里直接写那一列。写失败**抛错**：挂起状态
        写不进去却报告成功，比写失败更糟。

        **这一行不自己建。** 那一行带着相位（``current_phase`` 是唯一的工作流
        状态列，写它的只有路由仓库的相位 CAS），而这条路曾经自己
        ``INSERT OR IGNORE`` 一行 —— 相位写死 ``'interview'``。那是**造一行**：
        造出来的行带着一个编出来的相位，于是这条道就成了相位 CAS 之外的第二个
        相位写入者。现在这里只 ``UPDATE``：行不在 → 抛错。写不进计划状态却报告
        成功，比写失败更糟。

        行本身是由**路由仓库**建的（``plan_state`` 的挂起/解挂走
        ``RoutingRepository.insert_if_missing``）—— 谁建行不是这里的事，这里只
        负责写自己那一列。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE plan_routing SET framework_fix = ?, updated_at = ? "
                    "WHERE plan_id = ?",
                    (
                        json.dumps(block, ensure_ascii=False),
                        utcnow_iso(),
                        plan_id,
                    ),
                )
                if cur.rowcount == 0:
                    raise RuntimeError(
                        "the framework_fix block of plan "
                        f"{plan_id!r} could not be written: the plan has "
                        "no routing row, and this path does not fabricate "
                        "one (a fabricated row would carry a made-up "
                        "workflow phase)."
                    )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    def _plan_kind(self, plan_id: str) -> str:
        """这个计划是普通计划还是框架修复计划。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT plan_kind FROM plan_routing WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row[0]:
            return "normal"
        return str(row[0])

    def _framework_fix_block(self, plan_id: str) -> Dict[str, Any]:
        """这个计划的 ``framework_fix`` 块。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row[0]:
            return {}
        block = _loads(row[0])
        return block if isinstance(block, dict) else {}

    # -- 内部：挂起写到一半失败 --------------------------------------------
    def _rollback_hold(
        self,
        attach_id: str,
        fingerprint: str,
        plan_id: str,
        vp_id: str,
        *,
        created: bool,
    ) -> None:
        """挂起写到一半失败 → 撤销它做过的事。

        * 这次挂起**新落下**的挂接行 → 撤掉（留下半截的挂接行，下一次挂起会
          以为已经挂上了）；
        * 这次挂起**复用**的挂接行（挂过一次又放开的）→ 不动：那是历史，
          撤掉它等于抹掉「曾经等过这个修复」这件事；
        * 审计链**只追加**：那条「挂起了」的事件不会被抹掉，回滚以另一条事件
          的形式记下来 —— 「发生过什么」和「现在是什么」是两件事。

        回滚本身失败**不掩盖**原始异常：原始异常才是调用方要处理的那个。
        """
        if created:
            with contextlib.suppress(Exception):
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.execute(
                            "DELETE FROM framework_fix_attach WHERE attach_id = ?",
                            (attach_id,),
                        )
                        conn.execute("COMMIT")
                    except BaseException:
                        self._rollback(conn)
                        raise
                finally:
                    conn.close()
        with contextlib.suppress(Exception):
            self._registry.append_event(
                EVENT_HOLD_ROLLED_BACK,
                {
                    "attach_id": attach_id,
                    "plan_id": plan_id,
                    "vp_id": vp_id,
                },
                fingerprint=fingerprint,
            )


# ---------------------------------------------------------------------------
# 模块级入口
# ---------------------------------------------------------------------------
def hold(
    *,
    plan_id: str,
    vp_id: str,
    fingerprint: str,
    round_no: int,
    source: str,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> HoldReceipt:
    """挂起一个验证点 —— :meth:`FrameworkFixLifecycle.hold` 的模块级入口。

    协作者不给时按生产路径自己造：注册表问应用现在用哪个库，计划状态按
    ``<plans>/<plan_id>`` 找。

    Returns:
        挂接 id（:class:`HoldReceipt`）。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.hold(
        plan_id=plan_id,
        vp_id=vp_id,
        fingerprint=fingerprint,
        round_no=round_no,
        source=source,
    )


def release_hits(
    fingerprint: str,
    *,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> int:
    """释放这个缺陷挂住的验证点 —— :meth:`FrameworkFixLifecycle.release_hits`
    的模块级入口。

    Returns:
        释放了几条挂接。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.release_hits(fingerprint)


def rerun_after_merge(
    fix_plan_id: str,
    *,
    rerun: Optional[Callable[[str, str], Any]] = None,
    worktree: Optional[Path] = None,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """修复合入后重跑挂接的验证点 —— :meth:`FrameworkFixLifecycle.rerun_after_merge`
    的模块级入口。

    Returns:
        每个被重跑的验证点一个结论。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.rerun_after_merge(
        fix_plan_id, rerun=rerun, worktree=worktree
    )


def reopen(
    fingerprint: str,
    *,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> int:
    """这个缺陷重新打开 —— :meth:`FrameworkFixLifecycle.reopen` 的模块级入口。

    Returns:
        重新打开了几条挂接。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.reopen(fingerprint)


def exhaust(
    fingerprint: str,
    *,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> int:
    """这个签名耗尽了 —— :meth:`FrameworkFixLifecycle.exhaust` 的模块级入口。

    Returns:
        转成耗尽状态的计划数。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.exhaust(fingerprint)


def handle_fix_plan_hit(
    *,
    hit_plan_id: str,
    fingerprint: str,
    envelope: Optional[Dict[str, Any]] = None,
    repo: str = "",
    scope_expanded: bool = False,
    scope_mergeable: bool = False,
    spawn: Optional[Callable[[Dict[str, Any]], str]] = None,
    merge: Optional[Callable[[Dict[str, Any]], Any]] = None,
    hitl: Optional[Callable[[Dict[str, Any]], Any]] = None,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> Dict[str, Any]:
    """一次框架缺陷命中的处置 —— :meth:`FrameworkFixLifecycle.handle_fix_plan_hit`
    的模块级入口。

    Returns:
        ``{"action": "spawned" | "merged" | "hitl", ...}``。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.handle_fix_plan_hit(
        hit_plan_id=hit_plan_id,
        fingerprint=fingerprint,
        envelope=envelope,
        repo=repo,
        scope_expanded=scope_expanded,
        scope_mergeable=scope_mergeable,
        spawn=spawn,
        merge=merge,
        hitl=hitl,
    )


def handle_late_hit(
    *,
    hit_plan_id: str,
    fingerprint: str,
    vp_id: str,
    rerun: Optional[Callable[[str, str], Any]] = None,
    worktree: Optional[Path] = None,
    round_no: int = 1,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> Dict[str, Any]:
    """已修好的签名又被撞上 —— :meth:`FrameworkFixLifecycle.handle_late_hit`
    的模块级入口。

    Returns:
        ``{"action": "late_hit_rerun" | "not_fixed", "spawned": False, ...}``。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.handle_late_hit(
        hit_plan_id=hit_plan_id,
        fingerprint=fingerprint,
        vp_id=vp_id,
        rerun=rerun,
        worktree=worktree,
        round_no=round_no,
    )


def record_initial_result(
    *,
    plan_id: str,
    vp_id: str,
    fingerprint: str,
    tree_fingerprint: str = "unknown",
    base_commit: str = "",
    round_no: int = 1,
    fix_plan_id: Optional[str] = None,
    clean: bool = True,
    registry: Optional[FrameworkFixRegistry] = None,
    plan_state: Optional[Any] = None,
) -> Dict[str, Any]:
    """记下这条链的第一行结果 —— :meth:`FrameworkFixLifecycle.record_initial_result`
    的模块级入口。

    ``clean`` 的默认值与方法一致（干净的检出）：复现按协议跑在基准 commit 的
    一份干净检出上，写库的那一层默认也是干净的。两个入口写的是**同一件事**，
    所以它们必须给出同一个答案 —— 否则同一行基线，读的人分不出是哪条路径
    写的。

    Returns:
        落下的那一行结果。
    """
    lifecycle = FrameworkFixLifecycle(registry, plan_state=plan_state)
    return lifecycle.record_initial_result(
        plan_id=plan_id,
        vp_id=vp_id,
        fingerprint=fingerprint,
        tree_fingerprint=tree_fingerprint,
        base_commit=base_commit,
        round_no=round_no,
        fix_plan_id=fix_plan_id,
        clean=clean,
    )


# ---------------------------------------------------------------------------
# 指纹材料
# ---------------------------------------------------------------------------
def hit_material_of(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """注册表行里的指纹材料 —— 缺了就返回 ``None``。

    ``key_json`` 存的就是 ``canonical_key(...)`` 的结果：三个跨 plan 稳定
    的字段。用它重算指纹，落回同一行。

    生产链路上（修复轮）要判一次失败，得先有这份材料：失败记录本身不带
    框架文件集与错误类别，而注册表那一行带着 —— 那个指纹登记的时候用过
    什么材料，这一行就存着什么材料。

    Args:
        row: 注册表行（``FrameworkFixRegistry.lookup`` 的返回形状）。

    Returns:
        ``{"framework_files": [...], "error_class": str,
        "first_error_line": str}``；材料不全就是 ``None``。
    """
    if not isinstance(row, dict):
        return None
    key = row.get("key")
    if key is None:
        key = row.get("key_json")
    if isinstance(key, str):
        try:
            key = json.loads(key)
        except (TypeError, ValueError):
            return None
    if not isinstance(key, dict):
        return None
    files = key.get("framework_files")
    error_class = key.get("error_class")
    first_error_line = key.get("first_error_line")
    if not isinstance(files, list) or not files:
        return None
    if not isinstance(error_class, str) or not error_class:
        return None
    if not isinstance(first_error_line, str) or not first_error_line:
        return None
    return {
        "framework_files": [str(item) for item in files],
        "error_class": error_class,
        "first_error_line": first_error_line,
    }


# ---------------------------------------------------------------------------
# 小助手
# ---------------------------------------------------------------------------
def _default_rerun(plan_id: str, vp_id: str) -> Any:
    """默认的重跑执行器 —— 从计划的验证报告里读结论。

    没有注入 ``rerun`` 时，重跑由**验证路径**执行（它产出验证报告），这一层
    负责把结论取出来、绑到树上、落成一行结果。

    报告里没有这个验证点的结论 → ``unknown``。``unknown`` 不是「通过」：一次
    没有被证明的修复不能算修好了。
    """
    report = _read_verification_report(plan_id)
    if report is None:
        return "unknown"
    for key in ("verification_points", "verification_results"):
        for entry in report.get(key) or []:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or entry.get("vp_id") or "")
            if entry_id and entry_id == vp_id:
                return entry.get("status") or entry.get("verdict") or "unknown"
    return "unknown"


def _hitl_reason(
    *,
    same_signature: bool,
    same_repo: bool,
    scope_expanded: bool,
    scope_mergeable: bool,
) -> str:
    """升级 HITL 的原因词 —— 说的是「为什么不能并进去」。

    四个条件各占一个词：人看到哪一个词就知道该拍什么板 —— 不同签名是另一个
    缺陷（要决定它归谁），扩大范围是改动变大了（要决定它还在不在验收边界
    内），换仓是另一个项目，范围不可合并是拼不到一起。
    """
    if not same_signature:
        return "different_signature"
    if scope_expanded:
        return "scope_expanded"
    if not same_repo:
        return "different_repo"
    if not scope_mergeable:
        return "scope_not_mergeable"
    return "not_mergeable"


def _read_verification_report(plan_id: str) -> Optional[Dict[str, Any]]:
    """读计划的验证报告；读不到就是 ``None``。"""
    path = Path(resolve_plans_dir()) / plan_id / "verification_report.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _round_entry(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """一轮里的一个验证点 —— 审计事件载荷里的一项。

    载荷说的是**整轮**：过了哪几个、没过哪几个，各是一条 (计划, 验证点)。
    """
    entry: Dict[str, Any] = {
        "plan_id": outcome.get("plan_id"),
        "vp_id": outcome.get("vp_id"),
        "verdict": outcome.get("new_status"),
        "tree_fingerprint": outcome.get("tree_fingerprint"),
    }
    error = outcome.get("error")
    if error:
        entry["error"] = error
    return entry


def _held(state: Dict[str, Dict[str, Any]]) -> Set[str]:
    """现在还挂着的那些挂接 id。

    「还挂着」= 这条挂接最后一条生命周期事件是 ``held``。
    """
    return {
        attach_id
        for attach_id, info in state.items()
        if info["event_type"] == EVENT_HELD
    }


def _matches_vp(row: Dict[str, Any], vp_id: str) -> bool:
    """这条任务是不是这个验证点的任务？

    验证点 id 为空 = 整条计划挂起 —— 那时计划名下所有任务都算它的。
    """
    if not vp_id:
        return True
    return str(row.get("failed_vp_id") or "") == vp_id


def _as_int(value: Any) -> Optional[int]:
    """一个整数；读不出来（``None``、坏值）就是 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _loads(value: Any) -> Any:
    """解开一个 JSON 列；坏值解成 ``None`` 而不是炸掉读取。"""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None