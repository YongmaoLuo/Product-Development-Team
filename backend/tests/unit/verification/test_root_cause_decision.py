"""TDD spec — 根因任务决策（``needs_root_cause_task``）。

背景
----
旧键 ``VP-x|分类`` 只回答「同一失败又重复了一次」。它答不了「根因有没有
演进」：两次失败分类相同（都是 ``deliverable_defect``），一次断在 JSON 路径、
一次断在状态码 —— 计数说「同一种失败」，根因其实换了。于是循环继续生成第 3、
第 4 个修复任务，每个都打在已经修好的地方。

失败指纹（``failure_fingerprint``）给出的答案是「根因有没有变」。这个模块的
纯函数把「指纹变了」翻译成一个**决定**：这一轮要不要先做根因任务，而不是
再生成一个盲目的修复任务。

决策的形状
----------
``needs_root_cause_task(history)`` 读四个字段：

``vp_id``
    验证点 id（本函数不据此分支，只是随记录一起传进来）。
``consecutive_failures``
    这个失败已经连续重复了几轮（首次失败那轮记 1）。计数单位是**轮数**。
``fp_prev`` / ``fp_curr``
    上一轮与本轮该验证点失败的指纹。

真值表（9 种组合）
------------------
只有一种组合为真：**连续失败 2 轮，且两个指纹都存在并且不同**。

* 连续失败 1 轮 → 假。第一次失败还没有「重复」可言，谈不上演进。
* 连续失败 2 轮、指纹相同 → 假。这是**同一个**失败，交给旧键（重复计数）
  处理 —— 新分支不抢旧键的活。
* 连续失败 2 轮、指纹不同 → **真**。这就是根因演进的那一轮。
* 连续失败 3 轮及以上 → 假。计数已经到了循环的硬上限（``max_rounds`` 之外
  的同因阈值），那条路径（语义分诊）接管这个失败；新分支只负责「从一次重复
  变成另一次重复」这个转折点，抢在它前面只会让两个分支互相覆盖。

缺键 → 假（fail-open）
----------------------
少一个字段不是「这个字段是空的」，而是「我们不知道」。旧 payload 里没有
指纹，于是这个决定只能是假：旧路径照旧走。**绝不能**因为少一个键就抛
``KeyError`` —— 一个读不懂的记录必须降级，不能让一轮验证整个炸掉。

TDD contract — 3 cases
-----------------------
1. ``test_truth_table_9_combos`` —— 9 种组合，只有 ``(2, 不同)`` 为真。
2. ``test_missing_keys_fail_open`` —— 缺键不抛错，答案是假。
3. ``test_documented_example`` —— 任务书给的那条输入为真。

边界条件
--------
* 纯函数：不读文件、不看时钟，同样的输入永远给同样的答案。
* 非 mapping 输入 → 假（不抛错）。
* 指纹比较是精确比较：两个不同的非空字符串才算演进。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.failure_fingerprint import needs_root_cause_task  # noqa: E402


# ---------------------------------------------------------------------------
# 真值表
# ---------------------------------------------------------------------------
#
#: ``(连续失败轮数, fp_prev, fp_curr, 期望)``。
#:
#: 轮数三档（1 / 2 / 3）× 指纹三态（相同 / 不同 / 缺失）= 9 种组合。
#: 只有 ``(2, 不同)`` 为真 —— 其余八种都是假。
_TRUTH_TABLE = (
    # --- 连续失败 1 轮：还没有「重复」，谈不上演进 ---
    (1, "aaa", "aaa", False),
    (1, "aaa", "bbb", False),
    (1, None, None, False),
    # --- 连续失败 2 轮：分叉点 ---
    (2, "aaa", "aaa", False),   # 同一个失败 —— 交旧键
    (2, "aaa", "bbb", True),    # 根因演进 —— 根因任务
    (2, None, None, False),     # 指纹缺失 —— fail-open
    # --- 连续失败 3 轮：同因阈值已到，那条路径接管 ---
    (3, "aaa", "aaa", False),
    (3, "aaa", "bbb", False),
    (3, None, None, False),
)


def _history(rounds, fp_prev, fp_curr, vp_id="VP-3"):
    """The decision record, in the shape the orchestrator builds it."""
    return {
        "vp_id": vp_id,
        "consecutive_failures": rounds,
        "fp_prev": fp_prev,
        "fp_curr": fp_curr,
    }


# ---------------------------------------------------------------------------
# 1. 真值表
# ---------------------------------------------------------------------------

def test_truth_table_9_combos():
    """9 种组合，只有「连续失败 2 轮且指纹不同」为真。"""
    assert len(_TRUTH_TABLE) == 9, "the truth table must cover 9 combinations"

    for rounds, fp_prev, fp_curr, expected in _TRUTH_TABLE:
        history = _history(rounds, fp_prev, fp_curr)
        actual = needs_root_cause_task(history)
        assert actual is expected, (
            f"({rounds}, {fp_prev!r}, {fp_curr!r}) -> {actual}, "
            f"expected {expected}"
        )

    truthy = [
        (rounds, fp_prev, fp_curr)
        for rounds, fp_prev, fp_curr, expected in _TRUTH_TABLE
        if expected
    ]
    assert truthy == [(2, "aaa", "bbb")], (
        "exactly one combination may be true: the round on which a failure "
        "repeats for the second time with a different fingerprint"
    )


def test_documented_example():
    """The task book's example is the one true combination."""
    history = {
        "vp_id": "VP-3",
        "consecutive_failures": 2,
        "fp_prev": "aaa",
        "fp_curr": "bbb",
    }
    assert needs_root_cause_task(history) is True


# ---------------------------------------------------------------------------
# 2. 缺键 fail-open
# ---------------------------------------------------------------------------

