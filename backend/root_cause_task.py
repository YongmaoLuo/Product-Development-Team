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

「不带 files_to_modify」说的是**任务自己**不许改代码；它**报告**里却必须
写清楚要改哪些文件 —— 那是下一个实现任务的唯一依据。所以根因任务的
description 里带着一份可照抄的改动范围模板（:data:`_SCOPE_CONTRACT_TEMPLATE`），
读者（``verification.orchestrator._root_cause_scope_from_text``）从报告里
读出路径集合；读不出来就是 ``root_cause_report_without_scope``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional

from orphan_rules import report_deliverable_marker
from repair_generator import (
    format_repair_task_id,
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
    "root_cause_report_contract",
    "root_cause_report_path",
]


#: 根因任务的 kind。repair 生成器靠它区分「诊断」与「修复」。
ROOT_CAUSE_KIND = "root_cause"

#: 实现任务的 kind。根因任务完成后只派这一个。
IMPLEMENTATION_KIND = "implementation"


#: 根因任务必须交付的五件事（arch 决策点 4）。少一件都不算完成根因定位：
#: 没有复现步骤就没法验证「修好了」；没有因果链就会重复上一轮的错；
#: 没有「一次覆盖所有层」的方案就会又剥一层洋葱；没有改动范围，下一个
#: 实现任务就没有 ``files_to_modify`` 可填 —— 根因链会停在
#: ``root_cause_report_without_scope``。
ROOT_CAUSE_DELIVERABLE = (
    "确定性复现步骤",
    "完整因果链",
    "一次覆盖所有层的修复方案",
    "验收方式（只选中这次失败的用例）",
    "改动范围（要改动的文件，相对仓库根的路径）",
)


#: 根因报告的改动范围模板（作者侧的契约）。
#:
#: 读者（``verification.orchestrator._root_cause_scope_from_text``）只读
#: **声明**：整份报告是 JSON，或者报告里有一个语言标注为 ``json`` 的围栏
#: 块。散文不是声明 —— 一段话、一张表、一串 bullet 都读不出「哪个路径是
#: 这次真要新建的、哪个只是举例」，所以读者不再按行扫「改动范围」小节。
#:
#: 模板因此只给形状，不给路径：形状是 ``files_to_modify`` + ``fix_plan``，
#: 路径由作者照自己这次的判断填。模板里**不能**出现任何具体路径 ——
#: 读者会把块里的每一个路径都当成白名单，一个占位名进去就是一个假
#: 白名单项出去。
#:
#: 标题用「改动范围」—— 它现在是给人看的小节标题，读者不再依赖它；但
#: 两边仍然叫同一个名字，读报告的人才找得到那一节。
#:
#: 围栏块是**同一个文件**里的类型化容器：报告任务只允许写一个文件
#: （``agent.py`` 的 ``task_report_path``），再单开一个 ``.scope.json``
#: 会被写盘闸门拒掉。
_SCOPE_CONTRACT_TEMPLATE = """

## 改动范围

报告里**必须**有一个语言标注为 `json` 的代码块，写明这次修复要改哪些文件：

```json
{
  "files_to_modify": ["<相对仓库根的路径>", "..."],
  "fix_plan": "改哪个文件、怎么改、为什么这样改覆盖了所有层"
}
```

* 块里写的**每一个**路径都会被当作本任务可以改写的文件白名单。
  写漏了，实现任务就**改不动**那个文件；写多了，就会把不该改的文件也锁进去。
* **新建的文件也要写进去** —— 还没存在的文件同样在白名单里。
* **不要写示例路径**，只写这次真正要改的路径。占位名不是路径。
"""


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


#: 根因报告路径在任务 description 里的形状：
#: ``plans/<plan_id>/reports/root-cause-<vp_id>.md``。
#:
#: 报告的**地址**由 :func:`root_cause_report_path` 决定，但持久化会把
#: ``deliverable`` / ``report_path`` 一起丢掉（``plan_tasks`` 表没有这两
#: 列，见 ``state_machine.repositories.plan_task_repository``
#: 的 ``ALLOWED_STATIC_TASK_FIELDS``），所以执行器加载这一行时能读到
#: 的只有任务文字里那个地址。要把「交付物是一份报告」这条契约还原回
#: 去，就得从文字里把它读回来。
_ROOT_CAUSE_REPORT_PATH_RE = re.compile(
    r"plans/[^\s`\"\u3002\uff1b\u3001]*/reports/(root-cause-[A-Za-z0-9._-]+\.md)"
)


