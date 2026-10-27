"""The effect report's metric set is a frozen contract, not a suggestion.

Why this gate exists
--------------------
收敛优化的效果指标被当成「**测出来给人看**」的东西，不是门禁。这带来一个
具体风险：指标名一旦可以随手增删，两张报表之间的比较就失去意义 —— 基线里
有 7 项、这一轮只有 6 项，缺的那一项不是「没测」，而是「没人记得它叫什么」，
读者无从判断该补测还是该忽略。

所以指标名集合是常量，且常量本身被这份门禁逐字钉住：新增或删除任何一项，
都必须显式改这份测试。改测试的那一次提交，就是「指标集合确实变了」的记录。

What it pins
------------
1. :data:`effect_report.METRIC_NAMES` 与本文件里的字面量**逐字相等** ——
   顺序也是契约（报表按这个顺序出表）。
2. :data:`effect_report.ESCAPE_SIGNALS` 同样是逐字钉死的三类。
3. 指标名互不重复 —— 重复的名字会让报表里两行同名，读者无从分辨。
4. 每个指标名都是可读的标识符（不是空串、不是带空格的口语）。

What it does not check
----------------------
指标**怎么算**。那是单元测试的事；这份门禁只管名字，因为名字是跨轮次
比较的锚点，而算法是每一轮都可以改的。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import effect_report  # noqa: E402


#: The frozen metric set, in report order. Changing a name here is the
#: explicit signal that the metric set changed — do it in the same commit
#: that changes the module, never silently.
EXPECTED_METRIC_NAMES: tuple[str, ...] = (
    "pdt_plan_tokens",
    "per_plan_tokens",
    "repair_ratio",
    "exec_vs_verify",
    "round_median",
    "round_p90",
    "unattributed_ratio",
)

#: The three escape signals. These are the counterweight to the metrics:
#: a report that shows "the numbers look fine" while defects keep escaping
#: is exactly the failure mode this set exists to make visible.
EXPECTED_ESCAPE_SIGNALS: tuple[str, ...] = (
    "later_plan_failures",
    "post_merge_reverts",
    "targeted_fix_commits",
)


def test_metric_names_frozen() -> None:
    """``METRIC_NAMES`` 与本文件的字面量逐字相等（含顺序）。"""
    assert effect_report.METRIC_NAMES == EXPECTED_METRIC_NAMES, (
        "the effect report's metric set is a frozen contract: the baseline "
        "and the next report are compared metric by metric, so a name that "
        "changes silently makes the two incomparable. Add or remove a "
        "metric by changing EXPECTED_METRIC_NAMES here in the same commit."
    )


def test_escape_signals_frozen() -> None:
    """``ESCAPE_SIGNALS`` 与本文件的字面量逐字相等（含顺序）。"""
    assert effect_report.ESCAPE_SIGNALS == EXPECTED_ESCAPE_SIGNALS, (
        "the escape signals are the counterweight to the metrics and are "
        "frozen for the same reason: changing one silently would let a "
        "regression hide behind a renamed signal."
    )


def test_metric_names_are_unique() -> None:
    """重复的名字会让报表里出现两行同名，读者无从分辨。"""
    assert len(set(effect_report.METRIC_NAMES)) == len(effect_report.METRIC_NAMES), (
        f"duplicate metric names: {effect_report.METRIC_NAMES!r}"
    )
    assert (
        len(set(effect_report.ESCAPE_SIGNALS)) == len(effect_report.ESCAPE_SIGNALS)
    ), f"duplicate escape signals: {effect_report.ESCAPE_SIGNALS!r}"


def test_metric_names_are_readable_identifiers() -> None:
    """每个名字都得像个标识符：非空、无空格、不是纯数字。"""
    for name in effect_report.METRIC_NAMES + effect_report.ESCAPE_SIGNALS:
        assert name, "a metric name must not be empty"
        assert " " not in name, f"metric name {name!r} contains a space"
        assert not name.isdigit(), f"metric name {name!r} is all digits"


def test_the_metric_set_has_seven_entries() -> None:
    """指标数量本身是契约的一部分 —— 少一项或多一项都要显式改这里。"""
    assert len(effect_report.METRIC_NAMES) == 7, (
        f"expected 7 metrics, got {len(effect_report.METRIC_NAMES)}: "
        f"{effect_report.METRIC_NAMES!r}"
    )
    assert len(effect_report.ESCAPE_SIGNALS) == 3, (
        f"expected 3 escape signals, got {len(effect_report.ESCAPE_SIGNALS)}: "
        f"{effect_report.ESCAPE_SIGNALS!r}"
    )
