"""产物判定模块 :mod:`plan_artifacts` 的单元测试。

为什么要有这组测试
------------------
"这次要产出哪类产物"只有三个答案：``dev`` / ``verification`` / ``empty``。
判定一旦漂了，症状出现在很远的地方：某个入口看到两类产物自己挑了一个，
另一个入口报错，第三个入口推断出错误阶段 —— 而且每条路径单独看都"能跑"。

所以这里穷举 ``tasks.json`` × ``verification-plan.md`` 的 2×2 存在组合，
而不是抽一个样例。门禁谓词（``require_branch`` / ``is_verification_branch``）
也按同一组组合验证：判定是纯函数，读一次和读一万次必须是同一个答案。

两类产物**同时存在**不再判为冲突：按「任务优先」的主分支语义它是 ``dev``，
计划书作为附加产物由 ``plan_doc_path`` 暴露 —— 这条契约的完整钉法见
``tests/unit/test_plan_artifact_coexistence.py``。

所有用例都用 ``tmp_path`` 真实落盘，不 mock 文件系统 —— 被测的就是
"文件在不在"。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from plan_artifacts import (  # noqa: E402
    ArtifactConflictError,
    ArtifactSet,
    detect,
    is_verification_branch,
    require_branch,
)
from tests.fixtures.plan_artifacts_fixture import snapshot  # noqa: E402

pytestmark = pytest.mark.unit

TASKS = "tasks.json"
PLAN_DOC = "verification-plan.md"

#: ``(tasks.json 存在, verification-plan.md 存在) → 期望分支``。
#: 2×2 是判定函数的完整输入域 —— 四行就是全部。两类产物同时存在时主分支是
#: ``dev``（计划书是附加产物，不是冲突）。
TRUTH_TABLE = [
    (True, False, "dev"),
    (False, True, "verification"),
    (True, True, "dev"),
    (False, False, "empty"),
]


def _write(path: Path, content: str = "artifact") -> None:
    path.write_text(content, encoding="utf-8")


def _make_plan_dir(
    tmp_path: Path,
    tasks_exists: bool = False,
    plan_doc_exists: bool = False,
) -> Path:
    if tasks_exists:
        _write(tmp_path / TASKS, "[]")
    if plan_doc_exists:
        _write(tmp_path / PLAN_DOC, "# 验证计划书")
    return tmp_path


# ---------------------------------------------------------------------------
# detect()：四行真值表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tasks_exists, plan_doc_exists, expected_branch",
    TRUTH_TABLE,
)
def test_detect_truth_table(
    tmp_path: Path,
    tasks_exists: bool,
    plan_doc_exists: bool,
    expected_branch: str,
) -> None:
    """四种存在组合 → 四种分支取值。"""
    _make_plan_dir(tmp_path, tasks_exists, plan_doc_exists)

    artifacts = detect(tmp_path)

    assert artifacts.branch == expected_branch
    assert (artifacts.tasks_path is not None) is tasks_exists
    assert (artifacts.plan_doc_path is not None) is plan_doc_exists


def test_detect_returns_the_paths_of_existing_artifacts(tmp_path: Path) -> None:
    """存在的那份产物必须给出可点的路径，不存在的那份必须是 ``None``。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    artifacts = detect(tmp_path)

    assert artifacts.tasks_path == tmp_path / TASKS
    assert artifacts.plan_doc_path == tmp_path / PLAN_DOC


def test_detect_on_missing_directory_is_empty(tmp_path: Path) -> None:
    """目录本身不存在时不得抛错 —— 判定是纯读盘。"""
    artifacts = detect(tmp_path / "does-not-exist")

    assert artifacts.branch == "empty"
    assert artifacts.tasks_path is None
    assert artifacts.plan_doc_path is None


def test_detect_is_recomputed_not_cached(tmp_path: Path) -> None:
    """判定每次从磁盘重算：产物落盘后，答案必须当场改变。"""
    assert detect(tmp_path).branch == "empty"

    _write(tmp_path / TASKS, "[]")
    assert detect(tmp_path).branch == "dev"

    _write(tmp_path / PLAN_DOC, "# 验证计划书")
    coexist = detect(tmp_path)
    assert coexist.branch == "dev"
    assert coexist.plan_doc_path == tmp_path / PLAN_DOC


def test_detect_does_not_write_into_the_plan_dir(tmp_path: Path) -> None:
    """判定是纯读函数：不得在 plan 目录里留下任何东西。

    快照必须带上每个文件的 sha256：只看文件名会放过一次「改写字节再还原
    mtime」的写入 —— 文件名与 mtime 都没变，字节变了。
    """
    _make_plan_dir(tmp_path, tasks_exists=True)
    before = snapshot(tmp_path)

    detect(tmp_path)

    assert snapshot(tmp_path) == before


