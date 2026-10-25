"""``verification_report.json`` → Markdown 的**唯一**派生视图。

背景
----
``verification_report.json`` 是验证结论的**唯一数据源**。Markdown 只是它
的派生视图 —— 同样的 JSON 必须渲染出同样的字节，反过来不成立：Markdown 可以
被手改，但那处改动不再是数据。

所以这个模块只有两条纯函数：

    render(report_json, plan_items, review_state, repair_rows, ...) -> str
    traceability_issues(report_json, plan_json, parsed) -> list[str]

它们**不读时钟、不读环境变量、不读文件系统** —— 输出只由入参决定。输入顺序
打乱（验证点、修复行）输出字节相同，因为所有表格行都按稳定键排序。

四条硬约束
----------
1. **确定性** —— 同一份入参两次渲染字节相同。
2. **顺序无关** —— 验证点按 VP id 排序、审核状态按键排序、修复行按任务 id
   排序；入参顺序打乱不改变输出的任何一个字节。
3. **注入免疫** —— 文本里的 ``|``、换行、HTML 标签一律转义（见
   :data:`_ESCAPES`）：表格不得被拆散，标签不得被解释。
4. **skip 语义** —— 被跳过的验证点显示「未验证」，**不显示**「通过 / 失败」。
   没验过就是没验过：把它记成「通过」会让整份报告在读者眼里变成全绿。

事实来源只有入参
----------------
报告里的每一句话都来自入参：最终结论来自 ``final_verdict``，验证点来自
``verification_points``，计划书条目标题来自 ``plan_items``，审核状态来自
``review_state``，修复行来自 ``repair_rows``。本模块**不补全、不推断、不
兜底**成「通过」—— 缺什么就显示成缺（``—`` / 「未验证」）。

回溯链
------
报告里的每个验证点都必须能回溯回去：

    报告 VP → 机器计划（``verification_plan.json``）→ 计划书条目

链断在哪一环，报告就不可信 —— 读者没法确认「这条结论对应的是哪条判据」。
:func:`traceability_issues` 就是这条链的检查器，它把断链的地方列成**问题
清单**（而不是抛异常）：报告本身还要给人看，缺一条回溯不该让整份报告渲染
不出来。

两个方向都会毁掉这条链：

  * **凭空多出来的 VP** —— 报告里出现机器计划里没有的 id；
  * **delta VP 伪装成已审核条目** —— 增量新增的验证点**不在**已审核的计划书
    里，它必须自报家门（``origin="delta"`` + ``added_reason``），否则一条没人
    审过的判据会以「已审核条目」的样子出现在报告里。

被跳过的验证点同样要能查证：报告里写了「跳过」，就必须出现在
``skipped_items[]`` 里 —— 否则「跳过」和「没跑过」在报告里长得一模一样。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

#: 表格单元格里没有内容时的占位符。
_MISSING_CELL = "—"

#: 裁决 → 报告里显示的中文标签。
#:
#: **skip 的标签是「未验证」而不是「跳过」**：跳过的验证点没有产生任何结论，
#: 写成「跳过」会让人以为「验过、决定不验」；「未验证」说的是同一件事里更
#: 要紧的一半 —— 没有结论。
_STATUS_LABELS: Dict[str, str] = {
    "passed": "通过",
    "pass": "通过",
    "failed": "失败",
    "fail": "失败",
    "error": "失败",
    "skipped": "未验证",
    "skip": "未验证",
    "not_run": "未验证",
    "notrun": "未验证",
    "unverified": "未验证",
    "partial": "部分通过",
    "partially_passed": "部分通过",
    "blocked": "阻塞",
}

#: 文本 → 安全 Markdown 的替换表。**顺序是契约的一部分**：
#:
#: 反斜杠必须第一个转义，否则后面加上的 ``\|`` 会被二次转义成 ``\\|``，
#: 表格就散了；``&`` 必须早于 ``<`` / ``>``，否则实体里的 ``&`` 会被再
#: 转义一次。
_ESCAPES: Tuple[Tuple[str, str], ...] = (
    ("\\", "\\\\"),
    ("|", "\\|"),
    ("`", "\\`"),
    ("*", "\\*"),
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ("\r\n", "\\n"),
    ("\r", "\\r"),
    ("\n", "\\n"),
)

#: 最终结论在报告 JSON 里的候选键（按优先级）。
_VERDICT_KEYS = ("final_verdict", "overall_status", "verdict", "status")

#: 结论本身是 dict 时，取哪个字段当结论文本。
_VERDICT_TEXT_KEYS = (
    "verdict",
    "status",
    "result",
    "state",
    "decision",
    "value",
    "text",
    "name",
)

#: 计划书条目的标题字段（``plan_items`` 的值是 dict 时）。
_PLAN_ITEM_TITLE_KEYS = ("title", "name", "summary", "description", "text", "content")

#: 验证点的说明文字字段（按优先级取第一个非空）。
_VP_DETAIL_KEYS = (
    "title",
    "detail",
    "details",
    "evidence",
    "notes",
    "note",
    "reason",
    "summary",
    "message",
    "description",
    "command",
    "test_command",
)

#: 审核状态的状态字段 / 备注字段。
_REVIEW_STATE_KEYS = ("status", "state", "verdict", "decision", "result")
_REVIEW_NOTE_KEYS = ("note", "reason", "comment", "detail", "details", "summary")

#: 修复行的 id / 验证点 / 说明字段。
_REPAIR_ID_KEYS = ("task_id", "id", "repair_id", "task")
_REPAIR_VP_KEYS = ("vp_id", "verification_point", "vp", "plan_item_id")
_REPAIR_DETAIL_KEYS = (
    "title",
    "description",
    "detail",
    "details",
    "note",
    "notes",
    "reason",
    "summary",
    "message",
)

#: 三张表的表头。
_VP_HEADER = ("VP", "计划书条目", "状态", "说明")
_REVIEW_HEADER = ("条目", "审核状态", "说明")
_REPAIR_HEADER = ("修复任务", "验证点", "状态", "说明")

#: 报告里列出「被跳过」的验证点 id 的字段。
#:
#: **必须有** —— 报告里写了「跳过」却没进这个清单，「跳过」和「没跑过」在
#: 报告里就长得一模一样，读者没法判断这条结论到底是被跳过了还是漏跑了。
SKIPPED_ITEMS_FIELD = "skipped_items"

#: 增量新增的验证点在报告里的来源标记值。
DELTA_ORIGIN = "delta"

#: 增量新增的验证点必须自带的依据字段（为什么会有这条 VP）。
DELTA_REASON_FIELD = "added_reason"

#: 判定为「跳过」的状态（小写比较）。
#:
#: 只收**明确的**跳过语义：``not_run`` / ``unverified`` 这类是「还没验过」，
#: 与「决定不验」不是一回事，把它们混进来会让「跳过」这个清单失去意义。
_SKIP_STATUSES = frozenset({"skipped", "skip"})

#: 机器计划里验证点列表的候选键（LLM schema / 执行器 schema 两套叫法）。
_PLAN_POINT_KEYS = ("verification_points", "vps")


def _escape(text: Any) -> str:
    """把一段文本变成表格 / 正文里安全的 Markdown。

    转义五件事（顺序见 :data:`_ESCAPES`）：

      1. 反斜杠 —— 先转，否则后加的 ``\\|`` 会被二次转义；
      2. 竖线 —— 表格分隔符，不转义表格就散架；
      3. 反引号 / 星号 —— 不转义会被解释成代码 / 强调；
      4. ``&`` / ``<`` / ``>`` —— HTML 实体，标签不得被解释；
      5. 换行 / 回车 —— 一行表格不能被换行拆成两行。

    :param text: 任意值（非字符串按空处理）。
    :return: 转义后的文本。
    """
    if not isinstance(text, str):
        return ""
    escaped = text
    for raw, replacement in _ESCAPES:
        escaped = escaped.replace(raw, replacement)
    return escaped


def _text(value: Any) -> str:
    """取一段纯文本：字符串去首尾空白，其它类型一律空。

    :param value: 任意值。
    :return: 文本（可能为空串）。
    """
    if not isinstance(value, str):
        return ""
    return value.strip()


def _first_text(value: Any, keys: Sequence[str]) -> str:
    """从一个值里取第一段非空文本。

    * 字符串 → 去掉首尾空白后的自身；
    * dict → 按 ``keys`` 的顺序取第一个非空字段（字段值再递归一次）；
    * 其它类型 → 空串（**不 str() 兜底** —— 拿一个 dict 的 repr 当标题只会
      把内部结构泄进报告）。

    :param value: 任意值。
    :param keys: dict 情形下按优先级尝试的字段名。
    :return: 文本（可能为空串）。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in keys:
            text = _first_text(value.get(key), keys)
            if text:
                return text
        return ""
    return ""


