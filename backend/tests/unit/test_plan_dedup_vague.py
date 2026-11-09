"""验证点重复检测：空泛判据标记（2026-11-09, R2）。

契约来源：R2 要求把空泛判据单独标记为 ``vague_criterion``，与重复组一起进
报告。此前**没有任何检查** —— 一条 ``expected_result`` 写「验证通过」的验证点
照常出厂：它没有说明怎么算通过，跑挂了也没人拦。

本文件钉住三道缝：

* **占位词命中** —— 「验证通过」这类只声明"过了就算过"的判据必须被标出来，
  而不是当作一条合格判据放过去。
* **缺失判据命中** —— ``expected_result`` 字段都不写，等于没写判据。
* **描述性判据不误报** —— 「整仓门禁退出码 0，全部测试通过」说清了怎么算
  通过，不该被当成空泛判据。误报比漏报贵：把一条合格判据标成空泛，会让人
  白改一遍。

本模块是**纯函数**（无 IO、无网络），所以判据本身没有"环境不对"这种失败
模式 —— 红灯只可能来自判据写错了。

风格说明：本文件用 ``import plan_dedup`` + 属性访问，而不是模块顶部的
``from plan_dedup import ...`` —— 后者在函数还不存在时会让 pytest **收集
报错**（error），而不是用例**失败**（failure）。TDD 要看的是后者。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import plan_dedup  # noqa: E402


# ---------------------------------------------------------------------------
# 占位词
# ---------------------------------------------------------------------------


def test_placeholder_expected_result_is_vague():
    """「验证通过」是占位词 —— 它没说怎么算通过，必须被标成空泛判据。"""
    points = [
        {"id": "VP-024", "expected_result": "验证通过"},
        {"id": "VP-025", "expected_result": "整仓门禁退出码 0，全部测试通过"},
    ]

    findings = plan_dedup.detect_vague_criteria(points)

    assert len(findings) == 1
    assert findings[0]["id"] == "VP-024"
    assert findings[0]["reason"] == "vague_criterion: placeholder"
    assert findings[0]["expected_result"] == "验证通过"


def test_placeholder_words_are_matched_case_insensitively():
    """占位词转小写后**精确匹配** —— ``OK`` 与 ``ok`` 是同一个占位词。"""
    for text in ("OK", "ok", "Ok"):
        findings = plan_dedup.detect_vague_criteria(
            [{"id": "VP-001", "expected_result": text}]
        )
        assert len(findings) == 1, text
        assert findings[0]["reason"] == "vague_criterion: placeholder", text


def test_placeholder_wins_over_short_length():
    """占位词优先于「太短」 —— 「无」只有一个字，但它是被点名过的占位词。"""
    findings = plan_dedup.detect_vague_criteria(
        [{"id": "VP-002", "expected_result": "无"}]
    )

    assert len(findings) == 1
    assert findings[0]["reason"] == "vague_criterion: placeholder"


# ---------------------------------------------------------------------------
# 缺失 / 太短
# ---------------------------------------------------------------------------


def test_missing_expected_result_is_vague():
    """``expected_result`` 字段都不写 = 没写判据 = 空泛判据。"""
    findings = plan_dedup.detect_vague_criteria([{"id": "VP-026"}])

    assert len(findings) == 1
    assert findings[0]["id"] == "VP-026"
    assert findings[0]["reason"] == "vague_criterion: too_short"
    assert findings[0]["expected_result"] == ""


def test_short_expected_result_is_too_short():
    """归一化后不超过 6 个字 → 太短，说不清怎么算通过。"""
    findings = plan_dedup.detect_vague_criteria(
        [{"id": "VP-027", "expected_result": "全部通过"}]
    )

    assert len(findings) == 1
    assert findings[0]["reason"] == "vague_criterion: too_short"


def test_blank_expected_result_is_too_short():
    """只有空白/标点的判据归一化后为空 —— 同样太空泛。"""
    findings = plan_dedup.detect_vague_criteria(
        [{"id": "VP-028", "expected_result": "   。"}]
    )

    assert len(findings) == 1
    assert findings[0]["reason"] == "vague_criterion: too_short"


# ---------------------------------------------------------------------------
# 不误报
# ---------------------------------------------------------------------------


def test_descriptive_expected_result_is_not_vague():
    """说清了怎么算通过的判据不误报 —— 误报比漏报贵。"""
    points = [
        {"id": "VP-025", "expected_result": "整仓门禁退出码 0，全部测试通过"},
    ]

    assert plan_dedup.detect_vague_criteria(points) == []


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_non_dict_entries_are_skipped_without_raising():
    """非 dict 条目跳过，不抛异常 —— 判据跑在别人写的 JSON 上。"""
    points = [
        "not-a-point",
        None,
        42,
        {"id": "VP-001", "expected_result": "验证通过"},
    ]

    findings = plan_dedup.detect_vague_criteria(points)

    assert len(findings) == 1
    assert findings[0]["id"] == "VP-001"


def test_empty_list_returns_no_findings():
    assert plan_dedup.detect_vague_criteria([]) == []


def test_entries_without_id_are_skipped():
    """没有 id 的条目无法在报告里定位 —— 跳过，不抛异常。"""
    points = [{"expected_result": "验证通过"}]

    assert plan_dedup.detect_vague_criteria(points) == []


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_strips_whitespace_and_punctuation():
    """归一化去掉空白与标点 —— 「写法不同、说的一样」要合成一个。"""
    assert (
        plan_dedup.normalize_expected_result(" 整仓门禁退出码 0，全部测试通过 ")
        == "整仓门禁退出码0全部测试通过"
    )


def test_normalize_of_non_string_is_empty():
    """非字符串（含 ``None``、缺失）归一化后为空 —— 判据不存在。"""
    assert plan_dedup.normalize_expected_result(None) == ""
    assert plan_dedup.normalize_expected_result(42) == ""