def root_cause_report_contract(
    row: Mapping[str, Any],
    plan_dir: Any,
) -> Optional[Dict[str, str]]:
    """把「这一行的产出是一份根因报告」还原成执行器认得的契约。

    持久化丢掉了 ``deliverable`` / ``report_path``，于是执行器加载这一行
    时看到的只是「一个没有改动范围的任务」——它按代码任务处理，于是问了
    一个错的问题（「仓库变了吗」），把一份写好的报告判成
    ``empty_diff_no_changes``，一轮又一轮地重试：诊断任务没有
    ``files_to_modify``，它的产出按定义就不是 diff。

    判定用**两个**信号，缺一不可：

    1. description 里点名了一份根因报告（``plans/.../reports/root-cause-*.md``）；
    2. 这一行没有声明任何要改动的文件 —— 诊断任务的产出是判断，不是改动
       （这是 :mod:`root_cause_task` 的契约）。

    第二个信号是必须的：实现任务的 description 里**也**写着同一份报告的
    路径（"按 <report> 的根因结论修复 ..."），只按路径判定会把「修 bug 的
    代码任务」变成「欠一份报告的任务」，那个修复就永远做不出来。

    Args:
        row: 持久化后的任务行。
        plan_dir: 该 plan 的目录 —— 报告落在它的 ``reports/`` 下，与
            ``verification.orchestrator._root_cause_report_file`` 读的
            是同一个位置。

    Returns:
        ``{"deliverable": "report", "report_path": "<绝对路径>"}``；
        这一行不是根因报告任务时返回 ``None``。
    """
    if str(row.get("deliverable") or "").strip():
        # 行上已经带着交付物声明 —— 那是权威的，不许猜。
        return None
    declared = row.get("files_to_modify") or []
    concrete = [
        f
        for f in declared
        if isinstance(f, str) and f and not f.startswith("__")
    ]
    if concrete:
        return None
    match = _ROOT_CAUSE_REPORT_PATH_RE.search(
        str(row.get("description") or "")
    )
    if not match:
        return None
    report_path = Path(plan_dir).absolute() / "reports" / match.group(1)
    return {
        "deliverable": "report",
        "report_path": str(report_path),
    }


