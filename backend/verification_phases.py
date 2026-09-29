"""两阶段验证计划的阶段语义（2026-09-16）。

背景
----
E2E 贯通 VP 与 nightly CI 都是必须经过的步骤，但它们属于最终关卡，
应当排在所有前置 VP 之后：等 Phase 1 逐条迭代通过，再跑全量 CI 与 E2E。
顺序上先 Nightly CI、后 E2E。

由此确定的规则：

* 计划里**允许**没有 Phase 2；
* Phase 1 里**绝对不允许**出现 Phase 2 的任务；
* 一个 VP 属于哪一阶段，由生成任务列表的模型在同一次调用里判断，
  **不是**用字符串匹配去猜。

于是验证计划被分成两段：

* **Phase 1（子任务级验证）**：界定到具体用例的单元/集成/API/契约/UI/
  代码审查。失败就直接回炉迭代——这是日常迭代的主体。
* **Phase 2（全量关卡）**：**只有 Phase 1 全部通过之后**才执行。
  顺序固定：先全量 Nightly CI（``phase_order=1``），后全量 E2E
  （``phase_order=2``），且 E2E 还要等 Nightly CI 通过。Phase 1 有失败时
  这些验证点标 ``DEFERRED``（本轮延后），不执行、不计失败。

谁来决定一个 VP 属于哪一阶段
----------------------------
**生成阶段的规划 LLM。** 它在同一个调用里既产出 VP 清单，也产出每个 VP 的
``verification_phase``（见 :data:`prompts.VERIFICATION_PLAN_SYSTEM_PROMPT`
的"两阶段结构"一节）。

本模块**不**做阶段判定。它只做与语义无关的归一化：

1. 缺省 / 非法的 ``verification_phase`` → ``PHASE_CORE``；
2. Phase 1 上的 ``phase_order`` 清掉（该字段只对 Phase 2 有意义）；
3. Phase 2 缺 ``phase_order`` → 按计划顺序补位，并记进
   ``summary["missing_phase_order"]`` 供审计；
4. **Phase 2 专用方法（``e2e`` / ``full_ci``）被写在 Phase 1 上 →
   提升到 Phase 2**，记 ``phase_promoted_by``。

第 4 条不是"判定阶段"，是**读声明**：VP 既然写了
``verification_method: full_ci``，那它自己就声明了整仓门禁的语义——
``e2e`` / ``full_ci`` 的定义就是"全量关卡"，写在 Phase 1 上是计划内部
自相矛盾。这里读的正是那个字段，与第 3 条"按计划顺序补位"不同：补位是
不猜，提升是**不猜**地执行一条 VP 自己给出的声明。

为什么不做字符匹配兜底（2026-09-16 实测推翻）
------------------------------------------------
第一版实现里有一层"确定性兜底"：命令命中 ``cargo test --workspace`` /
``e2e`` / ``playwright test`` 之类的形态特征、或标题命中 ``端到端`` /
``整仓`` 之类的词，就把 VP 从 Phase 1 提升到 Phase 2。on that plan
实测的结果是**它比 LLM 错得多**：

* 命令特征 ``re.compile(r"e2e")`` 是裸子串匹配，命中的是**路径**
  ``tests/e2e/tooltip_consolidation_0903.spec.ts``——5 条"只跑单个 spec
  文件"的有界 VP 被误提升；
* 标题特征把 ``端到端`` / ``全流程`` 这类**方法词**当成**范围词**，又误
  提升 3 条（它们是几十秒的 curl 活服务检查）；
* 结果 Phase 2 从设计上的 2 条涨到 11 条，一条 5 分钟的 ruff 门禁被排到
  3600s 的全量 E2E **之后**。

而 LLM 自己给出的标注是**正确的**：那 9 条本来都该留在 Phase 1。
所以判定权交回模型，本模块只负责让字段自洽。护栏（
:mod:`verification_command_guard`）继续独立地拦"命令范围与断言不相称"，
那是另一件事——它报的是命令的**范围**，不是它属于哪一阶段。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

#: 子任务级验证（默认）。
PHASE_CORE = 1
#: 全量关卡（nightly CI / E2E），只在 Phase 1 全绿后执行。
PHASE_FINAL_GATE = 2

#: Phase 2 内部顺序的**契约值**——由规划 LLM 在 ``phase_order`` 里给出：
#: Nightly CI 必须先于 E2E。本模块不再据此做任何字符串推断，常量保留是
#: 为了让提示词、本条契约与下游比较逻辑引用同一个数字。
ORDER_NIGHTLY = 1
ORDER_E2E = 2

PHASE_FIELD = "verification_phase"
ORDER_FIELD = "phase_order"


def is_final_gate(vp: Dict[str, Any]) -> bool:
    """该 VP 是否属于 Phase 2（全量关卡）。"""
    if not isinstance(vp, dict):
        return False
    try:
        return int(vp.get(PHASE_FIELD, PHASE_CORE)) == PHASE_FINAL_GATE
    except (TypeError, ValueError):
        return False


def infer_phase_order(vp: Dict[str, Any], fallback_index: int = 0) -> int:
    """Phase 2 VP 的执行顺序。

    只读 LLM 写下的 ``phase_order``（正整数）。缺失或不合法时退回
    ``fallback_index``（该 VP 在计划里的位置），**不做**任何关键字推断——
    "先 Nightly 后 E2E"是提示词对模型的明确要求，不是本函数猜出来的。
    """
    if not isinstance(vp, dict):
        return fallback_index
    raw = vp.get(ORDER_FIELD)
    if isinstance(raw, bool):          # bool 是 int 的子类，不能当序号用
        return fallback_index
    if isinstance(raw, int) and raw >= 1:
        return raw
    return fallback_index


def normalize_plan_phases(plan_data: Dict[str, Any]) -> Dict[str, Any]:
    """就地归一化阶段字段，返回变更摘要（可审计）。

    只做字段自洽，**不判定阶段**（判定权在规划 LLM，见模块 docstring）：

    1. 缺省 / 非法的 ``verification_phase`` → ``PHASE_CORE``。
    2. Phase 1 上的 ``phase_order`` 清掉，避免下游误读。
    3. Phase 2 缺 ``phase_order`` → 按计划顺序补位，并记进
       ``summary["missing_phase_order"]``——这是审计线索，不是分类依据。
    4. **Phase 2 专用方法（``e2e`` / ``full_ci``）被写在 Phase 1 上 →
       提升到 Phase 2**，记 ``phase_promoted_by``。

    第 4 条是**声明驱动**的：VP 自己写了 ``verification_method: full_ci``，
    那它自己就声明了整仓门禁的语义。这里读的是那个字段，不做任何命令文本
    的特征匹配 —— 2026-09-16 被实测推翻的正是后者（``re.compile("e2e")``
    命中路径 ``tests/e2e/tooltip.spec.ts``，把 5 条只跑单个 spec 的有界 VP
    误提升成 Phase 2，Phase 2 从 2 条涨到 11 条）。

    纯函数（就地修改传入的 dict），不调 LLM、不碰磁盘。
    """
    # 懒导入：``verification_subagent`` 是执行层模块，本模块是纯语义内核，
    # 顶层导入会把执行层的依赖（coding_tool 等）拖进只想要阶段语义的调用方。
    from verification_subagent import PHASE_2_ONLY_METHODS

    summary: Dict[str, Any] = {
        "total": 0,
        "phase1": 0,
        "phase2": 0,
        "gates": [],
        "missing_phase_order": [],
        "phase_promoted": [],
    }
    points = plan_data.get("verification_points") if isinstance(plan_data, dict) else None
    if not isinstance(points, list):
        return summary

    for index, vp in enumerate(points):
        if not isinstance(vp, dict):
            continue
        summary["total"] += 1

        raw_phase = vp.get(PHASE_FIELD)
        try:
            phase = int(raw_phase) if raw_phase is not None else PHASE_CORE
        except (TypeError, ValueError):
            phase = PHASE_CORE
        if phase not in (PHASE_CORE, PHASE_FINAL_GATE):
            phase = PHASE_CORE

        # 声明驱动的提升：方法本身就是"这是全量关卡"的声明。
        method = str(vp.get("verification_method") or "").strip()
        if phase == PHASE_CORE and method in PHASE_2_ONLY_METHODS:
            phase = PHASE_FINAL_GATE
            vp["phase_promoted_by"] = f"method:{method}"
            summary["phase_promoted"].append(
                {"id": vp.get("id"), "method": method}
            )

        if phase == PHASE_CORE:
            vp.pop(ORDER_FIELD, None)
            vp[PHASE_FIELD] = PHASE_CORE
            summary["phase1"] += 1
            continue

        vp[PHASE_FIELD] = PHASE_FINAL_GATE
        raw_order = vp.get(ORDER_FIELD)
        has_order = (
            isinstance(raw_order, int)
            and not isinstance(raw_order, bool)
            and raw_order >= 1
        )
        if not has_order:
            summary["missing_phase_order"].append(vp.get("id"))
        # 缺 ``phase_order`` 时按**关卡之间的相对顺序**补位（1 起），
        # 也就是退回计划顺序；不猜这条 VP 是不是 Nightly / E2E。
        _gate_ordinal = len(summary["gates"]) + 1
        vp[ORDER_FIELD] = raw_order if has_order else _gate_ordinal
        summary["phase2"] += 1
        summary["gates"].append(
            {"id": vp.get("id"), ORDER_FIELD: vp[ORDER_FIELD]}
        )

    return summary


def split_by_phase(
    verification_points: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按阶段拆分 VP 列表，Phase 2 按 ``phase_order`` 稳定排序。"""
    core = [vp for vp in verification_points if not is_final_gate(vp)]
    gates = [vp for vp in verification_points if is_final_gate(vp)]
    gates = sorted(
        enumerate(gates),
        key=lambda pair: (infer_phase_order(pair[1], pair[0]), pair[0]),
    )
    return core, [vp for _, vp in gates]


def phase_of(vp: Dict[str, Any]) -> int:
    """该 VP 的阶段编号（1 或 2），便于日志与断言。"""
    return PHASE_FINAL_GATE if is_final_gate(vp) else PHASE_CORE


__all__ = [
    "PHASE_CORE",
    "PHASE_FINAL_GATE",
    "PHASE_FIELD",
    "ORDER_FIELD",
    "ORDER_NIGHTLY",
    "ORDER_E2E",
    "is_final_gate",
    "phase_of",
    "infer_phase_order",
    "normalize_plan_phases",
    "split_by_phase",
]