def test_missing_keys_fail_open():
    """A record without the new keys degrades to False — never a KeyError."""
    # 旧 payload：只有旧键，没有指纹。
    legacy = {"vp_id": "VP-3", "consecutive_failures": 2}
    assert needs_root_cause_task(legacy) is False

    # 一个键都没有。
    assert needs_root_cause_task({}) is False

    # 指纹只有一半 —— 同样是「我们不知道」。
    assert needs_root_cause_task(
        {"consecutive_failures": 2, "fp_prev": "aaa"}
    ) is False
    assert needs_root_cause_task(
        {"consecutive_failures": 2, "fp_curr": "bbb"}
    ) is False

    # 计数不是整数 —— 不是「轮数」，因此不是一次重复。
    assert needs_root_cause_task(
        {"consecutive_failures": "2", "fp_prev": "aaa", "fp_curr": "bbb"}
    ) is False
    assert needs_root_cause_task(
        {"consecutive_failures": None, "fp_prev": "aaa", "fp_curr": "bbb"}
    ) is False


def test_non_mapping_input_is_false():
    """A caller that hands over the wrong shape gets False, not a crash."""
    for value in (None, [], "VP-3", 2, object()):
        assert needs_root_cause_task(value) is False


# ---------------------------------------------------------------------------
# 3. 纯函数
# ---------------------------------------------------------------------------

def test_pure_function_same_input_same_answer():
    """The decision reads only its argument — no clock, no file, no state."""
    history = _history(2, "aaa", "bbb")
    first = needs_root_cause_task(history)
    history["extra"] = "ignored"
    assert needs_root_cause_task(history) is first


# ---------------------------------------------------------------------------
# 4. 两条规则算出来的指纹不能拿来比
# ---------------------------------------------------------------------------
#
# 「根因演进」这个结论,是拿上一轮和本轮的两个指纹**比**出来的。指纹是 16 位
# hex,值里装不下「这是哪条规则算的」(形状是契约),所以规则得跟着指纹一起
# 被记下来,并且在决定之前被读出来。
#
# 两条不同的规则给出两个不同的值,哪怕底下是**同一个**失败。旧五段式量的是
# 「这一轮跑成什么样」,身份态量的是「哪个缺陷」。把这两个值放在一起比,读到
# 的「根因演进」是**量具换了**,不是缺陷变了 —— 循环于是每轮重开一个根因任
# 务,每轮重新描述同一批失败,这条链结构上不可能收敛。
#
# 所以:规则不一样的两个指纹**不可比**。不可比就不下结论(返回假),把这一轮
# 交给修复任务,而不是再开一个根因任务。

#: 旧五段式规则(带运行噪声屏蔽)与身份态规则的 id。
_LEGACY_RULE = "legacy-v2"
_IDENTITY_RULE = "identity-v1"


def _history_with_rules(rounds, fp_prev, fp_curr, rule_prev=None, rule_curr=None):
    """The decision record, with the rules the two fingerprints were made under."""
    history = _history(rounds, fp_prev, fp_curr)
    if rule_prev is not None:
        history["rule_prev"] = rule_prev
    if rule_curr is not None:
        history["rule_curr"] = rule_curr
    return history


def test_fingerprints_from_different_rules_are_not_compared():
    """规则不一样 -> 不可比 -> 不下「根因演进」的结论。"""
    history = _history_with_rules(
        2, "0ceab0d5d41cc058", "1251be5eba9e3a92",
        rule_prev=_LEGACY_RULE, rule_curr=_IDENTITY_RULE,
    )
    assert needs_root_cause_task(history) is False, (
        "two fingerprints computed under different rules are not evidence "
        "that the root cause evolved — the instrument changed, not the "
        "defect"
    )


def test_the_same_rule_still_reports_evolution():
    """同一条规则下,指纹不同仍然是演进 —— 旧判据不许被削弱。"""
    assert needs_root_cause_task(
        _history_with_rules(
            2, "1251be5eba9e3a92", "d2416ecf3d6a4c93",
            rule_prev=_IDENTITY_RULE, rule_curr=_IDENTITY_RULE,
        )
    ) is True
    assert needs_root_cause_task(
        _history_with_rules(
            2, "aaa", "aaa", rule_prev=_LEGACY_RULE, rule_curr=_LEGACY_RULE,
        )
    ) is False


def test_one_unknown_rule_is_not_comparable():
    """只有一边知道规则 -> 不可比(我们不知道另一边是怎么算的)。"""
    assert needs_root_cause_task(
        _history_with_rules(
            2, "0ceab0d5d41cc058", "1251be5eba9e3a92", rule_curr=_IDENTITY_RULE,
        )
    ) is False
    assert needs_root_cause_task(
        _history_with_rules(
            2, "0ceab0d5d41cc058", "1251be5eba9e3a92", rule_prev=_LEGACY_RULE,
        )
    ) is False


def test_two_unknown_rules_keep_the_old_comparison():
    """两边都不知道规则 -> 回到旧比较(历史记录一条也不能作废)。"""
    assert needs_root_cause_task(_history(2, "aaa", "bbb")) is True
    assert needs_root_cause_task(_history(2, "aaa", "aaa")) is False


def test_a_malformed_rule_is_unknown():
    """读不懂的规则 = 不知道规则,不是崩掉。"""
    for bad in ("", 3, [], {}):
        assert needs_root_cause_task(
            _history_with_rules(
                2, "aaa", "bbb", rule_prev=bad, rule_curr=_IDENTITY_RULE,
            )
        ) is False
        assert needs_root_cause_task(
            _history_with_rules(
                2, "aaa", "bbb", rule_prev=_IDENTITY_RULE, rule_curr=bad,
            )
        ) is False