def test_artifact_set_is_immutable(tmp_path: Path) -> None:
    """``ArtifactSet`` 是冻结的：判定结果不能被调用方就地改写。"""
    _make_plan_dir(tmp_path, tasks_exists=True)

    artifacts = detect(tmp_path)

    assert isinstance(artifacts, ArtifactSet)
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifacts.branch = "verification"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# require_branch()：期望分支匹配则返回，不匹配则抛错
# ---------------------------------------------------------------------------


def test_require_branch_returns_artifact_set_on_match(tmp_path: Path) -> None:
    _make_plan_dir(tmp_path, tasks_exists=True)

    artifacts = require_branch(tmp_path, "dev")

    assert artifacts.branch == "dev"
    assert artifacts.tasks_path == tmp_path / TASKS


def test_require_branch_rejects_mismatch(tmp_path: Path) -> None:
    """期望 dev 实为 verification → 抛 ``ArtifactConflictError``。"""
    _make_plan_dir(tmp_path, plan_doc_exists=True)

    with pytest.raises(ArtifactConflictError) as excinfo:
        require_branch(tmp_path, "dev")

    assert excinfo.value.paths == (tmp_path / PLAN_DOC,)


def test_require_branch_rejects_mismatch_in_the_other_direction(tmp_path: Path) -> None:
    """期望 verification 实为 dev → 同样抛错（判定不偏袒任一分支）。"""
    _make_plan_dir(tmp_path, tasks_exists=True)

    with pytest.raises(ArtifactConflictError) as excinfo:
        require_branch(tmp_path, "verification")

    assert excinfo.value.paths == (tmp_path / TASKS,)


def test_require_branch_rejects_mismatch_on_coexist(tmp_path: Path) -> None:
    """两类产物同时存在 → 主分支是 ``dev``；要求 ``verification`` 抛错。

    共存合法，但门禁没失效：期望与实际不符时仍要拦住，两个产物路径都留在
    异常里（不自动择一）。
    """
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    with pytest.raises(ArtifactConflictError) as excinfo:
        require_branch(tmp_path, "verification")

    assert set(excinfo.value.paths) == {tmp_path / TASKS, tmp_path / PLAN_DOC}


def test_require_branch_accepts_dev_on_coexist(tmp_path: Path) -> None:
    """共存目录要求 ``dev`` 必须放行 —— 共存不是冲突。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    artifacts = require_branch(tmp_path, "dev")

    assert artifacts.branch == "dev"
    assert artifacts.plan_doc_path == tmp_path / PLAN_DOC


def test_require_branch_rejects_conflict_even_when_conflict_is_expected(
    tmp_path: Path,
) -> None:
    """``conflict`` 不再是合法期望值 —— 要求它一律抛错。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    with pytest.raises(ArtifactConflictError):
        require_branch(tmp_path, "conflict")


def test_require_branch_rejects_empty_when_a_branch_is_expected(
    tmp_path: Path,
) -> None:
    """产物一个都没有时，不猜意图：期望 dev / verification 都抛错。"""
    for expected in ("dev", "verification"):
        with pytest.raises(ArtifactConflictError):
            require_branch(tmp_path, expected)


def test_require_branch_accepts_empty_when_empty_is_expected(tmp_path: Path) -> None:
    """期望 empty 时，空目录是匹配而不是冲突。"""
    artifacts = require_branch(tmp_path, "empty")

    assert artifacts.branch == "empty"


def test_artifact_conflict_error_carries_paths_and_message(tmp_path: Path) -> None:
    """异常必须能同时回答"是哪些路径出的问题"和"期望/实际是什么"。"""
    _make_plan_dir(tmp_path, tasks_exists=True, plan_doc_exists=True)

    with pytest.raises(ArtifactConflictError) as excinfo:
        require_branch(tmp_path, "verification")

    error = excinfo.value
    assert len(error.paths) == 2
    assert "dev" in str(error)
    assert "conflict" in str(error)


# ---------------------------------------------------------------------------
# is_verification_branch()：布尔映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tasks_exists, plan_doc_exists, expected_branch",
    TRUTH_TABLE,
)
def test_is_verification_branch_mapping(
    tmp_path: Path,
    tasks_exists: bool,
    plan_doc_exists: bool,
    expected_branch: str,
) -> None:
    """只有 verification 分支为真；共存（dev）不算 verification。"""
    _make_plan_dir(tmp_path, tasks_exists, plan_doc_exists)

    assert is_verification_branch(tmp_path) is (expected_branch == "verification")