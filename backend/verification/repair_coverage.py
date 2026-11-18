"""修复覆盖判定 —— 「每个失败 VP 必须有出路」的纯函数。

这个模块解决什么
================

一轮验证里可能同时有多个 ``deliverable_defect`` 失败，而 repair 内容是
**一次**生成的：生成器拿到所有候选 VP，产出若干条 repair 内容，
``RepairTaskAssembler.assemble`` 再把这些内容变成任务行。

两件事可以让一个候选 VP 悄悄地消失：

* 生成器压根没为它产出内容（模型漏了一条）；
* 生成器产出了，但 ``assemble`` 把 ``files_to_modify`` 为空的行丢掉
  （模型说了「要返工」，却没说改哪个文件）。

消失的那条 VP 既没有 repair 任务，也没有 HITL —— 没有人知道它失败过，
直到下一轮它再次失败。2026-11-17 的一次验证轮就是这样丢了一个 VP-023。

本模块给出的是那个**核对**（arch 决策点 1）：哪些候选 VP 没有拿到
repair 任务，以及**为什么**没有（arch 决策点 7 —— 原因要能在日志里
看见，因为「生成器漏了」和「被 assemble 丢了」要修的地方完全不同：
前者要改生成器的输入，后者要改生成器的输出契约）。

为什么是纯函数
==============

不读文件、不发请求、不看时钟、不导入 orchestrator。orchestrator 将来要
import 这个模块（arch 决策点 1 的接线），方向反了就是环；而且「这一批
到底覆盖了谁」是一个可以单独复算的判定，不该藏在一段有副作用的编排里。

模块末尾的 :func:`ensure_repair_coverage` 是唯一的例外：它是**编排**，但
它自己什么都不做 —— 调模型、装配任务、开 HITL 这三件事都由注入进来的
回调完成，所以它同样可以单测。它会打印 R7 要求的覆盖日志（``print``，
不读不写任何外部状态）。

三条边界
========

* **候选顺序就是返回顺序** —— 调用方按候选顺序提问，顺序变了，人看到的
  提问顺序就变了。
* **重复的候选 id 只算一次** —— 一个 VP 出现两遍不代表它缺两遍。
* **空的 id 跳过** —— 没有 id 的候选既没法被任务覆盖，也没法被提问；
  它不是「缺失」，它是「不存在」。

选项与默认值
============

HITL 的选项表写在这里，而不是写在 orchestrator 里：

* :data:`MISSED_VP_OPTIONS` —— R3：VP 失败了却没拿到 repair 任务。
  三个出路：人来修（``manual_fix``）、判据本身有问题要改判据
  （``revise_criterion``）、暂时搁置（``defer``）。
* :data:`INFRA_TERMINAL_OPTIONS` —— R4：infra 重试预算耗尽
  （``infrastructure_unresolved``）。三个出路：再重试
  （``retry_more``）、跳过这个 VP（``skip_this_vp``）、暂时搁置
  （``defer``）。

两者的默认选项都是**第一个**（``manual_fix`` / ``retry_more``），但真正
写进决策的默认值走 :func:`recommended_default`：默认跟随这条 VP 自己的
``recommendation``，只有当它不在选项表内（或根本没有）时才回落到这个
默认值 —— 与已合入的「HITL 默认跟随建议」一致。

原因 token 是字面量
==================

:data:`REASON_GENERATOR_MISSING` 与 :data:`REASON_EMPTY_FILES_TO_MODIFY`
会被写进日志与 HITL 问题里，所以它们的拼写是**接口**，不是实现细节：
改一个字母，跨进程的日志对不上，历史记录也读不懂。描述文本同样被钉住
—— 任务 7（``test_repair_coverage_hitl.py``）直接断言
``describe_reason`` 的文本片段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Tuple

__all__ = [
    "INFRA_TERMINAL_DEFAULT_OPTION",
    "INFRA_TERMINAL_OPTIONS",
    "MISSED_VP_DEFAULT_OPTION",
    "MISSED_VP_OPTIONS",
    "REASON_EMPTY_FILES_TO_MODIFY",
    "REASON_GENERATOR_MISSING",
    "RepairCoverageOutcome",
    "describe_reason",
    "ensure_repair_coverage",
    "infra_terminal_question",
    "missed_reason",
    "missed_vp_question",
    "missing_repair_targets",
    "recommended_default",
]


# ---------------------------------------------------------------------------
# 选项与默认值
# ---------------------------------------------------------------------------

#: R3：VP 失败了却没拿到 repair 任务时，给人的三个出路。
#:
#: * ``manual_fix`` —— 人自己修；
#: * ``revise_criterion`` —— 判据本身有问题，要改判据（本轮失败的判据
#:   作废，由操作者另行补充新判据）；
#: * ``defer`` —— 暂时搁置，下一轮再说。
MISSED_VP_OPTIONS: Tuple[str, ...] = (
    "manual_fix",
    "revise_criterion",
    "defer",
)

#: R3 的默认选项：**没有 recommendation 时的回落值**，不是无条件生效的
#: 默认值。真正写进决策的默认值见 :func:`recommended_default`。
MISSED_VP_DEFAULT_OPTION: str = "manual_fix"

#: R4：infra 重试预算耗尽（``infrastructure_unresolved``）时，给人的三个
#: 出路。
#:
#: * ``retry_more`` —— 再重试一轮（超出原预算，由人决定）；
#: * ``skip_this_vp`` —— 跳过这个 VP，不再管它；
#: * ``defer`` —— 暂时搁置。
INFRA_TERMINAL_OPTIONS: Tuple[str, ...] = (
    "retry_more",
    "skip_this_vp",
    "defer",
)

#: R4 的默认选项：同样只是 ``recommendation`` 不在表内时的回落值。
INFRA_TERMINAL_DEFAULT_OPTION: str = "retry_more"


# ---------------------------------------------------------------------------
# 原因 token
# ---------------------------------------------------------------------------

#: 生成器压根没为这个 VP 产出内容 —— 模型漏了一条。
REASON_GENERATOR_MISSING: str = "generator_returned_no_entry"

#: 生成器产出了内容，但 ``assemble`` 把这一行丢掉了 —— 它的
#: ``files_to_modify`` 是空的。
REASON_EMPTY_FILES_TO_MODIFY: str = "files_to_modify_was_empty"

#: 每个原因 token 的人类可读描述。
#:
#: 这些句子是**从句**：它们接在 "because" 后面，所以主句由调用方写。
#: 文本被钉住 —— 任务 7 直接断言其中的片段。
_REASON_DESCRIPTIONS: Dict[str, str] = {
    REASON_GENERATOR_MISSING: "the generator returned no entry for this VP",
    REASON_EMPTY_FILES_TO_MODIFY: (
        "its files_to_modify was empty and the row was dropped"
    ),
}

#: 一个没见过的 token 的描述。
#:
#: 宁可说「不知道」，也不把一个 token 原样拼进给人看的句子 —— 那读起来
#: 像机器码，不像解释。
_UNKNOWN_REASON_DESCRIPTION: str = "the reason it has no repair task is unknown"


# ---------------------------------------------------------------------------
# 覆盖判定
# ---------------------------------------------------------------------------


def _as_id_list(value: Any) -> List[str]:
    """A list of ids, blanks dropped, order preserved.

    A bare string is read as **one** id rather than as an iterable of
    characters: ``"VP-013"`` is a point, and splitting it into ``V``,
    ``P``, ``-``… would answer a question nobody asked.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[Any] = (value,)
    else:
        try:
            items = list(value)
        except TypeError:
            return []
    ids: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            ids.append(text)
    return ids


