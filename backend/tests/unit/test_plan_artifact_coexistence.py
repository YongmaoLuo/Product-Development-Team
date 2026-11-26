"""产物共存契约 —— ``tasks.json`` 与 ``verification-plan.md`` 同时存在时怎么判。

背景
----
一个 plan 目录里可能躺着两类产物：

* ``tasks.json`` —— 开发任务清单；
* ``verification-plan.md`` —— 验证计划书（人工审阅对象）。

两类同时存在**不再是冲突**：按「任务优先」的主分支语义，它就是一条**开发
分支**的计划，只不过额外带着一份计划书。判定因此从四值域收敛为三值域
（``dev`` / ``verification`` / ``empty``），附加产物由 ``plan_doc_path`` 与
派生属性 ``has_plan_doc`` / ``is_reviewed_plan`` 暴露。

钉住的不变量
------------
1. 共存时 ``branch == "dev"``，且 ``plan_doc_path`` 非空（附加产物没被吞掉）；
2. ``detect()`` 的输出域**恰为三值** —— ``"conflict"`` 不再出现；
3. ``is_verification_branch()`` 保持狭窄定义：只有计划书才算验证分支。
   它若对共存目录返回 ``True``，验证轮会把开发分支误判成可跳过执行；
4. ``require_branch()`` 期望不匹配时**仍**抛 ``ArtifactConflictError`` ——
   共存合法不等于门禁失效。

所有用例都用 ``tmp_path`` 真实落盘，不 mock 文件系统 —— 被测的就是"文件在不
在"。判定必须是纯读：调用两次答案一致，且目录里的文件列表、字节（sha256）
与 mtime 都不许变。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from plan_artifacts import (  # noqa: E402
    ArtifactConflictError,
    detect,
    is_verification_branch,
    require_branch,
)
from tests.fixtures.plan_artifacts_fixture import snapshot  # noqa: E402

pytestmark = pytest.mark.unit

TASKS = "tasks.json"
PLAN_DOC = "verification-plan.md"

#: ``detect()`` 的输出域 —— 恰为三个取值。
OUTPUT_DOMAIN = {"dev", "verification", "empty"}

#: ``(tasks.json 存在, verification-plan.md 存在)`` 的 2×2 输入域。
COMBINATIONS = [
    (True, False),
    (False, True),
    (True, True),
    (False, False),
]


def _make_plan_dir(
    tmp_path: Path,
    tasks_exists: bool = False,
    plan_doc_exists: bool = False,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    if tasks_exists:
        (tmp_path / TASKS).write_text("{}", encoding="utf-8")
    if plan_doc_exists:
        (tmp_path / PLAN_DOC).write_text("# doc", encoding="utf-8")
    return tmp_path


def _snapshot(plan_dir: Path) -> dict:
    """目录内容快照：文件名 → ``sha256`` + ``mtime``。

    走共享实现（``tests.fixtures.plan_artifacts_fixture.snapshot``）——
    摘要逻辑只有一份真源。字节这一维是承重的：只看文件名与 mtime 会放过
    一次「改写字节再还原 mtime」的写入。
    """
    return snapshot(plan_dir)


# ---------------------------------------------------------------------------
# 1) 共存 = 开发分支 + 附加产物
# ---------------------------------------------------------------------------


def test_coexist_detect_is_dev(tmp_path: Path) -> None:
    """两类产物同时存在 → ``dev``，且计划书路径仍被暴露（附加产物没被吞掉）。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    artifacts = detect(tmp_path)

    assert artifacts.branch == "dev"
    assert artifacts.plan_doc_path == tmp_path / PLAN_DOC
    assert artifacts.tasks_path == tmp_path / TASKS


