"""TDD spec — backend/verification/tree_fingerprint.py 树指纹采集。

这个模块要解决什么
------------------
PRD 要求验证结果与**代码树内容**绑定：``plan_verification_result`` 每行带一个
``tree_fingerprint``，重跑只追加、不覆盖。仓库现有的比较只到 **commit** 级
（``git rev-parse HEAD``），缺 **tree** 级指纹 —— 同一个 commit 可以被 rebase
改写 sha，内容却没变；同一份内容也可以先提交、后改工作区。

三条不可让步的性质（架构决策点 6）
----------------------------------
1. **同一棵树 = 同一个指纹。** 身份跟着**内容**走，不跟着 commit 走 ——
   两个 commit 指向同一棵 tree 时，指纹必须相同。
2. **脏树必须与干净树分开。** ``HEAD^{tree}`` 说的是**已提交**的那棵树；工作区
   有未提交改动时，磁盘上的内容是另一回事。脏树的指纹并入工作区摘要
   （``git diff HEAD`` + 未跟踪文件清单及其内容摘要），并标 ``dirty=True``。
3. **两种失败不是一回事。** 目录不是 git 仓库 → :class:`ValueError`（参数不对）；
   git 跑了但失败 → :class:`RuntimeError`，带着 git 的退出码（环境不对）。

TDD contract — 5 cases
-----------------------
1. ``test_same_tree_same_fp`` —— 两个 commit、同一棵树 → 指纹相同。
2. ``test_dirty_changes_fp`` —— 改了文件 → 指纹变化，且 ``dirty=True``。
3. ``test_not_git`` —— 非 git 目录 → ``ValueError``。
4. ``test_untracked_file_changes_fp`` —— 未跟踪文件进摘要 → 指纹变化。
5. ``test_git_failure_carries_exit_code`` —— git 失败 → ``RuntimeError`` 带退出码。

真实 git，不是 mock
-------------------
指纹的口径**就是** git 算出来的 tree oid。mock 掉 git 等于把要测的东西换成
别的东西（测试决策点 3）：断言会绿，而真实口径已经跑了。

临时目录自己开仓库
------------------
每个用例在 ``tmp_path`` 里 ``git init`` 一个自己的仓库：不碰本仓（worktree
里的 HEAD 不能被测试改），也不碰别人正在跑的目录。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification.tree_fingerprint import (  # noqa: E402
    capture_tree_fingerprint,
    compute_tree_fingerprint,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    """跑一条 git 命令并返回 stdout（去空白）。"""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(path: Path) -> Path:
    """在 ``path`` 上开一个自己的仓库。

    身份在仓库内配置（不在全局配置里改）：CI 上跑测试的机器可能没有
    ``user.email``，缺了它 ``git commit`` 会直接拒绝。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        capture_output=True,
        check=True,
    )
    _git(path, "config", "user.email", "tree-fingerprint@example.invalid")
    _git(path, "config", "user.name", "Tree Fingerprint Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(repo: Path, message: str) -> None:
    """把工作区当前状态提交一笔。"""
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)


def _isolate_from_any_repo(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """让 git 的目录发现在 ``path`` 这一层停下。

    ``git`` 从当前目录**向上**找仓库。测试要的是「一个确实不是仓库的目录」，
    而临时目录落在哪儿不是测试能控制的 —— 把它的每一层祖先都写进
    ``GIT_CEILING_DIRECTORIES``，向上找就一定停在这层。
    """
    ancestors = []
    current = Path(path).resolve()
    while True:
        ancestors.append(str(current))
        parent = current.parent
        if parent == current:
            break
        current = parent
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", ":".join(reversed(ancestors)))


# ---------------------------------------------------------------------------
# 1. 同一棵树 → 同一个指纹
# ---------------------------------------------------------------------------
def test_same_tree_same_fp(tmp_path: Path) -> None:
    """两个 commit、同一棵 tree → 指纹相同（commit sha 变了也不变）。

    这条是 rebase 场景的判据：rebase 改写 commit sha，内容树不变，
    于是「当前树是否通过」这个问题在 rebase 前后给出同一个答案。
    """
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("content\n", encoding="utf-8")
    _commit(repo, "first")

    first_fp = compute_tree_fingerprint(repo)
    first_head = _git(repo, "rev-parse", "HEAD")

    # 空提交：commit 变了，tree 没变。
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "empty")
    second_head = _git(repo, "rev-parse", "HEAD")
    second_fp = compute_tree_fingerprint(repo)

    assert first_head != second_head, "两个 commit 必须真的不同，否则本用例什么都没测"
    assert first_fp == second_fp, "同一棵树必须给出同一个指纹"


