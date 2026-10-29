"""根因任务与唯一实现任务的构建器。

背景
----
「根因演进」被判定之后（``verification.failure_fingerprint.needs_root_cause_task``），
框架不能接着生成第三个、第四个「修复 VP-xxx」任务 —— 那些任务打的还是已经
修好的地方。本模块提供这条收敛链上的两个任务形状：

1. **根因任务**（``kind == "root_cause"``）—— 只读。先把当前根因定位清楚，
   交付一份根因报告；它**没有** ``files_to_modify``，因为它的产出是判断，
   不是改动。
2. **实现任务**（``kind == "implementation"``）—— 根因任务完成后只派**一个**，
   它的 ``files_to_modify`` 取自根因报告里的路径集合。

编号
----
两类任务的 id **复用既有 repair 序列分配器**
（:func:`repair_generator.next_repair_seq_base` /
:func:`repair_generator.parse_repair_task_id`），不引入第二套编号空间。
理由是 ``plan_tasks`` 按 ``(plan_id, task_id)`` 定位：两个都叫「第 3 号」
但编号规则不同的任务，会在同一个 plan 里互相覆盖（见
:func:`repair_generator.next_repair_seq_base` 的复盘）。两类任务靠 ``kind``
区分，不靠 id 的形状。

只读与范围
----------
根因任务 ``read_only=True`` 且**不带** ``files_to_modify``：执行器读不到
改动范围，就不会有「只读任务顺手改了代码」这种事。实现任务反过来，
``files_to_modify`` 是**硬要求** —— 根因报告没给出路径时**拒绝生成**并
报错。宁可这一轮不派任务（人来看一眼），也不要派一个没有改动范围的任务：
那等于把「改哪里」这个问题原样丢回给执行器，而根因任务刚刚回答过它。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional

from repair_generator import (
    next_repair_seq_base,
    normalise_declared_files,
    parse_repair_task_id,
)
from test_command_quality import (
    covers_failing_cases,
    failing_case_ids,
    inspect_command,
)

__all__ = [
    "IMPLEMENTATION_KIND",
    "ROOT_CAUSE_DELIVERABLE",
    "ROOT_CAUSE_KIND",
    "build_implementation_task",
    "build_root_cause_task",
    "root_cause_report_path",
]


#: 根因任务的 kind。repair 生成器靠它区分「诊断」与「修复」。
ROOT_CAUSE_KIND = "root_cause"

#: 实现任务的 kind。根因任务完成后只派这一个。
IMPLEMENTATION_KIND = "implementation"


#: 根因任务必须交付的四件事（arch 决策点 4）。少一件都不算完成根因定位：
#: 没有复现步骤就没法验证「修好了」；没有因果链就会重复上一轮的错；
#: 没有「一次覆盖所有层」的方案就会又剥一层洋葱。
ROOT_CAUSE_DELIVERABLE = (
    "确定性复现步骤",
    "完整因果链",
    "一次覆盖所有层的修复方案",
    "验收方式（只选中这次失败的用例）",
)


def root_cause_report_path(plan_id: str, vp_id: str) -> str:
    """根因报告的落盘路径（相对仓库根）。

    ``plans/<plan_id>/reports/root-cause-<vp_id>.md`` —— 一个 plan 一个
    报告目录，一个验证点一份报告。
    """
    plan = str(plan_id or "").strip()
    vp = str(vp_id or "").strip()
    if not plan:
        raise ValueError("根因任务必须带 plan_id")
    if not vp:
        raise ValueError("根因任务必须带 vp_id")
    return f"plans/{plan}/reports/root-cause-{vp}.md"


def _allocate_task_id(
    round_no: int,
    existing_task_ids: Optional[Iterable[str]] = None,
    seq_base: Optional[int] = None,
    task_id: Optional[str] = None,
) -> str:
    """从既有 repair 序列里分配一个任务 id。

    显式 ``task_id`` 优先；否则用 ``seq_base``（调用方已经算好的号），
    再否则用 :func:`repair_generator.next_repair_seq_base` 在既有 id 里
    找下一个空号。产出的形状是 ``repair-r{round}-{seq}`` —— 既有解析器
    :func:`repair_generator.parse_repair_task_id` 读得出来，所以它占的号
    会被后来的分配看见。

    显式给的 ``task_id`` 也要读得出来才收：读不出来的 id 属于另一套编号
    空间，会绕开序列分配器 —— 那正是这个模块要避免的事。
    """
    round_number = int(round_no)
    if round_number < 1:
        raise ValueError(f"round_no must be >= 1, got {round_no}")

    explicit = str(task_id or "").strip()
    if explicit:
        if parse_repair_task_id(explicit) is None:
            raise ValueError(
                f"task_id 必须落在既有 repair 序列里（repair-r<round>-<seq>），"
                f"收到 {explicit}"
            )
        return explicit

    if seq_base is not None:
        seq = int(seq_base)
        if seq < 1:
            raise ValueError(f"seq_base must be >= 1, got {seq_base}")
    else:
        seq = next_repair_seq_base(list(existing_task_ids or ()), round_number)
    return f"repair-r{round_number}-{seq:02d}"


def _report_paths(root_cause_report: Any) -> List[str]:
    """从根因报告里读出「要改哪些文件」。

    报告是诊断的产物，形状由写它的人决定，所以这里读几个约定俗成的键：
    ``paths`` / ``files_to_modify``，以及 ``fix_plan`` 里嵌套的同名字段。
    一个路径列表也算一份报告（调用方已经知道路径，只是没有别的内容）。
    """
    if isinstance(root_cause_report, Mapping):
        for key in ("paths", "files_to_modify"):
            value = root_cause_report.get(key)
            if value:
                if isinstance(value, (list, tuple)):
                    return list(value)
                return [value]
        fix_plan = root_cause_report.get("fix_plan")
        if isinstance(fix_plan, Mapping):
            for key in ("files_to_modify", "paths"):
                value = fix_plan.get(key)
                if value:
                    if isinstance(value, (list, tuple)):
                        return list(value)
                    return [value]
        return []
    if isinstance(root_cause_report, str):
        # 字符串是路径的字符序列，不是路径集合 —— 当成集合会把每个字母
        # 都算成一个文件。宁可报错也不要生成一个胡说八道的改动范围。
        raise ValueError(
            "根因报告必须是 mapping 或路径列表，收到字符串"
        )
    if isinstance(root_cause_report, (list, tuple)):
        return list(root_cause_report)
    return []


def _evidence_payload(evidence: Any) -> Dict[str, Any]:
    """证据字典化。mapping 原样收下；不是 mapping 的收进 ``raw``。

    证据来自失败现场，形状不归我们管 —— 但任务 dict 的字段形状归我们管，
    所以这里把它压成一个字典，而不是让 ``dict()`` 在一个字符串上炸掉。
    """
    if evidence is None:
        return {}
    if isinstance(evidence, Mapping):
        return dict(evidence)
    return {"raw": evidence}


def build_root_cause_task(
    plan_id: str,
    vp_id: str,
    round_no: int,
    evidence: Optional[Mapping[str, Any]] = None,
    *,
    task_id: Optional[str] = None,
    existing_task_ids: Optional[Iterable[str]] = None,
    seq_base: Optional[int] = None,
) -> Dict[str, Any]:
    """构建一个只读的根因任务。

    Args:
        plan_id: 计划 id，决定根因报告落在 ``plans/<plan_id>/reports/``。
        vp_id: 根因演进的那个验证点。
        round_no: 当前验证轮次。
        evidence: 本轮的失败证据（命令、错误原文等），随任务带过去 ——
            根因任务要复现的就是它。
        task_id: 显式指定 id（调用方自己分配时用）。
        existing_task_ids: 既有任务 id，用来在 repair 序列里找空号。
        seq_base: 调用方已经算好的起始序号。

    Returns:
        任务 dict。``kind`` 是 :data:`ROOT_CAUSE_KIND`，``read_only`` 为
        ``True``，``report_path`` 指向根因报告，**没有** ``files_to_modify``。
    """
    report_path = root_cause_report_path(plan_id, vp_id)
    round_number = int(round_no)
    vp = str(vp_id).strip()

    return {
        "id": _allocate_task_id(
            round_number, existing_task_ids, seq_base, task_id,
        ),
        "kind": ROOT_CAUSE_KIND,
        "title": f"根因定位：{vp}（第 {round_number} 轮失败，根因已演进）",
        "description": (
            f"{vp} 连续第 {round_number} 轮失败，但失败指纹已变化 —— "
            "上一轮针对旧根因的修复方案不再适用。先根据本轮证据定位当前"
            "根因，再给出一次覆盖所有层的修复方案，不要重复上一轮的方案。"
            f"根因报告写到 {report_path}。"
        ),
        # 只读：诊断任务的产出是报告，不是改动。
        "read_only": True,
        "report_path": report_path,
        "deliverable": list(ROOT_CAUSE_DELIVERABLE),
        "failed_vp_id": vp,
        "round": round_number,
        "task_group": f"repair-round-{round_number}",
        "priority": "high",
        "status": "pending",
        "evidence": _evidence_payload(evidence),
    }


def build_implementation_task(
    root_cause_report: Any,
    plan_id: Optional[str] = None,
    vp_id: Optional[str] = None,
    round_no: Optional[int] = None,
    *,
    evidence: Optional[Mapping[str, Any]] = None,
    task_id: Optional[str] = None,
    existing_task_ids: Optional[Iterable[str]] = None,
    seq_base: Optional[int] = None,
) -> Dict[str, Any]:
    """根据根因报告构建唯一的实现任务。

    Args:
        root_cause_report: 根因报告。mapping（``paths`` /
            ``files_to_modify``，或 ``fix_plan`` 里的同名字段）或路径列表。
            **必须**给出改动范围。
        plan_id: 计划 id；缺省时从报告的 ``plan_id`` 读。
        vp_id: 验证点 id；缺省时从报告的 ``vp_id`` 读。
        round_no: 轮次；缺省时从报告的 ``round`` 读，再缺省为 1。
        evidence: 失败证据，可选。
        task_id: 显式指定 id。
        existing_task_ids: 既有任务 id，用来在 repair 序列里找空号。
        seq_base: 调用方已经算好的起始序号。

    Returns:
        任务 dict。``kind`` 是 :data:`IMPLEMENTATION_KIND`，
        ``files_to_modify`` 取自根因报告。

    Raises:
        ValueError: 报告里没有可用路径 —— 拒绝生成一个没有改动范围的
            实现任务。
    """
    report: Mapping[str, Any] = (
        root_cause_report if isinstance(root_cause_report, Mapping) else {}
    )

    plan = str(plan_id or report.get("plan_id") or "").strip()
    vp = str(vp_id or report.get("vp_id") or "").strip()
    if not plan:
        raise ValueError("实现任务必须带 plan_id")
    if not vp:
        raise ValueError("实现任务必须带 vp_id")

    round_number = int(
        round_no if round_no is not None else (report.get("round") or 1)
    )

    files = normalise_declared_files(_report_paths(root_cause_report))
    if not files:
        raise ValueError(
            f"根因报告里没有可用的相对路径，拒绝生成 {vp} 的实现任务"
            "（改动范围为空的任务等于把「改哪里」丢回给执行器）"
        )

    report_path = root_cause_report_path(plan, vp)
    root_cause = str(report.get("root_cause") or "").strip()
    fix_plan = str(report.get("fix_plan") or "").strip()

    description_parts = [f"按 {report_path} 的根因结论修复 {vp}。"]
    if root_cause:
        description_parts.append(f"根因：{root_cause}")
    if fix_plan:
        description_parts.append(f"修复方案：{fix_plan}")

    test_command = _narrow_test_command(
        str(report.get("test_command") or ""), evidence, vp,
    )

    # 2026-10-29 — the criterion is the task's ``test_command`` exit code.
    # When the command was dropped (it was wide, or it was unusable) there
    # is no exit code to speak of, and the report's own 验收方式 is what the
    # task is judged by — so the criteria say so instead of naming a
    # command the task does not carry.
    if test_command:
        acceptance = f"{vp} 的验收命令在改动后退出 0"
    else:
        acceptance = f"{vp} 按 {report_path} 里写的验收方式验收"

    return {
        "id": _allocate_task_id(
            round_number, existing_task_ids, seq_base, task_id,
        ),
        "kind": IMPLEMENTATION_KIND,
        "title": f"实现 {vp} 的根因修复方案",
        "description": " ".join(description_parts),
        "acceptance_criteria": (
            f"{acceptance}，且 {report_path} 里列出的"
            "每一层根因都有对应的改动或被证明无关。"
        ),
        # 改动范围来自根因报告 —— 报告说改哪里，这里就改哪里。
        "files_to_modify": files,
        "test_command": test_command,
        "source_report": report_path,
        "failed_vp_id": vp,
        "round": round_number,
        "task_group": f"repair-round-{round_number}",
        "priority": "high",
        "status": "pending",
        "evidence": _evidence_payload(evidence),
    }


def _evidence_text(evidence: Optional[Mapping[str, Any]]) -> str:
    """Every string in the evidence mapping, concatenated.

    The run output is spread over several fields (the verdict's own
    message, the captured output) and *which* field carries pytest's
    short summary depends on the runner — so there is no field
    whitelist here. A whitelist would silently miss the runner that
    puts its summary somewhere else, and the narrowing would then never
    fire.
    """
    parts: List[str] = []

    def _collect(value: Any) -> None:
        if isinstance(value, str):
            if value:
                parts.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                _collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _collect(item)

    _collect(evidence)
    return "\n".join(parts)


def _narrow_test_command(
    command: str,
    evidence: Optional[Mapping[str, Any]],
    vp_id: str,
) -> str:
    """The implementation task's criterion — narrowed to the failed cases.

    The task used to take the verification point's command verbatim, and
    it did not even run the structural check the ordinary repair path
    runs. For a ``full_ci`` point that command is the whole-repository
    gate: one round of this task is then 45-70 minutes, and a fix needs
    5-10 rounds. The gate is also a bad criterion — it is red for reasons
    that have nothing to do with this fix.

    So the command is checked twice: it has to be structurally sound (the
    same check every other command in the codebase gets), and it has to
    select the cases that actually failed. A command that fails either
    check is dropped and the task ships without one — the same
    degradation as a missing command: it falls back to the audit second
    pass, which can actually pass, whereas a wide command guarantees a
    false failure every round.

    No case id in the evidence means there is nothing to narrow towards,
    and the command is kept as it was.
    """
    command = str(command or "").strip()
    if not command:
        return ""

    issues = inspect_command(command)
    if issues:
        print(
            f"[RootCause] Discarding unusable test_command for {vp_id} "
            f"({issues[0].code}): {command[:120]}"
        )
        return ""

    case_ids = failing_case_ids(_evidence_text(evidence))
    if case_ids and not covers_failing_cases(command, case_ids):
        print(
            f"[RootCause] Discarding wide test_command for {vp_id} "
            f"(it does not select the cases that failed): {command[:120]}"
        )
        return ""

    return command