def _status_label(raw: Any, missing: str = "未验证") -> str:
    """把一条状态翻成报告里显示的标签。

    * 已知状态 → :data:`_STATUS_LABELS` 里的中文标签；
    * 未知状态 → 原文（调用方负责转义）—— 不猜、不兜底成「通过」；
    * 状态缺失 → ``missing``。

    :param raw: 报告里的 ``status`` 字段。
    :param missing: 状态缺失时显示什么。
    :return: 标签文本。
    """
    text = _text(raw)
    if not text:
        return missing
    return _STATUS_LABELS.get(text.lower(), text)


def _plan_item_cell(plan_items: Mapping[str, Any], plan_item_id: str) -> str:
    """计划书条目在表格里的展示文本。

    查得到标题 → ``标题（条目 id）``（留 id 是为了能反查回计划书）；查不到
    标题 → 条目 id 本身。

    :param plan_items: ``{条目 id: 标题或条目 dict}``。
    :param plan_item_id: 条目 id。
    :return: 展示文本（可能为空串）。
    """
    if not plan_item_id:
        return ""
    title = _first_text(plan_items.get(plan_item_id), _PLAN_ITEM_TITLE_KEYS)
    if not title:
        return plan_item_id
    if title == plan_item_id:
        return title
    return f"{title}（{plan_item_id}）"