def missing_repair_targets(
    candidate_ids: Any,
    repair_tasks: Any,
) -> List[str]:
    """The candidates that got no repair task.

    Args:
        candidate_ids: The VPs that need a repair task — the points
            whose failure is a ``deliverable_defect`` (the
            ``retry_counted`` disposition).
        repair_tasks: The assembled repair tasks. Each row names the
            point it repairs in ``failed_vp_id``.

    Returns:
        The candidates with no task pointing at them, in the order the
        candidates were given. Blank ids are skipped and a repeated id
        is counted once.

    A task whose ``failed_vp_id`` names a candidate **covers** that
    candidate, however many tasks point at it: coverage is a
    yes-or-no per point, not a count.
    """
    covered = set()
    for task in repair_tasks or ():
        if not isinstance(task, dict):
            continue
        vp_id = str(task.get("failed_vp_id") or "").strip()
        if vp_id:
            covered.add(vp_id)

    missed: List[str] = []
    seen = set()
    for vp_id in _as_id_list(candidate_ids):
        if vp_id in seen:
            continue
        seen.add(vp_id)
        if vp_id not in covered:
            missed.append(vp_id)
    return missed


# ---------------------------------------------------------------------------
# 缺失原因
# ---------------------------------------------------------------------------


def missed_reason(vp_id: Any, generated_vp_ids: Any) -> str:
    """Why a candidate has no repair task.

    The two causes need different fixes, so they are told apart:

    * the generator **never mentioned** this VP → the generator is the
      thing to fix (:data:`REASON_GENERATOR_MISSING`);
    * the generator **did mention** it, but no task came out → the row
      was dropped on the way in, almost always because its
      ``files_to_modify`` was empty
      (:data:`REASON_EMPTY_FILES_TO_MODIFY`).

    Args:
        vp_id: The point that got no task.
        generated_vp_ids: The VPs the generator produced content for.

    Returns:
        One of the two reason tokens.
    """
    target = str(vp_id or "").strip()
    if target:
        for generated in _as_id_list(generated_vp_ids):
            if generated == target:
                return REASON_EMPTY_FILES_TO_MODIFY
    return REASON_GENERATOR_MISSING


