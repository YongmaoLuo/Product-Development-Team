"""plan 级统一生命周期门面：按实时状态分派（R11）。

背景
----
停一个计划要停哪一层，取决于**当下**谁在跑：执行轮次在跑就停执行，验证
轮次在跑就停验证。这两个判断都在 ``server`` 里（``_plan_execution_in_flight``
与 ``_is_verification_in_flight``），**进程外复制不了** —— 外部系统手上
没有那份进程内状态。于是每个调用方都自己写一遍分支，写漏一次就是
「停了执行，验证却接着跑」。

本模块把那次分支收进进程内：调用方只说「停这个计划」，门面按**实时
状态**决定派给谁。既有 stop 端点是同步 ``def``，直接调用即可。

分派顺序
--------
1. 验证在飞 → ``stop_verification(plan_id)``；
2. 否则执行在飞 → ``stop_execution(plan_id, discard=False)``；
3. 否则什么都不派发（idle），相位保持原样。

验证优先于执行。一个计划同时被验证轮次与执行轮次占用是**异常态**
（``detail='both'``），但真到了那一步必须先停验证 —— 验证是外层循环，
它不停就会在执行被停之后又拉起新一轮。

三条硬约束
----------
* **进程内直接调用处理器**。禁止 httpx / requests：那会把「同进程按
  实时状态分派」退化成「同进程发 HTTP 请求」，状态判断又变成两套，
  而且 HTTP 调用会额外背上服务自身的重试与超时语义。
* **不捕获处理器异常**。处理器抛什么（``HTTPException`` / 409 / 400）
  就原样抛给调用方：调用方要的是真实的状态码，而不是被门面换成一个
  含糊的 500。
* **未知计划抛既有 ``PlanNotFoundError``**，不吞成「什么都没发生」——
  吞掉之后调用方会以为「停了」，而实际上什么都没停。

两个相位
--------
``stop_plan`` 返回两个相位，因为「停到哪儿」和「调用方最终看到什么」在
requeue 下会分叉：

* **停泊相位（``berth_phase``）** —— 停完之后计划停在哪儿。派发了处理器
  就是 ``'stopped'``（把相位停到那儿是处理器的事，门面不自己改相位）；
  idle 时是当前相位 —— 没有东西可停，相位原样不动。
* **最终相位（``final_phase``）** —— 调用方最终看到的相位。停了不迁回
  就是 ``'stopped'``；迁回成功是 ``'queued'``；迁移失败回落 ``'stopped'``。

requeue
-------
``requeue=True`` 时，门面在停完之后把计划迁回等待相位 ``queued``。那条
``stopped → queued`` 的边由状态机承担，门面只发起迁移。

三条边界：

* **idle 忽略 requeue** —— 没有在跑的东西就没有可停的。把一个 idle 的
  计划塞进队列等于凭空多出一个待执行的计划。
* **迁移失败不外抛** —— ``transition_to`` 抛 ``ValueError`` 时记一条
  warning，最终相位回落 ``'stopped'``。stop 本身是成功的，「没进队列」
  不是 stop 的失败：让它冒泡会把一次成功的停变成 500。
* **checkpoint 只读回显** —— 读 ``read_pause_checkpoint(plan_dir)`` 把它
  带回来，但**不消费、不删除**：恢复是恢复路径的事。
* **迁回之后清掉执行行的终态裁决** —— 迁回只动了**路由相位**；执行行上
  那条 ``stopped`` 是 stop 写的，没有跟着清。计划列表的终态判定读**两个**
  裁决，于是两个裁决互相矛盾：相位是 ``queued``（非终态），执行裁决是
  ``stopped``（终态）—— 计划被列表过滤掉，用户点了「重新排队」，卡片却
  从页面上消失了。见 ``_clear_terminal_exec_verdict``。

requeue 的前提是**处理器把计划停在了 ``'stopped'``**（执行 stop 是这样
的）。处理器停在别处时（验证 stop 把相位停在 ``'verification'``），迁移
就是非法的 —— 于是走上面那条「迁移失败不外抛」的边界：记 warning，最终
相位回落 ``'stopped'``。门面不替处理器改相位：停到哪儿是处理器的事。

start
-----
恢复启动要回到**被打断的那一侧**：执行被打断就继续执行，验证被打断就
继续验证。这件事由 pause 检查点的 ``kind`` 决定（``execution`` /
``verification``；没有 ``kind`` 视为 ``execution`` —— 与
``routes/execution.py`` 的既有语义一致）。

分派顺序
--------
1. ``kind == 'verification'`` → ``start_verification(plan_id, ...)``；
2. 其它（``None`` / ``'execution'``）→ ``start_execution(plan_id, ...)``；
3. 没有检查点 → 全新执行，而且必须带 ``project_dir``。

**绝不能把 verification 检查点交给 ``start_execution``**：执行侧只认
``execution`` 那一种 kind，拿到验证的检查点会当成「没有检查点」—— 恢复
退化成全新执行，还会误判「缺 project_dir」（验证的恢复不需要它）。

三条边界：

* **检查点在派发之前读** —— 被派发的那一侧启动成功后会把检查点退休掉
  （验证 / 执行各自 retire 自己写的那份），派发之后再读就只剩 ``None``。
* **门面不自己编错误** —— 缺 ``project_dir`` 那个 400 由
  ``start_execution`` 说，门面原样透传。
* **拒绝也要说出来** —— start 侧的处理器对冲突（409 / 410）是**返回**一个
  错误响应而不是抛异常；门面把它还原成异常，免得对外谎报「已派发」。

pause 与 requeue
----------------
调度器要的两件事也收在这个门面上，而它们走的都是**既有**的合法边 ——
不新增相位、不新增边、不改 CAS 语义：

* ``pause_plan`` —— **在跑** → 既有 ``stop_plan(requeue=False)``；
  **排队中** → 既有合法边 ``queued → stopped``（``plan_state`` 的边表
  里那一行），并且把队列行删掉。
* ``requeue_plan`` —— **停住 / 失败** → 既有合法边 ``stopped → queued`` 与
  ``failed → queued``（复用 ``_requeue_to_queued``）。

两条边都由状态机承担，门面只发起迁移：合法性查的是状态机那两张边表，
门面不自己判断「这条边存不存在」——自己判断就是一份会漂移的边表副本。

冲突
----
两件不同的事都叫「冲突」，这里把它们分开：

* **相位前提不成立**（这个操作对这个相位没有边）—— 是**返回**，不是抛
  异常：调用方要的是「没成」加上「它现在在哪儿」，而不是一个 500。
  结果的 ``detail`` 是 ``'conflict'``、``ok`` 是 ``False``，两个相位都
  还是计划原来所在的那个相位 —— 冲突不改相位。
* **CAS 冲突**（行在读与写之间被别的手改了）—— 照既有语义往外抛
  ``ConflictError``：调用方按既有说法报成 409，而且**不重试** —— 重试
  就是拿自己的写入盖掉别人的。

结果类型的形状是**冻住的**：``LifecycleResult`` 恰好六个字段，因为两个
生命周期端点原样拿它当响应体，仓库另一侧的消费者按那六个键解析
（``docs/development/cross-repo-contract.md`` §1.3）。``ok`` 与 ``phase``
是那六个字段的**只读视图**，不是两个新字段 —— 多一个字段就是响应体里
多一个别人没准备过的键。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

# 既有异常，不新造：未知计划沿用状态机里那一个（``plan_routing`` 层的
# ``PlanNotFoundError``），这样调用方要区分「计划不存在」与「CAS 前提
# 不成立」时不必认识本模块。
from state_machine.repositories.routing_repository import PlanNotFoundError

# 相位的唯一真相是状态机（``server.PlanState`` 就是这个类）。直接 import
# 它而不是绕道 ``server``：``server`` 是应用进程，``plan_state`` 是叶子
# 模块，门面读相位不需要把整个应用拖进来。
from plan_state import PlanState

# 「在等人」的相位只有一套说法（``queued``，``ready`` 是它的历史别名）。
# 读哪一行是等待相位由这个模块说了算 —— 门面不自己判一次「ready 算不算」。
from phase_vocabulary import is_waiting_phase

# checkpoint 只读回显，不消费（见模块 docstring）。
from pause_state import read_pause_checkpoint

logger = logging.getLogger(__name__)

#: 冲突结果的 ``detail`` 取值：相位前提不成立，操作没有发生，相位原样
#: 不动。沿用既有结果里 ``'idle'`` 那一种形状 —— 不是动作，是「没做成
#: 什么」。
CONFLICT_DETAIL = "conflict"

__all__ = [
    "CONFLICT_DETAIL",
    "LifecycleResult",
    "PlanLifecycleService",
    "PlanNotFoundError",
    "StartPlanSpec",
]


@dataclass
class StartPlanSpec:
    """一次「启动这个计划」的参数。

    字段**全部可选**：缺什么由被派发的那一侧按它自己的规矩补。

    * ``project_dir`` —— 执行侧的工作目录。**全新执行必须带**（没有检查点
      时它没有别处可取）；恢复执行时检查点里的目录优先，请求体里的这份
      只是兜底。
    * ``tool`` —— 编码工具（``claude`` / ``opencode`` / ``vendor-c``）。
    * ``max_rounds`` —— 验证轮次预算（只对验证侧有意义）。
    * ``sync_targets`` —— 下游通知落点（只对执行侧有意义）。``None`` 是
      「用默认」，``[]`` 是「不推给任何落点」。
    * ``force`` —— 验证侧「已有报告也重跑」（只对验证侧有意义）。
    """

    project_dir: Optional[str] = None
    tool: Optional[str] = None
    max_rounds: Optional[int] = None
    sync_targets: Optional[List[str]] = None
    force: bool = False


@dataclass
class LifecycleResult:
    """一次生命周期操作的结果。

    字段与含义：

    * ``plan_id`` —— 被操作的计划。
    * ``dispatched`` —— 实际派给了谁（``'verification'`` / ``'execution'``），
      没有派发给任何人时是 ``None``（idle）。
    * ``berth_phase`` —— 停泊相位：这次操作之后计划停在哪儿。stop 派发了
      处理器就是 ``'stopped'``；idle 时是当前相位；start 派发执行是
      ``'queued'``（等待相位），派发验证是 ``'verification'``。
    * ``final_phase`` —— 最终相位：调用方最终看到的相位。``'queued'`` 只在
      requeue 成功时出现；迁移失败回落 ``'stopped'``；start 之后是
      ``'executing'`` / ``'verification_running'``（已经在跑）。
    * ``checkpoint`` —— 恢复点；本模块不消费它，只回显。idle 时是
      ``None`` —— 那个计划现在没有被打断过。start 侧回显的是**派发之前**
      读到的那一份（派发之后它已经被那一侧退休掉了）。
    * ``detail`` —— 这次分派的具体动作（``'stop_verification'`` /
      ``'stop_execution'`` / ``'both'`` / ``'idle'``；start 侧是
      ``'start_verification(resume)'`` / ``'start_execution(resume)'`` /
      ``'start_execution(fresh)'``；pause / requeue 侧是
      ``'pause_queued'`` / ``'requeue'`` / ``'conflict'``）。请求了
      requeue 时追加 ``'+requeue'``（如 ``'stop_execution+requeue'``）——
      它说的是**动作**，最终相位落在哪儿看 ``final_phase``。

    **字段恰好六个**：两个生命周期端点原样拿这个 dataclass 当响应体，
    仓库另一侧的消费者按那六个键解析
    （``docs/development/cross-repo-contract.md`` §1.3）。多一个字段就是
    响应体里多一个别人没准备过的键。

    两个**只读视图**（不是字段，不进响应体）：

    * ``phase`` —— ``final_phase`` 的另一个名字。
    * ``ok`` —— 这次操作做成了没有。冲突（``detail`` 是 ``'conflict'``）
      时是 ``False``，其余是 ``True``。
    """

    plan_id: str
    dispatched: Optional[str]
    berth_phase: str
    final_phase: str
    checkpoint: Optional[Dict[str, Any]]
    detail: str

    @property
    def phase(self) -> str:
        """调用方最终看到的相位 —— ``final_phase`` 的另一个名字。

        两个名字说的是同一件事，所以这里给的是**只读别名**而不是第二个
        字段：两个可以各写各的字段，就是两份会漂移的真相。
        """
        return self.final_phase

    @property
    def ok(self) -> bool:
        """这次操作**做成了没有**。

        冲突（相位前提不成立）时是 ``False`` —— 那时两个相位都还是计划
        原来所在的那个相位（冲突不改相位）。

        这是**视图**不是字段：``detail`` 已经说了是不是冲突，再加一个
        字段就是给响应体多加一个键。
        """
        return not self.detail.startswith(CONFLICT_DETAIL)


class PlanLifecycleService:
    """plan 级生命周期门面（进程内，按实时状态分派）。

    两个方向：

    * ``stop_plan`` —— 按**实时状态**决定停执行还是停验证；
    * ``start_plan`` —— 按**检查点的 kind** 决定恢复执行还是恢复验证。

    门面本身**不持有状态**：它每次调用都重新读一次进程内的实时状态
    （stop）或磁盘上的检查点（start），所以「谁在跑」永远是调用那一刻
    的答案，而不是缓存下来的旧答案。
    """

    # ------------------------------------------------------------------
    # stop
    # ------------------------------------------------------------------

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """按实时状态把「停这个计划」派给验证或执行。

        :param plan_id: 目标计划。
        :param requeue: 停完之后是否把计划迁回等待相位（``queued``）。
            idle 时被忽略（没有东西可停，也就没有「停完之后」可言）。
        :return: :class:`LifecycleResult`。
        :raises PlanNotFoundError: 计划不存在。
        :raises fastapi.HTTPException: 处理器抛出的异常原样透传。
        """
        import server as _server

        plan_dir = _server._plan_dir(plan_id)
        if not plan_dir.exists():
            raise PlanNotFoundError(plan_id)

        verification_alive = self._verification_alive(plan_id)
        execution_alive = self._execution_alive(plan_id)

        if verification_alive:
            self._stop_verification(plan_id)
            dispatched = "verification"
            detail = "stop_verification"
            if execution_alive:
                # 异常态：两边都在飞。验证是外层循环，它不停就会在执行
                # 被停之后又拉起新一轮 —— 所以先停验证，再停执行。
                self._stop_execution(plan_id)
                detail = "both"
        elif execution_alive:
            self._stop_execution(plan_id)
            dispatched = "execution"
            detail = "stop_execution"
        else:
            # idle：没有在跑的东西可停，派发本身就是错的。相位保持原样，
            # requeue 也被忽略 —— 把一个 idle 的计划塞进队列等于凭空多出
            # 一个待执行的计划。
            phase = self._current_phase(plan_dir)
            return LifecycleResult(
                plan_id=plan_id,
                dispatched=None,
                berth_phase=phase,
                final_phase=phase,
                checkpoint=None,
                detail="idle",
            )

        # 停完了。停泊相位恒为 'stopped'：把相位停到那儿是处理器的事，
        # 门面不自己改相位。
        berth_phase = "stopped"
        final_phase = berth_phase
        # checkpoint 只读回显：处理器刚写下的那一份。恢复是恢复路径的事，
        # 门面不消费、也不删除它。
        checkpoint = read_pause_checkpoint(plan_dir)

        if requeue:
            # detail 说的是**动作**（停了，并请求了迁回）；迁回成没成功
            # 看 final_phase。
            detail = f"{detail}+requeue"
            if self._requeue_to_queued(plan_dir):
                final_phase = "queued"
                # 迁回队列之后，执行行上那条终态裁决必须一起清掉 ——
                # 否则两个裁决互相矛盾，计划会从列表里消失（见
                # ``_clear_terminal_exec_verdict``）。
                self._clear_terminal_exec_verdict(plan_id)

        return LifecycleResult(
            plan_id=plan_id,
            dispatched=dispatched,
            berth_phase=berth_phase,
            final_phase=final_phase,
            checkpoint=checkpoint,
            detail=detail,
        )

    @staticmethod
    def _requeue_to_queued(plan_dir) -> bool:
        """把停住的计划迁回等待相位（``queued``）；返回迁移是否成功。

        ``stopped → queued`` 与 ``failed → queued`` 那两条边由状态机承担，
        门面只发起迁移。

        ``ValueError``（非法迁移）**不外抛**：stop 已经成功了，「没进队列」
        不是 stop 的失败 —— 让它冒泡会把一次成功的停变成 500。失败时记
        一条 warning，最终相位回落 ``'stopped'``。
        """
        return PlanLifecycleService._transition_to_phase(
            plan_dir, "queued", label="requeue",
        )

    @staticmethod
    def _transition_to_phase(plan_dir, phase: str, *, label: str) -> bool:
        """按状态机的边表把计划迁到 ``phase``；返回迁移是否成功。

        合法性**由状态机说了算**：``PlanState.transition_to`` 查的是
        ``PHASE_TRANSITIONS`` 与 ``VERIFICATION_PHASE_TRANSITIONS`` 那
        两张表。门面不自己判断哪条边存在 —— 自己判断就是一份会漂移的
        边表副本，而漂移的那一份正好是「多出一条状态机不认的边」。

        ``ValueError``（非法迁移）**不外抛**：迁移没成不是整个操作失败
        —— 调用方要的是「冲突」这个答案，不是 500。失败时记一条 warning。

        ``ConflictError``（CAS 冲突）**照旧往外抛**：那不是「这条边不
        合法」，而是「行在读与写之间被别的手改了」。调用方要按既有语义
        把它报成冲突，而且**不许重试** —— 重试就是拿自己的写入盖掉别人
        的。
        """
        try:
            state = PlanState(plan_dir)
            # 处理器刚刚改过相位；reload 拿最新的那份，免得拿缓存里的旧
            # 相位去迁移（plan_state 的 docstring 明确要求 transition 前
            # 先 reload）。
            state.reload()
            state.transition_to(phase)
        except ValueError as exc:
            logger.warning(
                "plan_lifecycle_%s_failed: plan=%s could not be moved to "
                "the phase %r (%s); it stays where it was",
                label,
                getattr(plan_dir, "name", plan_dir),
                phase,
                exc,
            )
            return False
        return True

    @staticmethod
    def _clear_terminal_exec_verdict(plan_id: str) -> None:
        """把执行行上的终态裁决清掉（requeue 迁回队列之后）。

        为什么必须清
        ------------
        计划列表的终态判定读**两个**裁决（``routes/plans.py`` 的
        ``_plan_is_terminal``）：路由相位不在终态相位里时，执行裁决
        ``stopped`` / ``failed`` **仍然是**终态，计划因此被列表过滤掉。

        两条裁决由两个写者分别落下：stop 把 ``stopped`` 写进执行行
        （``routes/execution.py`` 的 ``_stop_running_execution``），requeue
        只迁回路由相位（``stopped → queued``）—— 它没有碰执行行。于是被
        迁回队列的计划**不进列表**：用户点了「重新排队」，卡片却从页面
        上消失了（E2E ``test_plan_lifecycle_ui_e2e`` 的最后一步）。

        终态判据本身**不动**：它的第二个判据是为了兜住「进程崩了但路由
        相位没跟上」那条路径（崩溃只把 ``failed`` 写进执行行）。要修的是
        requeue 这条路径 —— 把执行行的终态裁决清掉，让两个裁决重新一致。

        写什么
        ------
        * ``exec_status='not_started'`` —— 计划在队列里等着下一次执行，
          此刻它确实「还没开始」。那是仓库里既有的 resting 状态（执行
          状态接口把读不到的状态就答成它，``routes/execution.py`` 的
          ``_row_has_verdict`` 也把它算成「没有裁决」）。
        * ``exec_pid=None`` —— 执行进程已经不在了，行上留着 pid 会让
          「这个执行还活着吗」的读者相信它还活着（accident B, 2026-10-23）。
        * ``stop_reason=None`` —— 那是上一轮运行的停止理由，不属于排队中
          的计划。

        只在**当前裁决是终态**时写：不是终态的裁决（``running`` /
        ``completed`` / ``not_started``）本来就不会把计划藏起来，动它只会
        毁掉别的东西。执行行不存在时无事可做；写失败只记一条 warning ——
        stop 与迁回都已经成功了，不该因为这一次附带写入变成 500。
        """
        import server as _server
        from routes.plans import TERMINAL_EXEC_STATUSES
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )

        try:
            conn = open_db(_server._state_db_path())
            try:
                migrate(conn)
                repo = ExecutionRepository(conn)
                row = repo.summary(plan_id)
                if not row:
                    # 没有执行行 = 没有执行裁决 = 没有东西要清。
                    return
                current = str(row.get("exec_status") or "").strip()
                if current not in TERMINAL_EXEC_STATUSES:
                    return
                repo.update_fields(
                    plan_id,
                    exec_status="not_started",
                    stop_reason=None,
                    exec_pid=None,
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — 附带写入不该让 stop 变成 500
            logger.warning(
                "plan_lifecycle_requeue_verdict_clear_failed: plan=%s could "
                "not clear the terminal execution verdict; the plan is "
                "queued but the listing may hide it",
                plan_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # pause / requeue
    # ------------------------------------------------------------------

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """暂停一个计划 —— 在跑的停掉，排队的从队里摘出来停掉。

        两种来源，两条**既有**合法边（不新增相位、不新增边）：

        * **在跑**（执行轮次或验证轮次在飞）→ 既有
          :meth:`stop_plan`（``requeue=False``）。处理器负责停到
          ``stopped``、负责写检查点；门面不自己改相位。
        * **排队中**（相位 ``queued``）→ 既有合法边 ``queued → stopped``
          （``plan_state.PHASE_TRANSITIONS``），并且把队列行删掉 ——
          停住的计划不该还排在队里等着被启动。

        其余相位 → **冲突**：结果原样返回，相位一个字节都不动。

        :param plan_id: 目标计划。
        :return: :class:`LifecycleResult`；冲突时 ``ok`` 是 ``False``，
            两个相位都还是计划原来所在的那个相位。
        :raises PlanNotFoundError: 计划不存在。
        :raises ConflictError: CAS 冲突（行在读与写之间被别的手改了）——
            照既有语义往外抛，调用方按既有说法报成 409；**不重试**。
        """
        import server as _server

        plan_dir = _server._plan_dir(plan_id)
        if not plan_dir.exists():
            raise PlanNotFoundError(plan_id)

        # 在跑 → 既有 stop 路径，而且明确**不**迁回队列：暂停不是恢复。
        # 这一步同时回答了「有没有在跑」——idle 时 ``dispatched`` 是 None。
        stopped = self.stop_plan(plan_id, requeue=False)
        if stopped.dispatched is not None:
            return stopped

        # idle：没有在跑的东西可停。相位决定这条边走不走得通。
        phase = self._current_phase(plan_dir)
        if not is_waiting_phase(phase):
            logger.info(
                "plan_lifecycle_pause_conflict: plan=%s cannot be paused "
                "from the phase %r", plan_id, phase,
            )
            return self._conflict(plan_id, phase)

        # 排队中 → 既有合法边 queued → stopped。
        #
        # 队列行**先**摘：摘完之后这个计划不再有资格被准入，所以相位迁移
        # 失败时它至多是一个「排队但没有队列行」的计划 —— 启动恢复会把
        # 这种计划按顺序补回队尾。反过来（先迁相位后摘行）留下的是
        # 「停了却还排在队里」：调度器捡起它，暂停就白做了。
        #
        # CAS 冲突（``ConflictError``）**不接**：它照既有语义往外抛，
        # 调用方按既有说法报成 409，而且不重试 —— 重试就是拿自己的写入
        # 盖掉别人的。那一刻队列行已经摘掉了：这个计划是「排队但没有队列
        # 行」，启动恢复会把它按顺序补回队尾。
        self._remove_from_queue(plan_id)
        if not self._transition_to_phase(plan_dir, "stopped", label="pause"):
            # 迁移被拒（相位在读与写之间变了，或那条边不合法）：冲突。
            # 相位原样不动。
            return self._conflict(plan_id, self._current_phase(plan_dir))

        return LifecycleResult(
            plan_id=plan_id,
            dispatched=None,
            berth_phase="stopped",
            final_phase="stopped",
            checkpoint=None,
            detail="pause_queued",
        )

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """把一个停住或失败的计划放回等待相位 —— 既有合法边
        ``stopped → queued`` 与 ``failed → queued``。

        **接受 ``stopped`` 与 ``failed``**：这两个相位都放回等待相位
        ``queued``。状态机的边表里那两条边（``stopped → queued`` 与
        ``failed → queued``）本来就存在 —— 收窄只发生在门面：入口相位
        集合少写了一个名字，于是那条本来合法的边永远走不到。

        别的相位一律冲突（相位原样不动）。**``executing`` 仍然是冲突**：
        在跑的计划没有「放回等待相位」这回事 —— 那是一次停止加一次入队，
        两件事，不能用一个名字糊过去。

        :param plan_id: 目标计划。
        :return: :class:`LifecycleResult`；冲突时 ``ok`` 是 ``False``。
        :raises PlanNotFoundError: 计划不存在。
        :raises ConflictError: CAS 冲突 —— 照既有语义往外抛，**不重试**。
        """
        import server as _server

        plan_dir = _server._plan_dir(plan_id)
        if not plan_dir.exists():
            raise PlanNotFoundError(plan_id)

        phase = self._current_phase(plan_dir)
        # 入口相位集合：``stopped`` 与 ``failed``。其余相位 —— 包括
        # ``executing`` —— 一律冲突。
        if phase not in ("stopped", "failed"):
            logger.info(
                "plan_lifecycle_requeue_conflict: plan=%s cannot be requeued "
                "from the phase %r", plan_id, phase,
            )
            return self._conflict(plan_id, phase)

        # ``stopped → queued`` / ``failed → queued`` 那两条边由状态机承担，
        # 门面只发起迁移。
        # CAS 冲突（``ConflictError``）照既有语义往外抛，不重试。
        if not self._requeue_to_queued(plan_dir):
            # 迁移被拒（相位在读与写之间变了，或那条边不合法）：冲突。
            return self._conflict(plan_id, self._current_phase(plan_dir))

        return LifecycleResult(
            plan_id=plan_id,
            dispatched=None,
            berth_phase="queued",
            final_phase="queued",
            checkpoint=None,
            detail="requeue",
        )

    @staticmethod
    def _conflict(plan_id: str, phase: str) -> LifecycleResult:
        """一个冲突结果：相位原样不动，操作没有发生。

        冲突**不是**一次成功的操作 —— ``ok`` 是 ``False``。两个相位都
        还是计划原来所在的那个相位：冲突不改相位，调用方拿到的就是
        「没成」加上「它现在在哪儿」。
        """
        return LifecycleResult(
            plan_id=plan_id,
            dispatched=None,
            berth_phase=phase,
            final_phase=phase,
            checkpoint=None,
            detail=CONFLICT_DETAIL,
        )

    @staticmethod
    def _remove_from_queue(plan_id: str) -> bool:
        """把这个计划从调度队列里摘出来；返回它本来在不在队列里。

        队列行是**调度事实**，相位是**plan 事实**：一个停住的计划留在
        队列里，调度器迟早会把它捡起来 —— 那就等于暂停没生效。

        队列行没有就是没有（``remove`` 对不存在的行静默返回），所以这不是一个会失败的操作：调用方不必为「这个计划本来就没排队」做
        任何判断。

        队列行摘不掉时（数据库打不开、锁着）异常照旧往外抛：那一刻相位
        还没动，调用方看到的是一次失败，而不是一个「停住了却还排在队
        里」的计划。
        """
        import server as _server
        from plan_scheduler.queue import remove as _queue_remove
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate

        conn = _open_db(_server._state_db_path())
        try:
            _migrate(conn)
            return _queue_remove(conn, plan_id)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------

    def start_plan(
        self, plan_id: str, spec: Optional[StartPlanSpec] = None,
    ) -> LifecycleResult:
        """按检查点的 kind 把「启动这个计划」派给验证或执行。

        恢复启动要回到**被打断的那一侧**：执行被打断就继续执行，验证被打断
        就继续验证。这件事由 pause 检查点的 ``kind`` 决定（``execution`` /
        ``verification``；没有 ``kind`` 视为 ``execution`` —— 与
        ``routes/execution.py`` 的既有语义一致）。

        ``kind`` 只有 ``'verification'`` 这一种走验证，其余一律走执行：
        执行侧自己只认 ``'execution'``，别的 kind 在那边等于「没有检查
        点」，所以门面也不该把它派给执行以外的任何一侧。

        :param plan_id: 目标计划。
        :param spec: 启动参数；``None`` 等价于一个全空的 :class:`StartPlanSpec`。
        :return: :class:`LifecycleResult`。
        :raises PlanNotFoundError: 计划不存在。
        :raises fastapi.HTTPException: 处理器抛出的异常原样透传；处理器
            用错误响应回答的拒绝（409 / 410）也会被还原成异常。
        """
        import server as _server

        spec = spec if spec is not None else StartPlanSpec()

        plan_dir = _server._plan_dir(plan_id)
        if not plan_dir.exists():
            raise PlanNotFoundError(plan_id)

        # 检查点必须在派发**之前**读：被派发的那一侧启动成功后会把检查点
        # 退休掉（验证 / 执行各自 retire 自己写的那份），派发之后再读就        # 只剩 None，回显里也就没有「恢复了什么」。
        checkpoint = read_pause_checkpoint(plan_dir)
        kind = (checkpoint or {}).get("kind")

        if kind == "verification":
            self._start_verification(plan_id, spec)
            return LifecycleResult(
                plan_id=plan_id,
                dispatched="verification",
                berth_phase="verification",
                final_phase="verification_running",
                checkpoint=checkpoint,
                detail="start_verification(resume)",
            )

        # 执行侧：kind 为 None（老检查点没有 kind）或 'execution' 都会被它
        # 认领；别的 kind 它不认（等同没有检查点），所以 detail 也说
        # fresh —— 那里说的是**动作**，不是「有没有检查点文件」。
        resumed = bool(checkpoint) and kind in (None, "execution")
        self._start_execution(plan_id, spec)
        return LifecycleResult(
            plan_id=plan_id,
            dispatched="execution",
            berth_phase="queued",
            final_phase="executing",
            checkpoint=checkpoint,
            detail="start_execution(resume)" if resumed else "start_execution(fresh)",
        )

    # ------------------------------------------------------------------
    # 实时状态探针
    # ------------------------------------------------------------------

    @staticmethod
    def _verification_alive(plan_id: str) -> bool:
        """验证轮次是不是正在跑。

        判定用 ``server`` 自己的谓词（``_is_verification_in_flight``），
        不在这里重写一份 —— 那份状态只有进程内有，重写一份就多一个会
        漂移的真相来源。
        """
        import server as _server

        return bool(
            _server._is_verification_in_flight(
                _server._verification_state.get(plan_id)
            )
        )

    @staticmethod
    def _execution_alive(plan_id: str) -> bool:
        """执行轮次是不是正在跑（同样是 ``server`` 的谓词）。"""
        import server as _server

        return bool(_server._plan_execution_in_flight(plan_id))

    @staticmethod
    def _current_phase(plan_dir) -> str:
        """读计划当前相位（idle 分支要如实回显「相位不变」）。"""
        return PlanState(plan_dir).get_current_phase()

    # ------------------------------------------------------------------
    # 处理器（进程内直接调用）
    # ------------------------------------------------------------------

    @staticmethod
    def _stop_verification(plan_id: str) -> None:
        """停验证轮次。异常不接：调用方要真实的状态码。"""
        from routes import verification as _routes_verification

        _routes_verification.stop_verification(plan_id)

    @staticmethod
    def _stop_execution(plan_id: str) -> None:
        """停执行轮次（``discard=False``：停是暂停，不是丢弃）。"""
        from routes import execution as _routes_execution

        _routes_execution.stop_execution(plan_id, discard=False)

    @staticmethod
    def _start_verification(plan_id: str, spec: StartPlanSpec) -> None:
        """派发验证启动。异常不接：调用方要真实的状态码。"""
        import server as _server
        from routes import verification as _routes_verification

        req = _server.StartVerificationRequest(
            max_rounds=spec.max_rounds,
            tool=spec.tool,
            force=spec.force,
        )
        response = _routes_verification.start_verification(plan_id, req)
        PlanLifecycleService._raise_if_refused(response)

    @staticmethod
    def _start_execution(plan_id: str, spec: StartPlanSpec) -> None:
        """派发执行启动。异常不接：调用方要真实的状态码。

        ``project_dir`` 传 ``None`` 也照样传：缺目录那个 400 由执行侧说，
        门面不自己编错误。
        """
        import server as _server
        from routes import execution as _routes_execution

        req = _server.StartExecutionRequest(
            project_dir=spec.project_dir,
            tool=spec.tool,
            sync_targets=spec.sync_targets,
        )
        response = _routes_execution.start_execution(plan_id, req)
        PlanLifecycleService._raise_if_refused(response)

    @staticmethod
    def _raise_if_refused(response: Any) -> None:
        """处理器用「错误响应」回答时，把它还原成异常。

        stop 那侧的处理器抛 ``HTTPException``；start 这侧的处理器对冲突
        （409 / 410）**返回**一个错误响应而不是抛异常。门面若把它当成
        成功，就会对外谎报「已派发」——那比报一个错更坏。

        只认「真的像错误响应」的东西：状态码必须是 ``int`` 且 >= 400。
        处理器返回 ``None``（或任何别的东西）就是成功。
        """
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or status < 400:
            return
        detail = getattr(response, "body", None)
        if isinstance(detail, (bytes, bytearray)):
            try:
                detail = json.loads(detail.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                detail = detail.decode("utf-8", "replace")
        raise HTTPException(status, detail)