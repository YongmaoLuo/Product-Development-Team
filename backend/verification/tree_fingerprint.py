"""树指纹 —— 把「被验证的那份代码」的内容身份算成一个可比较的值。

这个模块要解决什么
------------------
PRD 要求验证结果与**代码树内容**绑定：``plan_verification_result`` 每行带一个
``tree_fingerprint``，重跑只追加、不覆盖（append-only）。要做到这件事，每条
结果行都得记下「当时被验证的是哪一份代码」—— 而仓库现有的比较只到 **commit**
级（``git rev-parse HEAD``），缺 **tree** 级指纹。

为什么是 tree 而不是 commit
---------------------------
rebase 会改写 commit sha：同一份代码，rebase 前后是两个 commit、两个 sha，
但**内容树**没变。验证的对象是内容，不是提交历史 —— 所以身份必须是 tree
object 的 oid（``git rev-parse HEAD^{tree}``），不是 commit sha（架构决策点 6）。

脏树怎么办
----------
``HEAD^{tree}`` 说的是**已提交**的那棵树。工作区里有未提交改动时，「这棵树」
有两个答案：提交的那棵，和磁盘上的那棵。两者必须分开 —— 否则「当前树是否
通过」这个问题会用错答案。

所以脏树的指纹里并入一份工作区摘要（``git diff HEAD`` 加上未跟踪文件清单及
其内容摘要），并把 ``dirty`` 标成真（架构决策点 6）。进哈希的是**摘要**而不是
原文：指纹要回答的是「变了没有、变成什么样」，不是把整份 diff 存进数据库。

指纹怎么算
----------
::

    tree = git rev-parse HEAD^{tree}
    if 工作区不干净:
        tree = tree + ":dirty:" + sha256(git diff HEAD + 未跟踪文件列表)
    tree_fingerprint = sha256("tree:" + tree)

（架构决策点 6 的原文口径。）

纯度
----
除了问 git，本模块不读时钟、不发请求、不看数据库。git 问出来的都是**树的
事实**：tree oid、HEAD、diff。同一棵树在任何机器上给出同一个指纹。

异常口径
--------
* **不是 git 仓库** → :class:`ValueError`。参数不对，不是环境坏。
* **git 跑了但失败** → :class:`TreeFingerprintError`（:class:`RuntimeError`
  的子类），带着 git 的退出码。环境不对，或仓库状态不对（比如还没有任何
  commit，``HEAD^{tree}`` 无从解析）。两种失败不是一回事，调用方要能分开。

包形态
------
``backend/verification/`` 是一个有 ``__init__.py`` 的包；本模块不往包的
``__init__`` 里加导出 —— 它是按需取用的叶子模块，谁需要谁 import。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from bounded_subprocess import run_bounded
except ImportError:  # pragma: no cover
    from backend.bounded_subprocess import run_bounded  # type: ignore

__all__ = [
    "FINGERPRINT_PREFIX",
    "TreeFingerprintError",
    "capture_tree_fingerprint",
    "compute_tree_fingerprint",
]


#: 指纹的前缀。``sha256:`` 让值自带算法名：算法换了，旧值仍能被认出是另一
#: 种算法的值，而不是同一个键的另一种写法。
FINGERPRINT_PREFIX = "sha256:"

#: 单条 git 命令的时限。一个挂住的 git 不能挂住问指纹的那一方。
_GIT_TIMEOUT_SECONDS = 30

#: 读文件时的分块大小。未跟踪文件按内容摘要入哈希，大文件不必整个读进内存。
_HASH_CHUNK_SIZE = 1 << 20


class TreeFingerprintError(RuntimeError):
    """一次 git 调用失败了。

    继承 :class:`RuntimeError`：调用方要能把它和「这个目录不是 git 仓库」
    （:class:`ValueError`）分开处理 —— 前者是环境或仓库状态不对，后者是
    参数不对。

    ``exit_code`` 带着 git 的退出码：排查时它是唯一能定位问题的线索，
    「git 失败了」这句话本身什么也没说。
    """

    def __init__(self, message: str, exit_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
def _run_git(worktree: Path, args: List[str]) -> subprocess.CompletedProcess:
    """在 ``worktree`` 里跑一条 git 命令。

    退出码**不**在这里判断：每个调用方对非零退出的含义不同（有的当
    「不是仓库」，有的当「git 坏了」）。

    ``shell=False`` 配 argv 列表：参数里有 ``HEAD^{tree}`` 这种带花括号的
    记号，交给 shell 就多了一套要转义的语法。
    """
    try:
        return run_bounded(
            ["git", "-C", str(worktree), *args],
            shell=False,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise TreeFingerprintError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s "
            f"in {worktree}"
        ) from exc
    except OSError as exc:
        raise TreeFingerprintError(
            f"git {' '.join(args)} could not be started in {worktree}: {exc}"
        ) from exc


def _git_output(worktree: Path, args: List[str]) -> str:
    """跑一条 git 命令并返回 stdout；非零退出 → :class:`TreeFingerprintError`。"""
    completed = _run_git(worktree, args)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise TreeFingerprintError(
            f"git {' '.join(args)} failed in {worktree} "
            f"(exit {completed.returncode})"
            + (f": {detail}" if detail else ""),
            exit_code=completed.returncode,
        )
    return completed.stdout or ""


def _repo_root(worktree: Path) -> Path:
    """确认这个目录在 git 工作树里，并返回仓库的顶层目录。

    返回顶层目录而不是原样返回传进来的路径：``git diff`` 与 ``git ls-files``
    都以 git 的当前目录为基准，只覆盖那棵子树。从子目录问指纹、从根目录问
    指纹，必须给出同一个答案 —— 否则「这棵树」取决于你站在哪儿。

    Raises:
        ValueError: 这个路径不是目录，或不在 git 工作树里。
    """
    path = Path(worktree)
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")

    completed = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
    if completed.returncode != 0 or (completed.stdout or "").strip() != "true":
        raise ValueError(f"{path} is not a git working tree")

    top = _git_output(path, ["rev-parse", "--show-toplevel"]).strip()
    return Path(top) if top else path


# ---------------------------------------------------------------------------
# 工作区摘要
# ---------------------------------------------------------------------------
def _file_digest(path: Path) -> str:
    """一个文件的 sha256；读不出来时给一个显式标记。

    读不出来也是一个事实：这份工作区的内容与「那份工作区」不同。给一个
    标记而不是一个假摘要，两个不可读的工作区才不会算出同一个值。
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        return f"<unreadable:errno={exc.errno}>"
    return digest.hexdigest()