def _allocate_task_id(
    round_no: int,
    existing_task_ids: Optional[Iterable[str]] = None,
    seq_base: Optional[int] = None,
    task_id: Optional[str] = None,
) -> str:
    """从既有 repair 序列里分配一个任务 id。

    显式 ``task_id`` 优先；否则用 ``seq_base``（调用方已经算好的号），
    再否则用 :func:`repair_generator.next_repair_seq_base` 在既有 id 里
    找下一个空号。产出的形状是 ``repair-{seq}`` —— 既有解析器
    :func:`repair_generator.parse_repair_task_id` 读得出来，所以它占的号
    会被后来的分配看见。

    编号是 **plan 级**的，不是轮次级的（2026-11-02）：轮次计数会重置，
    重置后的轮次会重新发出同一批 id，而 ``plan_tasks`` 的 upsert 保留
    旧行的运行时状态，于是新任务一出生就是 ``completed``，整轮空转。
    所以 id 里不再带轮次，号在整个 plan 内单调递增。

    显式给的 ``task_id`` 也要读得出来才收：读不出来的 id 属于另一套编号
    空间，会绕开序列分配器 —— 那正是这个模块要避免的事。两种形状都收：
    新的 ``repair-{seq}`` 与旧的 ``repair-r{round}-{seq}``（旧 id 是历史，
    不迁移、不重写）。
    """
    round_number = int(round_no)
    if round_number < 1:
        raise ValueError(f"round_no must be >= 1, got {round_no}")

    explicit = str(task_id or "").strip()
    if explicit:
        if parse_repair_task_id(explicit) is None:
            raise ValueError(
                f"task_id 必须落在既有 repair 序列里"
                f"（repair-<seq> 或 repair-r<round>-<seq>），"
                f"收到 {explicit}"
            )
        return explicit

    if seq_base is not None:
        seq = int(seq_base)
        if seq < 1:
            raise ValueError(f"seq_base must be >= 1, got {seq_base}")
    else:
        seq = next_repair_seq_base(list(existing_task_ids or ()), round_number)
    return format_repair_task_id(seq)


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
    report_path: Optional[str] = None,
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
        report_path: 报告的落盘路径。缺省是仓库相对路径
            (``plans/<plan_id>/reports/root-cause-<vp_id>.md``)；调用方
            知道任务实际跑在哪个目录时，给**绝对路径** —— 围栏放行的那个
            路径必须与链读取的那个路径是同一个文件，而相对路径会被执行器
            按它自己的工作目录解析。

    Returns:
        任务 dict。``kind`` 是 :data:`ROOT_CAUSE_KIND`，``read_only`` 为
        ``True``，``report_path`` 指向根因报告，**没有** ``files_to_modify``。
    """
    report_path = str(
        report_path or root_cause_report_path(plan_id, vp_id)
    ).strip()
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
            # 2026-11-02 — 报告契约搭**描述**的便车。
            #
            # 执行器读的是 ``SubTask.deliverable`` / ``SubTask.report_path``，
            # 而这两个字段在链上的这一段要经过 ``plan_tasks``：那张表没有
            # 这两列，而验证模式的范围门禁（``tests/static_gates/
            # test_verification_mode_scope_gates.py``）把 schema 的字节
            # 冻死了，加列这条路走不通。描述是唯一原样存、逐字读回的
            # 自由文本列 —— 契约就走它。
            #
            # 标记放在**改动范围模板之前**：读者（``_root_cause_scope_from_text``）
            # 切出来的「## 改动范围」那一节因此逐字不变。
            f"\n{report_deliverable_marker(report_path)}\n"
            # 模板自带标题与代码块，逐行拼接反而难读。
            f"{_SCOPE_CONTRACT_TEMPLATE}"
        ),
        # 只读：诊断任务的产出是报告，不是改动。
        "read_only": True,
        "report_path": report_path,
        # 2026-11-02（main）— 这个 ``deliverable`` 是**任务 dict** 的字段，
        # 不是 ``SubTask`` 的那个：``plan_tasks`` 没有这一列，链在写库
        # 时会把它连同 ``kind`` / ``read_only`` 一起丢掉。执行器真正读到
        # 的那一对（``deliverable="report"`` + ``report_path``）由上面
        # description 里的标记带过去，由
        # ``orphan_rules.parse_report_deliverable`` 读回来。
        #
        # 2026-11-03（本分支）— 但**这一行**本身仍要自洽：
        # ``deliverable`` 是交付物的**类型**（"code" / "report"），
        # 执行器据此走报告分支（``is_report_task`` /
        # ``resolve_report_path`` / 写守卫的 ``PDT_REPORT_PATH`` 豁免）；
        # 报告必须包含的五件事是交付物的**内容**，放在另一个键上 ——
        # 塞进 ``deliverable`` 会让这一行连 ``SubTask`` 都构造不出来
        # （``SubTask.deliverable`` 是 ``Literal["code", "report"]``）。
        "deliverable": "report",
        # 交付物的**内容**：这五件事是报告里必须有的章节。
        "report_deliverables": list(ROOT_CAUSE_DELIVERABLE),
        # main 侧新增：章节名单独放在 ``required_sections``，那里的语义
        # 本来就是「必须包含哪几节」。
        "required_sections": list(ROOT_CAUSE_DELIVERABLE),
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
    report_path: Optional[str] = None,
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
        report_path: 根因报告的落盘路径。缺省是仓库相对路径；调用方知道
            任务实际跑在哪个目录时，给**绝对路径** —— 实现任务要读的就是
            链读过的那一份，相对路径会被执行器按它自己的工作目录解析。

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

    report_path = str(
        report_path or root_cause_report_path(plan, vp)
    ).strip()
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
