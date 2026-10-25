"""验证计划书（``verification-plan.md``）的确定性解析与结构校验。

这个模块解决什么
----------------
验证计划书既是人工审核的唯一对象，也是机器格式翻译的唯一来源。这两件事对
「文档格式」提的是同一个要求：**必须能被确定性解析**。人读得懂不等于机器读
得准，所以格式在这里被钉死：十个固定标题的二级章节、章节不得为空、
「被验证对象」章节里恰好一行 ``baseline:`` 行。

分层：解析与动作
----------------
本模块只做**语法与字段校验**：不执行子进程、不访问网络、不检查路径是否存在、
不解析 git 引用。把 ``ref`` 变成 ``commit_sha``、把 ``repo`` 变成一个真实
工作区，属于「基线冻结」阶段 —— 审核通过那一刻才做，且只做一次。

这条边界不是洁癖：结构校验器要能在任何环境里重跑（生成后跑一次、审核前跑
一次、每次改文档再跑一次）。它若去调 git，就会在「没有仓库」「没有网络」的
环境里给出与文档无关的失败，审核者就分不清「文档写错了」和「这台机器没配
好」——而后一种失败会把一份正确的文档挡在审核之外。

基线行是机器事实，不是叙述
--------------------------
``baseline: repo=<repo_dir> ref=<ref> pr=<number|null>`` 是 ``plan_state.json``
里 ``verification.baseline`` 的**唯一来源**。同一事实不能有第二个来源，否则
「审核覆盖的文档」与「实际被验证的代码」可以各说各话，而审核哈希保护不到
文档之外的副本。因此这一行按严格语法判定：三个键各出现一次、顺序固定、值
非空且不含空白。

十个错误码一一对应十类非法写法，每类都定位到行号 —— 只知道「拒绝了」而不
知道「拒绝在哪」的校验器，审核者用不了。

章节清单（模板顺序）
--------------------
被验证对象 / 验证范围 / 排除范围 / 验证方法 / 验证点清单 / 预期结果 /
证据要求 / 通过判据 / 环境与依赖 / 风险面。

生成与校验的关系
----------------
:func:`generate` 是这份文档的生成入口：它向模型要一份文档，**先校验后落盘**。
非法结构在落盘前就被拒绝（:class:`PlanDocError`），所以审核者看到的永远是
一份已经通过十章节契约的文档 —— 一份缺章节的计划书落到磁盘上，审核者只会
看到「看起来完整」的文件，而下游翻译管道要按契约去解析它。

:func:`refine` 是改写入口，走同一条纪律：不合契约的改写结果一次都不落盘，
磁盘上留下的是旧文档。改写另有一条约束 —— 验证点 id 集合不变（见
:func:`_refine_errors`）：改写是「把这份文档改好」，不是「换一份文档」。

基线行由生成上下文交给模型，不是模型自己想出来的：上下文里的 ``baseline``
与文档里的基线行共用同一套语法校验，因此「相对路径」「键顺序错误」这类写法
在两处得到同一种拒绝。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 「被验证对象」章节标题 —— 基线行的唯一合法归属章节。
OBJECT_SECTION = "被验证对象"

#: 「验证点清单」章节标题 —— VP 块的唯一合法归属章节。
ITEM_LIST_SECTION = "验证点清单"

#: 计划书必须包含的十个二级章节；元组顺序即模板顺序。
REQUIRED_SECTIONS: Tuple[str, ...] = (
    OBJECT_SECTION,
    "验证范围",
    "排除范围",
    "验证方法",
    ITEM_LIST_SECTION,
    "预期结果",
    "证据要求",
    "通过判据",
    "环境与依赖",
    "风险面",
)

#: ``baseline:`` 行的键；元组顺序即语法顺序。
BASELINE_KEYS: Tuple[str, ...] = ("repo", "ref", "pr")

#: 基线行的行首前缀。必须出现在行首：缩进过的 ``baseline:`` 不算基线行。
BASELINE_PREFIX = "baseline:"

# 二级章节标题。``###`` 及更深的标题是章节内容（VP 块），不是章节。
_SECTION_RE = re.compile(r"^##\s+(?P<title>\S.*?)\s*$")

# VP 块的开头：三级到六级标题，正文以稳定 id 开头。
_VP_HEADING_RE = re.compile(
    r"^#{3,6}\s+(?P<id>VP-\d+)\s*(?P<title>.*?)\s*$"
)

# VP 块内的标注行。分隔符允许半角与全角冒号。
_LABEL_RE = re.compile(
    r"^(?P<label>范围|验证方法|通过判据|证据要求|预期结果)\s*[:：]\s*(?P<value>.*)$"
)

# 通过判据与证据要求的编号条目。
_CRITERION_RE = re.compile(r"^(?P<id>C\d+)\s*[:：]\s*(?P<value>.*)$")
_EVIDENCE_RE = re.compile(r"^(?P<id>E\d+)\s*[:：]\s*(?P<value>.*)$")

# 列表项前缀（``- `` / ``* `` / ``1. ``）。
_LIST_MARKER_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")

# ``pr`` 的合法取值：十进制数字，或字面量 ``null``。
_PR_NUMBER_RE = re.compile(r"^[0-9]+$")


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineDoc:
    """``baseline:`` 行的解析产物。

    ``pr_number`` 为 ``None`` 表示基线不是由某个 PR head 给出（``pr=null``）。
    """

    repo_dir: Path
    ref: str
    pr_number: Optional[int]


@dataclass(frozen=True)
class StructureError(Exception):
    """一处结构违规。

    ``section`` 是违规所属章节（文档级问题用空串），``line`` 是 1 起的行号；
    ``line=0`` 表示这条错误不定位到具体行（例如缺失章节）。
    ``code`` 是稳定的机器可读错误码，``message`` 是给人看的一句话。
    """

    section: str
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        where = self.section or "<document>"
        return f"[{self.code}] {where}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Section:
    """一个二级章节。"""

    title: str
    line: int
    body: str
    lines: Tuple[Tuple[int, str], ...] = ()


@dataclass
class PlanItem:
    """「验证点清单」章节里的一个验证点（``VP-xxx`` 块）。"""

    id: str
    title: str
    line: int
    scope: str = ""
    method: str = ""
    criteria: Dict[str, str] = field(default_factory=dict)
    evidence: Dict[str, str] = field(default_factory=dict)
    expected_result: str = ""


@dataclass
class ParsedPlan:
    """一份合法计划书的解析产物。"""

    items: List[PlanItem]
    baseline: BaselineDoc
    sections: Dict[str, Section]


@dataclass
class _RawSection:
    """解析过程中的中间结构：还没组装成 :class:`Section`。"""

    title: str
    line: int
    body_lines: List[Tuple[int, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 文档切分
# ---------------------------------------------------------------------------


def _split_sections(md: str) -> Tuple[List[_RawSection], List[Tuple[int, str]]]:
    """把文档切成二级章节。

    返回 ``(章节列表, 首个章节之前的内容)``。章节前的内容是文档标题一类的
    抬头，不属于任何章节，也不参与「章节不得为空」的判定。
    """
    sections: List[_RawSection] = []
    preamble: List[Tuple[int, str]] = []
    current: Optional[_RawSection] = None
    for lineno, raw in enumerate(md.splitlines(), start=1):
        heading = _SECTION_RE.match(raw)
        if heading:
            current = _RawSection(title=heading.group("title"), line=lineno)
            sections.append(current)
            continue
        if current is None:
            preamble.append((lineno, raw))
        else:
            current.body_lines.append((lineno, raw))
    return sections, preamble


def _is_baseline_line(text: str) -> bool:
    """这一行是不是基线行（行首 ``baseline:``）。"""
    return text.startswith(BASELINE_PREFIX)


def _has_content(section: _RawSection) -> bool:
    """章节里有没有「人写的」内容。

    基线行是机器事实，不是章节内容：只有基线行的「被验证对象」章节仍然是空
    章节（对象描述为空）。
    """
    for _lineno, text in section.body_lines:
        if not text.strip():
            continue
        if _is_baseline_line(text):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# baseline 行
# ---------------------------------------------------------------------------


def _baseline_hits(text: str) -> List[Tuple[int, str]]:
    """返回 ``text`` 里所有基线行的 ``(行内偏移, 原文)``。

    偏移是 0 起的行内下标；调用方据此换算出文档里的 1 起行号。
    """
    return [
        (index, line)
        for index, line in enumerate(text.splitlines())
        if _is_baseline_line(line)
    ]


def _baseline_pairs(remainder: str) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """把 ``baseline:`` 之后的正文切成 ``(键, 值)`` 列表。

    键值以第一个 ``=`` 分隔。值里出现空白时，这一行会被切成好几个片段；把
    没有 ``=`` 的片段并回前一个值，值里的空白就能被原样看见（而不是被悄悄
    截断成一个合法值）。第一个片段就没有 ``=`` 时，这一行根本不是
    ``key=value`` 形式，返回第二个元素为错误说明。
    """
    pairs: List[Tuple[str, str]] = []
    for token in remainder.split():
        if "=" in token:
            key, _, value = token.partition("=")
            pairs.append((key, value))
        elif pairs:
            key, value = pairs[-1]
            pairs[-1] = (key, f"{value} {token}")
        else:
            return [], token
    return pairs, None


def _baseline_errors(
    text: str,
    *,
    line_number: int,
    section: str,
) -> List[StructureError]:
    """返回 ``text`` 里基线行语法的全部违规。

    ``line_number`` 是 ``text`` 第一行在文档里的行号（1 起），错误行号由此
    换算成文档行号。
    """
    errors: List[StructureError] = []
    hits = _baseline_hits(text)

    if not hits:
        errors.append(
            StructureError(
                section,
                line_number,
                "baseline_missing_line",
                "缺少 baseline: 行",
            )
        )
        return errors
    if len(hits) > 1:
        errors.append(
            StructureError(
                section,
                line_number + hits[1][0],
                "baseline_multiple_lines",
                "baseline: 行只能出现一次",
            )
        )
        return errors

    offset, raw = hits[0]
    absolute = line_number + offset
    remainder = raw[len(BASELINE_PREFIX):].strip()

    pairs, malformed = _baseline_pairs(remainder)
    if malformed is not None:
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_malformed",
                f"baseline: 行不是 key=value 形式：{malformed!r}",
            )
        )
        return errors

    keys = [key for key, _ in pairs]

    # 键层面的检查按「未知 → 重复 → 缺失 → 顺序」排：每一类都是独立的违规，
    # 先报的那一类不依赖后一类，单点变异语料因此只有一条错误。
    unknown = next((key for key in keys if key not in BASELINE_KEYS), None)
    if unknown is not None:
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_unknown_key",
                f"未知键 {unknown!r}；只允许 {'、'.join(BASELINE_KEYS)}",
            )
        )
        return errors

    duplicated = next((key for key in BASELINE_KEYS if keys.count(key) > 1), None)
    if duplicated is not None:
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_duplicate_key",
                f"键 {duplicated!r} 重复出现",
            )
        )
        return errors

    missing = next((key for key in BASELINE_KEYS if key not in keys), None)
    if missing is not None:
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_missing_key",
                f"缺少键 {missing!r}",
            )
        )
        return errors

    if keys != list(BASELINE_KEYS):
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_key_order",
                f"键顺序必须是 {'、'.join(BASELINE_KEYS)}，实际是 {'、'.join(keys)}",
            )
        )
        return errors

    values = dict(pairs)

    empty = next((key for key in BASELINE_KEYS if values[key] == ""), None)
    if empty is not None:
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_empty_value",
                f"键 {empty!r} 的值为空",
            )
        )
        return errors

    spaced = next(
        (
            key
            for key in BASELINE_KEYS
            if any(char.isspace() for char in values[key])
        ),
        None,
    )
    if spaced is not None:
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_whitespace_in_value",
                f"键 {spaced!r} 的值不得包含空白",
            )
        )
        return errors

    repo = values["repo"]
    if repo.startswith("~"):
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_home_path",
                "repo 必须是绝对路径，不能以 ~ 开头",
            )
        )
    elif not Path(repo).is_absolute():
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_relative_path",
                f"repo 必须是绝对路径，实际是 {repo!r}",
            )
        )

    ref = values["ref"]
    if ref.startswith("-"):
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_invalid_ref",
                "ref 不得以 - 开头（会被 git 当成选项）",
            )
        )

    pr = values["pr"]
    if pr != "null" and not _PR_NUMBER_RE.match(pr):
        errors.append(
            StructureError(
                section,
                absolute,
                "baseline_invalid_pr",
                f"pr 必须是十进制数字或 null，实际是 {pr!r}",
            )
        )

    return errors


def parse_baseline(
    text: str,
    *,
    line_number: int = 1,
    section: str = OBJECT_SECTION,
) -> BaselineDoc:
    """解析基线行，返回 :class:`BaselineDoc`。

    ``text`` 可以是基线行本身，也可以是包含基线行的章节正文或整份文档 ——
    本函数只找行首 ``baseline:`` 的那一行。``line_number`` 是 ``text`` 第一
    行在文档里的行号（1 起），错误行号据此换算。

    语法不合时抛 :class:`StructureError`（第一个违规）。
    """
    errors = _baseline_errors(text, line_number=line_number, section=section)
    if errors:
        raise errors[0]

    hits = _baseline_hits(text)
    # ``_baseline_errors`` 刚刚拒绝过「没有基线行」与「多行基线行」两种情况，
    # 所以这里必然恰好命中一行。
    assert len(hits) == 1
    _offset, raw = hits[0]
    pairs, _malformed = _baseline_pairs(raw[len(BASELINE_PREFIX):].strip())
    values = dict(pairs)
    pr_value = values["pr"]
    return BaselineDoc(
        repo_dir=Path(values["repo"]),
        ref=values["ref"],
        pr_number=None if pr_value == "null" else int(pr_value),
    )


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------


def validate_structure(md: str) -> List[StructureError]:
    """校验文档结构，返回全部违规（空列表表示合法）。

    只判定语法与字段：十个必需章节在不在、空不空、顺序对不对，「被验证对象」
    章节里的基线行合不合法。不执行子进程、不访问网络、不检查路径是否存在。
    """
    errors: List[StructureError] = []
    sections, preamble = _split_sections(md)

    first: Dict[str, _RawSection] = {}
    for section in sections:
        if section.title not in REQUIRED_SECTIONS:
            errors.append(
                StructureError(
                    section.title,
                    section.line,
                    "unexpected_section",
                    f"未知章节 {section.title!r}；计划书只允许十个固定章节",
                )
            )
            continue
        if section.title in first:
            errors.append(
                StructureError(
                    section.title,
                    section.line,
                    "duplicate_section",
                    f"章节 {section.title!r} 重复出现（首次在第 {first[section.title].line} 行）",
                )
            )
            continue
        first[section.title] = section

    for title in REQUIRED_SECTIONS:
        if title not in first:
            errors.append(
                StructureError(
                    title,
                    0,
                    "missing_section",
                    f"缺少必需章节 {title!r}",
                )
            )

    for title in REQUIRED_SECTIONS:
        section = first.get(title)
        if section is None:
            continue
        if not _has_content(section):
            errors.append(
                StructureError(
                    title,
                    section.line,
                    "empty_section",
                    f"章节 {title!r} 没有内容",
                )
            )

    # 顺序只在十个章节都在场时判定：缺章节时顺序错误只是缺章节的次生现象，
    # 单点变异语料不应该因此多出一条错误。
    if len(first) == len(REQUIRED_SECTIONS):
        ordered = [
            section.title
            for section in sections
            if first.get(section.title) is section
        ]
        for index, title in enumerate(ordered):
            if title != REQUIRED_SECTIONS[index]:
                offending = first[title]
                errors.append(
                    StructureError(
                        title,
                        offending.line,
                        "section_out_of_order",
                        f"章节 {title!r} 出现在第 {offending.line} 行，"
                        f"应在 {REQUIRED_SECTIONS[index]!r} 之后",
                    )
                )
                break

    errors.extend(_baseline_section_errors(sections, preamble))
    return errors


def _baseline_section_errors(
    sections: List[_RawSection],
    preamble: List[Tuple[int, str]],
) -> List[StructureError]:
    """判定基线行的归属与语法。

    基线行必须写在「被验证对象」章节里，且全文只出现一次。
    """
    located: List[Tuple[int, str, Optional[str]]] = []
    for lineno, text in preamble:
        if _is_baseline_line(text):
            located.append((lineno, text, None))
    for section in sections:
        for lineno, text in section.body_lines:
            if _is_baseline_line(text):
                located.append((lineno, text, section.title))

    if not located:
        return [
            StructureError(
                OBJECT_SECTION,
                0,
                "baseline_missing_line",
                f"「{OBJECT_SECTION}」章节缺少 baseline: 行",
            )
        ]

    if len(located) > 1:
        lineno, _text, title = located[1]
        return [
            StructureError(
                title or OBJECT_SECTION,
                lineno,
                "baseline_multiple_lines",
                "baseline: 行只能出现一次",
            )
        ]

    lineno, text, title = located[0]
    if title != OBJECT_SECTION:
        return [
            StructureError(
                title or OBJECT_SECTION,
                lineno,
                "baseline_wrong_section",
                f"baseline: 行必须写在「{OBJECT_SECTION}」章节里",
            )
        ]
    return _baseline_errors(text, line_number=lineno, section=OBJECT_SECTION)


# ---------------------------------------------------------------------------
# 条目解析
# ---------------------------------------------------------------------------


def _strip_list_marker(text: str) -> str:
    """去掉列表项前缀与首尾空白。"""
    return _LIST_MARKER_RE.sub("", text.strip(), count=1)


def _parse_items(body_lines: List[Tuple[int, str]]) -> List[PlanItem]:
    """把「验证点清单」章节的正文解析成 VP 块列表。

    传的是正文行而不是章节对象：章节存不存在由结构校验判定，条目解析只看
    正文，不重复判定同一件事。
    """
    items: List[PlanItem] = []
    current: Optional[PlanItem] = None
    current_label: Optional[str] = None

    for lineno, raw in body_lines:
        heading = _VP_HEADING_RE.match(raw.strip())
        if heading:
            current = PlanItem(
                id=heading.group("id"),
                title=heading.group("title").strip(),
                line=lineno,
            )
            items.append(current)
            current_label = None
            continue
        if current is None:
            continue

        text = _strip_list_marker(raw)
        if not text:
            continue

        label = _LABEL_RE.match(text)
        if label:
            current_label = label.group("label")
            value = label.group("value").strip()
            if current_label == "范围":
                current.scope = value
            elif current_label == "验证方法":
                current.method = value
            elif current_label == "预期结果":
                current.expected_result = value
            elif value:
                # 「通过判据：C1 xxx」这种把条目写在标签同一行的写法。
                entry = (
                    _CRITERION_RE.match(value)
                    if current_label == "通过判据"
                    else _EVIDENCE_RE.match(value)
                )
                if entry:
                    _add_entry(current, current_label, entry)
                else:
                    current_label = None
            continue

        criterion = _CRITERION_RE.match(text)
        if criterion and current_label == "通过判据":
            current.criteria[criterion.group("id")] = criterion.group("value").strip()
            continue

        evidence = _EVIDENCE_RE.match(text)
        if evidence and current_label == "证据要求":
            current.evidence[evidence.group("id")] = evidence.group("value").strip()

    return items


def _add_entry(item: PlanItem, label: str, entry: "re.Match[str]") -> None:
    """把一条编号条目记进判据或证据字典。"""
    if label == "通过判据":
        item.criteria[entry.group("id")] = entry.group("value").strip()
    else:
        item.evidence[entry.group("id")] = entry.group("value").strip()


def _item_errors(items: List[PlanItem], section_line: int) -> List[StructureError]:
    """判定条目层面的违规：重复 id、编号缺失、空判据。"""
    errors: List[StructureError] = []
    seen = set()
    for item in items:
        if item.id in seen:
            errors.append(
                StructureError(
                    ITEM_LIST_SECTION,
                    item.line,
                    "duplicate_vp_id",
                    f"验证点 id {item.id!r} 重复",
                )
            )
        seen.add(item.id)

        errors.extend(_numbered_errors(item, item.criteria, "通过判据", "C", "criteria"))
        errors.extend(_numbered_errors(item, item.evidence, "证据要求", "E", "evidence"))

    if not items:
        errors.append(
            StructureError(
                ITEM_LIST_SECTION,
                section_line,
                "no_verification_points",
                f"「{ITEM_LIST_SECTION}」章节里没有任何验证点",
            )
        )
    return errors


def _numbered_errors(
    item: PlanItem,
    entries: Dict[str, str],
    label: str,
    prefix: str,
    word: str,
) -> List[StructureError]:
    """判据 / 证据的编号必须从 1 连续，且不得为空文本。"""
    if not entries:
        return [
            StructureError(
                ITEM_LIST_SECTION,
                item.line,
                f"missing_{word}",
                f"验证点 {item.id} 缺少{label}（{prefix} 编号）",
            )
        ]

    errors: List[StructureError] = []
    expected = [f"{prefix}{index}" for index in range(1, len(entries) + 1)]
    if list(entries) != expected:
        errors.append(
            StructureError(
                ITEM_LIST_SECTION,
                item.line,
                f"{word}_numbering",
                f"验证点 {item.id} 的{label}编号必须是 "
                f"{expected[0]}..{expected[-1]}，实际是 {'、'.join(entries)}",
            )
        )
    if any(not text.strip() for text in entries.values()):
        errors.append(
            StructureError(
                ITEM_LIST_SECTION,
                item.line,
                f"empty_{word}",
                f"验证点 {item.id} 的{label}不得为空",
            )
        )
    return errors


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_plan_doc(md: str) -> ParsedPlan:
    """解析整份计划书。

    结构不合法时抛 :class:`StructureError`（第一个违规，按章节顺序、基线行
    顺序确定），合法时返回 :class:`ParsedPlan`。
    """
    errors = validate_structure(md)
    if errors:
        raise errors[0]

    raw_sections, _preamble = _split_sections(md)
    by_title = {section.title: section for section in raw_sections}

    sections: Dict[str, Section] = {}
    for title in REQUIRED_SECTIONS:
        raw = by_title[title]
        sections[title] = Section(
            title=title,
            line=raw.line,
            body="\n".join(text for _lineno, text in raw.body_lines),
            lines=tuple(raw.body_lines),
        )

    object_section = by_title[OBJECT_SECTION]
    object_body = "\n".join(text for _lineno, text in object_section.body_lines)
    object_start = (
        object_section.body_lines[0][0]
        if object_section.body_lines
        else object_section.line + 1
    )
    baseline = parse_baseline(object_body, line_number=object_start, section=OBJECT_SECTION)

    list_section = by_title[ITEM_LIST_SECTION]
    items = _parse_items(list_section.body_lines)
    item_errors = _item_errors(items, list_section.line)
    if item_errors:
        raise item_errors[0]

    return ParsedPlan(items=items, baseline=baseline, sections=sections)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

#: 计划书文件名。
PLAN_DOC_FILENAME = "verification-plan.md"

#: 生成尝试次数上限：首次生成 + 一次带结构错误反馈的重试。
#:
#: 两次之后仍然非法 → 放弃并抛 :class:`PlanDocError`。不是无限重试：模型连着
#: 两次都写不出十章节，多问几次也只是多烧几次模型调用。
GENERATE_ATTEMPTS = 2

#: 改写尝试次数上限：首次改写 + 一次带错误反馈的重试（与生成同一条口径）。
REFINE_ATTEMPTS = 2

#: 每个章节在提示词里的一句话说明（章节名本身是契约，顺序由
#: :data:`REQUIRED_SECTIONS` 决定）。
_SECTION_HINTS: Dict[str, str] = {
    OBJECT_SECTION: "被验证对象，以及下面那行机器事实",
    "验证范围": "本次要验证什么",
    "排除范围": "本次不验证什么，以及为什么",
    "验证方法": "怎么验证（命令 / 步骤）",
    ITEM_LIST_SECTION: "逐条验证点（VP-xxx 块）",
    "预期结果": "整体预期",
    "证据要求": "每个验证点要留下什么证据",
    "通过判据": "整体通过条件",
    "环境与依赖": "跑起来需要什么",
    "风险面": "可能让验证失真的因素",
}

#: 包住整份文档的代码块围栏。只剥「开头是 ``` 且结尾是 ```」的整块围栏，
#: 文档内部出现的围栏不动。
_FENCE_RE = re.compile(r"^```[^\n]*\n(?P<body>.*)\n?```\s*$", re.DOTALL)


class PlanDocError(Exception):
    """计划书生成失败。

    两种来源共用这一个异常：

    * **文档结构非法** —— 模型给的文档不满足十章节契约（缺章节、空章节、
      基线行不合法、条目层违规）。``errors`` 里有逐条违规，路由层把它逐条
      返回给调用方。
    * **生成上下文不合法** —— 上下文缺 baseline，或上下文里的 baseline
      不满足基线行的语法。错误码以 ``context_`` 开头，与文档违规区分。

    ``status_code`` 是路由层直接可用的 HTTP 状态码（422）：这份文档不可
    处理，但重试同一个请求没有意义 —— 要改的是上下文或提示词，不是请求。
    """

    #: 路由层用的 HTTP 状态码。
    status_code = 422

    def __init__(self, errors: List[StructureError]):
        self.errors = list(errors)
        super().__init__(self._render())

    def _render(self) -> str:
        if not self.errors:
            return "计划书生成失败"
        return "；".join(str(error) for error in self.errors)


def _strip_code_fence(text: str) -> str:
    """剥掉包住整份文档的代码块围栏。

    模型经常把整份 Markdown 包进代码块围栏里。围栏不是文档的一部分：留在
    文件里，审核者看到的第一行就是围栏标记，而不是标题。文档内部的围栏不动
    —— 那可能是合法的内容。
    """
    match = _FENCE_RE.match(text.strip("\n"))
    if match is None:
        return text
    return match.group("body")


def _context_baseline_line(
    context: Any,
) -> Tuple[str, List[StructureError]]:
    """从生成上下文取出基线事实，渲染成基线行。

    上下文里的基线与文档里的基线行是同一套语法：这里借用基线行自己的校验
    器，于是「相对路径」「键顺序错误」这类写法在两处得到同一种拒绝，而不是
    两套各写一遍的规则。
    """
    if not isinstance(context, dict):
        return "", [
            StructureError(
                "",
                0,
                "context_missing_baseline",
                "生成上下文必须是字典",
            )
        ]

    baseline = context.get("baseline")
    if not isinstance(baseline, dict):
        return "", [
            StructureError(
                "",
                0,
                "context_missing_baseline",
                "生成上下文缺少 baseline",
            )
        ]

    missing = [key for key in BASELINE_KEYS if key not in baseline]
    if missing:
        return "", [
            StructureError(
                "",
                0,
                "context_incomplete_baseline",
                f"baseline 缺少键 {'、'.join(missing)}",
            )
        ]

    repo = baseline["repo"]
    ref = baseline["ref"]
    pr = baseline["pr"]
    if not isinstance(repo, str) or not isinstance(ref, str):
        return "", [
            StructureError(
                "",
                0,
                "context_invalid_baseline",
                "baseline 的 repo / ref 必须是字符串",
            )
        ]

    # ``pr=None`` 是「基线不由某个 PR head 给出」，与文档里的 ``pr=null``
    # 是同一件事。
    pr_text = "null" if pr is None else str(pr)
    line = f"{BASELINE_PREFIX} repo={repo} ref={ref} pr={pr_text}"

    errors = [
        StructureError(
            error.section,
            error.line,
            error.code,
            f"上下文里的基线不合法：{error.message}",
        )
        for error in _baseline_errors(line, line_number=1, section=OBJECT_SECTION)
    ]
    if errors:
        return "", errors
    return line, []


def _build_prompt(context: Dict[str, Any], baseline_line: str) -> str:
    """生成提示词。

    提示词把三件事钉死：章节清单与顺序、基线行原文、验证点块格式。其余
    （验证范围、方法、风险）由模型按被验证对象自己写。
    """
    plan_id = str(context.get("plan_id", "") or "")
    outline = "\n".join(
        f"{index}. {title} —— {_SECTION_HINTS[title]}"
        for index, title in enumerate(REQUIRED_SECTIONS, start=1)
    )
    return f"""你要写一份「验证计划书」，它既是人工审核的对象，也是机器翻译的唯一来源。