def _vp_rows(report: Mapping[str, Any], plan_items: Mapping[str, Any]) -> List[List[str]]:
    """验证点表的数据行（已转义、按 VP id 排序）。

    排序键包含这一行渲染出来的全部内容，所以两个入参顺序不同的报告渲染
    出来的表格逐行相同。

    :param report: 报告 JSON。
    :param plan_items: 计划书条目表（由 :func:`render` 保证是 mapping）。
    :return: 数据行列表。
    """
    points = report.get("verification_points")
    if not isinstance(points, (list, tuple)):
        points = []

    prepared: List[Tuple[str, str, str, str, str]] = []
    for point in points:
        if not isinstance(point, Mapping):
            continue
        vp_id = _text(point.get("id")) or _text(point.get("vp_id"))
        plan_item_id = _text(point.get("plan_item_id")) or vp_id
        prepared.append(
            (
                vp_id,
                plan_item_id,
                _status_label(point.get("status")),
                _first_text(point, _VP_DETAIL_KEYS),
                _plan_item_cell(plan_items, plan_item_id),
            )
        )

    prepared.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    return [
        [
            _escape(row[0] or _MISSING_CELL),
            _escape(row[4] or row[1] or _MISSING_CELL),
            _escape(row[2]),
            _escape(row[3] or _MISSING_CELL),
        ]
        for row in prepared
    ]


def _review_rows(review_state: Mapping[str, Any]) -> List[List[str]]:
    """审核状态表的数据行（已转义、按条目键排序）。

    :param review_state: ``{条目 id: 审核状态}``，值可以是字符串或 dict
        （由 :func:`render` 保证是 mapping）。
    :return: 数据行列表。
    """
    rows: List[List[str]] = []
    for key in sorted(review_state.keys(), key=lambda item: str(item)):
        value = review_state[key]
        if isinstance(value, Mapping):
            state = _first_text(value, _REVIEW_STATE_KEYS)
            note = _first_text(value, _REVIEW_NOTE_KEYS)
        else:
            state = _text(value)
            note = ""
        rows.append(
            [
                _escape(str(key)),
                _escape(state or _MISSING_CELL),
                _escape(note or _MISSING_CELL),
            ]
        )
    return rows


