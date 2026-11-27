"""修复批次结局的四分类（纯函数）。

这个模块解决什么
----------------
生产上停链时只写了一个字符串::

    repair_batch_not_drained:repair_non_zero_exit:1_pending

从左往右读，它说的是「批次没排空，因为执行器非零退出，还有 1 个任务没跑」。
但**执行器非零退出**和**还有任务没跑**是两个互相独立的事实，字符串把它们按
一个顺序焊在了一起 —— 焊出来的顺序恰好是最难读的那个：调用方分不清
「还有活没干完」（该继续）和「门禁还是红的」（再跑一轮也是白跑）。这两种情形
要的反应正好相反。

所以结局被拆成一个四分类的纯函数：

``drained``
    批次排空了：退出码 0，没有遗留，没有失败任务。
``pending_remains``
    **有活没干完** —— 还有修复任务排队。批次还要继续跑。
``repair_failed``
    修复任务**本身**坏了。批次结束了，而且是坏的。
``gate_red``
    执行器非零退出但没有修复任务失败：**门禁红**。批次结束了，代码还是不对。

四分类的判定顺序
----------------
``pending_count > 0`` **先于一切退出码判断**。理由：只要还有任务排队，
这批就没排空 —— 哪怕退出码是 0（执行器自己认为跑完了，但运行时叠加层
显示还有任务没到终态），哪怕退出码非零。顺序反过来会把「还有活」误判成
「门禁红」或「修复失败」，那就又回到原来那个字符串的老毛病。

为什么是纯函数
--------------
判定要独立可测，就必须没有外部依赖：不读 DB、不导入 ``server``、不看时钟、
不读环境变量。同样的输入永远给同样的输出 —— 否则「这批到底算不算排空」
就成了一个没法复算的问题，跨进程、跨重试更是无从比对。

入参的容错
----------
计数（``pending_count`` / ``failed_task_count``）是负数时按 **0** 处理：
负数不是「有这么多」，也不是「出错了」，它是没有意义。调用方算错了不该
让判定抛异常 —— 抛了就等于把一次计数 bug 升级成一次停链事故。

退出码（``returncode``）**不**按 0 处理：负退出码是进程被信号打死
（``-9`` = SIGKILL），那是最不干净的一种结束，拿它当「成功」会把一次
崩溃伪装成一次通过。拿不到退出码（``None`` 或非数字）同样按**不干净**
处理 —— 没有证据就不宣布胜利。

对外的契约
----------
``RepairOutcome`` 是 frozen dataclass，四个字段：``kind`` /
``repair_failed`` / ``should_continue`` / ``stop_reason``。

* ``kind`` 取值恒在 ``{drained, pending_remains, repair_failed, gate_red}``
  之内。
* ``repair_failed`` 只在「有修复任务失败」时为 True —— 门禁红不是修复
  失败，修复任务没坏，是被验收的东西还是错的。
* ``should_continue`` 只在「还有活没干完」时为 True。
* ``stop_reason`` 在 ``drained`` 时为 ``None``（没什么可报的），其余三种
  各带一个稳定的短字符串。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "RepairOutcome",
    "classify_repair_outcome",
]


# ---------------------------------------------------------------------------
# 四种结局
# ---------------------------------------------------------------------------

#: 批次排空：退出码 0，没有遗留，没有失败任务。
KIND_DRAINED = "drained"

#: 还有修复任务没跑 —— 批次还没干完。
KIND_PENDING_REMAINS = "pending_remains"

#: 修复任务本身失败。
KIND_REPAIR_FAILED = "repair_failed"

#: 门禁红：退出码非零，但没有修复任务失败。
KIND_GATE_RED = "gate_red"


@dataclass(frozen=True)
class RepairOutcome:
    """一次修复批次执行的结局。"""

    #: 四分类之一：``drained`` / ``pending_remains`` / ``repair_failed``
    #: / ``gate_red``。
    kind: str

    #: 是否有修复任务失败。只有 ``kind == "repair_failed"`` 时为 True ——
    #: 门禁红不是修复失败：修复任务没坏，是被验收的东西还是错的。
    repair_failed: bool

    #: 批次是否还有活要干。只有 ``kind == "pending_remains"`` 时为 True。
    should_continue: bool

    #: 停链原因。``drained`` 时为 ``None``（没什么可报的）。
    stop_reason: Optional[str] = None


def _as_count(value: object) -> int:
    """把一个计数规整成非负整数。

    负数与非数一律按 0 处理 —— 见模块 docstring 的「入参的容错」。
    """
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _is_clean_exit(returncode: object) -> bool:
    """退出码是不是「干净」（即 0）。

    拿不到退出码（``None`` / 非数字）一律按**不干净**处理：没有证据就不
    宣布胜利。负退出码是信号打死（``-9`` = SIGKILL），同样不干净。
    """
    try:
        return int(returncode) == 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def classify_repair_outcome(
    returncode: int,
    pending_count: int,
    failed_task_count: int,
) -> RepairOutcome:
    """判定一次修复批次执行的结局。

    :param returncode: 修复执行器的退出码。
    :param pending_count: 还在排队、没跑到终态的修复任务数。
    :param failed_task_count: 已经失败掉的修复任务数。
    :return: 四分类的 :class:`RepairOutcome`。
    """
    pending = _as_count(pending_count)
    failed = _as_count(failed_task_count)

    # 1) 还有活没干完 —— 先于一切退出码判断。
    if pending > 0:
        return RepairOutcome(
            kind=KIND_PENDING_REMAINS,
            repair_failed=False,
            should_continue=True,
            stop_reason=f"repair_batch_not_drained:{pending}_pending",
        )

    # 2) 批次排空。
    if _is_clean_exit(returncode):
        return RepairOutcome(
            kind=KIND_DRAINED,
            repair_failed=False,
            should_continue=False,
            stop_reason=None,
        )

    # 3) 修复任务本身坏了。
    if failed > 0:
        return RepairOutcome(
            kind=KIND_REPAIR_FAILED,
            repair_failed=True,
            should_continue=False,
            stop_reason="repair_task_failed",
        )

    # 4) 门禁红：非零退出，但没有一个修复任务失败。
    return RepairOutcome(
        kind=KIND_GATE_RED,
        repair_failed=False,
        should_continue=False,
        stop_reason="repair_gate_red",
    )