def describe_reason(token: Any) -> str:
    """The human-readable description of a reason token.

    Args:
        token: One of the reason tokens.

    Returns:
        A phrase that reads as the tail of a sentence ("… because
        <here>"). An unknown token gets a neutral description instead of
        the token itself: a raw token in a question a human reads reads
        like machine noise, and "we do not know" is the truth.
    """
    return _REASON_DESCRIPTIONS.get(
        str(token or "").strip(), _UNKNOWN_REASON_DESCRIPTION
    )


# ---------------------------------------------------------------------------
# HITL 的选项与文案
# ---------------------------------------------------------------------------


def recommended_default(vp: Any, options: Any, fallback: Any) -> str:
    """The default answer for a question about ``vp``.

    The default **follows the point's own recommendation** (the
    already-merged "HITL defaults to the recommendation" rule). The
    fallback is what happens when there is no recommendation, or when the
    recommendation is not one of the answers on the table — a
    recommendation nobody can pick is not a default.

    Args:
        vp: The point the question is about. Its ``recommendation`` is
            read when there is one.
        options: The answers on the table.
        fallback: The answer to use when the recommendation cannot be
            used.

    Returns:
        The option id to use as the default.
    """
    allowed = tuple(str(option or "").strip() for option in (options or ()))
    allowed = tuple(option for option in allowed if option)
    fallback_option = str(fallback or "").strip()

    recommendation = ""
    if isinstance(vp, dict):
        recommendation = str(vp.get("recommendation") or "").strip()
    if recommendation and recommendation in allowed:
        return recommendation
    return fallback_option