输出要求（逐条照做；有一条不满足，这份文档会被直接丢弃）：
1. 只输出 Markdown 文档正文，不要解释、不要代码块围栏。
2. 文档必须恰好包含下面十个二级章节，标题逐字照抄，顺序不变，不得增删：
{outline}
3. 每个章节都不得为空，至少写一行实质内容。
4. 「{OBJECT_SECTION}」章节里必须原样包含下面这一行，且全文只出现一次：
{baseline_line}
5. 「{ITEM_LIST_SECTION}」章节里至少写一个验证点，格式如下。每个验证点都要有
   范围 / 验证方法 / 通过判据 / 证据要求 / 预期结果五项；判据用 C1..Cn 连续
   编号，证据用 E1..Em 连续编号，不得跳号：

### VP-001 验证点标题

- 范围：……
- 验证方法：……
- 通过判据：
  - C1: ……
- 证据要求：
  - E1: ……
- 预期结果：……

计划 id：{plan_id or "（未提供）"}
"""


def _build_repair_prompt(prompt: str, errors: List[StructureError]) -> str:
    """重试提示词：把上一份文档的结构错误原样带给模型。

    只说「重写」没有用 —— 模型不知道自己错在哪一行。重试是把错误逐条摊
    给它看，让它对着错误改，而不是重新猜一遍。
    """
    detail = "\n".join(f"- {error}" for error in errors)
    return (
        f"{prompt}\n\n"
        f"上一次生成的文档结构不合法，错误如下：\n{detail}\n\n"
        "请按上面的章节清单与基线行重新输出一份完整、合法的文档。"
    )


def _ask(tool: Any, prompt: str) -> str:
    """向模型要一次响应。

    ``tool`` 可以是一个接受提示词、返回文本的普通可调用对象（脚本化替身
    就是这种），也可以是一个暴露 ``query(prompt)`` 的工具实例（本仓库的
    ``CodingTool``）。
    """
    if callable(tool):
        response = tool(prompt)
        if isinstance(response, str):
            return response

    query = getattr(tool, "query", None)
    if callable(query):
        response = query(prompt)
        if isinstance(response, str):
            return response

    raise PlanDocError(
        [
            StructureError(
                "",
                0,
                "tool_no_response",
                "生成工具没有返回文本响应",
            )
        ]
    )


def _document_errors(md: str) -> List[StructureError]:
    """文档的全部违规：章节 / 基线行层面 + 条目层面。

    :func:`validate_structure` 只管章节与基线行；条目层面的违规（重复 id、
    判据编号断档、缺判据、清单里没有验证点）由 :func:`parse_plan_doc` 判定。
    两层都要过 —— 缺章节的文档根本走不到条目层。
    """
    errors = validate_structure(md)
    if errors:
        return errors
    try:
        parse_plan_doc(md)
    except StructureError as exc:
        return [exc]
    return []


def _write_plan_doc(plan_dir: Path, md: str) -> Path:
    """把已经通过校验的文档落盘，返回它的路径。

    写入发生在校验之后：非法文档没有任何一次落到磁盘上的机会。
    """
    plan_dir = Path(plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / PLAN_DOC_FILENAME
    path.write_text(md, encoding="utf-8")
    return path


def generate(plan_dir: Path, context: Dict[str, Any], tool: Any) -> Path:
    """生成验证计划书并落盘，返回文档路径。

    流程：取上下文里的基线 → 向模型要文档 → 校验 → 落盘。校验不通过就带着
    错误重试一次；两次都不通过则抛 :class:`PlanDocError`，磁盘上不会留下这
    份文档。

    重试那一次如果工具本身出了问题（没返回、超时），报出去的是第一次的
    结构错误 —— 工具的异常不盖住文档的错误；第一次就没拿到响应时，工具的
    异常原样抛出（那不是文档问题）。

    Args:
        plan_dir: 计划目录；不存在会被创建。
        context: 生成上下文，必须含 ``baseline``（``repo`` / ``ref`` /
            ``pr``），可选 ``plan_id``。
        tool: 向模型提问的入口 —— 接受提示词、返回文本的可调用对象，或暴露
            ``query(prompt)`` 的工具实例。

    Returns:
        ``plan_dir / "verification-plan.md"`` 的路径。

    Raises:
        PlanDocError: 上下文不合法、工具没有返回文本、或两次生成的文档都
            不满足十章节契约。错误带 ``status_code``（422）供路由层直接用。
    """
    baseline_line, context_errors = _context_baseline_line(context)
    if context_errors:
        raise PlanDocError(context_errors)

    prompt = _build_prompt(context, baseline_line)
    errors: List[StructureError] = []

    for _attempt in range(GENERATE_ATTEMPTS):
        try:
            response = _strip_code_fence(_ask(tool, prompt))
        except Exception:  # noqa: BLE001 — 重试拿不到响应，见下面三行注释
            if not errors:
                # 第一次就没拿到响应：这不是文档问题，是工具问题，原样抛出。
                raise
            # 重试这一次没能拿到任何响应。第一次的结构错误仍然是最有用的
            # 信息 —— 把它报出去，比把工具的异常盖住更好。
            break
        errors = _document_errors(response)
        if not errors:
            return _write_plan_doc(Path(plan_dir), response)
        prompt = _build_repair_prompt(prompt, errors)

    raise PlanDocError(errors)


# ---------------------------------------------------------------------------
# 改写
# ---------------------------------------------------------------------------
#
# 改写是审核者对**整篇文档**提反馈之后的动作：文档被换掉，审核随之失效。
# 所以它与生成共用一条纪律 —— **先校验后落盘**：不合契约的改写结果一次都不
# 落到磁盘上，磁盘上留下的是旧文档。
#
# 改写与生成有一条不同的约束，它属于「改写」而不是「生成」：
#
#   * **验证点 id 集合不变** —— 改写是「把这份文档改好」，不是「换一份文档」。
#     审核条目按 id 归因，id 变了，旧审核就落在了别的条目上；更糟的是「少一个
#     验证点」是静默的：条目少了一条，审核者未必发现，而那个验证点从此没人验。
#
# 基线行由提示词要求原样保留（见 :func:`_build_refine_prompt`），但它不作为
# 一条拒绝规则：基线只有一个来源 —— 计划状态里记录的那份，而计划状态是在重新
# 审核通过时按文档的基线行冻结的。文档改写之后基线随之失效，重新审一遍时
# 基线重新冻结，审核者会再一次看见「被验证对象」章节里的那一行。


def _heading_vp_ids(md: str) -> List[str]:
    """文档里出现的验证点 id（按出现顺序）。

    用的是解析器自己的标题正则，所以「什么算一个验证点」在这两处是同一条
    规则 —— 不另写一份 id 的形状。
    """
    ids: List[str] = []
    for raw in md.splitlines():
        heading = _VP_HEADING_RE.match(raw.strip())
        if heading:
            ids.append(heading.group("id"))
    return ids


def _declared_item_ids(md: str) -> List[str]:
    """文档声明的验证点 id（按文档顺序）。

    结构合法时用解析结果（那才是「验证点清单」章节里真正的验证点）；结构非法
    时退回标题扫描 —— 旧文档本来就可能是坏的，改写不该因为读不懂它就拒绝。
    """
    try:
        parsed = parse_plan_doc(md)
    except StructureError:
        return _heading_vp_ids(md)
    return [item.id for item in parsed.items]


def _refine_errors(new_md: str, current_md: str) -> List[StructureError]:
    """改写特有的那条约束：验证点 id 集合不变。

    Args:
        new_md: 改写结果。
        current_md: 改写前的文档。

    Returns:
        违规列表；都合规时是空列表。
    """
    old_ids = set(_declared_item_ids(current_md))
    new_ids = set(_declared_item_ids(new_md))
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    if not (added or removed):
        return []
    return [
        StructureError(
            ITEM_LIST_SECTION,
            0,
            "vp_ids_changed",
            "改写不得新增或删除验证点"
            f"（新增：{'、'.join(added) or '无'}；"
            f"删除：{'、'.join(removed) or '无'}）",
        )
    ]


def _build_refine_prompt(current_md: str, feedback: str) -> str:
    """改写提示词。

    提示词把四件事钉死：反馈原文、章节清单与顺序、基线行与验证点 id 不变、
    输出的是完整文档。
    """
    outline = "\n".join(
        f"{index}. {title} —— {_SECTION_HINTS[title]}"
        for index, title in enumerate(REQUIRED_SECTIONS, start=1)
    )
    return f"""你要改写一份「验证计划书」，它既是人工审核的对象，也是机器翻译的唯一来源。

