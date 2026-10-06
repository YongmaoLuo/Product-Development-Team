"""PRD 验收标准的覆盖护栏。

这个模块解决什么
----------------
``verification_plan`` 里每条 VP 都有 ``related_prd_criteria`` 字段，提示词
也要求规划 LLM 填它 —— 但**没有任何东西核对过它**。2026-10-04 的一次计划
（12 条验收标准、17 条计划内 VP）里，「FD 传递·多级」这条标准在计划阶段
一条 VP 都没有；它是执行期靠增量评估补出来的第 18 条。也就是说，测试设计
阶段漏掉一整条验收标准，机制上是**不可见**的：报告照样 PASSED，因为
``overall_status`` 只由 VP 决定，而漏掉的那条标准没有 VP 会去判它。

这与 :mod:`verification_plan_completeness` 是同一类缺陷的另一个轴。那一个
的 docstring 写着一句话，这里照抄作为理由：

    提示词说了但没人核对的东西，迟早还会再漂一次。

**为什么单独一个模块而不是并进 completeness**
------------------------------------------------
``verification_plan_completeness`` 刻意**不读 PRD 正文**（见它的 docstring），
理由是「正文是修辞，只认文件系统事实」—— 那条判据对「项目有没有全量 CI
门禁」是���的，因为 CI 入口在磁盘上是客观的。而验收标准的正文**就是事实本身**：
PRD 写了 12 条就是要验 12 条。所以这里的判据方向相反（读 PRD），两者的依据
不同，放一起会让其中一个的判据被另一个的适用条件污染。

匹配为什么只能保守
--------------------
VP 的 ``related_prd_criteria`` 是 LLM 填的自由文本，PRD 的 ``acceptance``
也是自由文本，靠语义匹配必然误判。这里的做法是**只认显式标签**：PRD 验收
标准写成 ``【标签】正文`` 时取标签，在所有 VP 的 ``related_prd_criteria``
里做子串匹配。没有 ``【标签】`` 的验收标准**跳过不检查** —— 宁可漏报也不
误报，因为误报会让一个本来没问题的计划被判失败，而漏报只是回到今天的状态。

缺口怎么办
----------
与全量关卡缺口同一条回路：这里只负责"发现"和"记录"，把缺口写进计划顶层的
``acceptance_gap`` 并记一条 warning。**绝不静默出厂**，也绝不自动判失败 ——
补一条 VP 是规划 LLM 的活，不是这个纯函数的活。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 计划顶层记录验收标准缺口的字段名。
GAP_FIELD = "acceptance_gap"

#: ``【标签】`` 前缀。中文方括号，PRD 的验收标准惯用这个形状。
_LABEL_RE = re.compile(r"^\s*【([^】]{2,40})】")

#: 标签短于这个长度就不检查。2 个中文字（如「范围」「文档」）在本仓的 PRD
#: 里区分度足够，而 1 个字（假设将来出现「密」）在任何一段正文里都会命中，
#: 那种情况宁可漏报。设成 2 而不是 3 是有代价的：一条真正没被覆盖的短标签
#: 标准，理论上可能被另一条 VP 的 related_prd_criteria 顺带命中而漏报 ——
#: 两种阈值都会漏，方向不同而已，2 字是本仓实测下漏得最少的一个。
_MIN_LABEL_LEN = 2


@dataclass(frozen=True)
class UncoveredCriterion:
    """一条 PRD 里有、验证计划里没有任何 VP 声明要验的验收标准。"""

    index: int
    label: str
    excerpt: str
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "excerpt": self.excerpt,
            "evidence": self.evidence,
        }


def _criterion_text(entry: Any) -> str:
    """Pull the prose out of one PRD ``acceptance`` entry.

    The field has been both a bare string and a dict with a ``text``
    key across versions; accept either rather than assume.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("text", "criterion", "description", "content"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # A dict with none of those keys is not a criterion we can read.
        return ""
    return ""


def extract_criterion_labels(
    acceptance: Any,
) -> List[Tuple[int, str, str]]:
    """``[(index, label, text)]`` for every explicitly-labelled criterion.

    Entries without a ``【label】`` prefix are omitted rather than
    guessed at — see the module docstring on why a false positive here
    is worse than a false negative.
    """
    if not isinstance(acceptance, (list, tuple)):
        return []
    out: List[Tuple[int, str, str]] = []
    for index, entry in enumerate(acceptance, start=1):
        text = _criterion_text(entry)
        if not text:
            continue
        match = _LABEL_RE.match(text)
        if not match:
            continue
        label = match.group(1).strip()
        if len(label) < _MIN_LABEL_LEN:
            continue
        out.append((index, label, text))
    return out


def find_uncovered_criteria(
    plan_data: Any, acceptance: Any,
) -> List[UncoveredCriterion]:
    """Which PRD acceptance criteria no VP claims to verify.

    A criterion counts as covered when any VP's
    ``related_prd_criteria`` contains its ``【label】``. The check is
    deliberately one-directional: a VP citing a criterion that is not
    in the PRD is a different (also real) problem, and is not this
    function's business.
    """
    criteria = extract_criterion_labels(acceptance)
    if not criteria:
        return []

    if not isinstance(plan_data, dict):
        return []
    points = plan_data.get("verification_points")
    if not isinstance(points, list):
        return []

    claims: List[str] = []
    for vp in points:
        if not isinstance(vp, dict):
            continue
        claim = vp.get("related_prd_criteria")
        if isinstance(claim, str):
            claims.append(claim)

    missing: List[UncoveredCriterion] = []
    for index, label, text in criteria:
        if any(label in claim for claim in claims):
            continue
        excerpt = text.strip().splitlines()[0][:160]
        missing.append(UncoveredCriterion(
            index=index,
            label=label,
            excerpt=excerpt,
            evidence=(
                f"PRD 验收标准第 {index} 条「{label}」没有任何 VP 的 "
                f"related_prd_criteria 引用它"
            ),
        ))
    return missing


def annotate_acceptance_gap(
    plan_data: Any, missing: Sequence[UncoveredCriterion],
) -> bool:
    """Record the gap at the top level of the plan. True if annotated.

    Mirrors ``_annotate_gate_completeness``: a plan that is missing a
    verification point must say so where an operator will see it, not
    only in a log line nobody reads after the round closes.
    """
    if not isinstance(plan_data, dict) or not missing:
        return False
    plan_data[GAP_FIELD] = [m.to_dict() for m in missing]
    return True


def render_gap_feedback(missing: Sequence[UncoveredCriterion]) -> str:
    """The same facts as a planning-LLM retry hint."""
    if not missing:
        return ""
    lines = [
        "以下 PRD 验收标准在验证计划里没有任何 VP 覆盖，请为每条补一个 "
        "verification_point，并在 related_prd_criteria 里写明对应的标准标签：",
    ]
    for item in missing:
        lines.append(f"  - 第 {item.index} 条「{item.label}」：{item.excerpt}")
    return "\n".join(lines)
