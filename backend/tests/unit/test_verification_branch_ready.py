"""验证分支的「就绪」谓词：六维真值表、异常折叠与 ref 漂移。

背景
----
验证分支上有**两道**门禁：状态机自己的阶段门禁，和 ``/start`` 接口的接口门禁。
两道门禁各问各的，就会出现「状态机放行、接口永远 409」——同一个事实被判断两次，
两次给出不同答案，而且没人知道该信哪一个。

所以判断收敛到一个谓词：:func:`verification_gate.verification_branch_ready`。
本文件钉住它答应下来的三件事：

* **六维真值表**：六个条件全真 → ``True``；每个条件单独弄假 → ``False``；
* **异常折叠**：文档缺失、审核 JSON 损坏、git 失败、模块内部抛错 → ``False``，
  而不是把异常抛给接口变成 500；
* **ref 漂移**：PR head 移动之后，记录的 commit 与 ref 当前解析结果不再一致 →
  ``False``。

测试数据
--------
真实 git 仓库（两个提交 + ``refs/pull/7/head``）与一份 canonical 验证计划书。
真实 git 是刻意的：ref 的解析结果就是谓词的一个维度，mock 掉它等于把要测的
东西测没了。审核条目用任务 3 的 :func:`record_action` 落盘，与生产路径同一条。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_gate  # noqa: E402
from verification_gate import verification_branch_ready  # noqa: E402
from verification_plan_review import record_action  # noqa: E402

#: 一份合法的计划书：十个章节按模板顺序排列，两个 VP 块。与任务 3 的语料同源。
CANONICAL_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为 product-development-team 仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡
- 双档配额耗尽时取最晚重开的窗口

## 排除范围

- 前端交互（本次未改动前端）
- 与配额无关的执行器行为

## 验证方法

- 以单元测试为主，必要时做一次手工核验

## 验证点清单

### VP-001 配额按真实重开时刻睡眠

- 范围：backend/verification_loop.py 的配额等待路径
- 验证方法：单元测试
- 通过判据：
  - C1: 配额耗尽时睡到真实重开时刻
  - C2: 双档耗尽时取最晚重开的窗口
- 证据要求：
  - E1: tests/unit/test_quota_wait.py 的 pytest 输出
- 预期结果：两条判据全部满足，无超时

### VP-002 报告把 skip 条目标注为未验证

- 范围：backend/verification_report.py
- 验证方法：单元测试
- 通过判据：
  - C1: skip 条目显示为「未验证」
- 证据要求：
  - E1: tests/unit/test_report_render.py 的 pytest 输出
- 预期结果：渲染结果与期望一致

## 预期结果

- 全部验证点通过，未发现需要修复的缺陷

## 证据要求

- 每个验证点给出可复跑的命令与原始输出

## 通过判据

- 所有验证点的 C 编号判据全部满足

## 环境与依赖

- Python 3.11 与 pytest 可用

## 风险面

- 上游 PR head 可能移动，导致基线漂移
"""

#: canonical 文档的审核条目数：5 个计划级条目 + 2 个验证点条目。
ITEM_COUNT = 7

#: 「用默认基线」的信标：区别于「显式不记录基线」。
_DEFAULT_BASELINE = object()


# ---------------------------------------------------------------------------
# 真实 git，确定性
# ---------------------------------------------------------------------------