审核者的反馈（逐条落实，不要自行扩大范围）：
{feedback}

输出要求（逐条照做；有一条不满足，这份文档会被直接丢弃，旧文档原样保留）：
1. 只输出改写后的 Markdown 文档正文，不要解释、不要代码块围栏。
2. 输出的是**完整**的文档：不是补丁、不是差异说明、不是节选。
3. 文档必须恰好包含下面十个二级章节，标题逐字照抄，顺序不变，不得增删：
{outline}
4. 每个章节都不得为空，至少写一行实质内容。
5. 「{OBJECT_SECTION}」章节里的 baseline: 行必须原样保留，一个字节都不改。
6. 「{ITEM_LIST_SECTION}」章节里的验证点 id 集合必须与原文完全一致：不得新增、
   不得删除、不得改号。验证点本身的内容（范围 / 验证方法 / 通过判据 / 证据要求 /
   预期结果）可以按反馈改写。
7. 判据用 C1..Cn 连续编号，证据用 E1..Em 连续编号，不得跳号。

下面是当前的文档原文（改写的对象）：
--- 当前文档开始 ---
{current_md}
--- 当前文档结束 ---
"""


def _build_refine_repair_prompt(prompt: str, errors: List[StructureError]) -> str:
    """重试提示词：把上一份改写结果的问题逐条摊给模型。

    只说「重写」没有用 —— 模型不知道自己错在哪一处。重试是把问题摊给它看，
    让它对着问题改，而不是重新猜一遍。
    """
    detail = "\n".join(f"- {error}" for error in errors)
    return (
        f"{prompt}\n\n"
        f"上一次改写的文档不合要求，问题如下：\n{detail}\n\n"
        "请对照上面的反馈与输出要求，重新输出改写后的完整文档。"
    )


def refine(plan_dir: Path, feedback: str, tool: Any) -> Path:
    """按反馈改写计划书并落盘，返回文档路径。

    流程：读当前文档 → 向模型要改写结果 → 校验 → 落盘。校验不通过就带着
    错误重试一次；两次都不通过则抛 :class:`PlanDocError`，磁盘上留下的是旧
    文档 —— 不合法的改写没有任何一次落到磁盘上的机会。

    校验有两层：十章节契约（:func:`_document_errors`）与改写特有的那条约束
    （:func:`_refine_errors`）。

    Args:
        plan_dir: 计划目录。
        feedback: 审核者的反馈。不能为空 —— 没有反馈就没有要改的东西。
        tool: 向模型提问的入口 —— 接受提示词、返回文本的可调用对象，或暴露
            ``query(prompt)`` 的工具实例。

    Returns:
        ``plan_dir / "verification-plan.md"`` 的路径。

    Raises:
        FileNotFoundError: 计划书不存在（没有可改写的对象）。
        PlanDocError: 反馈为空，或两次改写的结果都不满足契约。错误带
            ``status_code``（422）供路由层直接用。
    """
    plan_dir = Path(plan_dir)
    doc_path = plan_dir / PLAN_DOC_FILENAME
    try:
        current_bytes = doc_path.read_bytes()
    except OSError as exc:
        raise FileNotFoundError(f"验证计划书不存在：{doc_path}") from exc

    note = str(feedback or "").strip()
    if not note:
        raise PlanDocError(
            [
                StructureError(
                    "",
                    0,
                    "feedback_empty",
                    "改写反馈不能为空：没有反馈就没有要改的东西",
                )
            ]
        )

    current_md = current_bytes.decode("utf-8", errors="replace")
    prompt = _build_refine_prompt(current_md, note)
    errors: List[StructureError] = []

    for _attempt in range(REFINE_ATTEMPTS):
        try:
            response = _strip_code_fence(_ask(tool, prompt))
        except Exception:  # noqa: BLE001 — 口径与 generate 一致，见那里三行注释
            if not errors:
                # 第一次就没拿到响应：这不是文档问题，是工具问题，原样抛出。
                raise
            break
        errors = _document_errors(response)
        if not errors:
            errors = _refine_errors(response, current_md)
        if not errors:
            return _write_plan_doc(plan_dir, response)
        prompt = _build_refine_repair_prompt(prompt, errors)

    raise PlanDocError(errors)
