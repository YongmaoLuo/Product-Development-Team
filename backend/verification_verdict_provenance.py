"""verdict 的出处（provenance）—— 结论只对产生它的那条命令有效。

背景（2026-09-15）
---------------------------

采用"只做 C"方案：**只让真正重跑过的结论进入本次判定**。撞到轮次上限
并不等于结论失效——该重跑还是要重跑，继续迭代循环进行验证。

起因：a production plan 的 round 1 重跑里，30 个 VP 只有 **VP-023 真的执行了**，
其余 29 个的结论全部来自历史。铁证是 VP-013 的失败理由写着
``cargo test --lib ... running 0 tests; 175 filtered out`` —— 而计划早在
17:47 就把这条命令改成了 ``cargo test --test signal_classification``。
**那份结论比它要评判的命令还旧。**

根因：``VerificationExecutor`` 从 ``plan_verification.verdicts``（DB 里跨轮
累积的那张表）**无条件**水合 ``_verdicts``，据此把 PASSED 的 VP 放进
``completed_set`` 跳过。而 ``/start`` 的 ``force`` / ``resume`` 两个参数
都到不了这里 —— ``VerificationExecutor.__init__`` 连 ``resume`` 形参都没有，
``resume`` 只用在"要不要清 state 文件"那一处。

**为什么不做"force 就全量重验"**（B 方案）：重置轮次计数常常只是因为撞了
上限、而操作者想继续迭代验证，这时候把已挣到的结论全部作废是错的。
作废的判据应该是**命令变没变**，不是"操作者重置了计数"。

本模块给出那个判据：记录 verdict 时一并存下当时那条 ``test_command`` 的
指纹；分桶时指纹对不上的结论一律视为**未完成**，该 VP 重新跑。

安全方向：**没有指纹 = 失效**。旧结论无法证明自己对应哪条命令，唯一诚实
的处理是让它重新挣一次。反过来（当有效）等于把 bug 原样保留 —— VP-013
那条 ``--lib`` 结论恰恰就是"没有指纹"的。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

#: verdict 里存指纹的字段名。
FINGERPRINT_FIELD = "command_fingerprint"

#: 这些状态的 verdict 不带命令语义，不参与指纹校验。
#:  ``SPLIT`` 是"这个 VP 被拆成子 VP 了"——真正的结论在子 VP 身上，
#:  父 VP 本就不该重跑（见 ``verification_executor`` 的 completed_set 契约）。
_PROVENANCE_EXEMPT_STATUSES = frozenset({"SPLIT"})


def _normalise(command: Any) -> str:
    """把命令折成用于比对的形式。

    只做折叠空白：纯粹重排缩进/换行不该让一份结论失效，但真正的改动
    （``--lib`` → ``--test``）必须能被认出来。
    """
    if not isinstance(command, str):
        return ""
    return " ".join(command.split())


def command_fingerprint(command: Any) -> str:
    """返回一条验收命令的指纹（sha256 前 16 位十六进制）。

    空命令返回 ``""`` —— 没有命令就没有出处可言，调用方据此走
    "无指纹 = 失效"的同一条路。
    """
    normalised = _normalise(command)
    if not normalised:
        return ""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def stamp(verdict: Dict[str, Any], command: Any) -> Dict[str, Any]:
    """给 verdict 盖上产生它的那条命令的指纹（返回副本，不改原对象）。"""
    stamped = dict(verdict)
    stamped[FINGERPRINT_FIELD] = command_fingerprint(command)
    return stamped


def is_stale(
    verdict: Dict[str, Any],
    current_command: Optional[Any],
) -> bool:
    """这份 verdict 还能代表 ``current_command`` 吗？

    Args:
        verdict: 已记录的结论（可能带也可能不带指纹）。
        current_command: 计划里这个 VP **现在**的 ``test_command``。
            传 ``None`` 表示"这个 VP 已不在计划里"——此时无从比较，
            返回 ``False``，维持旧行为，不擅自作废。

    Returns:
        ``True`` = 结论已失效，该 VP 必须重跑。

    规则：
      * ``SPLIT`` 不参与校验（子 VP 带真结论）。
      * VP 不在计划里 → 不作废。
      * 没有指纹 → **失效**（无法证明出处）。
      * 指纹对不上 → 失效。
    """
    if not isinstance(verdict, dict):
        return False
    if str(verdict.get("status") or "") in _PROVENANCE_EXEMPT_STATUSES:
        return False
    if current_command is None:
        return False
    return verdict.get(FINGERPRINT_FIELD) != command_fingerprint(current_command)
