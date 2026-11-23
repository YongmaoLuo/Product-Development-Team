"""失败计划的分析来源加载与 problem / suggestion 提炼 —— failure_review
的**确定性**输入。

R1 要把「计划终态失败时已经落盘的分析」提炼成一条简明消息（问题 +
建议修复）。提炼本身不新增 LLM 调用，所以它需要的每一个输入都必须是
**读文件就能拿到**的事实。这个模块做那三件事：把四份产物读进来装进
一个 dataclass，再从这些事实里确定性地提炼出 problem 一句与
suggestion 一句。

四份来源
--------
* ``verification_report.json`` —— 验证结论（summary / defects / verdict）；
* ``verification_plan.json`` —— 本轮验证计划（VP 列表）；
* ``verification_repair_tasks.json`` —— 各轮修复任务快照；
* ``plan_state.json`` —— 计划相位状态（这里只取 ``verification.round``）。

读取失败的语义
--------------
失败分析是**旁路**产物：主流程已经到终态了，这里读不到东西只应该让
消息少一段，不应该把终态写入变成一次崩溃。所以：

* 文件缺失 → 对应字段 ``None``；
* JSON 损坏（截断、非法编码）→ 对应字段 ``None``；
* 文件存在但不是一个 JSON **对象** → 同样 ``None``（下游按 dict 用它，
  塞一个 list 进去只会把错误推迟到更远的地方）。

``failed_tasks`` 恒为列表，``round`` 恒为整数 —— 缺失时是 ``[]`` 和
``0``，不是 ``None``。

轮次从哪来
----------
``round`` 用于「同一 plan 同一轮只留一条决定」的去重，所以它必须能在
文件里被确定地找到。取值顺序是固定的：

1. 报告自己的 ``_metadata.round``；
2. 计划状态的 ``verification.round``；
3. 都没有 → ``0``。

导入边界
--------
本模块**不在顶层** import ``server`` / ``routes.*`` /
``verification_loop``：它们都会把这条旁路链路拉回主流程，形成循环依赖。
除标准库外，本模块只 import 三处：

* ``plan_state`` 的一个常量 —— 计划相位快照的文件名在仓库里只有那一个
  定义（见 ``phase_migration`` 里的同一处约定），而 ``plan_state`` 本身
  不 import 上面那三个模块；
* ``config_paths`` 的两个解析器 —— 计划根目录与状态库路径各自在仓库里
  只有**一个**解析器（见 ``config_paths`` 的模块说明）；
* ``hitl`` 的决策服务 / 模型 / 存储，加上 ``utils.atomic_io`` 的原子写
  —— 决策只有一条落库路径（``hitl.service.create_decision``），旁路
  记录只有一种写法（``atomic_write_json``）。这几个模块都不 import
  上面那三个主流程模块。

problem 提炼
-----------
:func:`extract_problem` 按固定的优先级取一句「出了什么问题」：

1. 报告 ``summary`` 的**首段**（段落之间以空行分隔 —— 真实报告把失败
   细节写在第二块里，首段才是结论）；
2. 没有报告（或 summary 是纯空白）时，用 stop_reason 拼上**最多 2 条**
   失败 VP 标题；
3. 再没有，用失败任务的标题与 failure_reason；
4. 全都没有，给固定兜底句。

失败 VP 的**判定**来自报告的 ``verification_results``（``status !=
"PASSED"`` 的 id 集合），**顺序**来自计划的 ``verification_points`` ——
操作者熟悉的顺序是计划的顺序。报告压根没带结果清单时（没有报告这一
份产物），「哪些点失败了」是未知的，于是退化成点名计划里的前 2 条。

提炼结果不得超过 :data:`MAX_PROBLEM_CHARS` 字 —— 过长就截断并以省略号
收尾，因为这句话要塞进等待行和推送通知。

suggestion 提炼
---------------
:func:`extract_suggestion` 按 R1 固定的三级优先级给一句「接下来做什么」：

1. **未完成的修复任务**（最多 3 条）—— ``verification_repair_tasks.json``
   里带 ``status`` 的条目，凡 ``completed`` / ``done`` / ``skipped`` 的
   都算做完了，不进 suggestion；
2. 报告的 ``human_decisions`` / ``unresolved``（前 3 条）；
3. 固定兜底句 :data:`FALLBACK_SUGGESTION`。

每条目的文本取 ``title``，没有就取 ``description``；字典条目两个字段
都没有时用 ``json.dumps(..., ensure_ascii=False)`` 整条序列化 —— 宁可给
一句生硬的原文，也不丢掉一条修复任务。

提炼结果不得超过 :data:`MAX_SUGGESTION_CHARS` 字（省略号算在内）。

失败通告
--------
:func:`announce_failure` 把提炼出来的 problem / suggestion 变成一条
**决策** —— 计划终态失败时，人要看到「出了什么问题」和「接下来做什么」。

三条硬规则：

1. **绝不外抛。** 整个函数体包在 ``try`` 里：失败通告是**旁路**，
   主流程已经到终态了，通告失败绝不能把终态写入变成一次崩溃。出错时
   记一条 ``warning``（带 ``exc_info``），返回 ``None``。
2. **同 plan 同轮只建一条。** 幂等键是 ``(plan_id, round)``：已经有一
   条 ``source='failure_review'`` 且轮次相同的决策时，直接复用它的
   ``decision_id``，不再建第二条 —— 失败通告会被重复触发（重试、收尾
   的第二次扫描），重复触发只能复用同一条决策。
3. **旁路记录。** 通告同时把结论写进
   ``<plan_dir>/failure_review.json``（原子写），供只读端点与等待行
   读取 —— 它们读的是文件，不查库。写失败只记日志：决策已经建好了，
   记录写不进去不该把 ``decision_id`` 也吞掉。
4. **只有终态失败才通告。** 「这一次是不是该通告的终态失败」由
   :func:`is_terminal_failure` 一处判定，执行侧与验证侧共用同一条规则：
   * **理由**说这一轮在**等**（``hitl_decision_pending`` 等人类决策、
     ``waiting_for_quota`` 等配额），那不是失败 —— 等的时候问「出了什么
     问题」，问的是没有答案的问题。调用方给的理由与 executor 自己写进
     ``tasks.json`` 的理由**都**要看：调用方从外面看这一轮，executor 从
     里面看。
   * **相位**还没到终态（``post_processing`` / ``executing``），那计划
     还没停下来 —— 通告说的是一个它马上就要离开的状态。
   * 相位**读不到**时不否决：读不到意味着这个计划还没进状态机（老计划、
     没有 routing 行），这种时候宁可靠通告那一侧 —— 没人要的复盘只是
     面板上多一行，没人被告知的失败是一个悄悄死掉的计划。

用法
----
::

    from failure_review import announce_failure, extract_problem, load_sources

    sources = load_sources(plan_dir, stop_reason="verification_failed")
    print(sources.report, sources.round)
    print(extract_problem(sources))
    print(announce_failure(plan_id, plan_dir=plan_dir))
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# 计划相位快照的文件名。**不**在本模块里再写一遍字面量：仓库里已经有一处
# 规范定义（``plan_state.PLAN_STATE_FILENAME``），而
# ``backend/tests/static_gates/test_no_json_state_filename_in_backend_source.py``
# 把「生产代码里出现该文件名」当成违规 —— 第二份字面量既会污染那个闸门的
# 噪音，也会让「文件名只有一个定义」这件事失去意义。
#
# 终态相位的集合也取自同一处 —— 「什么是终态」只有一个定义。
from plan_state import PLAN_STATE_FILENAME, TERMINAL_PHASES

# 计划根目录与状态库路径 —— 各自在仓库里只有一个解析器。
from config_paths import resolve_plans_dir, resolve_state_db_path
# 决策的模型 / 落库路径 / 存储。
from hitl.models import DecisionRequest
from hitl.service import create_decision
from hitl.store import HitlStore
# 旁路记录的原子写。
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

__all__ = [
    "COMPLETED_REPAIR_STATUSES",
    "FAILURE_REVIEW_FILENAME",
    "FAILURE_REVIEW_OPTION",
    "FAILURE_REVIEW_SOURCE",
    "FALLBACK_PROBLEM",
    "FALLBACK_SUGGESTION",
    "FailureSources",
    "MAX_FAILED_VP_TITLES",
    "MAX_PROBLEM_CHARS",
    "MAX_SUGGESTION_CHARS",
    "MAX_SUGGESTION_ITEMS",
    "PARKED_STOP_REASONS",
    "announce_failure",
    "build_question",
    "extract_problem",
    "extract_suggestion",
    "is_terminal_failure",
    "load_sources",
    "read_review",
]

#: 验证结论报告 —— summary / defects / verdict 都在这里。
REPORT_FILENAME = "verification_report.json"
#: 本轮验证计划（VP 列表）。
VERIFICATION_PLAN_FILENAME = "verification_plan.json"
#: 各轮修复任务快照。
REPAIR_TASKS_FILENAME = "verification_repair_tasks.json"

#: problem 的长度上限（**含**结尾的省略号）。R1 规定 problem ≤300 字。
MAX_PROBLEM_CHARS = 300
#: stop_reason 一级最多带几条失败 VP 标题（R1：最多 2 条）。
MAX_FAILED_VP_TITLES = 2
#: 所有来源都空着时的兜底句。
FALLBACK_PROBLEM = "计划进入终态失败，未见验证报告"

#: suggestion 的长度上限（**含**结尾的省略号）。R1 规定 suggestion ≤200 字。
MAX_SUGGESTION_CHARS = 200
#: suggestion 一级最多点名几条修复任务 / 待确认项（R1：最多 3 条）。
MAX_SUGGESTION_ITEMS = 3
#: 所有来源都空着时的 suggestion 兜底句。
FALLBACK_SUGGESTION = "建议重跑验证或人工排查（详见 verification_report.json）"

#: 修复任务的「已完成」状态值。带这些状态的条目不再需要建议，R1 规定
#: suggestion 只点名**未完成**的修复任务。
COMPLETED_REPAIR_STATUSES = frozenset({"completed", "done", "skipped"})

#: 停止理由里那些说的是「这一轮在**等**」而不是「这一轮失败了」的。
#:
#: ``hitl_decision_pending`` —— 这一轮停下来等人回答一个决策；等的时候
#: 问「出了什么问题」，问的是一个没有答案的问题。
#: ``waiting_for_quota`` —— 这一轮停下来等配额；那是排队，不是失败。
#:
#: 这个集合是 :func:`is_terminal_failure` 的判据之一，**不**在这里判断
#: 相位：相位与理由合起来才是「该不该通告」的那一条规则。
PARKED_STOP_REASONS = frozenset({
    "hitl_decision_pending",
    "waiting_for_quota",
})

#: 失败通告的**旁路记录** —— 写在 ``<plan_dir>/`` 下，供只读端点与等待行
#: 读取。它是计划目录里的运行时产物，不是仓库里的文件。
FAILURE_REVIEW_FILENAME = "failure_review.json"

#: 失败通告决策的 ``source``。决策的 schema 里没有 ``kind`` 字段，所以
#: 「这是一条失败通告」由 ``source`` 承载（问题文本里的前缀是给人看的
#: 同一件事）。
FAILURE_REVIEW_SOURCE = "failure_review"

#: 失败通告问题唯一提供的答案：确认「我看到了」。
FAILURE_REVIEW_OPTION = "ack"

#: 旁路记录里**必须**有的字段。少一个就说明这份记录不是本模块写的，
#: 读它的人（只读端点、等待行）宁可什么也不读，也不要读一半。
_REVIEW_REQUIRED_KEYS = ("decision_id", "problem", "suggestion", "created_at")

#: 段落之间的分隔 —— 一行空行。真实报告的 summary 把失败细节写在第二块
#: （``\n\n`` 之后），所以「首段」要按空行切，而不是按换行切。
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


@dataclass
class FailureSources:
    """失败提炼的输入事实。

    ``report`` / ``verification_plan`` / ``repair_tasks`` 是解析出来的
    JSON 对象，或者 ``None``（没有那份文件，或者那份文件坏了）。
    ``stop_reason`` 与 ``failed_tasks`` 由调用方给 —— 提炼不负责推导
    「为什么停了」。``round`` 是去重用的轮次号，见 :func:`load_sources`。
    """

    report: Optional[Dict[str, Any]] = None
    verification_plan: Optional[Dict[str, Any]] = None
    repair_tasks: Optional[Dict[str, Any]] = None
    stop_reason: Optional[str] = None
    failed_tasks: List[Dict[str, Any]] = field(default_factory=list)
    round: int = 0


def _read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    """Read one JSON **object**, or ``None``.

    ``None`` means: the file is not there, it cannot be read, it does not
    parse, or it parses into something that is not an object. None of
    those is worth an exception here — the caller wants "what is known",
    not "why it is unknown".
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _as_round(value: Any) -> Optional[int]:
    """A round number, or ``None`` when the value is not one.

    ``bool`` is an ``int`` in Python, and ``True`` is not round 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _round_from_report(report: Optional[Dict[str, Any]]) -> Optional[int]:
    """The round the report itself carries (``_metadata.round``)."""
    if not isinstance(report, dict):
        return None
    metadata = report.get("_metadata")
    if not isinstance(metadata, dict):
        return None
    return _as_round(metadata.get("round"))


def _round_from_plan_state(plan_state: Optional[Dict[str, Any]]) -> Optional[int]:
    """The round the plan state carries (``verification.round``)."""
    if not isinstance(plan_state, dict):
        return None
    verification = plan_state.get("verification")
    if not isinstance(verification, dict):
        return None
    return _as_round(verification.get("round"))


def load_sources(
    plan_dir: Path,
    *,
    stop_reason: Optional[str] = None,
    failed_tasks: Optional[List[Dict[str, Any]]] = None,
) -> FailureSources:
    """Load the analysis artefacts a failed plan left behind.

    :param plan_dir: The plan's directory (the one holding
        ``verification_report.json`` and friends). A directory that does
        not exist is not an error — every field comes back empty.
    :param stop_reason: Why the plan stopped, as the caller knows it.
        Carried through untouched; this module never infers it.
    :param failed_tasks: The plan tasks that failed, as the caller knows
        them. Carried through untouched (copied, so a later mutation of
        the caller's list cannot rewrite what was loaded).
    :return: The four artefacts plus the carried-through facts and the
        round number.

    The round number is taken from the report's ``_metadata.round``; when
    the report does not carry one, from the plan state's
    ``verification.round``; when neither does, ``0``.
    """
    directory = Path(plan_dir)

    report = _read_json_object(directory / REPORT_FILENAME)
    verification_plan = _read_json_object(directory / VERIFICATION_PLAN_FILENAME)
    repair_tasks = _read_json_object(directory / REPAIR_TASKS_FILENAME)
    plan_state = _read_json_object(directory / PLAN_STATE_FILENAME)

    round_number = _round_from_report(report)
    if round_number is None:
        round_number = _round_from_plan_state(plan_state)
    if round_number is None:
        round_number = 0

    return FailureSources(
        report=report,
        verification_plan=verification_plan,
        repair_tasks=repair_tasks,
        stop_reason=stop_reason,
        failed_tasks=list(failed_tasks) if failed_tasks else [],
        round=round_number,
    )


def _first_paragraph(text: str) -> str:
    """The first non-empty paragraph of ``text`` (``""`` when there is none).

    Paragraphs are separated by a blank line — the convention the
    report's ``summary`` is written in.  Whitespace-only text has no
    paragraph at all, which is exactly how "the summary is empty" is
    detected.
    """
    for chunk in _PARAGRAPH_SPLIT.split(text):
        chunk = chunk.strip()
        if chunk:
            return chunk
    return ""


def _summary_problem(sources: FailureSources) -> str:
    """Level 1 — the report's ``summary`` first paragraph."""
    report = sources.report
    if not isinstance(report, dict):
        return ""
    summary = report.get("summary")
    if not isinstance(summary, str):
        return ""
    return _first_paragraph(summary)


def _failed_vp_ids(report: Optional[Dict[str, Any]]) -> Optional[Set[str]]:
    """Ids the report marks as *not* passed; ``None`` when it says nothing.

    ``None`` means "the report carries no result list at all" — that is
    a different fact from "no VP failed" (an empty set).  The
    difference is why the stop_reason level falls back to naming the
    first verification points: with no report there is nothing to
    narrow them down with.
    """
    if not isinstance(report, dict):
        return None
    results = report.get("verification_results")
    if not isinstance(results, list):
        return None
    failed: Set[str] = set()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        vp_id = entry.get("id")
        if not isinstance(vp_id, str) or not vp_id:
            continue
        if entry.get("status") != "PASSED":
            failed.add(vp_id)
    return failed


def _failed_vp_titles(sources: FailureSources) -> List[str]:
    """Titles of the failing verification points, at most two.

    The **membership** comes from the report (``status != "PASSED"``);
    the **order** comes from the plan's ``verification_points`` — the
    order the operator knows the points in.
    """
    plan = sources.verification_plan
    if not isinstance(plan, dict):
        return []
    points = plan.get("verification_points")
    if not isinstance(points, list):
        return []
    failed = _failed_vp_ids(sources.report)
    titles: List[str] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        if failed is not None and point.get("id") not in failed:
            continue
        title = point.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        titles.append(title.strip())
        if len(titles) >= MAX_FAILED_VP_TITLES:
            break
    return titles


def _stop_reason_problem(sources: FailureSources) -> str:
    """Level 2 — the stop reason plus up to two failing VP titles."""
    parts: List[str] = []
    stop_reason = sources.stop_reason
    if isinstance(stop_reason, str) and stop_reason.strip():
        parts.append(stop_reason.strip())
    parts.extend(_failed_vp_titles(sources))
    return "；".join(parts)


def _failed_tasks_problem(sources: FailureSources) -> str:
    """Level 3 — the failed tasks' titles and their failure reasons."""
    tasks = sources.failed_tasks
    if not isinstance(tasks, list):
        return ""
    parts: List[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        reason = task.get("failure_reason")
        if isinstance(reason, str) and reason.strip():
            parts.append(f"{title.strip()}（{reason.strip()}）")
        else:
            parts.append(title.strip())
    return "；".join(parts)


def _truncate(text: str) -> str:
    """Cap ``text`` at :data:`MAX_PROBLEM_CHARS`, ellipsis included."""
    if len(text) <= MAX_PROBLEM_CHARS:
        return text
    return text[: MAX_PROBLEM_CHARS - 1] + "…"


def extract_problem(sources: FailureSources) -> str:
    """The one-line "what went wrong" message for a failed plan.

    Three levels, in the order R1 fixes them:

    1. the report's ``summary`` first paragraph;
    2. the stop reason plus at most two failing verification point
       titles;
    3. the failed tasks' titles and their failure reasons.

    When no level has anything to say, the fixed sentence
    :data:`FALLBACK_PROBLEM` is returned.  The result never exceeds
    :data:`MAX_PROBLEM_CHARS` characters — a long summary is cut and
    marked with an ellipsis, because the message has to fit into a
    waiting row and a push notification.
    """
    for level in (
        _summary_problem(sources),
        _stop_reason_problem(sources),
        _failed_tasks_problem(sources),
    ):
        if level:
            return _truncate(level)
    return FALLBACK_PROBLEM


def _entry_text(entry: Any) -> str:
    """The human-readable text of one entry (``""`` when there is none).

    A string entry is its own text.  A dict entry speaks through
    ``title``, or ``description`` when it has no title.  A dict that
    carries neither is dumped whole with ``json.dumps`` — a raw JSON
    blob is a poor sentence, but it still names the repair task, and
    dropping the entry would drop the only thing the operator has to
    act on.
    """
    if isinstance(entry, str):
        return entry.strip()
    if entry is None:
        return ""
    if isinstance(entry, dict):
        for key in ("title", "description"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    try:
        return json.dumps(entry, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _is_completed_repair_task(entry: Any) -> bool:
    """Whether a repair task entry says it is already done.

    Only dict entries can carry a ``status``.  The comparison ignores
    case and surrounding whitespace: the status is written by a
    generator, not by a schema, and ``Completed`` means the same thing
    as ``completed``.
    """
    if not isinstance(entry, dict):
        return False
    status = entry.get("status")
    if not isinstance(status, str):
        return False
    return status.strip().lower() in COMPLETED_REPAIR_STATUSES


def _repair_task_titles(sources: FailureSources) -> List[str]:
    """Titles of the repair tasks that are still outstanding, at most three.

    The tasks live in ``verification_repair_tasks.json`` under the
    ``tasks`` key.  A snapshot without that key (or with an empty list)
    simply has nothing to suggest — the caller falls through to the
    next level.
    """
    repair_tasks = sources.repair_tasks
    if not isinstance(repair_tasks, dict):
        return []
    tasks = repair_tasks.get("tasks")
    if not isinstance(tasks, list):
        return []
    titles: List[str] = []
    for task in tasks:
        if _is_completed_repair_task(task):
            continue
        text = _entry_text(task)
        if not text:
            continue
        titles.append(text)
        if len(titles) >= MAX_SUGGESTION_ITEMS:
            break
    return titles


def _report_decision_items(sources: FailureSources) -> List[str]:
    """The report's human decisions and unresolved items, at most three.

    ``human_decisions`` comes first: those are the questions an
    operator was already asked to answer, so they are the ones worth
    repeating.  ``unresolved`` follows — what the verification could
    not settle on its own.
    """
    report = sources.report
    if not isinstance(report, dict):
        return []
    items: List[str] = []
    for key in ("human_decisions", "unresolved"):
        entries = report.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            text = _entry_text(entry)
            if text:
                items.append(text)
    return items[:MAX_SUGGESTION_ITEMS]


def _truncate_suggestion(text: str) -> str:
    """Cap ``text`` at :data:`MAX_SUGGESTION_CHARS`, ellipsis included."""
    if len(text) <= MAX_SUGGESTION_CHARS:
        return text
    return text[: MAX_SUGGESTION_CHARS - 1] + "…"


def extract_suggestion(sources: FailureSources) -> str:
    """The one-line "what to do about it" message for a failed plan.

    Three levels, in the order R1 fixes them:

    1. the titles of the repair tasks that are **not** done yet (at
       most three) — ``建议修复：…``;
    2. the report's ``human_decisions`` / ``unresolved`` (at most three)
       — ``待人工确认：…``;
    3. the fixed sentence :data:`FALLBACK_SUGGESTION`.

    The result never exceeds :data:`MAX_SUGGESTION_CHARS` characters —
    a long title is cut and marked with an ellipsis, because the
    message has to fit into a waiting row and a push notification.
    """
    titles = _repair_task_titles(sources)
    if titles:
        return _truncate_suggestion("建议修复：" + "；".join(titles))
    items = _report_decision_items(sources)
    if items:
        return _truncate_suggestion("待人工确认：" + "；".join(items))
    return FALLBACK_SUGGESTION


# ---------------------------------------------------------------------------
# The announcement
# ---------------------------------------------------------------------------


def build_question(problem: str, suggestion: str) -> str:
    """The question a human is asked about a failed plan.

    The text is the problem, then the suggestion, on two lines::

        [failure_review] 验证失败：登录接口 500
        建议：建议修复：修复登录接口

    The ``[failure_review]`` prefix is how the kind is marked **in the
    text**: the decision schema carries no ``kind`` field, so the
    prefix is what a reader of the question sees.  The machine-readable
    counterpart is the decision's ``source`` field, which says the same
    thing.
    """
    return "[failure_review] " + problem + "\n建议：" + suggestion


def _plan_directory(plan_id: str, plan_dir: Optional[Path]) -> Path:
    """The directory the failure is announced in.

    Given explicitly when the caller knows it; otherwise the plan's own
    directory under the plans root — the same root every other
    plan-artefact writer resolves through
    :func:`config_paths.resolve_plans_dir`.
    """
    if plan_dir is not None:
        return Path(plan_dir)
    return resolve_plans_dir() / plan_id


def _announcement_round(
    round_number: Optional[int], sources: FailureSources
) -> int:
    """The round the announcement belongs to.

    The caller's round wins when it is a usable one (a non-negative
    integer — a round is a counter); otherwise the round the loaded
    sources carry, which is ``0`` when nothing said which round this
    was.
    """
    if isinstance(round_number, bool) or not isinstance(round_number, int):
        return sources.round
    if round_number < 0:
        return sources.round
    return round_number


def _decision_round(decision: Any) -> int:
    """The round a stored decision belongs to.

    A decision that carries no round belongs to round ``0`` — the same
    convention :func:`load_sources` uses for "nothing said which round
    this was".  Without it a decision written without a round would
    never match the announcement that looks for it.
    """
    value = getattr(decision, "round", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _existing_review_decision(
    store: HitlStore, plan_id: str, round_number: int
) -> Optional[Any]:
    """The plan's failure-review decision for this round, or ``None``.

    The idempotence key is ``(plan_id, round)``: a decision asked with
    the same ``source`` **and** the same round is the one this
    announcement would have created, so it is reused instead of asked
    again.
    """
    for decision in store.list_by_plan(plan_id):
        if getattr(decision, "source", None) != FAILURE_REVIEW_SOURCE:
            continue
        if _decision_round(decision) != round_number:
            continue
        return decision
    return None


def _write_review(directory: Path, payload: Dict[str, Any]) -> None:
    """Write the sidecar, atomically, and never raise.

    A sidecar that cannot be written is a lost convenience, not a lost
    decision: the decision is already in the store, and the caller
    still gets its ``decision_id`` back.
    """
    try:
        atomic_write_json(
            directory / FAILURE_REVIEW_FILENAME, payload, logger=logger
        )
    except Exception:  # noqa: BLE001 - the sidecar is a convenience.
        logger.warning(
            "failure_review: writing %s failed", FAILURE_REVIEW_FILENAME,
            exc_info=True,
        )


def _is_parked_reason(reason: Optional[str]) -> bool:
    """Whether the reason says the run is *waiting*, not *failing*."""
    if not reason:
        return False
    return str(reason).strip() in PARKED_STOP_REASONS


def _executor_stop_reason(directory: Path) -> Optional[str]:
    """The reason the executor wrote down, as the executor saw it.

    The caller of :func:`announce_failure` sees the run from the outside
    ("the executor exited with unfinished tasks"); the executor saw the
    reason from the inside ("waiting for a human decision").  The two
    are different facts about the same run, and only the inner one says
    whether the run actually failed — so both are read.

    ``None`` means "the executor wrote nothing down" — no file, an
    unreadable file, a broken file, or a file that is not an object.
    """
    from plan_artifacts import TASKS_FILENAME

    data = _read_json_object(Path(directory) / TASKS_FILENAME)
    if not data:
        return None
    reason = data.get("stop_reason")
    if not reason:
        return None
    return str(reason)


def _plan_phase(plan_id: str) -> Optional[str]:
    """The plan's current phase, read from the one authority for it.

    ``plan_routing.current_phase`` is the authority; the legacy
    ``plan_state.json`` is a read-only cache of it, so it is not read
    here.  ``None`` means **unknown** — the row is not there, or it
    cannot be read — and unknown is not the same as "not terminal".

    The state-machine imports are inside the function on purpose: this
    module's top level imports nothing but ``plan_state`` /
    ``config_paths`` / ``hitl`` / ``utils``, and the import boundary is
    part of the module's contract (see the module docstring).
    """
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = open_db(resolve_state_db_path())
        try:
            migrate(conn)
            row = RoutingRepository(conn).current(plan_id)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the phase is a fact, not a verdict.
        logger.warning(
            "failure_review: reading the phase of plan %s failed", plan_id,
            exc_info=True,
        )
        return None
    if not row:
        return None
    phase = row.get("current_phase")
    if not phase:
        return None
    return str(phase)


def is_terminal_failure(
    plan_id: str,
    *,
    plan_dir: Optional[Path] = None,
    stop_reason: Optional[str] = None,
) -> bool:
    """Whether this failure is a terminal one worth announcing.

    **This is the one rule.**  The execution side (a run that ended
    ``failed``) and the verification side (a verification chain that
    ended ``failed``) ask the same question — "is this a terminal
    failure a human should look at?" — and they get the same answer,
    because they ask the same function.

    Two facts decide it.

    1. **The reason.**  A run that stopped because it is *waiting*
       (:data:`PARKED_STOP_REASONS`) has not failed.  Both the caller's
       reason and the executor's own record are consulted: the caller
       looks at the run from the outside, the executor saw the reason
       from the inside, and the inner reason is the one that says
       whether the work broke.
    2. **The phase.**  A failure is worth announcing when the plan has
       reached a terminal phase.  A plan that is still on its way
       (``post_processing``, ``executing``) is not there yet, and the
       announcement would describe a state the plan is about to leave.

    A phase that cannot be read is **not** a veto.  Unknown means the
    plan is not in the state machine yet (a legacy plan, a plan with
    no routing row), and the doubt resolves towards announcing: a
    review nobody needed is one row on a panel, a failure nobody was
    told about is a plan that died quietly.
    """
    directory = _plan_directory(plan_id, plan_dir)
    if _is_parked_reason(stop_reason):
        return False
    if _is_parked_reason(_executor_stop_reason(directory)):
        return False
    phase = _plan_phase(plan_id)
    if phase is not None and phase not in TERMINAL_PHASES:
        return False
    return True


def announce_failure(
    plan_id: str,
    *,
    plan_dir: Optional[Path] = None,
    stop_reason: Optional[str] = None,
    failed_tasks: Optional[List[Dict[str, Any]]] = None,
    round: Optional[int] = None,
) -> Optional[str]:
    """Announce a failed plan to a human, and report the decision.

    The plan has reached its terminal failure.  This asks the question
    "what went wrong, and what should happen next?" as a **decision** —
    a row a human can answer on the panel — and leaves a sidecar behind
    for the read-only consumers.

    Parameters
    ----------
    plan_id:
        The plan the failure belongs to.
    plan_dir:
        Where the plan's artefacts live.  Defaults to the plan's own
        directory under the plans root.
    stop_reason:
        Why the plan stopped, as the caller knows it.
    failed_tasks:
        The tasks that failed, as the caller knows them.
    round:
        The round this failure belongs to.  Defaults to the round the
        loaded artefacts carry.

    Returns
    -------
    The ``decision_id`` of the decision the failure was announced as, or
    ``None`` when the announcement itself failed.

    The function **never raises**.  The announcement is a side path: the
    plan has already failed, and a failure to report the failure must
    not become a second failure.  Every error is logged (with
    ``exc_info``) and reported as ``None``.

    Announcing the same plan twice in the same round is idempotent: the
    decision that is already there is reused, and no second row is
    created.

    **Only a terminal failure is announced.**  The question "is this a
    terminal failure worth announcing?" is asked once, by
    :func:`is_terminal_failure`, and both callers — the execution side
    and the verification side — get the same answer.  A run that is
    waiting for a human, and a plan that has not reached its terminal
    phase yet, produce no decision and no sidecar: there is no failure
    to review.
    """
    try:
        directory = _plan_directory(plan_id, plan_dir)
        if not is_terminal_failure(
            plan_id, plan_dir=directory, stop_reason=stop_reason,
        ):
            logger.info(
                "failure_review: plan %s is not a terminal failure — "
                "nothing is announced",
                plan_id,
            )
            return None
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A directory that cannot be created is not fatal here: the
            # decision does not need it, and the sidecar write below
            # reports the problem on its own.
            pass

        sources = load_sources(
            directory,
            stop_reason=stop_reason,
            failed_tasks=failed_tasks,
        )
        round_number = _announcement_round(round, sources)
        problem = extract_problem(sources)
        suggestion = extract_suggestion(sources)
        question = build_question(problem, suggestion)

        with HitlStore(resolve_state_db_path()) as store:
            decision = _existing_review_decision(store, plan_id, round_number)

        if decision is None:
            decision = create_decision(
                DecisionRequest(
                    plan_id=plan_id,
                    source=FAILURE_REVIEW_SOURCE,
                    # The question is about the plan, not about one
                    # task: a task id would put the plan's whole
                    # schedule on hold behind it.
                    task_id=None,
                    round=round_number,
                    question=question,
                    options=[FAILURE_REVIEW_OPTION],
                    default_option=FAILURE_REVIEW_OPTION,
                ),
                directory,
                # The question has to stay answerable: it is read on a
                # panel, and a plan with no outlet would otherwise
                # close it as unroutable before anybody could answer.
                settle_unrouted=False,
                # It also has to wait for a person rather than for the
                # clock.  The plan's configuration would otherwise make
                # it an ``auto`` decision with a deadline, and the expiry
                # sweep would settle it as ``defaulted`` half an hour
                # later — the system answering a question that asks a
                # human to confirm what to do about a failure.
                require_answer=True,
            )

        _write_review(
            directory,
            {
                "decision_id": decision.decision_id,
                "problem": problem,
                "suggestion": suggestion,
                "created_at": decision.created_at,
                "plan_id": plan_id,
                "round": round_number,
                "question": question,
            },
        )
        return decision.decision_id
    except Exception:  # noqa: BLE001 - the announcement must never raise.
        logger.warning(
            "failure_review: announcing the failure of plan %s failed", plan_id,
            exc_info=True,
        )
        return None


def read_review(plan_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the failure-review sidecar of a plan, or ``None``.

    The read-only consumers — the panel's read-only endpoint, the
    waiting row — read the sidecar rather than the store: they show
    what was announced, and they must not be able to change it.

    ``None`` means "there is nothing to show": no sidecar, a sidecar
    that does not parse, or a sidecar that is missing one of the fields
    :data:`_REVIEW_REQUIRED_KEYS`.  The function never raises — a
    read-only consumer must not fall over because a file is broken.
    """
    path = Path(plan_dir) / FAILURE_REVIEW_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if any(key not in data for key in _REVIEW_REQUIRED_KEYS):
        return None
    return data
