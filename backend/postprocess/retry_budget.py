"""收尾阶段的续跑预算 —— 「能重跑几次」这件事的唯一账本。

收尾阶段（postprocess）失败一次之后，计划会停在 ``postprocess_failed``
相位上等人来决定是否续跑。这条相位边存在，但**没有人能触发续跑**，于是
任何收尾失败过的计划都成了死胡同。这个模块提供的不是续跑本身，而是
续跑之前必须先过的那道闸：**还有没有额度**。

为什么有上限
------------

续跑是一个会**执行对外动作**的动作（开 PR、推送、跑命令），而自动重跑
如果没有上限，就是一个可能永远不收敛的循环：一条收尾命令永远失败 →
自动再跑 → 永远失败 → …… 每一次重跑都在重复同一个对外动作，每一次
重跑都在消耗真实的时间与额度。所以额度是**定死的常量**（:data:`MAX_RETRIES`），
不是配置项、不是环境变量：配置项会在某次深夜被人调大，而「最多再跑几次」
是一条产品决定，不是部署参数。

初始那一次不算在额度里 —— 用户批准的是「重跑两次」，也就是初始失败之后
**还可以再跑两次**。所以 :data:`MAX_RETRIES` 是 2，账本里的
``retries_used`` 从 1 开始计数。

为什么不自动重跑
----------------

A5：产品代码越界之后，**背着人重复那个动作正是被禁止的**。收尾阶段的
编排器在遇到「改动了产品代码」这种越界时会停下（``_violation``），
因为此时再跑一次就是把同一个越界动作再做一遍，而且是在没有人看着的
情况下做的。因此这个模块只提供「记账」与「额度查询」，**任何地方都不会
自动调用它**：额度只能被人显式调用的那个 HTTP 端点消耗掉。

账本文件
--------

``plans/{plan_id}/postprocess_retries.json``，内容形如::

    {
      "plan_id": "...",
      "retries_used": 1,
      "history": [{"at": "<iso8601>", "reason": "<str>"}]
    }

写盘是**原子**的：先写同目录下的临时文件，再用 ``os.replace`` 盖上目标
文件（同 :mod:`postprocess.progress` 的做法）。半写的账本会被读成「没
用过额度」，于是下一次续跑会再放行一次 —— 额度就会被绕过。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping


#: 初始那一次不算在额度里 —— 之后最多**再跑**几次。用户定死的数字。
#:
#: 这是一个常量，不是配置项：见模块 docstring 的「为什么有上限」。
MAX_RETRIES: int = 2

#: 账本文件名（放在计划目录里，与收尾阶段的其它产物同住）。
BUDGET_FILENAME: str = "postprocess_retries.json"


class RetryBudgetExhausted(RuntimeError):
    """续跑额度用完了 —— 再跑就得有人先介入。

    带着两个数字：已经用掉几次、上限是几次。调用方要能把这两个数字
    原样告诉操作者，所以它们是属性，不只是消息里的字。

    Args:
        retries_used: 已经用掉的续跑次数。
        max_retries: 续跑次数的上限。
    """

    def __init__(self, retries_used: int, max_retries: int) -> None:
        super().__init__(
            f"续跑额度已用完：retries_used={retries_used}, "
            f"max_retries={max_retries}；需要人工介入。"
        )
        self.retries_used = retries_used
        self.max_retries = max_retries


def budget_path(plan_dir: Any) -> Path:
    """账本文件所在的位置。

    Args:
        plan_dir: 计划自己的目录。

    Returns:
        ``{plan_dir}/postprocess_retries.json`` 的路径。
    """
    return Path(plan_dir) / BUDGET_FILENAME


def read_budget(plan_dir: Any) -> Dict[str, Any]:
    """读账本 —— **不写盘**。

    文件不存在时返回一份空的账本（``retries_used`` 为 0，``history`` 为空），
    并且**不创建那个文件**：读一个账本不是一次续跑，一次续跑才有资格在
    磁盘上留下痕迹。

    Args:
        plan_dir: 计划自己的目录。

    Returns:
        账本的两部分：``retries_used``（整数）与 ``history``（列表）。
    """
    path = budget_path(plan_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"retries_used": 0, "history": []}
    document = json.loads(raw)
    history = document.get("history") or []
    if not isinstance(history, list):
        history = []
    return {
        "retries_used": int(document.get("retries_used") or 0),
        "history": list(history),
    }


def claim_retry(plan_dir: Any, *, reason: str) -> int:
    """占用一次续跑额度，并把它记在账本上。

    顺序是「先查额度，再记账」：额度用完时**不动文件**，账本上的
    ``retries_used`` 保持原样 —— 一个用完的额度不该因为一次被拒绝的
    续跑而再涨一格。

    Args:
        plan_dir: 计划自己的目录。
        reason: 这次续跑的理由（写进 history，供人看）。

    Returns:
        新的 ``retries_used``，从 1 开始计数。

    Raises:
        RetryBudgetExhausted: 额度已经用完。
    """
    budget = read_budget(plan_dir)
    retries_used = int(budget["retries_used"])
    if retries_used >= MAX_RETRIES:
        raise RetryBudgetExhausted(retries_used, MAX_RETRIES)

    new_used = retries_used + 1
    history: List[Mapping[str, Any]] = list(budget["history"])
    history.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason),
    })
    document = {
        "plan_id": Path(plan_dir).name,
        "retries_used": new_used,
        "history": history,
    }
    _write_budget(budget_path(plan_dir), document)
    return new_used


def _write_budget(path: Path, document: Mapping[str, Any]) -> None:
    """把账本原子地写到盘上。

    临时文件与目标文件**同目录**，所以 ``os.replace`` 不跨文件系统边界、
    在本仓库跑到的每个文件系统上都是原子的。替换失败时临时文件被清掉，
    异常原样抛出 —— 账本的上一版仍然是完整可读的那一版。

    Args:
        path: 账本的目标路径。
        document: 要写进去的文档。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