def missed_vp_question(vp_id: Any, reason_token: Any, round_number: Any) -> str:
    """The question asked when a failed VP got no repair task.

    The question has to say two things: **which** point is stuck, and
    **why** it is stuck — a person who is only told "VP-023 failed"
    cannot tell whether the repair generator missed it or the assembler
    dropped it, and those two need different answers.

    Args:
        vp_id: The point that got no task.
        reason_token: The reason token, as returned by
            :func:`missed_reason`.
        round_number: The verification round the failure came from.

    Returns:
        The question text.
    """
    label = str(vp_id or "").strip()
    subject = f"the verification point {label}" if label else "a verification point"
    return (
        f"{subject} failed in round {round_number}, but no repair task was "
        f"generated for it because {describe_reason(reason_token)}; it needs an "
        f"answer before the next round can count on it"
    )


def infra_terminal_question(vp_id: Any, round_number: Any) -> str:
    """The question asked when a point's infra retry budget ran out.

    The point was never judged — the environment kept breaking — so it
    buys no repair task either. The question says so, because the
    difference between "the deliverable is broken" and "we never got to
    look" is the whole reason the infra terminal is asked about at all.

    Args:
        vp_id: The point whose budget ran out.
        round_number: The verification round the failure came from.

    Returns:
        The question text.
    """
    label = str(vp_id or "").strip()
    subject = f"the verification point {label}" if label else "a verification point"
    return (
        f"{subject} failed in round {round_number} because its environment kept "
        f"failing, and its infra retry budget is now exhausted, so it was never "
        f"judged; it needs an answer before it can go further"
    )


# ---------------------------------------------------------------------------
# 编排：校验 → 补生成一次 → 再校验 → 逐个提问
# ---------------------------------------------------------------------------


@dataclass
class RepairCoverageOutcome:
    """一轮 repair 批次物化之后的覆盖结果。

    字段
    ----

    * ``repair_tasks`` —— 合并后的任务表（原批次 + 补生成出来的那几条）；
    * ``missed`` —— **最终**仍然没有任务的 VP（补齐之后还在缺的那些）；
    * ``regenerated`` —— 有没有发生过补生成；
    * ``regenerated_task_ids`` —— 补生成出来、并且真的合并进去的任务 id；
    * ``hitl_raised`` —— 真的开出去提问的 VP。``ask_human`` 返回空串的
      不算：没开出去就不是「开了」。
    """

    repair_tasks: List[Dict[str, Any]] = field(default_factory=list)
    missed: List[str] = field(default_factory=list)
    regenerated: bool = False
    regenerated_task_ids: List[str] = field(default_factory=list)
    hitl_raised: List[str] = field(default_factory=list)


def _candidate_ids(candidates: Any) -> List[str]:
    """The ids of the candidate points, in the order they were given.

    A candidate is normally a dict carrying an ``id`` (``{"id":
    "VP-023"}``); a bare string is accepted too, because a list of ids
    is a thing people write.
    """
    ids: List[str] = []
    for candidate in candidates or ():
        if isinstance(candidate, dict):
            raw = candidate.get("id") or candidate.get("vp_id")
        else:
            raw = candidate
        text = str(raw or "").strip()
        if text:
            ids.append(text)
    return ids


def _content_vp_ids(contents: Any) -> List[str]:
    """The VPs the repair contents speak about, blanks dropped."""
    vp_ids: List[str] = []
    for content in contents or ():
        if not isinstance(content, dict):
            continue
        vp_id = str(content.get("failed_vp_id") or "").strip()
        if vp_id:
            vp_ids.append(vp_id)
    return vp_ids


def _task_vp_id(task: Any) -> str:
    """The VP a task row repairs, or an empty string."""
    if not isinstance(task, dict):
        return ""
    return str(task.get("failed_vp_id") or "").strip()


