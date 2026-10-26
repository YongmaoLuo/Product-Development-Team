"""等待相位词汇的唯一归一化实现（R10：``queued`` 成为唯一等待相位）。

背景
----
R10 要求 ``queued`` 成为**唯一**的等待相位：计划停在「等人」这件事上，历史
上叫 ``ready``，现在叫 ``queued``。两套说法指向同一件事，于是读路径上必须
有一处归一化 —— 否则每读一行旧数据就要在各个调用点各判一次「这是不是
``ready``」，判漏一处就是一个卡在旧相位里出不来、也没人认领的计划。

但仓库里已经有一个 :func:`plan_state.normalize_phase`（返回 ``str``，处理
``verification_rerunning`` → ``verification_repairing`` 这类历史别名）。
它和本模块的归一化**语义不同**：

* 它回答「这个历史值当年被什么取代了」，返回 ``str``；
* 本模块回答「这个值是不是在等人」，返回 ``(值, 是不是历史别名)``。

两个函数同名会让读代码的人以为它们是一回事，而它们的返回值形状根本不同，
import 错的那一处会以 ``str`` 当 tuple 用炸掉。因此本模块的函数**不叫**
``normalize_phase``，叫 :func:`normalize_waiting_phase` —— 名字里必须带
``waiting``，把「哪一套语义」写进名字里。

为什么 ``ready`` 是历史别名而不是直接删掉
----------------------------------------
``ready`` 至今仍写在 ``plan_state.VALID_PHASES`` 里（它是「任务已生成、
等人决定何时开始」这个状态的合法取值），而且既有数据库行里存着它。
直接删会让这些行既不属于任何已知相位、又无法被推进。所以：

* **读** 路径把 ``ready`` 归一化成 ``queued``；
* **写** 路径不再产生 ``ready``（由调用方负责，本模块不做状态写入）；
* 每次读到都打一条 warning，带 plan_id 与来源 —— 旧数据要能清得掉，
  就得先知道它在哪儿。

模块边界
--------
本模块**不**做校验、不做状态写入、不认识任何具体 plan 的流程。它只提供
词汇层面的纯函数 + 一条弃用日志，因此可以被任何层（scheduler / routes /
状态机）安全地 import。它也**不** import ``plan_state``：那个模块会把
verification 一整套拖进来，而词汇表是更底层的东西。
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

__all__ = [
    "QUEUED",
    "LEGACY_READY",
    "normalize_waiting_phase",
    "is_waiting_phase",
    "log_deprecated_ready",
]


#: 现行词汇：唯一的等待相位。
QUEUED = "queued"

#: 历史词汇：``QUEUED`` 的旧名。仍会出现在既有数据库行里。
LEGACY_READY = "ready"


def normalize_waiting_phase(value: Optional[str]) -> Tuple[Optional[str], bool]:
    """把等待相位的历史别名归一化成现行词汇。

    返回 ``(归一化后的值, 是不是历史别名)``：

    * ``'ready'`` → ``('queued', True)``；
    * 其它值 → ``(原值, False)``。

    边界条件（全部**不抛异常**）：

    * ``None`` / 空串 → 原值返回、``aliased=False``。读路径上拿到的可能是
      缺字段的行，为这种情况抛异常等于把「读旧数据」变成「整个接口 500」。
    * 未知相位（如 ``'verification_running'``）→ 原值返回、``aliased=False``。
      本模块**不是校验器**：来路不明的值不吞、不改，只原样带回去。
    * 只精确匹配小写 ``'ready'``。``'Ready'`` 不是任何地方写出来的值，
      归一化它只会把真实的数据问题掩盖掉。

    注意：本函数**不叫** ``normalize_phase`` —— 那个名字属于
    :func:`plan_state.normalize_phase`，语义是「历史值被什么取代」，返回
    ``str``。两个同名函数必然被 import 错一处。
    """
    if not isinstance(value, str) or value == "":
        # None / 空串 / 非字符串：原值带回去，调用方自己决定怎么处理。
        return value, False
    if value == LEGACY_READY:
        return QUEUED, True
    return value, False


def is_waiting_phase(value: Optional[str]) -> bool:
    """这个值是不是「在等人」的相位。

    现行值 ``queued`` 与历史别名 ``ready`` 都算 —— 历史别名被当成「不在等」
    会让一行旧数据停在没有出边的相位里，还没人知道它卡住了。

    其它相位、``None``、空串都返回 ``False``。
    """
    if not isinstance(value, str) or value == "":
        return False
    return value in (QUEUED, LEGACY_READY)


def log_deprecated_ready(
    logger: logging.Logger,
    plan_id: Optional[str],
    source: Optional[str],
) -> None:
    """读到历史值 ``ready`` 时留一条 warning。

    弃用要能被执行：旧数据要清得掉，先得知道它在哪儿、是谁读到的。
    所以这条日志带上 plan_id 与来源（source），两个值都直接出现在消息正文
    里，同时以结构化字段挂在 record 上，便于日志检索。

    :param logger: 接收告警的 logger。
    :param plan_id: 读到旧相位的 plan 标识。
    :param source: 读取来源（哪个调用点/哪条路径读到了它）。
    """
    logger.warning(
        f"[phase_vocabulary] deprecated phase vocabulary: "
        f"phase={LEGACY_READY!r} is the legacy alias of {QUEUED!r}, "
        f"normalized on read. plan_id={plan_id} source={source}",
        extra={"plan_id": plan_id, "source": source},
    )