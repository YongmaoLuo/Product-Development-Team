"""验证点重复检测：R5「不得误伤」三条用例的回归锁（2026-11-09, R5）。

契约来源：R5 —— **误伤比漏报贵**。把两条其实不同的验证点判成重复，会把一条
该跑的关卡删掉（或者把两条真实验证点推给人工处置）。R5 点名三条不得判为重复：

* **命令相同但覆盖面明显不同、且标题不同**（同一入口脚本用于两个不同场景）——
  这同时是一条**实现约束**：命令判据不得把这样的两条成组；
* **只有 ``expected_result`` 相同**、命令与覆盖面都不同；
* **``covers_paths`` 为空**的两条之间的「标题相似」判定（空覆盖面不足以支撑
  判定）。

本文件钉死这三条，外加两道缝：

* **一侧覆盖面为空 → 例外不触发**（仍按命令判重）—— 空覆盖面不足以支撑任何
  判定，与 R5 第 3 条同一条道理；
* **命令相同 + 覆盖面相同 → 仍判重复** —— 防止有人把实现改成「永不报重复」
  来让这三条变绿。

另有一组用例钉住例外的**判定口径**（例外按对判定、同一片代码里的兄弟模块
不排除、标题相同不排除）—— 这几道缝是"例外收紧"与"例外放宽"两头都要拦的
方向。

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
    detect_vague_criteria,
)

# ---------------------------------------------------------------------------
# 素材
# ---------------------------------------------------------------------------

#: 同一份命令与验证方法，两条各跑各的场景。
SAME_CMD = {"verification_method": "full_ci", "ci_entry": "bash scripts/ci_gate.sh"}

#: R5 第 1 条：同一入口脚本用于两个不同场景。
PAIR_A = [
    dict(SAME_CMD, id="VP-1", title="后端整仓门禁", covers_paths=["backend/"]),
    dict(SAME_CMD, id="VP-2", title="前端整仓门禁", covers_paths=["frontend/"]),
]

#: R5 第 2 条：只有 ``expected_result`` 相同，命令与覆盖面都不同。
PAIR_B = [
    {
        "id": "VP-3",
        "expected_result": "接口返回 200 且字段完整",
        "covers_paths": ["backend/api/"],
        "verification_method": "api_test",
        "ci_entry": "pytest a",
    },
    {
        "id": "VP-4",
        "expected_result": "接口返回 200 且字段完整",
        "covers_paths": ["backend/web/"],
        "verification_method": "e2e",
    },
]

#: R5 第 3 条：覆盖面为空，标题互为子串。
PAIR_C = [
    {
        "id": "VP-5",
        "title": "验证登录流程",
        "covers_paths": [],
        "verification_method": "api_test",
    },
    {
        "id": "VP-6",
        "title": "验证登录流程完整",
        "covers_paths": [],
        "verification_method": "api_test",
    },
]


# ---------------------------------------------------------------------------
# R5 三条误伤用例
# ---------------------------------------------------------------------------


def test_same_entry_different_scope_and_title_is_not_duplicate():
    """R5 第 1 条：命令相同、覆盖面不相交、标题不同 → 不得成组。

    同一入口脚本（``bash scripts/ci_gate.sh``）用于后端与前端两个不同场景 ——
    判成重复等于让操作员删掉其中一条关卡。
    """
    assert detect_duplicate_points(PAIR_A) == []


def test_same_expected_result_only_is_not_duplicate():
    """R5 第 2 条：只有 ``expected_result`` 相同 → 不是重复，也不是空泛判据。

    ``expected_result`` 相同说明"两边都这么说"，不说明"两边是同一件事" ——
    命令不同、覆盖面不同、方法不同，就是两件事。判据写得够具体（不是
    「验证通过」那种空话），也不该被标成 vague。
    """
    assert detect_duplicate_points(PAIR_B) == []
    assert detect_vague_criteria(PAIR_B) == []


def test_empty_covers_title_similarity_is_not_duplicate():
    """R5 第 3 条：覆盖面为空 → 标题相似不构成判据。

    两条都没声明范围，"验证登录流程" 与 "验证登录流程完整" 像得像，但空覆盖面
    不足以支撑判定。
    """
    assert detect_duplicate_points(PAIR_C) == []


# ---------------------------------------------------------------------------
# R5 例外**按对**判定：被排除的那条不进组
# ---------------------------------------------------------------------------


def test_excepted_point_is_not_pulled_into_a_duplicate_group():
    """桶里既有真重复、又有被 R5 排除的 → 组只收真重复，被排除的不进组。

    A、B 跑同一条命令、覆盖同一片代码 —— 是一对**真重复**；C 跑同一条命令，
    但覆盖的是另一片代码、说的是另一件事 —— R5 排除它。例外**按对**判定，
    所以 C 不能因为"跟 A、B 在同一个命令桶里"被顺带拉进组。
    """
    points = [
        {
            "id": "VP-11",
            "title": "整仓门禁",
            "covers_paths": ["backend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
        {
            "id": "VP-12",
            "title": "整仓门禁",
            "covers_paths": ["backend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
        {
            "id": "VP-13",
            "title": "前端整仓门禁",
            "covers_paths": ["frontend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-11", "VP-12"]


def test_same_area_paths_still_group_by_command():
    """同一个父目录下的两个模块（``backend/auth`` 与 ``backend/user``）→ 仍判重复。

    它们是**同一片代码**里的两个模块，不是"明显不同的两件事"：拿"覆盖面不相交"
    证明它们是两件事，证据不足（仓库里同一条命令同时覆盖多个模块是常见写法）。
    """
    points = [
        {
            "id": "VP-14",
            "title": "验证登录流程",
            "covers_paths": ["backend/auth"],
            "verification_method": "api_test",
            "ci_entry": "pytest a",
        },
        {
            "id": "VP-15",
            "title": "验证注册流程",
            "covers_paths": ["backend/user"],
            "verification_method": "api_test",
            "ci_entry": "pytest a",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-14", "VP-15"]


def test_nested_scope_and_its_parent_are_not_duplicate():
    """一个目录与它里面的子目录（``backend/`` 与 ``backend/api/``）→ 不是重复。

    覆盖面归一化后是两条不同的路径 → 不相交；两者也不是同一个父目录下的兄弟
    目录 → 明显冲突 → R5 例外触发，不因命令相同成组。
    """
    points = [
        {
            "id": "VP-16",
            "title": "甲门禁",
            "covers_paths": ["backend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
        {
            "id": "VP-17",
            "title": "乙门禁",
            "covers_paths": ["backend/api/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
    ]

    assert detect_duplicate_points(points) == []


def test_same_title_different_scope_still_groups():
    """标题**相同**、覆盖面明显不同 → 仍判重复（例外要标题也不同才触发）。

    两条都叫「整仓门禁」，那就是同一个门禁被写了两遍 —— 哪怕一条写的是后端、
    一条写的是前端，标题本身已经说明它们指的是同一件事。
    """
    points = [
        {
            "id": "VP-18",
            "title": "整仓门禁",
            "covers_paths": ["backend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
        {
            "id": "VP-19",
            "title": "整仓门禁",
            "covers_paths": ["frontend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-18", "VP-19"]


# ---------------------------------------------------------------------------
# 两道缝
# ---------------------------------------------------------------------------


def test_one_side_empty_covers_still_groups_by_command():
    """一侧覆盖面为空 → R5 例外**不触发**，仍按命令判重。

    空覆盖面不足以支撑判定（与 R5 第 3 条同一条道理）：不能因为其中一条没写
    ``covers_paths`` 就放过一对同命令的验证点。
    """
    points = [
        {
            "id": "VP-7",
            "title": "后端整仓门禁",
            "covers_paths": ["backend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
        {
            "id": "VP-8",
            "title": "前端整仓门禁",
            "covers_paths": [],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-7", "VP-8"]


def test_same_command_and_same_scope_still_groups():
    """命令相同 + 覆盖面相同 → 仍判重复（例外不能把判据废掉）。

    这是防「永不报重复」的那道缝：R5 例外只排除"明显不同"的两条。
    """
    points = [
        {
            "id": "VP-9",
            "title": "整仓门禁",
            "covers_paths": ["backend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
        {
            "id": "VP-10",
            "title": "整仓门禁",
            "covers_paths": ["backend/"],
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gate.sh",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-9", "VP-10"]
