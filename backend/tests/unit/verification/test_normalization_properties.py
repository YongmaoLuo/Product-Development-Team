"""TDD spec — normalize_criterion_text 的归一化性质。

为什么归一化要单独立一组性质测试
--------------------------------
判定内核的第一层（确定性层）把两段自由文本放在一起比：VP 声明的判据和
计划里的判据正文。两段文本都是人/LLM 写的，差异只在于噪声：全角半角、
中文标点、连续空白、大小写。

如果归一化只覆盖了其中几种写法，那第一层的命中率就会随写法漂移 —— 同一
条判据，写成 ``导出， CSV`` 命中、写成 ``导出CSV`` 不命中。这类缺陷不会
报错，只会让第一层时灵时不灵，然后被第二层的模型调用掩盖过去。

所以这里钉的不是某一个具体输出，而是**性质**：

P1 幂等 —— 归一化两次与归一化一次同形。
P2 噪声不敏感 —— 只差全角/半角、中文标点、连续空白、大小写的两个串，
   归一化后同形。
P3 输出只含字母数字 —— 归一化不会把标点或空白留下来当噪声以外的信号。
P4 包含关系对称 —— 归一化之后 ``a ⊂ b`` 与 ``b ⊂ a`` 同真同假，所以判定
   不取决于哪一段是 VP 的声明、哪一段是判据的正文。
P5 不误命中 —— 明显不相关的两段文本不会被包含关系沾上。

全部是纯函数性质，不碰模型、不碰文件系统。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification.topic_relevance import (  # noqa: E402
    RelevanceDecision,
    judge_topic_relevance,
    normalize_criterion_text,
)


#: 只差噪声的等价写法。每组里的串归一化后必须同形。
_EQUIVALENT_GROUPS = [
    # 全角 → 半角
    ("导出 CSV", "导出 ＣＳＶ"),
    ("AC-2", "ＡＣ－２"),
    # 中文标点 → 无
    ("导出，CSV", "导出CSV"),
    ("导出、CSV。", "导出CSV"),
    ("导出（CSV）", "导出CSV"),
    ("导出「CSV」", "导出CSV"),
    ("导出【CSV】", "导出CSV"),
    # 连续空白
    ("导出 CSV", "导出   CSV"),
    ("导出 CSV", "导出\tCSV"),
    ("导出 CSV", "导出\nCSV"),
    # 大小写
    ("导出 CSV", "导出 csv"),
    ("Export CSV", "export csv"),
]


#: 归一化后必须互相包含的文本对（双向包含都成立）。
_CONTAINING_PAIRS = [
    ("导出 CSV", "导出 CSV"),
    ("导出 CSV", "导出 CSV 报表并落盘"),
    ("导出， CSV", "导出CSV"),
    ("导出 ＣＳＶ", "导出 csv 报表"),
    ("状态码 200", "接口返回状态码 200"),
]


#: 明显不相关的文本对：归一化之后不能互相包含。
_UNRELATED_PAIRS = [
    ("导出 CSV", "登录鉴权"),
    ("导出 CSV", "性能压测"),
    ("状态码 200", "数据库迁移"),
    ("导出 CSV", "导出 PDF"),
]


def _judge(vp_text, criterion_text, criteria_id="AC-2"):
    """用一段 VP 声明和一段判据正文跑一次判定，返回那条 VP 的结果。"""
    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": [vp_text]}],
        criteria=[{"id": criteria_id, "text": criterion_text}],
        llm_batch=None,
    )
    return result["VP-1"]


# ---------------------------------------------------------------------------
# P1 幂等
# ---------------------------------------------------------------------------


def test_normalization_is_idempotent():
    for text in ["导出 CSV", "导出， CSV", "ＡＣ－２", "  ", "Export CSV", "状态码 200"]:
        once = normalize_criterion_text(text)
        assert normalize_criterion_text(once) == once


# ---------------------------------------------------------------------------
# P2 噪声不敏感
# ---------------------------------------------------------------------------


def test_noise_only_differences_normalize_to_the_same_text():
    for left, right in _EQUIVALENT_GROUPS:
        assert normalize_criterion_text(left) == normalize_criterion_text(right), (
            left,
            right,
        )


def test_noise_only_differences_produce_the_same_verdict():
    for left, right in _EQUIVALENT_GROUPS:
        left_verdict = _judge(left, "导出 CSV 报表")
        right_verdict = _judge(right, "导出 CSV 报表")
        assert left_verdict.decision == right_verdict.decision, (left, right)


# ---------------------------------------------------------------------------
# P3 输出只含字母数字
# ---------------------------------------------------------------------------


def test_normalized_text_keeps_only_alphanumerics():
    for text in ["导出， CSV！", "  Export\tCSV\n", "【AC-2】导出", "ＡＣ－２"]:
        normalized = normalize_criterion_text(text)
        assert normalized == "".join(ch for ch in normalized if ch.isalnum())
        assert " " not in normalized
        assert "，" not in normalized


def test_cjk_characters_survive_normalization():
    assert normalize_criterion_text("导出 CSV") == "导出csv"


# ---------------------------------------------------------------------------
# P4 包含关系对称
# ---------------------------------------------------------------------------


def test_containment_pairs_are_contained_in_at_least_one_direction():
    """双向包含 = 两个方向都试一遍，命中任意一个方向即可。"""
    for left, right in _CONTAINING_PAIRS:
        left_text = normalize_criterion_text(left)
        right_text = normalize_criterion_text(right)
        assert left_text in right_text or right_text in left_text, (left, right)


def test_containment_verdict_does_not_depend_on_which_side_is_the_declaration():
    for left, right in _CONTAINING_PAIRS:
        as_declaration = _judge(left, right)
        as_criterion = _judge(right, left)
        assert as_declaration.decision == RelevanceDecision.DETERMINISTIC_MATCH
        assert as_criterion.decision == RelevanceDecision.DETERMINISTIC_MATCH
        assert as_declaration.matched_criterion_ids == as_criterion.matched_criterion_ids


# ---------------------------------------------------------------------------
# P5 不误命中
# ---------------------------------------------------------------------------


def test_unrelated_texts_do_not_contain_each_other():
    for left, right in _UNRELATED_PAIRS:
        assert normalize_criterion_text(left) not in normalize_criterion_text(right), (
            left,
            right,
        )
        assert normalize_criterion_text(right) not in normalize_criterion_text(left), (
            left,
            right,
        )


def test_unrelated_texts_fall_through_to_the_llm_tier():
    for left, right in _UNRELATED_PAIRS:
        verdict = _judge(left, right)
        # 没命中确定性层 → 落到第二层；模型不可用 → 兜底。
        assert verdict.decision == RelevanceDecision.LLM_RELEVANT
        assert verdict.tier == "fail_open"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_non_string_input_normalizes_to_empty_string():
    for value in (None, 42, [], {}, object()):
        assert normalize_criterion_text(value) == ""


def test_empty_text_normalizes_to_empty_string():
    assert normalize_criterion_text("") == ""
    assert normalize_criterion_text("   ") == ""
    assert normalize_criterion_text("，、。") == ""