def _git_env() -> dict:
    """An environment that makes git deterministic and offline.

    The fixed dates and identity make the commit SHAs reproducible, and the
    neutralised configuration keeps the developer's own git config out of
    the result.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "PDT Verification Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "PDT Verification Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A local repository whose ``refs/pull/7/head`` points at the second commit.

    The first commit is the ancestor of the reviewed baseline, the second one
    is the PR head under verification.
    """
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    (repo / "feature.txt").write_text("pr head\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "--quiet", "-m", "pr head")
    _git(repo, "update-ref", "refs/pull/7/head", "HEAD")
    return repo


# ---------------------------------------------------------------------------
# Plan 目录
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic state.db — the baseline lives in the plan's routing row."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _record_baseline(plan_dir: Path, baseline: Dict[str, Any]) -> None:
    """Write a frozen baseline into the plan state.

    The baseline lives in the ``verification`` column of the plan's routing
    row, so the test writes it through the repository layer rather than
    through a hand-made file — the reader under test must find it where the
    freeze step puts it.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_dir.name,
            phase="verification",
            completed_phases=[],
            review_rounds={},
            flags={},
            verification={
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
                "baseline": baseline,
            },
            last_updated=None,
        )
    finally:
        conn.close()


def _frozen_baseline(repo: Path) -> Dict[str, Any]:
    """The baseline a review of ``repo`` would have frozen."""
    return {
        "repo_dir": str(repo),
        "ref": "refs/pull/7/head",
        "commit_sha": _git(repo, "rev-parse", "refs/pull/7/head"),
        "pr_number": 7,
    }


def _accept_all(plan_dir: Path) -> None:
    """把全部审核条目标记为 accepted。"""
    for index in range(ITEM_COUNT):
        record_action(plan_dir, index, "accept")


@pytest.fixture
def make_plan(tmp_path: Path, git_repo: Path):
    """Build a plan directory whose six dimensions all hold.

    Every call gets its own plan id: the plan id is the routing row's key,
    and two plans with the same name would read each other's baseline.
    """
    counter = {"n": 0}

    def _make(
        baseline: Any = _DEFAULT_BASELINE,
        **overrides: Any,
    ) -> Path:
        counter["n"] += 1
        plan_dir = tmp_path / f"plan-{counter['n']}"
        plan_dir.mkdir()
        (plan_dir / "verification-plan.md").write_bytes(
            CANONICAL_MD.encode("utf-8")
        )
        _accept_all(plan_dir)
        if baseline is _DEFAULT_BASELINE:
            recorded = _frozen_baseline(git_repo)
            recorded.update(overrides)
        else:
            recorded = baseline
        if recorded is not None:
            _record_baseline(plan_dir, recorded)
        return plan_dir

    return _make


# ---------------------------------------------------------------------------
# 1. 六维真值表
# ---------------------------------------------------------------------------


def test_six_dimension_truth_table(make_plan, tmp_path: Path, git_repo: Path) -> None:
    """六个条件全真 → True；每个条件单独弄假 → False。"""
    # 全真
    assert verification_branch_ready(make_plan()) is True

    # 维度 1 —— 产物不是验证分支（目录里多了一份 tasks.json → 冲突）
    conflict = make_plan()
    (conflict / "tasks.json").write_text("[]", encoding="utf-8")
    assert verification_branch_ready(conflict) is False

    # 维度 2 —— 审核不完备（一个条目回到 pending，文档没动）
    incomplete = make_plan()
    record_action(incomplete, 3, "reset")
    assert verification_branch_ready(incomplete) is False

    # 维度 3 —— 哈希不符（审核完之后文档被改了一个字节）
    rewritten = make_plan()
    doc = rewritten / "verification-plan.md"
    doc.write_bytes(doc.read_bytes().replace(b"pr=7", b"pr=8"))
    assert verification_branch_ready(rewritten) is False

    # 维度 4 —— 基线未记录
    without_baseline = make_plan(baseline=None)
    assert verification_branch_ready(without_baseline) is False

    # 维度 5 —— 仓库不存在
    missing_repo = make_plan(repo_dir=str(tmp_path / "absent-repository"))
    assert verification_branch_ready(missing_repo) is False

    # 维度 6 —— ref 解析不到记录的 commit（记录的是另一个 commit）
    other_commit = make_plan(commit_sha=_git(git_repo, "rev-parse", "HEAD~1"))
    assert verification_branch_ready(other_commit) is False


#: 六个维度各自对应的内部判定。把它单独弄假，就绪判定就必须为假——
#: 这条断言证明每个维度**真的被问到**，而不是被下游更笼统的检查顺带接管。
_DIMENSIONS = (
    ("_is_verification_branch", False),
    ("_review_covers_document", False),
    ("_review_is_complete", False),
    ("_recorded_baseline", None),
    ("_repository_exists", False),
    ("_ref_matches_baseline", False),
)


@pytest.mark.parametrize("dimension,value", _DIMENSIONS)
def test_every_dimension_is_wired(
    dimension: str,
    value: Any,
    make_plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每个维度单独为假 → 不就绪。"""
    monkeypatch.setattr(verification_gate, dimension, lambda *a, **k: value)

    assert verification_branch_ready(make_plan()) is False


def test_hash_dimension_is_decisive_on_its_own(
    make_plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审完的文档被改过：即使完整性判定被放过，哈希这一维仍然拦住。

    这一条钉住的是「哈希一致」不是完整性判定的附属品：两者在
    :func:`verification_plan_review.is_resolved` 里是同一个判断，在本谓词里
    是两处，各自能独立拦下。
    """
    rewritten = make_plan()
    doc = rewritten / "verification-plan.md"
    doc.write_bytes(doc.read_bytes().replace(b"pr=7", b"pr=8"))

    monkeypatch.setattr(
        verification_gate, "_review_is_complete", lambda *a, **k: True
    )

    assert verification_branch_ready(rewritten) is False


def test_repository_dimension_is_decisive_on_its_own(
    make_plan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仓库不存在：即使 ref 判定被放过，仓库这一维仍然拦住。"""
    missing_repo = make_plan(repo_dir=str(tmp_path / "absent-repository"))

    monkeypatch.setattr(
        verification_gate, "_ref_matches_baseline", lambda *a, **k: True
    )

    assert verification_branch_ready(missing_repo) is False


def test_a_failed_dimension_stops_the_evaluation(
    make_plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前一维不成立时，后面的维度不再被问到。

    基线没记录时，仓库与 ref 这两维没有可问的对象——它们问的是基线里的字段。
    """
    plan_dir = make_plan(baseline=None)

    asked = []
    monkeypatch.setattr(
        verification_gate,
        "_repository_exists",
        lambda *a, **k: asked.append("repo") or True,
    )
    monkeypatch.setattr(
        verification_gate,
        "_ref_matches_baseline",
        lambda *a, **k: asked.append("ref") or True,
    )

    assert verification_branch_ready(plan_dir) is False
    assert asked == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"repo_dir": ""},
        {"repo_dir": 123},
        {"ref": ""},
        {"ref": 7},
        {"commit_sha": ""},
        {"commit_sha": None},
    ],
)
def test_unusable_baseline_field_is_not_ready(make_plan, overrides) -> None:
    """基线记了，但字段不可用 → 不就绪。"""
    plan_dir = make_plan(**overrides)

    assert verification_branch_ready(plan_dir) is False


def test_malformed_baseline_is_not_ready(make_plan) -> None:
    """基线字段不是映射（状态被写坏了）→ 不就绪。"""
    plan_dir = make_plan(baseline="not-a-mapping")

    assert verification_branch_ready(plan_dir) is False


def test_dev_branch_is_not_ready(make_plan) -> None:
    """只有 ``tasks.json`` 的开发分支不是验证分支，就绪判定为假。"""
    plan_dir = make_plan(baseline=None)
    (plan_dir / "verification-plan.md").unlink()
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")

    assert verification_branch_ready(plan_dir) is False


# ---------------------------------------------------------------------------
# 2. 异常折叠
# ---------------------------------------------------------------------------


def test_any_exception_folds_to_false(
    make_plan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读不懂的计划与没准备好的计划是同一件事：还没到能跑的时候。"""
    # 文档缺失
    missing_doc = make_plan()
    (missing_doc / "verification-plan.md").unlink()
    assert verification_branch_ready(missing_doc) is False

    # 审核 JSON 损坏
    broken_json = make_plan()
    (broken_json / "verification-plan-review.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert verification_branch_ready(broken_json) is False

    # git 失败：目录在，但不是 git 仓库
    plain_dir = tmp_path / "not-a-repository"
    plain_dir.mkdir()
    not_a_repo = make_plan(repo_dir=str(plain_dir))
    assert verification_branch_ready(not_a_repo) is False

    # 内部异常：模块内部抛错也不外泄成 500
    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(verification_gate, "resolve_ref_commit", _boom)
    assert verification_branch_ready(make_plan()) is False


# ---------------------------------------------------------------------------
# 3. ref 漂移
# ---------------------------------------------------------------------------


def test_ref_moved_is_false(make_plan, git_repo: Path) -> None:
    """PR head 移动之后，记录的 commit 与 ref 当前解析结果不再一致 → False。"""
    plan_dir = make_plan()
    assert verification_branch_ready(plan_dir) is True

    _git(git_repo, "update-ref", "refs/pull/7/head", "HEAD~1")

    assert verification_branch_ready(plan_dir) is False