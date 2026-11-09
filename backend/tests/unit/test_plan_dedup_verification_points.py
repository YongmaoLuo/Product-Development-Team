"""验证点重复检测：覆盖面 + 方法 + 标题相似判据（2026-11-08, R1 判据 2）。

契约来源：R1 第二条判据 —— ``covers_paths`` 集合相同（非空）且方法相同，
标题归一化后相同或互为子串 → 判为重复；且**同一验证点不得出现在两个组里**。

任务 1 只实现了「命令相同」这一条；本文件补上第二条，并钉住三道缝：

* **覆盖面相同 + 方法相同** —— 两条验证点跑的命令不同（判据 1 抓不到），
  但它们验的是同一片代码、同一种方式，就是同一件事被写了两遍。
* **覆盖面为空 → 判据 2 不参与**（R5 第 3 条）：空覆盖面不足以支撑判定，
  标题再像也不能判重。
* **组间互斥** —— 已进入命令组的 id 不再进第二个组。一个 id 出现在两个组里，
  意味着「删掉这一条」会得到两种不同的处置结果，读报告的人无从下手。

本模块是**纯函数**（无 IO、无网络），所以判据本身没有"环境不对"这种失败
模式 —— 红灯只可能来自判据写错了。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_dedup import (  # noqa: E402
    detect_duplicate_points,
    method_of,
    normalize_covers,
    normalize_title,
)


# ---------------------------------------------------------------------------
# 判据 2：覆盖面相同 + 方法相同 + 标题相似
# ---------------------------------------------------------------------------


def test_same_covers_and_method_forms_group():
    """覆盖面（归一化后）相同 + 方法相同 + 标题说的是同一件事 → 一个组。

    标题是同一句话的两种写法（``验证登录流程`` / ``登录流程验证``）——
    中文标题的语序会飘，只认"逐字相同"会漏掉最常见的一类重复。
    """
    points = [
        {
            "id": "VP-003",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth/"],
            "verification_method": "api_test",
        },
        {
            "id": "VP-004",
            "title": "登录流程验证",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-003", "VP-004"]
    assert groups[0]["reason"] == (
        "same_covers_and_method: covers_paths=['backend/auth'] method=api_test"
    )


def test_same_covers_different_method_does_not_group():
    """方法不同 → 不是同一件事（同一片代码，验法不一样）。"""
    points = [
        {
            "id": "VP-010",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
        },
        {
            "id": "VP-011",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth"],
            "verification_method": "e2e",
        },
    ]

    assert detect_duplicate_points(points) == []


def test_same_method_different_covers_does_not_group():
    """覆盖面不同 → 不是同一件事（覆盖面是判据 2 的第一道闸）。"""
    points = [
        {
            "id": "VP-012",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
        },
        {
            "id": "VP-013",
            "title": "验证登录流程",
            "covers_paths": ["backend/user"],
            "verification_method": "api_test",
        },
    ]

    assert detect_duplicate_points(points) == []


def test_title_substring_similarity_groups():
    """标题互为子串也算同一件事（归一化后）。"""
    points = [
        {
            "id": "VP-020",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
        },
        {
            "id": "VP-021",
            "title": "验证登录流程完整",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-020", "VP-021"]


def test_empty_covers_never_groups_by_title():
    """覆盖面为空 → 判据 2 不参与（R5 第 3 条）：标题再像也不判重。

    两条标题互为子串，但都没有声明覆盖面 —— 空覆盖面不足以支撑判定。
    """
    points = [
        {
            "id": "VP-030",
            "title": "验证登录流程",
            "covers_paths": [],
            "verification_method": "api_test",
        },
        {
            "id": "VP-031",
            "title": "验证登录流程完整",
            "covers_paths": [],
            "verification_method": "api_test",
        },
    ]

    assert detect_duplicate_points(points) == []


def test_missing_covers_never_groups_by_title():
    """``covers_paths`` 整个键都没有 → 与空列表同义，不参与判据 2。"""
    points = [
        {
            "id": "VP-032",
            "title": "验证登录流程",
            "verification_method": "api_test",
        },
        {
            "id": "VP-033",
            "title": "验证登录流程完整",
            "verification_method": "api_test",
        },
    ]

    assert detect_duplicate_points(points) == []


def test_member_ids_are_unique_across_groups():
    """同一验证点不得出现在两个组里 —— 命令组优先，判据 2 不再收它。"""
    points = [
        {
            "id": "VP-040",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
            "ci_entry": "pytest a",
        },
        {
            "id": "VP-041",
            "title": "登录流程验证",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
            "ci_entry": "pytest a",
        },
        {
            "id": "VP-042",
            "title": "验证注册流程",
            "covers_paths": ["backend/user"],
            "verification_method": "api_test",
            "ci_entry": "pytest a",
        },
    ]

    groups = detect_duplicate_points(points)

    all_ids = [pid for group in groups for pid in group["member_ids"]]
    assert len(all_ids) == len(set(all_ids)), f"同一个 id 出现在多个组里：{groups}"
    assert [group["member_ids"] for group in groups] == [
        ["VP-040", "VP-041", "VP-042"]
    ]


def test_groups_are_ordered_by_first_member_position():
    """组按首个成员在输入中的位置排序 —— 报告读起来跟输入顺序一致。"""
    points = [
        {
            "id": "VP-050",
            "title": "验证注册流程",
            "covers_paths": ["backend/user"],
            "verification_method": "api_test",
            "ci_entry": "pytest b",
        },
        {
            "id": "VP-051",
            "title": "验证注册流程完整",
            "covers_paths": ["backend/user"],
            "verification_method": "api_test",
            "ci_entry": "pytest c",
        },
        {
            "id": "VP-052",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
            "ci_entry": "pytest a",
        },
        {
            "id": "VP-053",
            "title": "登录流程验证",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
            "ci_entry": "pytest d",
        },
    ]

    groups = detect_duplicate_points(points)

    assert [group["member_ids"] for group in groups] == [
        ["VP-050", "VP-051"],
        ["VP-052", "VP-053"],
    ]


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------


def test_normalize_title_strips_whitespace_and_punctuation():
    """去空白与标点后小写。"""
    assert normalize_title("  Login Flow, Please!  ") == "loginflowplease"


def test_normalize_title_keeps_chinese_characters():
    """中文标点也算标点。"""
    assert normalize_title("【全量】Nightly CI。") == "全量nightlyci"


def test_normalize_title_of_blank_is_empty():
    """只剩空白或标点 → 归一化后为空 → 不做标题判定。"""
    assert normalize_title("   ") == ""
    assert normalize_title("【】，。 ") == ""
    assert normalize_title(None) == ""


# ---------------------------------------------------------------------------
# normalize_covers
# ---------------------------------------------------------------------------


def test_normalize_covers_normalises_paths():
    """去首尾空白、反斜杠转 ``/``、去尾部 ``/``。"""
    covers = normalize_covers(
        {"covers_paths": [" backend/auth/ ", "backend\\core\\", "backend/api/"]}
    )

    assert covers == frozenset({"backend/auth", "backend/core", "backend/api"})


def test_normalize_covers_of_empty_is_empty():
    """没有覆盖面 → 空集合（判据 2 不参与）。"""
    assert normalize_covers({}) == frozenset()
    assert normalize_covers({"covers_paths": []}) == frozenset()
    assert normalize_covers({"covers_paths": ["  ", "/"]}) == frozenset()


# ---------------------------------------------------------------------------
# method_of
# ---------------------------------------------------------------------------


def test_method_of_prefers_verification_method():
    """方法读 ``verification_method``。"""
    assert method_of({"verification_method": "api_test"}) == "api_test"


def test_method_of_falls_back_to_method():
    """``verification_method`` 缺失（或为空）→ 回落 ``method``。"""
    assert method_of({"method": "e2e"}) == "e2e"
    assert method_of({"verification_method": "  ", "method": "e2e"}) == "e2e"


def test_method_of_missing_is_empty():
    assert method_of({}) == ""
