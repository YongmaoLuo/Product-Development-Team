"""验证分支的「就绪」谓词：状态机与 ``/start`` 接口共用的那一个判断。

为什么要有这一个谓词
--------------------
验证分支上有**两道**门禁：状态机自己的阶段门禁，和 ``/start`` 接口的接口门禁。
两道门禁各问各的，就会出现「状态机放行、接口永远 409」——同一个事实被判断两次，
两次给出不同答案，而且没有人知道该信哪一个。接口 409 是最坏的一种表现：状态机
已经把阶段推进到「可以验证」，接口却永远说「还没准备好」，操作者看到的只有一句
没有出处的 409。

所以判断收敛到这里：**一个纯函数、六个条件合取**。两道门禁都调用它，答案必然
一致。

六个条件
--------
==================  ==========================================================
条件                判据
==================  ==========================================================
产物是验证分支      :func:`plan_artifacts.detect` 判定目录里只有验证计划书
哈希一致            审核记录的文档哈希 = 当前文档字节的哈希
审核完备            全部审核条目 ``accepted`` / ``skipped``
基线已记录          计划状态的 ``verification.baseline``
仓库存在            基线里的 ``repo_dir`` 是一个存在的目录
ref 解析到记录的     ``git rev-parse <ref>`` 的结果 = 基线记录的
commit              ``commit_sha``
==================  ==========================================================

任一条件不成立 → 不就绪。

判定的**顺序**是有意义的：先问具体的原因，再问笼统的原因。哈希对不上时，
「审完的文档被改过」比「审核不完备」更接近真相——条目状态之所以全变 pending，
正是因为文档变了。仓库不在时，「仓库不存在」比「ref 解析不出来」更接近真相。
反过来排，两处都会被更笼统的那个条件接管，具体原因就丢了。

「哈希一致」为什么单列
----------------------
审核状态是按**文档字节**绑定的：审完之后文档被改了一个字节，条目状态还停在
「全部 accepted」，判定却会说「审过了」。这正是 :func:`verification_plan_review.is_resolved`
把哈希写进完备性定义的原因。本谓词把它拆成两个条件，是为了让「审核没做完」与
「审完的文档被改过」在代码里落在两个位置——两者的处置不一样：前者等人审，后者
要重新审。

异常一律折叠为 False
--------------------
这个谓词回答的是「能不能开始」，所以「读不懂」必须读作「不能开始」：文档缺失、
审核文件损坏、git 失败、状态库打不开——统统折叠成 ``False``，而不是把异常抛给
接口变成 500。一个读不懂的计划和一份没准备好的计划，对调用方是同一件事：**还没
到能跑的时候**。

折叠发生在最外层：内部任何一步抛出异常，整个谓词返回 ``False``。折叠不是吞掉
错误——调用方要的是「现在能不能跑」，不是「为什么不能跑」；不能跑的原因由各层
自己的日志负责。

基线只有一个来源
----------------
基线只从**计划状态**的 ``verification.baseline`` 读：那是审核冻结下来的事实。
计划书文档不在此列——文档是评审覆盖的内容，评审之后被改写的文档是另一份文档，
需要重新评审。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from plan_artifacts import BRANCH_VERIFICATION, PLAN_DOC_FILENAME, detect
from verification_plan_review import (
    ReviewState,
    doc_sha256,
    is_resolved,
    load,
)
from verification_workspace import resolve_ref_commit

__all__ = ["verification_branch_ready"]


def verification_branch_ready(plan_dir: Path) -> bool:
    """这个 plan 目录是否已就绪，可以开始验证。

    六个条件合取，任一不成立即为不就绪；内部任何异常都折叠成 ``False``。

    Args:
        plan_dir: plan 目录。

    Returns:
        就绪为 ``True``；不就绪为 ``False``。本函数**不抛异常**。
    """
    try:
        plan_dir = Path(plan_dir)

        # 条件 1 —— 产物是验证分支。冲突（两类产物同时存在）不算：冲突的处置
        # 是拒绝，不是当成验证分支跑。
        if not _is_verification_branch(plan_dir):
            return False

        # 条件 2 + 3 —— 审核覆盖当前文档，且条目全部已解决。文档缺失在这一行
        # 抛出，交给最外层的折叠处理。
        doc_bytes = (plan_dir / PLAN_DOC_FILENAME).read_bytes()
        state = load(plan_dir)
        if not _review_covers_document(state, doc_bytes):
            return False
        if not _review_is_complete(state, doc_bytes):
            return False

        # 条件 4 —— 基线已记录，且记录的字段可用。没有基线时下面三个取值都是
        # ``None``，所以一处判断就够：缺基线与基线字段不可用是同一件事。
        baseline = _recorded_baseline(plan_dir)
        repo_dir = _baseline_repo_dir(baseline)
        ref = _baseline_ref(baseline)
        commit_sha = _baseline_commit_sha(baseline)
        if repo_dir is None or ref is None or commit_sha is None:
            return False

        # 条件 5 —— 仓库存在。
        if not _repository_exists(repo_dir):
            return False

        # 条件 6 —— ref 当前解析到记录的 commit。PR head 移动过就是移动过：
        # 记录的是审核冻结下来的那个 commit，现在解析到别的 commit，说明
        # 要验的东西已经不是被审过的那一份。
        if not _ref_matches_baseline(repo_dir, ref, commit_sha):
            return False

        return True
    except Exception:  # noqa: BLE001 - 读不懂的计划 = 还没到能跑的时候
        return False


# ---------------------------------------------------------------------------
# 六个维度
# ---------------------------------------------------------------------------


def _is_verification_branch(plan_dir: Path) -> bool:
    """目录里是不是只有验证计划书。"""
    return detect(plan_dir).branch == BRANCH_VERIFICATION


def _review_covers_document(
    state: Optional[ReviewState],
    doc_bytes: bytes,
) -> bool:
    """审核针对的是不是当前这份文档。

    没有审核状态时为假：没有审核就没有「针对哪份文档」这回事。
    """
    if state is None:
        return False
    return state.doc_sha256 == doc_sha256(doc_bytes)


def _review_is_complete(
    state: Optional[ReviewState],
    doc_bytes: bytes,
) -> bool:
    """审核条目是不是全部已解决。

    :func:`verification_plan_review.is_resolved` 的定义是「条目全部
    ``accepted`` / ``skipped`` **且** 哈希一致」。哈希那一半由
    :func:`_review_covers_document` 单独回答，这里传进来的文档正是刚刚对上
    哈希的那一份，所以这一维问的其实是它的前半句。
    """
    return is_resolved(state, doc_bytes)


def _recorded_baseline(plan_dir: Path) -> Optional[Dict[str, Any]]:
    """计划状态里冻结的基线；没有记录时返回 ``None``。

    ``PlanState`` 在函数内局部导入：这个模块同时被状态机与 ``/start`` 接口
    导入，而状态层会拉起状态库，模块级导入会形成循环依赖。

    读的是 :func:`verification_workspace.resolve_baseline` 读的同一处
    ``verification.baseline``——基线只有这一个来源。
    """
    from plan_state import PlanState

    verification = PlanState(plan_dir).get_state().get("verification") or {}
    recorded = verification.get("baseline")
    return recorded if isinstance(recorded, dict) else None


def _repository_exists(repo_dir: Path) -> bool:
    """基线记录的仓库在不在。"""
    return repo_dir.is_dir()


def _ref_matches_baseline(repo_dir: Path, ref: str, commit_sha: str) -> bool:
    """``ref`` 当前解析到的 commit 是不是基线记录的那一个。

    git 失败（仓库没了、ref 解析不出）时抛异常，由最外层折叠成不就绪。
    """
    return resolve_ref_commit(repo_dir, ref) == commit_sha


# ---------------------------------------------------------------------------
# 基线字段
# ---------------------------------------------------------------------------


def _baseline_repo_dir(baseline: Optional[Dict[str, Any]]) -> Optional[Path]:
    """基线记录的仓库目录；基线缺失、字段缺失或不是非空字符串时返回 ``None``。"""
    if not isinstance(baseline, dict):
        return None
    repo_dir = baseline.get("repo_dir")
    if not isinstance(repo_dir, str) or not repo_dir.strip():
        return None
    return Path(repo_dir).expanduser()


def _baseline_ref(baseline: Optional[Dict[str, Any]]) -> Optional[str]:
    """基线记录的 ref；基线缺失、字段缺失或不是非空字符串时返回 ``None``。"""
    if not isinstance(baseline, dict):
        return None
    ref = baseline.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    return ref


def _baseline_commit_sha(baseline: Optional[Dict[str, Any]]) -> Optional[str]:
    """基线记录的 commit；基线缺失、字段缺失或不是非空字符串时返回 ``None``。"""
    if not isinstance(baseline, dict):
        return None
    commit_sha = baseline.get("commit_sha")
    if not isinstance(commit_sha, str) or not commit_sha.strip():
        return None
    return commit_sha