def _repair_rows(repair_rows: Sequence[Mapping[str, Any]]) -> List[List[str]]:
    """修复任务表的数据行（已转义、按任务 id 排序）。

    :param repair_rows: 修复行列表（由 :func:`render` 保证是序列）。
    :return: 数据行列表。
    """
    prepared: List[Tuple[str, str, str, str]] = []
    for row in repair_rows:
        if not isinstance(row, Mapping):
            continue
        prepared.append(
            (
                _first_text(row, _REPAIR_ID_KEYS),
                _first_text(row, _REPAIR_VP_KEYS),
                _status_label(row.get("status"), missing=_MISSING_CELL),
                _first_text(row, _REPAIR_DETAIL_KEYS),
            )
        )

    prepared.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    return [[_escape(value or _MISSING_CELL) for value in row] for row in prepared]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """拼一张 Markdown 表格。

    :param header: 表头单元格（已转义）。
    :param rows: 数据行（已转义）。
    :return: 表格文本。
    """
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _final_verdict(report: Mapping[str, Any]) -> str:
    """报告的最终结论。

    按 :data:`_VERDICT_KEYS` 的优先级找；找不到就说「未提供」—— 不猜。

    :param report: 报告 JSON。
    :return: 结论文本。
    """
    for key in _VERDICT_KEYS:
        text = _first_text(report.get(key), _VERDICT_TEXT_KEYS)
        if text:
            return text
    return "未提供"


# ---------------------------------------------------------------------------
# 回溯链检查
# ---------------------------------------------------------------------------


def _entry_id(entry: Any) -> str:
    """取一个条目的 id：dict 看 ``id`` / ``vp_id``，对象看同名属性。

    :param entry: 条目（dict、dataclass、任意带 ``id`` 的对象）。
    :return: 条目 id（可能为空串）。
    """
    if isinstance(entry, Mapping):
        return _text(entry.get("id")) or _text(entry.get("vp_id"))
    for name in ("id", "vp_id"):
        text = _text(getattr(entry, name, ""))
        if text:
            return text
    return ""


def _plan_index(plan_json: Any) -> Dict[str, Any]:
    """机器计划里「验证点 id → 条目」的索引。

    认三种形状：``{"verification_points": [...]}`` / ``{"vps": [...]}`` /
    列表本身；以及 ``{id: 条目}`` 的映射（键本身就是 id）。

    :param plan_json: 机器计划的内容（``None`` 表示「没有计划」）。
    :return: id → 条目的索引（没有计划时是空表）。
    """
    index: Dict[str, Any] = {}
    if isinstance(plan_json, Mapping):
        points: Optional[List[Any]] = None
        for key in _PLAN_POINT_KEYS:
            if key in plan_json:
                value = plan_json.get(key)
                points = list(value) if isinstance(value, (list, tuple)) else []
                break
        if points is not None:
            for point in points:
                vp_id = _entry_id(point)
                if vp_id:
                    index[vp_id] = point
            return index
        # ``{id: 条目}`` 的形状：键本身就是 id。
        for key, value in plan_json.items():
            if isinstance(key, str) and key:
                index[key] = value
        return index
    if isinstance(plan_json, (list, tuple)):
        for point in plan_json:
            vp_id = _entry_id(point)
            if vp_id:
                index[vp_id] = point
    return index


def _doc_ids(parsed: Any) -> Set[str]:
    """计划书条目的 id 集合。

    认三种形状：解析产物（``items`` 属性 / ``items`` 键）、条目列表、
    ``{id: 条目}`` 映射。

    :param parsed: 计划书的解析产物（``None`` 表示「没有计划书」）。
    :return: 条目 id 集合。
    """
    ids: Set[str] = set()
    if parsed is None:
        return ids
    if isinstance(parsed, Mapping):
        if "items" in parsed:
            value = parsed.get("items")
            entries = list(value) if isinstance(value, (list, tuple)) else []
            for entry in entries:
                vp_id = _entry_id(entry)
                if vp_id:
                    ids.add(vp_id)
            return ids
        # ``{id: 条目}`` 的形状：键本身就是 id。
        ids.update(key for key in parsed.keys() if isinstance(key, str) and key)
        return ids
    entries = getattr(parsed, "items", None)
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            vp_id = _entry_id(entry)
            if vp_id:
                ids.add(vp_id)
        return ids
    if isinstance(parsed, (list, tuple)):
        for entry in parsed:
            vp_id = _entry_id(entry)
            if vp_id:
                ids.add(vp_id)
    return ids


