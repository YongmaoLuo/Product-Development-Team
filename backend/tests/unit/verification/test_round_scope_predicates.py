"""TDD spec — verification_round_scope 的轮次作用域判定纯函数。

背景
----
verdict 是一次复核的**结论**，它对"被复核的那份代码"负责，不对"某一次运行"
负责。执行器按 ``vp_id`` 查历史 verdict 时没有轮次作用域，于是上一轮 PASSED
的验证点在这一轮被当成"已经跑过"直接跳过（实测 27 个 VP 只跑了 7 个）。

复用不是不能做，但**必须说得出凭什么**。本文件钉住那套"凭什么"：四条判据
（上一轮 PASSED / 声明了范围 / 变更集与范围不相交 / 变更集不碰共享面）、
范围前缀的归一化口径、共享面清单、以及变更集的解析与合并。

全部是纯函数：不读环境变量、不发网络请求、不调模型、不读文件。

TDD contract — 5 条主线 + 若干补充
----------------------------------
1. ``test_may_reuse_requires_all_four`` — 四条判据全真才复用；任一条为假
   都必须重跑。
2. ``test_empty_covers_always_rerun`` — 没有 ``covers_paths`` 的 VP 每轮重跑。
3. ``test_shared_surface_blocks_reuse`` — 共享面命中 → 全量重跑。
4. ``test_prefix_normalisation_and_dedup`` — 前缀归一化（尾斜杠、反斜杠）
   之后去重且保序。
5. ``test_shared_surface_list_matches_exact_files_and_prefixes`` — 共享面
   清单逐字固定；目录项用 ``startswith``、文件项用相等匹配。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification_round_scope import (  # noqa: E402
    SHARED_SURFACE_PREFIXES,
    covers_paths_for_files,
    is_shared_surface,
    may_reuse,
    merge_change_sets,
    normalise_prefix,
    parse_porcelain,
)


# ---------------------------------------------------------------------------
# 1. 四条复用判据
# ---------------------------------------------------------------------------


def test_may_reuse_requires_all_four():
    """四条件全真 → True；任一条件为假 → False。"""
    # 全真：上轮 PASSED、有范围、变更集与范围不相交、没有共享面。
    assert may_reuse("PASSED", ("backend/verification/",), ("backend/server.py",), False) is True

    # 判据 1：上轮不是 PASSED。
    assert may_reuse("FAILED", ("backend/verification/",), ("backend/server.py",), False) is False
    # 判据 2：没有范围。
    assert may_reuse("PASSED", (), ("backend/server.py",), False) is False
    # 判据 3：变更集落在范围里。
    assert (
        may_reuse("PASSED", ("backend/verification/",), ("backend/verification/orchestrator.py",), False)
        is False
    )
    # 判据 4：共享面命中。
    assert may_reuse("PASSED", ("backend/verification/",), ("backend/server.py",), True) is False


def test_empty_covers_always_rerun():
    """``covers_paths`` 为空 → 恒 False（哪怕其它三条全真）。"""
    for covers in ((), [], None):
        assert may_reuse("PASSED", covers, (), False) is False


def test_shared_surface_blocks_reuse():
    """``shared_hit=True`` → False；变更集里含共享面文件 → 同样 False。"""
    assert may_reuse("PASSED", ("backend/verification/",), ("backend/server.py",), True) is False

    shared_file = "backend/prompts.py"
    assert is_shared_surface(shared_file) is True
    assert may_reuse("PASSED", ("backend/verification/",), (shared_file,), False) is False


def test_prefix_normalisation_and_dedup():
    """前缀归一化：尾斜杠去掉、反斜杠转正斜杠，然后去重且保序。"""
    # 单个前缀
    assert normalise_prefix("backend/verification/") == "backend/verification"
    assert normalise_prefix("backend\\verification\\") == "backend/verification"

    # 一组前缀：混合写法归一化之后折叠成一个
    assert normalise_prefix(
        ["backend/verification/", "backend\\verification", "backend/verification"]
    ) == ("backend/verification",)

    # 去重保序：先出现的排前面
    assert normalise_prefix(["b/", "a/", "b"]) == ("b", "a")

    # 文件清单派生的范围同样归一化去重
    assert covers_paths_for_files(
        [
            "backend/verification/a.py",
            "backend\\verification\\b.py",
            "backend/verification/c.py",
        ]
    ) == ("backend/verification",)


def test_shared_surface_list_matches_exact_files_and_prefixes():
    """共享面清单逐字固定；目录项 startswith、文件项相等。"""
    assert SHARED_SURFACE_PREFIXES == (
        "backend/state_machine/",
        "backend/phase_vocabulary.py",
        "backend/prompts.py",
        "scripts/run_tests.sh",
        "scripts/ci_gha.sh",
        ".github/workflows/",
    )

    # 文件项：相等命中
    assert is_shared_surface("scripts/run_tests.sh") is True
    assert is_shared_surface("scripts/ci_gha.sh") is True
    assert is_shared_surface("backend/phase_vocabulary.py") is True
    assert is_shared_surface("backend/prompts.py") is True

    # 目录项：前缀命中
    assert is_shared_surface("backend/state_machine/db/schema.py") is True
    assert is_shared_surface(".github/workflows/ci.yml") is True

    # 不在清单里
    assert is_shared_surface("backend/server.py") is False
    assert is_shared_surface("scripts/other.sh") is False
    assert is_shared_surface("backend/state_machine_other/x.py") is False


# ---------------------------------------------------------------------------
# 2. 补充用例（同一份文件）
# ---------------------------------------------------------------------------


def test_non_passed_previous_status_never_reuses():
    """上一轮不是 PASSED（SKIPPED 也不参与跨轮复用）→ 恒 False。"""
    for status in ("FAILED", "SKIPPED", "BLOCKED", "DEFERRED", "", None):
        assert may_reuse(status, ("backend/verification/",), (), False) is False


def test_empty_change_set_reuses():
    """本轮没有变更 + 其余三条成立 → True。"""
    assert may_reuse("PASSED", ("backend/verification/",), (), False) is True


def test_sibling_directory_is_not_covered():
    """``backend/verification_extra/`` 不在 ``backend/verification`` 范围内。"""
    assert (
        may_reuse("PASSED", ("backend/verification",), ("backend/verification_extra/x.py",), False)
        is True
    )


def test_covers_paths_for_files_derives_directory_prefixes():
    """范围由文件所在目录派生，按目录去重保序，不跨目录合并。"""
    assert covers_paths_for_files(
        ["backend/verification/orchestrator.py", "backend/tests/unit/x.py"]
    ) == ("backend/verification", "backend/tests/unit")

    # 同一目录下的多个文件只产生一个前缀
    assert covers_paths_for_files(["backend/verification/a.py", "backend/verification/b.py"]) == (
        "backend/verification",
    )

    # 仓库根目录下的文件没有目录前缀 → 没有范围
    assert covers_paths_for_files(["README.md"]) == ()

    # 哨兵不是文件路径
    assert covers_paths_for_files(["__NO_FILE_CHANGES__"]) == ()
    assert covers_paths_for_files(["__UNKNOWN_MODIFICATIONS__"]) == ()


def test_parse_porcelain_reads_status_output():
    """``git status --porcelain`` 的输出能解析出文件路径。"""
    text = (
        " M backend/server.py\n"
        "?? backend/new.py\n"
        "A  backend/added.py\n"
        "R  backend/old.py -> backend/renamed.py\n"
    )
    assert parse_porcelain(text) == (
        "backend/server.py",
        "backend/new.py",
        "backend/added.py",
        "backend/old.py",
        "backend/renamed.py",
    )

    # 空输入不抛
    assert parse_porcelain("") == ()


def test_merge_change_sets_dedups_preserving_order():
    """多组变更集合并 → 去重且保序。"""
    assert merge_change_sets(("a.py", "b.py"), ("b.py", "c.py")) == ("a.py", "b.py", "c.py")
    assert merge_change_sets((), ("b.py",)) == ("b.py",)
    assert merge_change_sets() == ()