def _untracked_summary(worktree: Path) -> str:
    """未跟踪（且未忽略）文件的清单与内容摘要。

    只列路径不够：同一个路径换一份内容是另一棵树。路径 + 内容摘要才
    说得出「工作区里现在是什么」。

    被忽略的文件（``--exclude-standard``）不算数：构建产物与本地状态
    不是「被验证的代码」。
    """
    listed = _git_output(worktree, ["ls-files", "--others", "--exclude-standard"])
    lines: List[str] = []
    for rel in sorted(line for line in listed.splitlines() if line.strip()):
        lines.append(f"{rel}\t{_file_digest(worktree / rel)}")
    return "\n".join(lines)


def _workspace_digest(worktree: Path) -> Optional[str]:
    """工作区偏离 ``HEAD`` 的摘要；工作区干净时返回 ``None``。

    输入是 ``git diff HEAD``（暂存区与工作区对 ``HEAD`` 的差异）加上未跟踪
    文件的清单与内容摘要。暂存与未暂存都要算进去：两种都是「工作区与
    已提交内容不同」的证据，只看一种会把另一种漏成「干净」。
    """
    diff_text = _git_output(worktree, ["diff", "HEAD"])
    untracked = _untracked_summary(worktree)
    if not diff_text.strip() and not untracked.strip():
        return None
    payload = diff_text + "\n--untracked--\n" + untracked
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def _collect(worktree: Path) -> Tuple[str, str, bool]:
    """一次采集：指纹、HEAD commit、脏标记。

    Returns:
        ``(tree_fingerprint, head_commit, dirty)``。
    """
    root = _repo_root(worktree)

    tree_sha = _git_output(root, ["rev-parse", "HEAD^{tree}"]).strip()
    head_commit = _git_output(root, ["rev-parse", "--short", "HEAD"]).strip()

    workspace_digest = _workspace_digest(root)
    material = "tree:" + tree_sha
    if workspace_digest is not None:
        material += ":dirty:" + workspace_digest

    fingerprint = FINGERPRINT_PREFIX + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()
    return fingerprint, head_commit, workspace_digest is not None


def compute_tree_fingerprint(worktree: Path) -> str:
    """一棵树的内容指纹。

    Args:
        worktree: 工作树的目录。它在 git 工作树里即可 —— 目录下面哪一层
            都行，指纹按仓库顶层算：从子目录问与从根目录问给出同一个答案。

    Returns:
        ``sha256:<64 位小写 hex>``。同一棵树恒定给出同一个值；工作区脏了
        时，这个值与干净时不同（脏树的材料里多了工作区摘要）。

    Raises:
        ValueError: ``worktree`` 不是目录，或不在 git 工作树里。
        TreeFingerprintError: git 跑了但失败（带退出码）。它是
            :class:`RuntimeError` 的子类。
    """
    fingerprint, _head, _dirty = _collect(Path(worktree))
    return fingerprint


def capture_tree_fingerprint(worktree: Path) -> dict:
    """一棵树的内容指纹，连同它是在哪个 commit 上、当时脏不脏。

    Args:
        worktree: 工作树的目录。

    Returns:
        一个字典，三个键：

        * ``tree_fingerprint`` —— ``sha256:<64 位小写 hex>``；
        * ``head_commit`` —— 当时的 HEAD commit（缩写形式）；
        * ``dirty`` —— 工作区当时是否偏离 HEAD。

    Raises:
        ValueError: ``worktree`` 不是目录，或不在 git 工作树里。
        TreeFingerprintError: git 跑了但失败（带退出码）。
    """
    fingerprint, head_commit, dirty = _collect(Path(worktree))
    return {
        "tree_fingerprint": fingerprint,
        "head_commit": head_commit,
        "dirty": dirty,
    }