def _skipped_ids(report: Mapping[str, Any]) -> Set[str]:
    """报告里 ``skipped_items[]`` 列出的验证点 id。

    认两种形状：id 字符串列表 / ``{id: 依据}`` 映射。

    :param report: 报告 JSON。
    :return: 被跳过的验证点 id 集合。
    """
    value = report.get(SKIPPED_ITEMS_FIELD)
    ids: Set[str] = set()
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    ids.add(text)
                continue
            vp_id = _entry_id(item)
            if vp_id:
                ids.add(vp_id)
    elif isinstance(value, Mapping):
        ids.update(key for key in value.keys() if isinstance(key, str) and key)
    return ids


def _is_delta_vp(point: Mapping[str, Any], plan_entry: Any) -> bool:
    """这条验证点是不是**增量新增**的（delta）。

    判据是「有人在报告或机器计划里承认它是增量来的」：报告里写了
    ``origin="delta"`` / ``added_reason``，或者机器计划里那条带着
    ``added_reason``（增量模块给新增 VP 留的审计字段）。

    :param point: 报告里的验证点。
    :param plan_entry: 机器计划里对应的条目（可能没有）。
    :return: 是不是 delta VP。
    """
    if _text(point.get("origin")).lower() == DELTA_ORIGIN:
        return True
    if _text(point.get(DELTA_REASON_FIELD)):
        return True
    if isinstance(plan_entry, Mapping):
        if _text(plan_entry.get(DELTA_REASON_FIELD)):
            return True
        if _text(plan_entry.get("origin")).lower() == DELTA_ORIGIN:
            return True
    return False


def traceability_issues(
    report_json: Mapping[str, Any],
    plan_json: Optional[Mapping[str, Any]] = None,
    parsed: Any = None,
) -> List[str]:
    """检查报告的回溯链，返回**问题清单**（没有问题就是空列表）。

    链是这样一条：

        报告 VP → 机器计划（``verification_plan.json``）→ 计划书条目

    查三类问题：

    1. **断链** —— 报告里的 VP 在机器计划里找不到（``… not in
       verification_plan.json``），或在计划书里找不到（``… not in plan
       document``）。
    2. **delta VP 伪装成已审核条目** —— 增量新增的验证点必须自报家门：
       ``origin="delta"`` 与 ``added_reason`` 缺一不可。
    3. **跳过没有登记** —— 报告里状态是「跳过」的验证点必须出现在
       ``skipped_items[]`` 里。

    delta VP **不查前两类断链**：它本来就不在已审核的计划书里（增量条目只
    活在内存中），强求它出现在计划书里等于要求它伪装成已审核条目。它必须
    自报家门，靠的是那两个字段。

    :param report_json: ``verification_report.json`` 的内容。
    :param plan_json: 机器计划 ``verification_plan.json`` 的内容（``None``
        表示「没有计划」，不查断链）。
    :param parsed: 计划书的解析产物（``None`` 表示「没有计划书」，不查
        计划书那一环）。
    :return: 问题清单（每条一句话，按 VP id 排序，同一条 VP 的问题按固定
        顺序排列）。

    **不抛异常**：报告还要给人看，缺一条回溯不该让整份报告渲染不出来。
    """
    report = report_json if isinstance(report_json, Mapping) else {}
    points = report.get("verification_points")
    if not isinstance(points, (list, tuple)):
        points = []

    plan_index = _plan_index(plan_json) if plan_json is not None else {}
    doc_ids = _doc_ids(parsed) if parsed is not None else set()
    skipped = _skipped_ids(report)

    prepared: List[Tuple[Tuple[str, ...], str, str, Mapping[str, Any]]] = []
    for point in points:
        if not isinstance(point, Mapping):
            continue
        vp_id = _text(point.get("id")) or _text(point.get("vp_id"))
        plan_item_id = _text(point.get("plan_item_id"))
        sort_key = (
            vp_id,
            plan_item_id,
            _text(point.get("status")).lower(),
            _text(point.get("origin")).lower(),
            _text(point.get(DELTA_REASON_FIELD)),
        )
        prepared.append((sort_key, vp_id, plan_item_id, point))
    prepared.sort(key=lambda row: row[0])

    issues: List[str] = []
    for _sort_key, vp_id, plan_item_id, point in prepared:
        if not vp_id:
            issues.append("verification point missing id")
            continue

        plan_entry = plan_index.get(vp_id)
        delta = _is_delta_vp(point, plan_entry)

        if delta:
            # 增量新增的 VP：不在已审核的计划书里是正常的，但它必须说清自己
            # 是增量来的 —— 否则它以「已审核条目」的样子出现在报告里。
            if _text(point.get("origin")).lower() != DELTA_ORIGIN:
                issues.append(f"{vp_id} delta VP missing origin=delta")
            if not _text(point.get(DELTA_REASON_FIELD)):
                issues.append(f"{vp_id} delta VP missing added_reason")
        else:
            # 第一环：报告 VP → 机器计划 VP。
            if plan_json is not None:
                traced = vp_id in plan_index or (
                    bool(plan_item_id) and plan_item_id in plan_index
                )
                if not traced:
                    issues.append(f"{vp_id} not in verification_plan.json")
            # 第二环：机器计划 VP → 计划书条目。
            if parsed is not None:
                traced = vp_id in doc_ids or (
                    bool(plan_item_id) and plan_item_id in doc_ids
                )
                if not traced:
                    issues.append(f"{vp_id} not in plan document")

        # 跳过的验证点必须在 ``skipped_items[]`` 里登记。
        status = _text(point.get("status")).lower()
        if status in _SKIP_STATUSES and vp_id not in skipped:
            issues.append(f"{vp_id} skipped but not listed in skipped_items")

    return issues


