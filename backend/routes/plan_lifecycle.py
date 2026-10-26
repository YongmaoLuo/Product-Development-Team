"""plan 级统一生命周期门面的 HTTP 门面（R11）。

这个文件是**门面的门**：让「停这个计划」和「启动这个计划」成为两个**进程外
可调用**的 URL。门面本身在 :mod:`plan_lifecycle_service`（按实时状态分派
停执行还是停验证、按检查点的 kind 分派恢复执行还是恢复验证），这里只做
三件事：解析请求、调门面、把结果序列化回去。

为什么需要这一层
----------------
分派的判断（谁在跑？检查点的 kind 是什么？）依赖**进程内**状态 —— 执行轮次
和验证轮次是不是活着，只有这个进程知道。进程外复制不了那份状态，于是每个
调用方都自己写一遍分支，写漏一次就是「停了执行，验证却接着跑」。

这一层把那次分支收进进程内之后，外部系统只需要说「停 p1」或者「启动 p1」，
不需要知道此刻跑的是执行还是验证。调用方不再自己分支，正是这一层的目的。

两个端点
--------
``POST /api/plan/{plan_id}/lifecycle/stop``
    停这个计划。体是 ``StopPlanRequest(requeue: bool = False)``；
    ``requeue=True`` 表示停完之后把它迁回等待相位 ``queued``。
``POST /api/plan/{plan_id}/lifecycle/start``
    启动这个计划。体是 ``StartPlanRequest``，转成门面的 ``StartPlanSpec``。

两条硬约束
----------
* **不自己编错误** —— 门面的处理器抛什么（409 / 400 / 410）就原样透传。
  调用方要的是真实的状态码，而不是被换成一个含糊的 500。
* **响应体就是 ``LifecycleResult`` 的字段**（``dataclasses.asdict``）——
  一个键不多一个键不少。调用方读到的是门面的原话，不是门面替它翻译过的
  另一套词。

未知的 plan → 404 ``{'error': 'Plan not found'}``。那一条**不是**门面自己
判的：门面抛既有的 ``PlanNotFoundError``（状态机里那一个），这里把它翻成
HTTP 的 404。翻，而不是包成 500 —— 「计划不存在」和「服务器坏了」是两件
完全不同的事，调用方要能分开。

注册位置
--------
``server.py`` 在前端 catch-all（``GET /{path:path}``）**之前** include 这个
router。路由匹配先到先得：注册在 catch-all 之后的路由会被它抢先。这两条
都是 POST，所以顺序今天不是承重墙 —— 但它随时会变成承重墙（哪天加一条 GET
来读回停完的状态），所以现在就放在对的位置上。
"""

from __future__ import annotations

import dataclasses
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plan_lifecycle_service import (
    LifecycleResult,
    PlanLifecycleService,
    PlanNotFoundError,
    StartPlanSpec,
)

# Late binding into the application module: ``server`` owns the shared
# helpers (notably ``_plan_dir``, the one place a request-supplied plan id
# is allowed to become a path).  The suite monkeypatches them as
# ``server.<name>``.  See ``routes/phases.py`` for the rule.
import server as _server

__all__ = [
    "StartPlanRequest",
    "StopPlanRequest",
    "router",
    "start_plan",
    "stop_plan",
]

router = APIRouter()

#: 门面本身**不持有状态**：它每次调用都重新读一次进程内的实时状态（stop）
#: 或磁盘上的检查点（start），所以「谁在跑」永远是调用那一刻的答案。一个
#: 共享实例因此是安全的 —— 它没有可以变脏的缓存。
_SERVICE = PlanLifecycleService()


class StopPlanRequest(BaseModel):
    """``POST /api/plan/{plan_id}/lifecycle/stop`` 的请求体。

    ``requeue`` —— 停完之后是否把计划迁回等待相位 ``queued``。默认 ``False``
    （停下就停着，不塞回队列）。
    """

    requeue: bool = False


class StartPlanRequest(BaseModel):
    """``POST /api/plan/{plan_id}/lifecycle/start`` 的请求体。

    五个字段**全部可选**，缺什么由被派发的那一侧按它自己的规矩补：

    * ``project_dir`` —— 执行侧的工作目录。**全新执行必须带**（没有检查点
      时它没有别处可取）。
    * ``tool`` —— 编码工具。
    * ``max_rounds`` —— 验证轮次预算（只对验证侧有意义）。
    * ``sync_targets`` —— 下游通知落点（只对执行侧有意义）。取值是闭集，
      写错的那个在**这里**就被拒（422），不会等到派发出去才炸。
    * ``force`` —— 验证侧「已有报告也重跑」（只对验证侧有意义）。
    """

    project_dir: Optional[str] = None
    tool: Optional[str] = None
    max_rounds: Optional[int] = None
    sync_targets: Optional[List[Literal["feishu", "telegram"]]] = None
    force: bool = False


def _not_found() -> HTTPException:
    """未知计划的 404。

    形状与仓库里其它「计划不存在」的回答一致：``{'error': 'Plan not found'}``。
    ``server.custom_http_exception_handler`` 看到 ``detail`` 是带 ``error``
    键的 dict 就把它原样当响应体，所以客户端读到的是这一个 dict。
    """
    return HTTPException(404, {"error": "Plan not found"})


def _result_body(result: LifecycleResult) -> dict:
    """把门面的结果序列化成响应体。

    ``dataclasses.asdict`` 而不是手写 dict：手写的那一份迟早会跟 dataclass
    漂移（加了一个字段，响应体里没有），而这里漂移的代价是「调用方读到
    的字段和门面说的不是一回事」。
    """
    return dataclasses.asdict(result)


@router.post("/api/plan/{plan_id}/lifecycle/stop")
def stop_plan(
    plan_id: str,
    req: Optional[StopPlanRequest] = None,
) -> dict:
    """按实时状态停这个计划（执行在跑就停执行，验证在跑就停验证）。

    ``requeue=True`` 时停完之后把计划迁回等待相位 ``queued``。

    :raises fastapi.HTTPException: 未知计划 404；处理器抛出的异常原样透传。
    """
    body = req if req is not None else StopPlanRequest()
    # The id is admitted through the shared guard: ``_plan_dir`` is the one
    # place a request-supplied id may become a path, and it refuses a
    # traversal (``..``) with 400.  Nothing here builds a path from the id
    # — the facade resolves the same directory itself, through the same
    # guard.
    _server._plan_dir(plan_id)
    try:
        result = _SERVICE.stop_plan(plan_id, requeue=body.requeue)
    except PlanNotFoundError:
        raise _not_found()
    return _result_body(result)


@router.post("/api/plan/{plan_id}/lifecycle/start")
def start_plan(
    plan_id: str,
    req: Optional[StartPlanRequest] = None,
) -> dict:
    """按检查点的 kind 启动这个计划（恢复执行或恢复验证）。

    :raises fastapi.HTTPException: 未知计划 404；处理器抛出的异常原样透传
        （缺 ``project_dir`` 那个 400 由执行侧说，门面不自己编）。
    """
    body = req if req is not None else StartPlanRequest()
    # 同 stop：id 先过共享守卫，再谈别的。
    _server._plan_dir(plan_id)
    spec = StartPlanSpec(
        project_dir=body.project_dir,
        tool=body.tool,
        max_rounds=body.max_rounds,
        sync_targets=body.sync_targets,
        force=body.force,
    )
    try:
        result = _SERVICE.start_plan(plan_id, spec)
    except PlanNotFoundError:
        raise _not_found()
    return _result_body(result)