def ensure_repair_coverage(
    *,
    candidates: Any,
    repair_tasks: Any,
    generated_vp_ids: Any,
    regenerate: Callable[[List[str]], List[Dict[str, Any]]],
    assemble: Callable[[List[Dict[str, Any]], List[str]], List[Dict[str, Any]]],
    ask_human: Callable[[str, str], str],
    round_number: Any,
) -> RepairCoverageOutcome:
    """Check the coverage of a repair batch, regenerate once, ask about the rest.

    The batch is checked, and the points that got no task are given a
    **second** chance — once. A second chance that is allowed to loop
    is a second chance that spends a model's budget on every candidate
    in the plan, so the regeneration happens at most one time; whatever
    is still missing afterwards is a question for a human, not another
    round of generation.

    Every side effect is injected, so this function is testable without
    a model, an assembler or a HITL service:

    Args:
        candidates: The points that need a repair task — each one names
            its point in ``id``.
        repair_tasks: The batch that was just assembled.
        generated_vp_ids: The VPs the batch's contents spoke about.
        regenerate: Called with the missing VP ids; returns repair
            contents (rows carrying ``failed_vp_id``). Called **at most
            once**, and not at all when nothing is missing.
        assemble: Called with the regenerated contents and the missing
            VP ids; returns the assembled task rows.
        ask_human: Called once per point that is still missing, with the
            point's id and the reason token from :func:`missed_reason`.
            An empty return means no question was actually opened.
        round_number: The verification round the failures came from —
            it is only used in the coverage log lines.

    Returns:
        A :class:`RepairCoverageOutcome` carrying the merged task table,
        the points that are still missing, whether a regeneration
        happened, the ids of the tasks it produced, and the points that
        were raised to a human.

    A regenerated row about a point that already has a task is dropped
    instead of merged: two tasks for one point is not more coverage, it
    is a duplicated repair.
    """
    candidate_ids = _candidate_ids(candidates)
    tasks: List[Dict[str, Any]] = list(repair_tasks or ())
    missed = missing_repair_targets(candidate_ids, tasks)
    print(
        f"[Repair] round {round_number}: "
        f"repair_coverage_checked missing={missed}"
    )

    if not missed:
        return RepairCoverageOutcome(repair_tasks=tasks)

    missed_set = set(missed)
    already_covered = {_task_vp_id(task) for task in tasks}
    already_covered.discard("")

    # R2: 补生成最多 1 次 —— 这里调一次，之后无论补上没有都直接再校验。
    contents = list(regenerate(list(missed)) or ())
    print(f"[Repair] round {round_number}: repair_coverage_regenerated")

    # 补生成里提到已覆盖 VP 的行丢掉：那个点已经有出路了，再来一条
    # 不是「多覆盖」，是「同一条 VP 被修两次」。
    relevant = [
        content
        for content in contents
        if isinstance(content, dict) and _task_vp_id(content) in missed_set
    ]
    assembled = list(assemble(relevant, list(missed)) or ())
    new_tasks = [
        task
        for task in assembled
        if isinstance(task, dict) and _task_vp_id(task) not in already_covered
    ]

    merged: List[Dict[str, Any]] = tasks + new_tasks
    regenerated_task_ids = [
        str(task.get("id") or "").strip()
        for task in new_tasks
        if str(task.get("id") or "").strip()
    ]

    # 第二次校验看到的「生成器提到过」= 首批提到的 ∪ 补生成提到的。
    # 少了补生成那一半，被 assemble 丢掉的那条 VP 就会被误报成
    # 「生成器漏了」——而它其实被提到过，只是 scope 是空的。
    second_generated = list(_as_id_list(generated_vp_ids)) + _content_vp_ids(contents)
    still_missed = missing_repair_targets(candidate_ids, merged)

    hitl_raised: List[str] = []
    for vp_id in still_missed:
        reason = missed_reason(vp_id, second_generated)
        response = ask_human(vp_id, reason)
        if str(response or "").strip():
            hitl_raised.append(vp_id)

    print(
        f"[Repair] round {round_number}: "
        f"repair_coverage_hitl_raised={hitl_raised}"
    )

    return RepairCoverageOutcome(
        repair_tasks=merged,
        missed=still_missed,
        regenerated=True,
        regenerated_task_ids=regenerated_task_ids,
        hitl_raised=hitl_raised,
    )