def render(
    report_json: Mapping[str, Any],
    plan_items: Optional[Mapping[str, Any]] = None,
    review_state: Optional[Mapping[str, Any]] = None,
    repair_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    plan_json: Optional[Mapping[str, Any]] = None,
    parsed: Any = None,
) -> str:
    """把验证报告渲染成 Markdown。

    :param report_json: ``verification_report.json`` 的内容（含
        ``final_verdict`` 与 ``verification_points``）。
    :param plan_items: ``{计划书条目 id: 标题或条目 dict}``。
    :param review_state: ``{条目 id: 审核状态}``（值可以是字符串或 dict）。
    :param repair_rows: 修复行（每行一个 dict）。
    :param plan_json: 机器计划 ``verification_plan.json`` 的内容。给了它
        （或给了 ``parsed``）才会做回溯检查。
    :param parsed: 计划书的解析产物（:func:`traceability_issues` 认得
        ``items`` 属性 / ``items`` 键 / ``{id: 条目}`` 映射三种形状）。
    :return: Markdown 文本（以单个换行结尾）。

    输出结构固定，「最终结论」与「验证点结果」两节恒在；「审核状态」与
    「修复任务」两节**没有内容时整节省略** —— 空表不携带信息。

    「回溯问题」一节只在**给了机器计划 / 计划书且确实查出问题**时出现：
    没给就不查（无从查起），查了没问题就不渲染（空节不携带信息）。
    """
    report = report_json if isinstance(report_json, Mapping) else {}
    items = plan_items if isinstance(plan_items, Mapping) else {}
    reviews = review_state if isinstance(review_state, Mapping) else {}
    repairs = repair_rows if isinstance(repair_rows, (list, tuple)) else []

    sections = [
        "# 验证报告",
        "## 最终结论\n" + _escape(_final_verdict(report)),
        "## 验证点结果\n" + _table(_VP_HEADER, _vp_rows(report, items)),
    ]

    review_rows = _review_rows(reviews)
    if review_rows:
        sections.append("## 审核状态\n" + _table(_REVIEW_HEADER, review_rows))

    repair_table = _repair_rows(repairs)
    if repair_table:
        sections.append("## 修复任务\n" + _table(_REPAIR_HEADER, repair_table))

    if plan_json is not None or parsed is not None:
        issues = traceability_issues(report, plan_json, parsed)
        if issues:
            sections.append(
                "## 回溯问题\n"
                + "\n".join("- " + _escape(issue) for issue in issues)
            )

    return "\n\n".join(sections) + "\n"