# ---------------------------------------------------------------------------
# 2. 脏树 → 指纹变化且 dirty=True
# ---------------------------------------------------------------------------
def test_dirty_changes_fp(tmp_path: Path) -> None:
    """改了工作区的文件 → 指纹变化，且 ``dirty=True``。

    ``HEAD^{tree}`` 说的是已提交的那棵树；工作区改了文件，两者是两份内容，
    指纹必须能分开它们 —— 否则「当前树是否通过」会用已提交那棵树的答案去
    回答一个没提交的树。
    """
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("content\n", encoding="utf-8")
    _commit(repo, "first")

    clean = capture_tree_fingerprint(repo)
    assert clean["dirty"] is False, "刚提交完的工作区是干净的"
    assert clean["tree_fingerprint"] == compute_tree_fingerprint(repo)

    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    dirty = capture_tree_fingerprint(repo)

    assert dirty["dirty"] is True, "改了文件的工作区是脏的"
    assert (
        dirty["tree_fingerprint"] != clean["tree_fingerprint"]
    ), "脏树的指纹必须与干净树不同"


# ---------------------------------------------------------------------------
# 3. 非 git 目录 → ValueError
# ---------------------------------------------------------------------------
def test_not_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不是仓库的目录 → :class:`ValueError`。

    参数不对，不是环境坏 —— 所以是 ``ValueError``，而不是与「git 跑了但
    失败」同一种异常。
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    _isolate_from_any_repo(monkeypatch, outside)

    with pytest.raises(ValueError):
        compute_tree_fingerprint(outside)


# ---------------------------------------------------------------------------
# 4. 未跟踪文件进摘要
# ---------------------------------------------------------------------------
def test_untracked_file_changes_fp(tmp_path: Path) -> None:
    """多一个未跟踪文件 → 指纹变化。

    未跟踪的文件也是「当前树」的一部分：磁盘上有它，``HEAD^{tree}`` 里没有。
    只算已提交内容会把两份不同的树算成同一棵（架构决策点 6）。
    """
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("content\n", encoding="utf-8")
    _commit(repo, "first")

    before = compute_tree_fingerprint(repo)

    (repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    after = compute_tree_fingerprint(repo)

    assert after != before, "未跟踪文件必须进摘要"


# ---------------------------------------------------------------------------
# 5. git 失败 → RuntimeError，带退出码
# ---------------------------------------------------------------------------
def test_git_failure_carries_exit_code(tmp_path: Path) -> None:
    """git 跑了但失败 → :class:`RuntimeError`，异常上带着退出码。

    空仓库（``git init`` 但没有 commit）是最真实的触发方式：``HEAD^{tree}``
    无从解析，git 退出 128。退出码是排查时唯一能定位问题的线索，不能丢。
    """
    repo = _init_repo(tmp_path / "empty-repo")

    with pytest.raises(RuntimeError) as excinfo:
        compute_tree_fingerprint(repo)

    assert getattr(excinfo.value, "exit_code", None) == 128, (
        "异常必须带着 git 的退出码，实际拿到："
        f"{getattr(excinfo.value, 'exit_code', None)}"
    )