def test_coexist_plan_exposes_both_artifacts(tmp_path: Path) -> None:
    """共存时三个派生属性同时为真；没有附加产物时 ``has_plan_doc`` 为假。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    artifacts = detect(tmp_path)

    assert artifacts.has_tasks is True
    assert artifacts.has_plan_doc is True
    assert artifacts.is_reviewed_plan is True

    only_tasks = detect(_make_plan_dir(tmp_path / "only-tasks", tasks_exists=True))
    assert only_tasks.has_tasks is True
    assert only_tasks.has_plan_doc is False
    assert only_tasks.is_reviewed_plan is False


# ---------------------------------------------------------------------------
# 2) 输出域恰为三值
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tasks_exists, plan_doc_exists", COMBINATIONS)
def test_detect_output_domain_is_three_values(
    tmp_path: Path,
    tasks_exists: bool,
    plan_doc_exists: bool,
) -> None:
    """四种存在组合 → 分支取值必落在三值域内，``"conflict"`` 不再出现。"""
    _make_plan_dir(tmp_path, tasks_exists, plan_doc_exists)

    artifacts = detect(tmp_path)

    assert artifacts.branch in OUTPUT_DOMAIN
    assert artifacts.branch != "conflict"


def test_detect_output_domain_has_no_conflict_value(tmp_path: Path) -> None:
    """穷举四组合 —— 任何一个都不得吐出 ``"conflict"``。"""
    seen = set()
    for tasks_exists, plan_doc_exists in COMBINATIONS:
        case_dir = tmp_path / f"c-{int(tasks_exists)}{int(plan_doc_exists)}"
        case_dir.mkdir()
        _make_plan_dir(case_dir, tasks_exists, plan_doc_exists)
        seen.add(detect(case_dir).branch)

    assert seen <= OUTPUT_DOMAIN
    assert "conflict" not in seen


# ---------------------------------------------------------------------------
# 3) is_verification_branch() 保持狭窄
# ---------------------------------------------------------------------------


def test_is_verification_branch_rejects_coexist(tmp_path: Path) -> None:
    """共存 → 不是验证分支；只有计划书 → 是验证分支。

    这条最要紧：验证分支**跳过初始执行**。共存目录里若被判成验证分支，
    开发任务就会被跳过。
    """
    coexist_dir = _make_plan_dir(
        tmp_path / "coexist", tasks_exists=True, plan_doc_exists=True
    )
    only_doc_dir = _make_plan_dir(tmp_path / "only-doc", plan_doc_exists=True)

    assert is_verification_branch(coexist_dir) is False
    assert is_verification_branch(only_doc_dir) is True


# ---------------------------------------------------------------------------
# 4) require_branch() 门禁仍然有效
# ---------------------------------------------------------------------------


def test_require_branch_still_raises_artifact_conflict_error(tmp_path: Path) -> None:
    """期望与实际不符 → 仍抛 ``ArtifactConflictError``（共存合法 ≠ 门禁失效）。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    with pytest.raises(ArtifactConflictError) as excinfo:
        require_branch(tmp_path, "verification")

    assert excinfo.value.expected == "verification"
    assert excinfo.value.actual == "dev"


def test_require_branch_accepts_dev_on_coexist(tmp_path: Path) -> None:
    """共存目录的主分支是 ``dev`` —— 要求 ``dev`` 必须放行。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    artifacts = require_branch(tmp_path, "dev")

    assert artifacts.branch == "dev"
    assert artifacts.has_plan_doc is True


# ---------------------------------------------------------------------------
# 5) 边界：目录不存在 / 两产物皆无 / 纯读
# ---------------------------------------------------------------------------


def test_detect_on_missing_directory_is_empty(tmp_path: Path) -> None:
    """目录不存在 → ``empty``，不抛错（判定是纯读）。"""
    artifacts = detect(tmp_path / "does-not-exist")

    assert artifacts.branch == "empty"
    assert artifacts.has_tasks is False
    assert artifacts.has_plan_doc is False
    assert artifacts.is_reviewed_plan is False


def test_detect_on_empty_directory_is_empty(tmp_path: Path) -> None:
    """目录在、两产物皆无 → ``empty``。"""
    artifacts = detect(tmp_path)

    assert artifacts.branch == "empty"


def test_detect_is_pure_and_uncached(tmp_path: Path) -> None:
    """纯读且不缓存：两次答案一致，目录的文件列表、字节（sha256）与 mtime
    一个都没变。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)
    before = _snapshot(tmp_path)

    first = detect(tmp_path)
    second = detect(tmp_path)

    assert first == second
    assert _snapshot(tmp_path) == before