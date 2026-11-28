"""Plan inventory: listing, status, summary, usage, task rows.

Extracted from ``server.py`` on 2026-09-25. See ``routes/phases.py`` for the
late-binding rule that governs every module in this package.
"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Dict, List, Mapping, NamedTuple, Optional, Set, Tuple, Any
from fastapi import Body, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pathlib import Path
from datetime import datetime, timezone
import json
import os
import sqlite3

import capacity_wait

# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server

# ``plan_artifacts`` 是叶子模块（只依赖 pathlib / typing），所以这里可以安全地
# 模块级导入 —— 判定「这条 plan 走哪条分支」只有一个入口，见
# :func:`plan_artifacts.detect`。
from plan_artifacts import (  # noqa: E402
    PLAN_DOC_FILENAME,
    TASKS_FILENAME,
    ArtifactSet,
    detect,
)

# The read model for "what may this plan's execution status be shown as".
# It is pure, and it lives in ``plan_status`` — the module that owns "one
# plan, one status" — so this module and ``routes.execution`` cannot
# answer differently about the same plan.
from plan_status import (  # noqa: E402
    listing_status_bucket,
    normalize_exec_status,
)

# ``phase_vocabulary`` 是纯词汇模块（只依赖 logging / typing，不 import
# ``plan_state``），所以这里可以安全地模块级导入：等待相位的历史别名
# （``ready``）必须在**读路径**上归一化成现行词汇（``queued``），否则列表会
# 把旧词原样透出给前端与调度器 —— 同一个等待状态，两套词汇。
from phase_vocabulary import (  # noqa: E402
    QUEUED,
    log_deprecated_ready,
    normalize_waiting_phase,
)

router = APIRouter()

#: The plan-doc review phase — the verification branch's human review gate
#: (introduced by task 14; the phase name lives in ``plan_state``'s
#: vocabulary, this is only the constant the read paths below need).
_PLAN_DOC_REVIEW_PHASE = "verification_plan_review"


# ---------------------------------------------------------------------------
# 产物判定：读路径共用的错误语义与载荷
# ---------------------------------------------------------------------------
#
# 一个 plan 目录里可能同时躺着两类产物：``tasks.json``（开发分支）与
# ``verification-plan.md``（验证分支）。**两者共存不是冲突** —— 按「任务
# 优先」的主分支语义，这条 plan 就是一条开发分支的计划，只不过额外带着一份
# 计划书（附加产物）。
#
# 判定收敛在 :func:`plan_artifacts.detect` 这一个纯函数里（任务 1）。这里收的是
# **判定结果的消费者**：单计划状态与计划列表把**主分支**和**两份产物的相对
# 名**交回去；方向不对的生成入口给出 409 ``artifact_conflict``。
#
# 共存是**单向**的，两个方向各归各位：计划书生成**放行**（计划书是任务清单
# 之后的可选附加动作，PRD 决策点 2），任务生成**被拒**（目录里已有计划书时
# 不再写任务清单 —— 不把一条纯验证分支的计划改造成开发分支）。所以任务生成
# 入口的这个 409 描述的是「**反向生成被拒**」，不再是「两类产物互斥」。
#
# 错误码字符串 ``artifact_conflict`` **保留**：它是既有 API 契约的一部分，
# 调用方拿它识别「产物状态不符」这一类回答。
#
# 响应体里只有 plan 的**相对名**（``tasks.json`` / ``verification-plan.md``）：
# 调用方拿它去自己的 plan 目录里找文件，不必知道服务器把 plan 放在哪 ——
# 绝对路径既泄露本机布局，也让响应在别的机器上不可用。

#: 产物状态不符的机器可读错误码（两个生成入口共用一个字符串）。
ARTIFACT_CONFLICT_ERROR = "artifact_conflict"

#: 载荷里交回去的两个产物：**相对名**，不是路径。
CONFLICT_ARTIFACTS: Tuple[str, ...] = (TASKS_FILENAME, PLAN_DOC_FILENAME)


def artifact_conflict_payload(
    plan_id: str,
    artifacts: "ArtifactSet",
) -> Dict[str, Any]:
    """从判定结果拼出 409 响应体（两个生成入口逐字相同）。

    Args:
        plan_id: plan id。
        artifacts: :func:`plan_artifacts.detect` 的判定结果。

    Returns:
        响应体字典。``artifacts`` 是**两个**候选产物的相对名，
        ``existing_artifacts`` 是盘上**实际存在**的那几个，``branch`` 是判定出的
        主分支。三个字段都不含绝对路径。
    """
    existing = _artifact_names(artifacts)
    has_tasks = artifacts.tasks_path is not None
    has_plan_doc = artifacts.plan_doc_path is not None
    if has_tasks and has_plan_doc:
        # 共存：**合法**。构造器拿不到「是哪个入口在问」这条信息 —— 文案一旦
        # 说「这个入口却要生成 X」，对另一个调用者来说那句话就是假的。所以这里
        # 说**规则**，不说「这个入口」。
        #
        # 共存是**单向**的，而且两个方向各归各位：计划书生成**放行**（计划书
        # 是任务清单之后的可选附加动作，PRD 决策点 2），任务生成**被拒**（目录
        # 里已有计划书时不再写任务清单）。文案不许把放行的那一侧也说成被拒 ——
        # 「已有任务清单时不再写计划书」是一句假话。
        detail = (
            "这个 plan 目录里同时存在开发任务清单（tasks.json）与验证计划书"
            "（verification-plan.md）：共存本身是合法的（主分支是开发分支，"
            "任务优先），验证计划书是任务清单之后的可选附加动作（生成计划书"
            "这个方向是放行的）；被拒的方向只有一个 —— 目录里已有计划书时不再"
            "写任务清单（不把一条纯验证分支的计划改造成开发分支）—— 框架不"
            "替你择一，也不删除任何文件 —— 由操作者裁决。"
        )
    elif has_plan_doc:
        # 只有计划书：这个载荷只可能被**任务生成**入口触发（计划书生成入口在
        # 只有计划书时是放行的），所以这里可以说死方向。
        detail = (
            "这个 plan 目录里已经有验证计划书（verification-plan.md），"
            "它属于验证分支；这个入口却要生成开发任务清单 —— 反向生成被拒："
            "框架不替你择一，也不删除任何文件 —— 由操作者裁决。"
        )
    elif has_tasks:
        # 只有任务清单：这个载荷只可能被**计划书生成**入口触发（任务生成入口
        # 在只有任务清单时是放行的），方向同样可以说死。
        detail = (
            "这个 plan 目录里已经有开发任务清单（tasks.json），"
            "它属于开发分支；这个入口却要生成验证计划书 —— 反向生成被拒："
            "框架不替你择一，也不删除任何文件 —— 由操作者裁决。"
        )
    else:
        # 盘上两类产物都没有：两个生成入口都不会走到这里，但载荷构造器不
        # 假设调用方守规矩 —— 说不知道，比说谎好。
        detail = (
            "这个 plan 目录里既没有开发任务清单（tasks.json），也没有验证计划书"
            "（verification-plan.md）—— 产物判定说它不属于任何一条分支："
            "框架不替你择一，也不删除任何文件 —— 由操作者裁决。"
        )
    return {
        "error": ARTIFACT_CONFLICT_ERROR,
        "detail": detail,
        "plan_id": plan_id,
        "artifacts": list(CONFLICT_ARTIFACTS),
        "existing_artifacts": existing,
        "branch": artifacts.branch,
    }


def _artifact_names(artifacts: "ArtifactSet") -> List[str]:
    """判定结果里**实际存在**的产物，相对名。

    两类产物的相对名是这份载荷对外的全部信息：调用方拿它去自己的 plan 目录
    里找文件，不必知道服务器把 plan 放在哪。顺序固定（任务清单在前、计划书
    在后），所以两个读者对同一条 plan 看到的顺序一样。
    """
    return [
        name
        for name, path in (
            (TASKS_FILENAME, artifacts.tasks_path),
            (PLAN_DOC_FILENAME, artifacts.plan_doc_path),
        )
        if path is not None
    ]


def _stable_phase_label(phase: Optional[str]) -> Optional[str]:
    """The stable label of ``phase``; ``None`` when the phase has none.

    The labels live in **one** place — ``plan_state.PHASE_LABELS`` (task
    14) — and every display surface reads them from there, so the same
    phase cannot be labelled two ways in two endpoints.
    """
    from plan_state import PHASE_LABELS

    key = str(phase or "")
    if not key:
        return None
    return PHASE_LABELS.get(key)


def _phase_label(phase: Optional[str]) -> str:
    """The label to display for ``phase``.

    The stable label when the phase has one, the phase name otherwise: a
    label is a **supplement** to the phase name, never a replacement for
    it (see ``plan_state.PHASE_LABELS``). An unknown phase therefore
    still has a name — it just does not have a pretty one.
    """
    stable = _stable_phase_label(phase)
    if stable is not None:
        return stable
    return str(phase or "")


def _artifact_status(
    *,
    has_tasks: bool,
    has_review: bool,
    has_prd: bool,
    has_interview: bool,
) -> str:
    """The status this plan's ARTEFACTS put it in.

    The answer for a plan that is NOT running — the phase it is in once
    the execution store has refuted the routing row's claim that it is.

    Shared by the listing and the single-plan status endpoint so the two
    readers cannot answer differently about the same plan: they used to
    (the listing fell back to this, the status endpoint repeated the
    routing row instead), and one plan with two answers is the whole
    problem this module exists to kill.

    Note the vocabulary is the listing's ``status`` vocabulary, not the
    phase vocabulary — ``reviewed`` / ``prd`` / ``interviewed`` / ``new``
    are not valid phases.  Only ``queued`` is in both: it is the single
    waiting phase, so the bucket for "tasks exist, nobody started them"
    is spelled the same way the phase is (``ready`` is only its legacy
    input alias and must not be emitted).
    """
    if has_tasks:
        return "queued"
    if has_review:
        return "reviewed"
    if has_prd:
        return "prd"
    if has_interview:
        return "interviewed"
    return "new"


def _artifact_status_for_plan(plan_id: str) -> str:
    """:func:`_artifact_status` for the plan's directory on disk."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        return "new"
    return _artifact_status(
        has_tasks=(plan_dir / "tasks.json").exists(),
        has_review=(plan_dir / "review.json").exists(),
        has_prd=(
            (plan_dir / "prd.json").exists()
            or (plan_dir / "prd.md").exists()
        ),
        has_interview=(plan_dir / "interview.json").exists(),
    )


def _execution_record(
    conn: sqlite3.Connection, plan_id: str,
) -> Optional[Dict[str, Any]]:
    """The ``plan_execution`` row for *plan_id*, or ``None``.

    ``None`` means "no execution record at all" — the executor never
    touched this plan, or its row was never written.  That is the only
    thing the runtime-phase rule needs to know, and it is the same
    question the listing asks with its bulk snapshot.
    """
    try:
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
    except Exception:  # pragma: no cover - import guard
        return None
    try:
        return ExecutionRepository(conn).summary(plan_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# How a degraded plans listing says so
# ---------------------------------------------------------------------------
#
# The listing filters the terminal phases (``failed``, ``completed``,
# ``stopped``, …) out of the default view, and the phase it filters on
# lives in ``plan_routing.current_phase``.  When the state database
# cannot be opened there is no phase to read: the listing degrades to a
# directory scan and answers with EVERY plan it can see.
#
# That answer is honest about what it knows and silent about what it
# does not, and the silence is the dangerous part.  "200 + a full list"
# is exactly what a reader takes for "the terminal plans were filtered
# out" — so a degraded listing has to announce itself rather than let
# the operator draw that conclusion from the shape of the answer.
#
# The verdict is reported twice, because there are two kinds of reader:
#
#   * on every row (``list_degraded`` / ``list_degraded_reason``), for
#     the consumer that reads the body — and, because a row travels
#     with the data, for the one that stores it;
#   * on the response (``X-PDT-Plans-List-Degraded``), for the consumer
#     that reads the status of the call rather than the payload.
#
# The marker is ABSENT from a listing that could read the phases.  A
# marker that is always on would say nothing, so the absence is half of
# the contract: it is what tells the two listings apart.
LIST_DEGRADED_KEY = "list_degraded"
LIST_DEGRADED_REASON = "state_db_unavailable"
LIST_DEGRADED_HEADER = "X-PDT-Plans-List-Degraded"


# ---------------------------------------------------------------------------
# Where the listing came from
# ---------------------------------------------------------------------------
#
# A listing answers 200 with an array of plans, and an array says
# nothing about WHERE it came from: an empty array from a process
# pointed at another worktree's plans root is the same shape as "this
# instance really has no plans".  A reader has to be able to tell the
# two apart, so every listing carries its provenance — which data
# source produced the rows, and which plans root that source read.
#
# The data source is deliberately coarse.  It answers the one question
# the rows themselves cannot: were the phases read from the state
# database (so the terminal filter was applied), or did the listing
# degrade to a directory scan (so it was not)?
LIST_DATA_SOURCE_STATE_DB = "state_db"
LIST_DATA_SOURCE_DIRECTORY = "directory"

#: The row-level spelling of the two provenance values.
LIST_DATA_SOURCE_KEY = "data_source"
LIST_PLANS_DIR_KEY = "plans_dir"

#: The facts every answer about this instance's data plane carries.
#:
#:   * ``kind`` — where the ROWS came from: ``state_db`` (the phases
#:     were read, so the terminal filter was applied) or ``directory``
#:     (the listing degraded to a scan);
#:   * ``state_db_path`` / ``state_db_inode`` — the database this
#:     instance reads, and the inode of that file.  The inode matters:
#:     a DIFFERENT file at the same path is a different data plane;
#:   * ``boot_id`` / ``pid`` — the process that produced the answer.
#:     The boot id is minted once per process and never changes, so two
#:     answers carrying the same one came from the same process;
#:   * ``row_count`` — how many rows the answer was built from.
#:
#: The reason they are on every row is the shape of the answer they
#: travel with: an empty array cannot say any of this by itself, and
#: an empty array is exactly what an instance serving ANOTHER tree's
#: data plane produces.
LIST_DATA_SOURCE_FIELDS = (
    "kind",
    "state_db_path",
    "state_db_inode",
    "boot_id",
    "pid",
    "row_count",
)

#: The reason a listing refuses to answer: the state database this
#: instance reads does not describe the plans tree it serves.
LIST_DATA_PLANE_MISMATCH_REASON = "state_db_does_not_describe_plans_dir"

#: The detail every such refusal answers with.
LIST_DATA_PLANE_MISMATCH_DETAIL = (
    "plans listing data source does not describe the plans directory"
)

#: The response headers carrying the same two values, for the consumer
#: that reads the status of the call rather than the payload.  The
#: first spelling of each is the canonical one; the rest are aliases,
#: so a consumer does not have to guess which one this route uses.
LIST_DATA_SOURCE_HEADERS = (
    "X-PDT-Plans-Data-Source",
    "X-PDT-Plans-Source",
)
LIST_PLANS_DIR_HEADERS = (
    "X-PDT-Plans-Dir",
    "X-PDT-Plans-Plans-Dir",
    "X-PDT-Plans-Root",
)

#: The phases that end a plan.  ``waiting_for_quota`` is NOT one of
#: them: a plan parked for capacity is alive, and it resumes when the
#: quota window opens.
#:
#: 2026-11-09: ``archived`` is one of them — an archived plan is
#: shelved for good, so the default listing (which drops terminal
#: plans) must drop it too.  It is a *phase*, not the ``archived``
#: flag this module derives for directory-archived plans: that flag
#: says "this plan's directory was moved out of the tree", this phase
#: says "we archived this plan and it never runs again".
TERMINAL_PHASES = frozenset({
    "completed", "failed", "stopped",
    "verification_passed",
    "verification_failed",
    "archived",
})

#: The execution verdicts that end a plan even when the routing phase
#: does not say so.  A run that failed or was stopped is over, whatever
#: the workflow phase says — and the workflow phase is exactly what
#: stays behind when a process dies: the crash path persists the
#: verdict on the execution row and the routing row never catches up.
TERMINAL_EXEC_STATUSES = frozenset({"failed", "stopped"})

#: The phases that end a plan's **workflow** — the plan's own answer,
#: as opposed to the run's answer (:data:`TERMINAL_EXEC_STATUSES`).
#:
#: ``failed`` / ``stopped`` are deliberately NOT in this set: they are
#: the words the *run* uses, and a run that died says nothing about a
#: verification loop that is still going.  A plan that carries a
#: verification row is therefore judged by this set alone — see
#: :func:`_workflow_phase_is_terminal`.
WORKFLOW_TERMINAL_PHASES = frozenset({
    "completed", "verification_passed", "verification_failed",
})


#: The prefix of the stop reason an eviction writes on the victim's
#: execution row: ``evicted:<requester>`` — the run was stopped because
#: another plan needed the capacity, and the victim was put back in the
#: queue to wait for its next turn.
#:
#: The reason is what tells the terminal verdict (see
#: :func:`_plan_is_terminal`) that a stopped run does not always mean a
#: dead plan.
EVICTED_STOP_REASON_PREFIX = "evicted:"

#: The execution verdict an eviction writes: the run is over, the
#: executor process is gone, the plan itself is not.
STOPPED_EXEC_STATUS = "stopped"

#: The provenance of the last rendered listing, per view.  The rendered
#: rows carry the same two values, so this is only needed for an EMPTY
#: listing — the one answer with no row to carry the provenance, and
#: the one answer that needs it most: "no plans here" and "another
#: instance's plans root" look alike without it.
_LISTING_PROVENANCE: Dict[bool, Dict[str, Any]] = {}


def _data_source_kind(source: Any) -> str:
    """The ``kind`` of a data-source block: ``state_db`` / ``directory``.

    The block itself carries the facts; the header carries the kind,
    because a header is a flat string and the kind is the one word a
    consumer needs to branch on.
    """
    if isinstance(source, Mapping):
        return str(source.get("kind") or "")
    return str(source or "")


def _data_source_block(
    kind: str,
    db_path: Optional[Path],
    row_count: int,
) -> Dict[str, Any]:
    """The facts about the data plane a listing was produced from.

    ``kind`` says where the ROWS came from; the rest says which
    instance produced them and which database it read.  The two are
    read off the same process — the one serving the request — so the
    two cannot drift apart the way a label and a filename can.
    """
    try:
        identity = _server._instance_identity(db_path)
    except Exception:  # noqa: BLE001 - an identity block must never 500
        identity = {}
    values = {
        "kind": kind,
        "state_db_path": identity.get("state_db_path"),
        "state_db_inode": identity.get("state_db_inode"),
        "boot_id": identity.get("boot_id"),
        "pid": identity.get("pid"),
        "row_count": row_count,
    }
    # The field list is the contract: a block carries exactly these
    # fields, in this order, and a new one is added by naming it there.
    return {field: values.get(field) for field in LIST_DATA_SOURCE_FIELDS}


def _resolved_state_db_path(request: Optional[Request] = None) -> Optional[Path]:
    """The state database this instance reads, or ``None``.

    ``None`` means "could not be resolved" — the live-database guard
    under pytest refuses to name it.  A listing that cannot name its
    database says so rather than naming the wrong one.
    """
    try:
        return _server._state_db_path(request)
    except Exception:  # noqa: BLE001 - a path that cannot be resolved
        return None


def _listing_provenance(data_source: Dict[str, Any]) -> Dict[str, Any]:
    """The provenance of a listing: its data source and its plans root."""
    return {
        LIST_DATA_SOURCE_KEY: data_source,
        LIST_PLANS_DIR_KEY: str(_server.PLANS_DIR),
    }


def _listing_provenance_from_rows(
    plans: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The provenance carried by the rendered rows, or ``None``.

    ``None`` means "the rows carry none" — an empty listing.  The rows
    and the remembered value are written together, so they always
    agree; the remembered value covers the empty case, where there is
    no row left to ask.
    """
    for row in plans:
        if isinstance(row, dict) and row.get(LIST_DATA_SOURCE_KEY):
            return {
                LIST_DATA_SOURCE_KEY: row.get(LIST_DATA_SOURCE_KEY),
                LIST_PLANS_DIR_KEY: str(
                    row.get(LIST_PLANS_DIR_KEY) or _server.PLANS_DIR
                ),
            }
    return None


def _remember_listing_provenance(
    include_terminal: bool,
    provenance: Dict[str, str],
) -> None:
    """Remember a listing's provenance for the empty-listing case."""
    _LISTING_PROVENANCE[bool(include_terminal)] = dict(provenance)


def _persisted_exec_status(exec_row: Any) -> Optional[str]:
    """``plan_execution.exec_status`` of one execution row, or ``None``.

    ``None`` means "there is no verdict to read" — no row, an empty
    status, or a listing that could not read the state database.  It
    never means "the plan failed".
    """
    if not isinstance(exec_row, dict):
        return None
    status = str(exec_row.get("exec_status") or "").strip()
    return status or None


def _workflow_phase_is_terminal(
    current_phase: Any,
    *,
    has_verification: bool = False,
) -> bool:
    """Is the **workflow's** verdict terminal?

    Two vocabularies live here and they must not be mixed:

      * the workflow's own answer — the phase the plan reached:
        ``completed`` / ``verification_passed`` / ``verification_failed``
        (:data:`WORKFLOW_TERMINAL_PHASES`);
      * the run's answer — the last execution's outcome: ``failed`` /
        ``stopped`` (:data:`TERMINAL_EXEC_STATUSES`).

    A plan that carries a ``plan_verification`` row is judged by the
    first one alone.  Its run may well have died — that is exactly
    what the recovery scan writes — but the verification loop it
    started is a separate piece of work, and the panel has to be able
    to show it: a plan in a verification round, a plan whose
    verification can be started, and the result of a verification loop
    that stopped are all things a reader must still be able to see.

    Without a verification row nothing changes: the phase vocabulary is
    the full :data:`TERMINAL_PHASES` set, and the run's verdict hides
    the plan on its own (see :func:`_plan_is_terminal`).
    """
    phase = str(current_phase or "").strip()
    if has_verification:
        return phase in WORKFLOW_TERMINAL_PHASES
    return phase in TERMINAL_PHASES


def _persisted_stop_reason(exec_row: Any) -> Optional[str]:
    """``plan_execution.stop_reason`` of one execution row, or ``None``.

    ``None`` means "there is no reason to read" — no row, or an empty
    reason.  It never means "the run stopped for no reason": the reason
    is what tells the terminal verdict *why* a run ended, and an empty
    reason is no reason at all.
    """
    if not isinstance(exec_row, dict):
        return None
    reason = str(exec_row.get("stop_reason") or "").strip()
    return reason or None


def _plan_is_terminal(
    current_phase: Any,
    exec_status: Any,
    stop_reason: Any = None,
    *,
    has_verification: bool = False,
) -> bool:
    """Is this plan in a terminal state?

    Three verdicts, one rule:

      * the routing phase — the workflow's own answer.  A terminal
        phase ends the plan;
      * the persisted execution verdict — the run's own answer.  A run
        that failed or was stopped is over, whatever the workflow phase
        says;
      * the persisted stop reason — why that run ended.

    The second verdict is what the phase alone cannot give: when a
    process dies, the crash path persists ``failed`` on the execution
    row and the routing row stays wherever the workflow left it.  A
    listing that reads only the routing phase keeps such a plan in the
    list, as if it were still running.

    The third verdict is the one narrow exception to the second.  A
    plan the scheduler **evicted** gave way to another plan that needed
    the capacity: the run is over (``stopped``) and the plan was put
    **back in the queue** to wait for its next turn.  Its two verdicts
    contradict each other — the phase says ``queued``, the execution
    row says ``stopped`` — and a reader of only those two hides a plan
    that is still going to run.  The stop reason says which of the two
    meanings it is: ``evicted:<requester>`` means "the plan is waiting
    for its turn", not "the plan is over".

    The exception is deliberately narrow.  It needs the phase to be
    ``queued`` (a plan whose phase is ``stopped`` — the requeue that
    failed — is over whatever the reason says), it needs the run to have
    been stopped rather than failed (the crash path writes ``failed``,
    and a crashed run leaves no plan to wait), and it needs the reason
    to say ``evicted:`` (a run the operator stopped is over).  A
    two-argument call — every caller that predates the reason — keeps
    the old rule exactly.

    ``waiting_for_quota`` is not terminal: a plan parked for capacity
    is alive and resumes when the window opens, so it never enters
    :data:`TERMINAL_PHASES`.

    ``has_verification`` says the plan carries a ``plan_verification``
    row — it is in, or has been in, a verification loop.  Then the
    workflow's verdict alone decides: the run's verdict is about a
    *process*, and a dead process says nothing about a verification
    loop that is still going.  See :func:`_workflow_phase_is_terminal`.
    """
    if _workflow_phase_is_terminal(
        current_phase, has_verification=has_verification
    ):
        return True
    if has_verification:
        # The run's verdict is not this plan's verdict: the verification
        # loop is a separate piece of work, and it is still visible.
        return False
    phase = str(current_phase or "").strip()
    status = str(exec_status or "").strip()
    if status not in TERMINAL_EXEC_STATUSES:
        return False
    if status == STOPPED_EXEC_STATUS and phase == QUEUED:
        reason = str(stop_reason or "").strip()
        if reason.startswith(EVICTED_STOP_REASON_PREFIX):
            # The run is over, the plan is not: it gave way to another
            # plan and is waiting for its next turn.
            return False
    return True


def _mark_listing_provenance(
    response: Response,
    plans: List[Dict[str, Any]],
    include_terminal: bool,
) -> None:
    """Say, on the response, which tree this listing came from.

    The provenance is read off the rows when there are any.  An empty
    listing has no row to carry it, so the remembered provenance of the
    last rendering is used instead — and only when it was rendered from
    the root in effect now, because a provenance of another root would
    be worse than no provenance at all.
    """
    provenance = _listing_provenance_from_rows(plans)
    if provenance is None:
        remembered = _LISTING_PROVENANCE.get(bool(include_terminal))
        if remembered is not None and (
            remembered.get(LIST_PLANS_DIR_KEY) == str(_server.PLANS_DIR)
        ):
            provenance = remembered
    if provenance is None:
        return
    for header in LIST_DATA_SOURCE_HEADERS:
        response.headers[header] = _data_source_kind(
            provenance[LIST_DATA_SOURCE_KEY]
        )
    for header in LIST_PLANS_DIR_HEADERS:
        response.headers[header] = provenance[LIST_PLANS_DIR_KEY]


def _mark_listing_degraded(
    response: Response,
    plans: List[Dict[str, Any]],
) -> None:
    """Put the degradation verdict on the response, if the listing degraded.

    The verdict is read off the rows rather than recomputed: the rows
    are the ones the degraded path marked, and they are cached together
    with the rest of the payload, so a cache HIT reports the same
    degradation the cache MISS did.  A listing that could read the
    phases carries no marker, and therefore no header — that is the
    negative half of the contract.
    """
    if any(
        isinstance(row, dict) and row.get(LIST_DEGRADED_KEY)
        for row in plans
    ):
        response.headers[LIST_DEGRADED_HEADER] = LIST_DEGRADED_REASON


#: The prefix that marks a directory under the plans root as the
#: framework's own runtime state rather than a plan.
#:
#: Plan ids are ``<YYYYMMDD>-<slug>`` (``framework.ids.derive_plan_id``)
#: and ``slugify_plan_id`` strips leading and trailing ``-`` and ``.``, so
#: no plan directory can start with a dot.  The reaper's runtime
#: directory (``orphan_reaper.REAPER_DIRNAME``) does, and it is hidden on
#: purpose.
_INTERNAL_DIR_PREFIX = "."

#: The files that make a directory under the plans root a PLAN.
#:
#: A directory that carries none of them is not a plan — it is somebody
#: else's bookkeeping that happens to live under the plans root.  The
#: verification round writes its own root-cause archives there
#: (``<plan-dir>/reports/``), a reaped run leaves its directory behind,
#: and a scratch directory can be created by anything.  None of them is
#: a plan, and none of them says anything about the state database.
#:
#: The names are the ones the framework already uses for a plan's own
#: artifacts — the import, the PRD and its review, and the two branch
#: markers :func:`plan_artifacts.detect` reads (``tasks.json`` for the
#: dev branch, ``verification-plan.md`` for the verification branch).
#:
#: Two kinds of file are deliberately NOT here.  The legacy state
#: sidecars (``plan_state.json`` and ``execution.json``) are pinned as
#: "do not read" by the VP-006 static gate — production code may not
#: name them — and a plan directory that carries one of them carries a
#: task list beside it anyway.  And the OUTPUTS of a verification run
#: (``verification_plan.json``, ``verification_report.json``,
#: ``preflight_report.json``) are the run's own bookkeeping: a round
#: writes them into whatever directory it was pointed at, so they
#: cannot be what says "this directory is a plan".
PLAN_ARTIFACT_FILENAMES = (
    TASKS_FILENAME,
    PLAN_DOC_FILENAME,
    "interview.json",
    "prd.json",
    "prd.md",
    "review.json",
)


# ---------------------------------------------------------------------------
# When the data plane does not describe the tree
# ---------------------------------------------------------------------------
#
# A plans tree and a state database are two halves of one instance's
# world: the tree says which plans exist on disk, the database says what
# phase they are in.  When the tree holds plans and the database knows
# NONE of them, the two halves belong to DIFFERENT instances — the
# database is another worktree's, or another instance's.
#
# The listing built from that database is empty, and an empty array is
# exactly what "this instance has no plans" looks like on the wire.
# That is the shape the 2026-10-26 round kept meeting: the same URL
# answered ``[]`` twice and three plans once, and nothing but the
# instance behind the port had changed.
#
# So the listing REFUSES in that case instead of answering.  A refusal
# is honest ("I cannot answer this, and here is the data plane I could
# not read"); an empty array is a coincidence of shape that a reader
# takes for a fact about the plans.


class PlansDataPlaneMismatch(Exception):
    """The state database does not describe the plans tree this instance serves.

    Raised by the listing when the tree holds plans and the database
    knows none of them.  It carries the data plane that could not
    answer, so the refusal can name it.
    """

    def __init__(
        self,
        data_source: Dict[str, Any],
        directory_plan_ids: List[str],
    ) -> None:
        super().__init__(LIST_DATA_PLANE_MISMATCH_DETAIL)
        self.data_source = data_source
        self.directory_plan_ids = list(directory_plan_ids)


def _carries_plan_artifact(plan_dir: Path) -> bool:
    """Does this directory hold an artifact that only a plan holds?

    The answer decides whether the directory counts as tree evidence for
    the mismatch check.  A directory whose artifacts cannot be listed
    (an unreadable directory) contributes nothing: an unreadable
    directory is not evidence of a mismatch, and treating it as one
    would turn a permissions problem into a refusal.
    """
    for filename in PLAN_ARTIFACT_FILENAMES:
        try:
            if (plan_dir / filename).exists():
                return True
        except OSError:  # pragma: no cover - unreadable directory
            continue
    return False


def _tree_dir_names(plans_dir: Path) -> List[str]:
    """The names of EVERY directory under the plans root.

    A directory whose name starts with a DOT is the framework's own
    runtime state, not plan content.  A generated plan id is
    ``<YYYYMMDD>-<slug>`` (``framework.ids.derive_plan_id``): the date
    stamp leads, and ``slugify_plan_id`` strips leading and trailing
    ``-`` and ``.`` from the slug, so a GENERATED plan directory never
    begins with a dot.  The dot-prefixed names under the plans root are
    the framework's own runtime state — the reaper's directory
    (``orphan_reaper.REAPER_DIRNAME``, holding ``reaper.lock`` and
    ``daemon.log``) is the one that lives there — and they are hidden on
    purpose.

    Counting one of them as a plan made the mismatch check below refuse
    an instance that simply had no plans: the tree "held a plan" the
    database knew nothing about, so the answer was a 503 where the truth
    was an empty list.  The rejection contract itself is untouched — it
    still refuses a database that knows none of the REAL plans in the
    tree.

    A directory that cannot be read contributes nothing: an unreadable
    tree is not evidence of a mismatch, and treating it as one would
    turn a permissions problem into a refusal.
    """
    try:
        entries = [
            entry
            for entry in plans_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith(_INTERNAL_DIR_PREFIX)
        ]
    except OSError:
        return []
    return sorted(entry.name for entry in entries)


def _plan_dir_names(plans_dir: Path) -> List[str]:
    """The names of the PLAN directories under the plans root.

    "There is a directory here" is not "there is a plan here": the
    verification round writes its root-cause archives under the plans
    root (``<plan-dir>/reports/``), a reaped run leaves its directory
    behind, and a scratch directory can be created by anything.  Such a
    directory says nothing about the state database, so counting it as
    tree evidence refused instances that really had no plans.  A
    directory is tree evidence only when it carries a plan artifact —
    the same rule the rest of the framework uses to tell a plan from a
    non-plan.

    What the rule costs: a plan whose artifacts have not been written
    yet cannot PROVE that the tree holds a plan.  It can still prove
    that the database describes this tree — the refusal asks that
    question separately, against the tree's own directories (see
    :func:`_refuse_when_data_source_does_not_describe_the_tree`) — and
    such a plan is still LISTED, because the listing is built from the
    database's rows and not from this scan.
    """
    return [
        name
        for name in _tree_dir_names(plans_dir)
        if _carries_plan_artifact(plans_dir / name)
    ]


def _plan_ids_known_to_db(conn: Any) -> Set[str]:
    """The plan ids the state database holds a row for.

    EVERY ``plan_*`` table counts, not just ``plan_routing``: a plan
    can be in the tree with nothing but a verification row (its
    workflow phase was never routed), and that is a real state — the
    database describes that tree.  Only a database that knows NONE of
    the plans in the tree belongs to somebody else.

    The tables are discovered from the database itself, so a table
    added later is counted without this function knowing its name.
    """
    known: Set[str] = set()
    try:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name LIKE 'plan%'"
            )
        ]
    except Exception:  # noqa: BLE001 - an unreadable schema is not a mismatch
        return known
    for table in tables:
        quoted = table.replace('"', '""')
        try:
            columns = {
                str(row[1]) for row in conn.execute(f'PRAGMA table_info("{quoted}")')
            }
            if "plan_id" not in columns:
                continue
            for row in conn.execute(
                f'SELECT DISTINCT plan_id FROM "{quoted}"'
            ):
                if row and row[0]:
                    known.add(str(row[0]))
        except Exception:  # noqa: BLE001 - one unreadable table is not a mismatch
            continue
    return known


def _refuse_when_data_source_does_not_describe_the_tree(
    known_plan_ids: Set[str],
    data_source: Dict[str, Any],
) -> None:
    """Refuse to answer when the data plane does not describe the tree.

    Two questions, two kinds of evidence:

      * does the tree HOLD a plan?  A directory that carries a plan
        artifact does; a directory that carries none is somebody else's
        bookkeeping that happens to live under the plans root (the
        verification round's ``reports/`` archive, a reaped run's
        leftover).  A tree that holds no plan has nothing for the
        database to describe, so there is nothing to refuse about.
      * does the DATABASE describe this tree?  The check is an
        INTERSECTION, not a count: as long as the database knows ONE
        plan that lives in this tree, the two halves belong together and
        the listing answers normally.  Only when it knows NONE of them
        is the database somebody else's — and only then does an empty
        answer become a lie.

    The two questions are asked separately on purpose.  A plan whose
    artifacts have not been written yet carries no artifact, so it
    cannot answer the first question — but the database knows it and its
    directory is in this tree, so it answers the second one, and the
    listing answers.

    The archived plans count as known: they are read out of the tree
    itself, so a tree of archived plans is a tree this instance can
    describe.
    """
    directory_plan_ids = _plan_dir_names(_server.PLANS_DIR)
    if not directory_plan_ids:
        return
    if set(_tree_dir_names(_server.PLANS_DIR)) & known_plan_ids:
        return
    raise PlansDataPlaneMismatch(data_source, directory_plan_ids)


def _plans_data_plane_mismatch_response(
    mismatch: PlansDataPlaneMismatch,
) -> JSONResponse:
    """The refusal: ``503``, with the data plane that could not answer.

    The body carries the same facts the successful answer carries — the
    database by path and by inode, the process that answered, how many
    rows it found — so a reader can see WHICH database was asked and go
    and look at it.
    """
    return JSONResponse(
        status_code=503,
        content={
            "detail": LIST_DATA_PLANE_MISMATCH_DETAIL,
            LIST_DATA_SOURCE_KEY: mismatch.data_source,
            LIST_PLANS_DIR_KEY: str(_server.PLANS_DIR),
            "reason": LIST_DATA_PLANE_MISMATCH_REASON,
            "directory_plan_count": len(mismatch.directory_plan_ids),
        },
    )


def _read_task_rows(plan_id: str) -> Dict[str, Dict[str, Any]]:
    """``task_id -> row`` from ``plan_tasks``; ``{}`` when unreadable."""
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
    except Exception:  # pragma: no cover - import guard
        return {}
    try:
        conn = open_db(_server._state_db_path())
    except Exception:
        return {}
    try:
        rows = PlanTaskRepository(conn).load_all(plan_id)
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {
        str(tid): row for tid, row in (rows or {}).items()
        if isinstance(row, dict)
    }


def _read_tasks_payload(plan_id: str) -> Any:
    """The plan's parsed ``tasks.json``; ``None`` when it cannot be read.

    Only the *top level* of that file matters to the phase rule: the
    quota marker lives there (``stop_reason`` / ``reason_detail``),
    written by the executor when a run parks because every provider had
    no capacity left.

    ``None`` means "no readable file" — not "an empty plan".  The
    difference is the whole point: a plan with no ``tasks.json`` is a
    plan this rule knows nothing about, and a rule that cannot tell
    that apart from an empty plan is a rule that guesses.
    """
    try:
        raw = (_server._plan_dir(plan_id) / "tasks.json").read_text()
        return json.loads(raw)
    except (OSError, ValueError):
        return None


def _read_disk_task_ids(plan_id: str) -> Optional[set]:
    """Task ids in ``plans/<id>/tasks.json``; ``None`` when unreadable.

    ``None`` and ``set()`` mean different things: ``None`` is "no static
    DAG to compare against, so do not judge orphans", ``set()`` is "the
    static DAG is empty, so every DB row is an orphan".
    """
    data = _read_tasks_payload(plan_id)
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        return None
    return {
        str(t.get("id")) for t in tasks
        if isinstance(t, dict) and t.get("id")
    }


def _task_counts(
    rows: Dict[str, Dict[str, Any]], disk_ids: Optional[set] = None,
) -> Dict[str, int]:
    """Counted the way the card counts, or the two will disagree.

    Three things are not work and must not reach ``total``:

    * ``superseded`` rows — the dispatcher refuses to schedule them, and
      their work now belongs to the children that took it over.
    * ``voided`` rows (2026-10-16) — the premise was revoked, so they will
      never run. Same reason as ``superseded``, and deliberately counted the
      same way rather than given a bucket: a voided task is a decision, not
      a phase, and ``_STATUS_TASK_KEYS`` (the five phases a plan passes
      through) is the wrong home for it.
    * **terminal** rows that exist only in ``plan_tasks`` — "DB-only
      orphans". The card already drops these (``drop_terminal_db_orphans``);
      counting them here would put the status object's ``total`` out of
      step with the progress bar rendered right below the same header.
    """
    counts = {key: 0 for key in _server._STATUS_TASK_KEYS}
    counts["total"] = 0
    for task_id, row in rows.items():
        status = str(row.get("status") or "").strip().lower() or "pending"
        if status in ("superseded", "voided"):
            continue
        if (
            disk_ids is not None
            and task_id not in disk_ids
            and status in _server._TERMINAL_TASK_STATUSES
        ):
            continue
        if status in counts:
            counts[status] += 1
        counts["total"] += 1
    return counts


def _log_divergences(status: Any) -> None:
    """Say it out loud — once, per read, with everything needed to triage.

    Every state write in this backend is best-effort by design (a failed
    write must never break the workflow), so a divergence is otherwise
    silent. a production plan lied on the operator's card for
    80 minutes and the log recorded nothing at all; this is the line that
    would have caught it in the first second.
    """
    _server.logger.warning(
        "[plan_status] divergence plan=%s phase=%s verification=%s "
        "exec_in_flight=%s verif_in_flight=%s :: %s",
        status.plan_id, status.phase, status.verification_status,
        status.execution_in_flight, status.verification_in_flight,
        " | ".join(str(d) for d in status.divergences),
    )


# ---------------------------------------------------------------------------
# Where the verification fields come from
# ---------------------------------------------------------------------------
#
# ONE source: the ``plan_verification`` row.  ``plan_routing.verification``
# used to be a second one — a mirror column the state machine advanced at
# every round boundary — and :func:`_build_plan_status` reconciled the two:
# a TERMINAL mirror verdict overrode a stale ``plan_verification`` value,
# and said so in a rate-limited warning.
#
# That overlay is gone, because the mirror no longer carries a verdict:
# the round-close writes go to ``plan_verification`` (the authoritative
# table), and the column's one remaining writer —
# ``RoutingRepository.write_verification_baseline`` — freezes the reviewed
# baseline, and the hash of the document it was frozen from, into it.  A
# column whose only content is a frozen baseline cannot answer "what is
# this plan's verdict", so there is nothing in it for the read path to
# reconcile against, and letting it override the authoritative row
# published exactly that — the card header said "failed" above a body
# reading "正在跑 [repair-r1-01]".  The overlay was also a read-side patch
# for a write-side miss, which its own comment said did not belong there:
# "the fix belongs in the round-close write, not in the read path".
#
# The column itself was NOT deleted, and this note must not say it was:
# the baseline lives there and nowhere else.  "Nobody writes it any more"
# would be the wrong reason to stop reading it — the right reason is
# that what it holds is not verification state.
#
# The reconciliation's rate-limit table went with it: it existed only to
# throttle the warning that overlay produced.


# ---------------------------------------------------------------------------
# Where the phase comes from
# ---------------------------------------------------------------------------
#
# A plan's phase is a fact about THAT plan, and it is written down in
# that plan's own durable rows: the routing row's ``current_phase`` and
# the execution row's own verdict.  Everything else — this process's
# memory, the plans tree this instance happens to be pointed at, a value
# the implementation happened to be holding — is CONTEXT, and context
# changes while the plan does not.
#
# On 2026-10-25 that distinction cost a verification round.  Two plans
# with two different states — one parked for capacity, one merely waiting
# for a human — were both answered ``404 Plan not found`` by an instance
# whose tree held neither.  The answer was about the INSTANCE, and it
# collapsed every plan at once; and the collapse was silent, because a
# 404 is still an answer and looks like one.
#
# So the rule is a function of the rows, and the rows are its only
# inputs.  And a plan whose rows cannot be read is ANSWERED — "I cannot
# say", with the data source named — rather than handed a default
# phase: a default is a claim nobody made, and the one default that
# must never exist is the parked one, because it is global.

#: The refusal the phase rule makes when the plan's rows are not there.
#: It names the store that could not answer with
#: :data:`LIST_DATA_SOURCE_STATE_DB` — the same constant the plans
#: listing uses for the store its rows came from, so the two answers can
#: be compared field by field: a listing that says ``state_db`` and a
#: refusal that says ``state_db`` are talking about the same store.
class PlanDataMissing(RuntimeError):
    """The plan's durable state could not be read.

    Raised when the rows that decide a plan's phase are not there — no
    routing row, no execution row, or a state database that could not
    be read at all.  It is a REFUSAL, not a verdict: the instance says
    "I cannot answer this", and names the store that could not.

    The alternative — answering a phase nobody recorded — is how a
    default becomes global.  Every plan the instance cannot see would
    then read as the same phase, and the operator would be sent after a
    capacity wall that exactly one plan ever hit.
    """

    def __init__(
        self,
        data_source: str = LIST_DATA_SOURCE_STATE_DB,
        detail: str = "plan data missing",
    ) -> None:
        super().__init__(detail)
        self.data_source = data_source
        self.detail = detail


def _routing_phase_of(routing_rows: Any) -> str:
    """The routing row's own ``current_phase`` — ``""`` when there is none.

    ``routing_rows`` is the row (or the rows) read from ``state.db``.
    The plural is the honest shape of the question: this function is
    handed what was read, and it does not go looking for more.
    """
    if isinstance(routing_rows, Mapping):
        rows: List[Any] = [routing_rows]
    elif isinstance(routing_rows, (list, tuple)):
        rows = list(routing_rows)
    else:
        rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        phase = str(row.get("current_phase") or "").strip()
        if phase:
            return phase
    return ""


def derive_phase(
    routing_rows: Any,
    execution_row: Optional[Mapping[str, Any]] = None,
    marker: Any = None,
) -> Any:
    """The plan's phase, and the tier that answered.

    Pure: the three arguments are the only facts it reads.  No clock,
    no environment, no module-level registry — a rule that could see
    the process would answer a question about the PROCESS, and the
    process changes (it restarts, it serves other plans) while the plan
    does not.

    The priority is fixed here rather than in whichever route happened
    to read the rows first:

        routing row  >  durable execution row  >  resting default

    * **The routing row** (``plan_routing.current_phase``) is the
      workflow-phase authority: it is what ``/execution/start`` CASes
      and what every card renders.  When it is there, it is the answer.

    * **The durable execution row** is consulted only when the routing
      row could not be read, and it may answer ``waiting_for_quota``
      when **both** halves hold: the row's ``stop_reason`` says the run
      stopped for capacity, *and* the plan's ``tasks.json`` carries a
      capacity marker that parses.  Either half alone is not enough —
      the row says the run stopped, the marker says it stopped for
      capacity.

    * **The resting default** (``ready`` — the manual gate) is where
      every other shape lands.  Never on the parked phase.

    ``marker`` is the plan's capacity marker *as it was found*: the
    ``tasks.json`` payload that carries it, or the marker itself.  It is
    read through :func:`capacity_wait.parse_capacity_marker`, the single
    entry point for marker parsing — one reader, so that "is this a
    capacity fact?" has one answer.

    Raises:
        PlanDataMissing: when the rows carry no phase at all — no
            routing row and no execution row.  The rule refuses rather
            than inventing one; the caller decides what to answer.
    """
    from plan_status import (
        DEFAULT_PHASE,
        PHASE_SOURCE_DEFAULT,
        PHASE_SOURCE_EXECUTION,
        PHASE_SOURCE_ROUTING,
        QUOTA_WAIT_PHASE,
        DerivedPhase,
    )

    routing_phase = _routing_phase_of(routing_rows)
    row = execution_row if isinstance(execution_row, Mapping) else None

    if not routing_phase and row is None:
        raise PlanDataMissing(
            LIST_DATA_SOURCE_STATE_DB,
            "the plan's routing row and its execution row could not be read",
        )

    if routing_phase:
        return DerivedPhase(routing_phase, PHASE_SOURCE_ROUTING)

    if (
        row is not None
        and str(row.get("stop_reason") or "").strip() == QUOTA_WAIT_PHASE
        and capacity_wait.parse_capacity_marker(marker)
    ):
        return DerivedPhase(QUOTA_WAIT_PHASE, PHASE_SOURCE_EXECUTION)

    return DerivedPhase(DEFAULT_PHASE, PHASE_SOURCE_DEFAULT)


def _build_plan_status(plan_id: str) -> Any:
    """Assemble the one plan-status snapshot every reader renders from.

    One read, one answer. The stores that used to answer separately —
    ``plan_routing.current_phase``, ``plan_verification``,
    ``plan_tasks``, and the two in-memory liveness records — are read
    here, together. See :mod:`plan_status` for why that matters.

    The four verification fields have exactly ONE source: the
    ``plan_verification`` row.  The mirror column the state machine
    used to advance at every round boundary is not read here at all.
    Its only remaining writer,
    ``RoutingRepository.write_verification_baseline``, freezes the
    reviewed baseline into it — and on rows that predate that change
    the column still holds the old verdict beside the baseline, since
    the baseline writer keeps every other key verbatim.  A column that
    can hold both is not a source to reconcile against; it is a record
    of what was frozen.

    The phase is not decided here: it comes from :func:`derive_phase`,
    which owns the priority between the three things a reader can know
    (the routing row, the durable execution row, the plan's own
    ``tasks.json``) and says which tier answered.  A phase that is
    decided *here* is a phase with a default nobody can point at — and
    the one default that must never exist is the parked one, because it
    is global.

    2026-11-09: 快照**不再**因为「目录里两类产物同时存在」而拒绝。共存不是
    冲突 —— 按「任务优先」的主分支语义，这条 plan 就是一条开发分支的计划，
    只不过额外带着一份计划书。给出一个阶段不是替人择一，判定里已经有答案了。
    """
    from dataclasses import replace

    from plan_status import (
        PHASE_SOURCE_DEFAULT,
        DerivedPhase,
        PlanStatus,
        find_divergences,
        is_runtime_phase,
        resolve_phase,
    )

    v_status = ""
    v_round = 0
    v_max = 0
    v_stop: Optional[str] = None
    # Bound before the try: the phase rule below reads ``route``, and
    # the except path leaves it unassigned. A status read must degrade,
    # not raise NameError on top of the failure it is already handling.
    route: Optional[Dict[str, Any]] = None
    execution_row: Optional[Dict[str, Any]] = None
    try:
        conn, routing, verification = _server._open_verification_state()
        try:
            route = routing.current(plan_id)
            record = verification.current(plan_id)
            # The execution row is read on this same connection — the
            # phase rule needs it, and a second connection to the same
            # SQLite file buys nothing.
            #
            # 2026-10-26 (merge): main 的读路径只在相位是运行相位时才查
            # 这一行（少付一次查询的代价）。本分支的相位规则需要**执行行**
            # 作为第二层事实 —— 「等配额」这个相位只能从这一行读到
            # （routing 行读不到时），所以这里无条件读。读的是同一条连接上
            # 的同一张表，不额外开连接，也不改变读到的内容。
            execution_row = _execution_row_from_conn(conn, plan_id)
        finally:
            conn.close()
        if record:
            v_status = str(record.get("verification_status") or "")
            try:
                v_round = int(record.get("round") or 0)
            except (TypeError, ValueError):
                v_round = 0
            try:
                v_max = int(record.get("max_rounds") or 0)
            except (TypeError, ValueError):
                v_max = 0
            v_stop = record.get("verification_stop_reason") or None
    except Exception:
        # A status read must never raise — an operator asking "where is
        # this plan" is exactly the wrong moment to hand them a 500.
        _server.logger.exception("[plan_status] state.db read failed plan=%s", plan_id)

    # The phase, from the one rule that owns it.  The rule reads the
    # rows and nothing else, and it REFUSES when the rows are not
    # there: an unreadable row and an unreadable ``tasks.json`` are
    # missing information, and the answer to missing information is
    # "I cannot say" — never a phase nobody recorded, and never the
    # parked phase.
    tasks_payload = _read_tasks_payload(plan_id)
    try:
        derived = derive_phase(
            [route] if route else [],
            execution_row,
            tasks_payload,
        )
    except PlanDataMissing:
        if tasks_payload is not None:
            # The plan's files are here and its rows are not: this
            # instance cannot see the plan's state.  The refusal travels
            # up to the route, which answers 503 with the data source
            # named.
            raise
        # Nothing at all: no rows and no plan file.  There is no plan
        # here to sit at the manual gate, so the answer is the empty
        # phase — which is what the snapshot has always said for that
        # case.  It is not the parked phase either: a plan that does
        # not exist cannot be waiting for capacity.
        derived = DerivedPhase("", PHASE_SOURCE_DEFAULT)
    phase = derived.phase
    phase_source = derived.source

    # 2026-10-26 (merge): main 的运行相位反驳规则接在相位规则**之后**。
    #
    # ``executing`` 是词表里唯一一个描述**过程**而不是计划产物的相位 ——
    # 「执行器此刻正在跑这条计划」。所以它是唯一能被执行记录反驳的相位：
    # 一个 routing 行可以停在 ``executing`` 而身后没有任何进程在跑（导入
    # 旧 ``plan_state.json`` 的计划、被杀死的执行器留下的行都是这个形状），
    # 而读者不查就把这个相位复述出去，于是同一条计划在列表里读成
    # ``ready``、在这里读成 ``executing`` —— 一条计划，两个答案。
    #
    # 判定本身在 :func:`plan_status.resolve_phase` 里；这里只是它的 I/O 一半，
    # 列表接口调的是同一个函数、同一个兜底，所以两个读者不会给出不同的答案。
    # 「有执行记录」= 有 ``plan_execution`` 行 **或** 有内存里的
    # ``_execution_state`` 条目：执行器先写内存记录再盖相位，先落库再拉子
    # 进程，所以真在跑的计划一定有一个。
    exec_recorded = (
        execution_row is not None
        or _server._execution_state.get(plan_id) is not None
    )
    if is_runtime_phase(phase) and not exec_recorded:
        # The fallback is the status the plan's ARTEFACTS put it in —
        # the same answer the listing falls back to.  Computed only in
        # this rare case: a resting plan must not pay for it on a route
        # that is polled several times a minute per plan.
        phase = resolve_phase(
            phase,
            execution_recorded=exec_recorded,
            fallback=_artifact_status_for_plan(plan_id),
        )

    # The plan-doc review gate sits BEFORE the verification sub-machine:
    # a plan waiting for a human to accept / skip its plan-doc items has
    # not started a verification round, so its verdict is "pending" — not
    # an empty string the display surface has to guess at. A recorded
    # verdict always wins over this default.
    if phase == _PLAN_DOC_REVIEW_PHASE and not v_status:
        v_status = "pending"

    # The four verification fields are the ``plan_verification`` row and
    # nothing else.  The mirror column is still written — the baseline
    # freeze writes there, and the frozen baseline lives nowhere else —
    # but it carries no verdict of its own, and on the rows that predate
    # that change it still carries the OLD one beside the baseline.  So
    # it is not a source: there is nothing in it to reconcile against,
    # and a reader of it would be answering from a record of what was
    # frozen.  The overlay that used to let it override a stale row is
    # gone — see the note above for why, and for what it cost.
    #
    # The ``verification_running`` phase does NOT force ``running`` here,
    # and that is deliberate.  The snapshot reports the row; the phase
    # rule lives in the two readers that own a verdict of their own —
    # ``routes.verification._verification_status_from_db`` (the
    # verification status endpoint) and ``_authoritative_verification_block``
    # (the summary body) — while ``plan_status.find_divergences`` Rule 2
    # uses the phase/verdict disagreement as a detector.  Forcing
    # ``running`` over a terminal verdict in this snapshot would silence
    # that detector, and it would reintroduce, in a new shape, the
    # overlay this task removed: a value in the snapshot that no store
    # holds.

    rows = _read_task_rows(plan_id)
    current_task: Optional[Dict[str, Any]] = None
    next_task: Optional[Dict[str, Any]] = None
    for task_id, row in rows.items():
        status = str(row.get("status") or "").strip().lower()
        entry = {"id": task_id, "title": str(row.get("title") or "")}
        if status == "in_progress" and current_task is None:
            current_task = entry
        elif status in ("", "pending") and next_task is None:
            next_task = entry

    # What the executor is doing when no task row is in_progress. Read
    # here, not from the ``execution`` sub-payload assembled below, so
    # the header and the body are answered by the SAME read — the body
    # sections are permitted to fail independently (each degrades to
    # None), and a header that read them would inherit that flakiness.
    try:
        from execution_logger import ExecutionLogger

        current_activity: Optional[Dict[str, Any]] = (
            ExecutionLogger.current_activity(plan_id)
        )
    except Exception:
        _server.logger.exception(
            "[plan_status] current_activity read failed plan=%s", plan_id,
        )
        current_activity = None

    status_snapshot = PlanStatus(
        plan_id=plan_id,
        phase=phase,
        phase_source=phase_source,
        verification_status=v_status,
        verification_round=v_round,
        verification_max_rounds=v_max,
        verification_stop_reason=v_stop,
        execution_in_flight=_server._plan_execution_in_flight(plan_id),
        verification_in_flight=_server._is_verification_in_flight(
            _server._verification_state.get(plan_id)
        ),
        tasks=_task_counts(rows, _read_disk_task_ids(plan_id)),
        current_task=current_task,
        next_task=next_task,
        current_activity=current_activity,
    )

    divergences = find_divergences(status_snapshot)
    if divergences:
        status_snapshot = replace(status_snapshot, divergences=divergences)
        _log_divergences(status_snapshot)
    return status_snapshot


# ---------------------------------------------------------------------------
# Is the plans tree searchable at all?
# ---------------------------------------------------------------------------
#
# "Plan not found" is a true answer about a plan that is not there — and
# a false answer about a plan that IS there, in a tree this process is
# not looking at.  The two were told apart by nothing at all, because
# the route looked only for the plan directory and never asked whether
# the tree around it could be searched.
#
# The shape it exists for: an instance pointed at an **empty** plans
# root answers "Plan not found" for *every* plan id, and a caller that
# reads that as "this plan does not exist" never learns that the
# instance is looking in the wrong place (or that the tree it was told
# to look in is not there at all).
#
# So the tree is checked before the plan is.  A tree that cannot be
# searched is answered **503** — the plan may well exist; this instance
# cannot see it — and the directory that was searched is echoed in the
# body, so "this instance is looking in the wrong place" and "this plan
# does not exist" are told apart by the paths they carry rather than by
# a status code the caller has to interpret.

#: The detail a route answers with when the plans tree cannot be
#: searched.  Spelled the same way in every answer that carries it.
PLANS_DIR_UNAVAILABLE_DETAIL = "plans dir unavailable"

#: How many plan ids a diagnostic answer carries.  The list exists so a
#: reader can compare the instance that answered with the plan it asked
#: about; twenty is enough for that comparison and short enough to keep
#: the body small.
AVAILABLE_PLAN_IDS_LIMIT = 20

#: The state of the serving instance's state plane.
#:
#:   * ``"absent"`` — there is no state database file at all.  Nothing
#:     was ever routed through this instance, so there is no state to
#:     be empty about: the tree it serves is what it answers from.
#:   * ``"unreadable"`` — there is one and it could not be read.
#:   * ``"empty"`` — there is one and it holds no plan rows.  A state
#:     that knows none of the plans in the tree is somebody else's
#:     state — the same mismatch the plans listing refuses to answer
#:     through (see
#:     :func:`_refuse_when_data_source_does_not_describe_the_tree`).
#:   * ``"readable"`` — there is one and it holds plan rows.
STATE_PLANE_ABSENT = "absent"
STATE_PLANE_UNREADABLE = "unreadable"
STATE_PLANE_EMPTY = "empty"
STATE_PLANE_READABLE = "readable"

#: The detail a route answers with when the serving instance's state
#: plane holds no plan of its own.  Spelled the same way in every
#: answer that carries it.
PLAN_STATE_EMPTY_DETAIL = "plan state empty"

#: The detail a route answers with when the serving instance's state
#: plane could not be read at all.
PLAN_STATE_UNREADABLE_DETAIL = "plan state unavailable"


def _scan_plans_tree(plans_dir: Path) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Look in the plans tree — (why it cannot be searched, the plans in it).

    The ways a search is impossible, and none of them may be answered
    "the plan is not there":

    * **missing** — the tree this process was pointed at is not there;
    * **not a directory** — something that is not a tree is there;
    * **unreadable** — the tree cannot be listed (permissions);
    * **empty** — the tree lists, but there is nothing in it.

    The empty case is the one that cannot be told apart from "the plan
    does not exist" by looking at the plan alone: an instance whose data
    plane is empty says "not found" about every id.

    A stray *file* in the tree (``.DS_Store`` and friends) is not a plan
    and must not make an empty data plane look populated — so the tree
    counts as searchable only when it holds at least one directory.

    The plan ids come out of the same scan: they are the directories in
    the tree, and they are what the answer carries so a reader can see
    what the answering instance can serve.

    The path is used as it is configured (``str(plans_dir)``), not
    resolved: the answer names the directory the operator pointed the
    instance at, and resolving it would answer with a different
    spelling of the same place.
    """
    if not plans_dir.exists():
        return "missing", ()
    if not plans_dir.is_dir():
        return "not a directory", ()
    try:
        # ``os.scandir`` rather than ``Path.iterdir()``: this runs on
        # every status read, and the status route is polled every few
        # seconds per plan.  The scan reads the directory once and
        # builds no Path objects for the entries it looks at.
        with os.scandir(plans_dir) as entries:
            plan_ids = sorted(entry.name for entry in entries if entry.is_dir())
    except OSError:
        return "unreadable", ()
    if not plan_ids:
        return "empty", ()
    return None, tuple(plan_ids[:AVAILABLE_PLAN_IDS_LIMIT])


def _plans_dir_unavailable_reason(plans_dir: Path) -> Optional[str]:
    """Why the plans tree cannot be searched — ``None`` when it can."""
    return _scan_plans_tree(plans_dir)[0]


class _ServingPlane(NamedTuple):
    """What the SERVING instance can see.

    The facts every answer about a plan this instance cannot find
    carries: the state database it reads (by path and by inode), the
    plan ids that database holds, the plan directories in the tree it
    serves, and whether the plan the caller asked about has rows of its
    own.
    """

    state_db_path: Optional[Path]
    state_db_inode: Optional[int]
    state_db_plan_ids: Tuple[str, ...]
    tree_plan_ids: Tuple[str, ...]
    state_plane: str
    plan_has_rows: bool

    @property
    def available_plan_ids(self) -> Tuple[str, ...]:
        """The plan ids this instance can see — capped, in a stable order.

        The state's own rows come first: they are the plans this
        instance knows about.  The tree's directories follow: they are
        the plans it can serve.  A reader comparing this list with the
        plan it asked about can see at once whether it is talking to
        the instance that holds that plan.
        """
        ids: List[str] = []
        for plan_id in tuple(self.state_db_plan_ids) + tuple(self.tree_plan_ids):
            if plan_id in ids:
                continue
            ids.append(plan_id)
            if len(ids) >= AVAILABLE_PLAN_IDS_LIMIT:
                break
        return tuple(ids)


def _plan_ids_in_db(conn: Any) -> Tuple[str, ...]:
    """The plan ids the state database holds a row for — capped.

    Both tables that carry a plan's own row count: a plan can be in the
    tree with nothing but an execution row (its workflow phase was
    never routed), and that is a real state.
    """
    ids: List[str] = []
    for statement in (
        "SELECT DISTINCT plan_id FROM plan_routing ORDER BY plan_id LIMIT ?",
        "SELECT DISTINCT plan_id FROM plan_execution ORDER BY plan_id LIMIT ?",
    ):
        try:
            rows = conn.execute(statement, (AVAILABLE_PLAN_IDS_LIMIT,)).fetchall()
        except Exception:  # noqa: BLE001 - one unreadable table is not a crash
            continue
        for row in rows:
            plan_id = str(row[0]) if row and row[0] else ""
            if plan_id and plan_id not in ids:
                ids.append(plan_id)
    return tuple(ids[:AVAILABLE_PLAN_IDS_LIMIT])


def _serving_plane(plan_id: str, tree_plan_ids: Tuple[str, ...]) -> _ServingPlane:
    """Read the serving instance's data plane — once per answer.

    The state database is opened once and read for three things at
    once: the plan ids it holds (the ``available_plan_ids`` the answer
    carries), whether the plan the caller asked about has rows of its
    own, and whether the state plane holds anything at all.
    """
    db_path = _resolved_state_db_path()
    inode: Optional[int] = None
    if db_path is not None:
        try:
            inode = int(db_path.stat().st_ino)
        except OSError:
            inode = None

    state_plane = STATE_PLANE_ABSENT
    db_ids: Tuple[str, ...] = ()
    plan_has_rows = False
    if db_path is not None and db_path.exists():
        # There IS a state database.  Whether it holds anything is a
        # different question from whether there is one at all, and the
        # two are told apart here: an instance with no state database
        # has never routed a plan — the tree it serves is what it
        # answers from — while an instance whose state database holds
        # no plan rows has a state that does not describe the tree.
        state_plane = STATE_PLANE_UNREADABLE
        sm = None
        try:
            sm = _server._open_state_machine()
            if sm is not None:
                conn, routing, execution, _verification, _artifact = sm
                db_ids = _plan_ids_in_db(conn)
                plan_has_rows = (
                    bool(routing.current(plan_id))
                    or bool(execution.summary(plan_id))
                )
                state_plane = (
                    STATE_PLANE_READABLE if db_ids else STATE_PLANE_EMPTY
                )
        except Exception:  # noqa: BLE001 - an unreadable state is not a 500
            _server.logger.exception(
                "[plan_status] state plane read failed plan=%s", plan_id,
            )
            db_ids = ()
            plan_has_rows = False
        finally:
            _server._close_state_machine(sm)
    return _ServingPlane(
        state_db_path=db_path,
        state_db_inode=inode,
        state_db_plan_ids=db_ids,
        tree_plan_ids=tree_plan_ids,
        state_plane=state_plane,
        plan_has_rows=plan_has_rows,
    )


def _plane_diagnostics(plans_dir: Path, plane: _ServingPlane) -> Dict[str, Any]:
    """The facts the answer carries about the instance that gave it.

    The same fields in every answer this route gives about a plan it
    cannot find — the ``404`` and the ``503`` answers alike — so a
    caller can compare them field by field instead of guessing from a
    status code.
    """
    return {
        "searched_dir": str(plans_dir),
        "state_db_path": (
            str(plane.state_db_path) if plane.state_db_path is not None else None
        ),
        "state_db_inode": plane.state_db_inode,
        "available_plan_ids": list(plane.available_plan_ids),
    }


#: The detail a route answers with when the plan's durable rows could
#: not be read.  Spelled the same way in every answer that carries it.
PLAN_DATA_UNAVAILABLE_DETAIL = "plan data unavailable"


def _plan_data_unavailable_response(
    plan_id: str,
    plans_dir: Path,
    plan_dir: Path,
    error: PlanDataMissing,
    diagnostics: Dict[str, Any],
) -> JSONResponse:
    """The answer to "I cannot say what phase this plan is in".

    ``503`` because the plan may well be there — this instance cannot
    see it.  The body names the store that could not answer
    (``data_source``) and the tree that was searched, so "the plan does
    not exist" and "this instance is looking in the wrong place" are
    told apart by the paths and the store they carry rather than by a
    status code the caller has to interpret.
    """
    return JSONResponse(
        status_code=503,
        content={
            "detail": PLAN_DATA_UNAVAILABLE_DETAIL,
            LIST_DATA_SOURCE_KEY: error.data_source,
            "plan_id": plan_id,
            "plans_dir": str(plans_dir),
            "plan_dir": str(plan_dir),
            **diagnostics,
        },
    )


@router.get("/api/plan/{plan_id}/status")
def get_plan_status(plan_id: str):
    """The single, consistent view of one plan — everything a card needs.

    Prefer this over reassembling a verdict from
    ``/summary`` + ``/execution/{id}/progress`` + ``/verification/{id}/progress``:
    those are three reads at three instants, and re-deriving an answer
    from them is how the card and the database came to disagree
    (2026-09-23). The three sub-payloads are still here, for the card
    *body* — but they are gathered in one server-side pass, so they
    describe one moment. The verdict itself is the top-level
    ``PlanStatus`` fields.

    ``divergences`` is empty when the stores agree.

    The two ways this route can refuse to answer are a contract of their
    own, and they are told apart deliberately:

    * a **malformed** id is a bad request — ``400``, with the same body
      every other plan-id route answers it with;
    * a **legal but unknown** id is a plan that does not exist —
      ``404`` with the detail ``Plan not found``.

    And there is a third answer, which is not about the id at all:
    a plans tree this instance **cannot search** is answered ``503``,
    because in that state "the plan does not exist" is not a fact this
    instance is entitled to state.  An empty tree says "not found"
    about every id, and a caller that believes it is looking for a
    plan that was deleted never learns that the instance is looking in
    the wrong place.  See :func:`_plans_dir_unavailable_reason`.

    The same reasoning covers the state plane: an instance whose state
    database holds no plan of its own cannot say that a plan it does
    not hold is missing, so it answers ``503`` there too.  See
    :func:`_serving_plane`.

    Every one of those answers carries the facts of the **serving**
    instance — the tree that was searched, the state database that was
    read (by path and by inode), and the plan ids that instance can
    see — because an answer that cannot be attributed to an instance
    cannot be told apart from the answer of an instance that is
    looking at another round's data plane.  That confusion is what
    made two earlier rounds read "the plan is gone" out of an answer
    that really said "you asked the wrong instance".

    The validation is asked for here rather than left to happen on the
    way to ``PLANS_DIR / plan_id``.  ``_plan_dir`` does validate, so the
    refusal is observable either way — but a route whose contract rests
    on a helper it happens to call has no contract at all: the day that
    helper is relaxed (and it is a plausible relaxation, because the
    codebase already carries ``_is_renderable_plan_id`` for stored ids
    that predate the traversal guard), this route would answer ``200``
    with ``plan_id: ".."`` and the repository root's contents as if they
    were a plan.  Asking for the guard at the entry is what keeps the
    refusal a decision of this route instead of a side effect of
    another one.
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function
    plans_dir = Path(_server.PLANS_DIR)
    unavailable, tree_plan_ids = _scan_plans_tree(plans_dir)
    if unavailable is not None:
        # The tree could not be searched, so "the plan is not there" is
        # not an answer this instance may give.  503 says "ask again
        # once there is a tree to search", and the directory that could
        # not be searched is named so an operator can see *which* tree
        # was searched — the answer that tells "this instance is looking
        # in the wrong place" apart from "this plan does not exist".
        plane = _serving_plane(plan_id, tree_plan_ids)
        return JSONResponse(
            status_code=503,
            content={
                "detail": PLANS_DIR_UNAVAILABLE_DETAIL,
                **_plane_diagnostics(plans_dir, plane),
            },
        )
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        # 2026-10-25 (repair-r1-29): a 404 says **where** it looked.
        # "Plan not found" is a true answer about a plan that is not
        # there — and a false answer about a plan that IS there, in
        # another instance's tree.  The two are told apart by the paths
        # the answer carries: the plans root this process is configured
        # with, and the plan directory it resolved inside it.
        #
        # The ``detail`` stays what it was (a client that keys off the
        # message keeps working) and no stack trace is added: the point
        # of these fields is to say which directory was searched, not to
        # explain the search.  The execution routes answer the same way
        # — see ``backend/routes/execution.py``.
        #
        # ``searched_dir`` is the spelling the 503 answer above uses as
        # well, so the two answers can be compared field by field: a
        # 404 means "the tree was searched and the plan is not in it",
        # a 503 means "the tree was never searched".
        #
        # 2026-10-26 (repair-r1-44): the tree is not the only place a
        # plan lives.  When the durable state holds rows for this plan
        # and the tree does not hold its directory, "Plan not found" is
        # FALSE — the plan is there, in the store that decides where it
        # is, and this instance is simply pointed at a tree that does
        # not hold it.  That is the shape that collapsed every plan at
        # once in the failed round: one wrong directory, one answer,
        # every plan.  So the durable state gets the last word, and the
        # answer is a refusal with the store named.
        plane = _serving_plane(plan_id, tree_plan_ids)
        diagnostics = _plane_diagnostics(plans_dir, plane)
        if plane.plan_has_rows:
            return _plan_data_unavailable_response(
                plan_id,
                plans_dir,
                plan_dir,
                PlanDataMissing(
                    LIST_DATA_SOURCE_STATE_DB,
                    "the plan's rows are in the durable state, but its "
                    "directory is not in this instance's plans tree",
                ),
                diagnostics,
            )
        if plane.state_plane in (STATE_PLANE_EMPTY, STATE_PLANE_UNREADABLE):
            # 2026-10-26: the state plane holds no plan of this
            # instance's own.  The plan may well be somewhere else, and
            # this instance cannot see it — so "Plan not found" is not
            # an answer it is entitled to give, and it would be the
            # SAME answer for every plan id, which is exactly what the
            # earlier rounds ran into: an instance looking at somebody
            # else's data plane, saying "not found" about everything.
            # 503 says "this instance cannot see whether the plan
            # exists", and the diagnostics name the state that could
            # not answer.
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        PLAN_STATE_EMPTY_DETAIL
                        if plane.state_plane == STATE_PLANE_EMPTY
                        else PLAN_STATE_UNREADABLE_DETAIL
                    ),
                    **diagnostics,
                },
            )
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Plan not found",
                "plans_dir": str(plans_dir),
                "plan_dir": str(plan_dir),
                **diagnostics,
            },
        )
    try:
        return _plan_status_payload(plan_id)
    except PlanDataMissing as error:
        # The plan's files are here and its rows are not: the phase rule
        # refused to invent one.  The refusal is the answer — a default
        # phase would be a claim nobody made, and the parked one is
        # global by construction.
        plane = _serving_plane(plan_id, tree_plan_ids)
        return _plan_data_unavailable_response(
            plan_id, plans_dir, plan_dir, error,
            _plane_diagnostics(plans_dir, plane),
        )


def _is_verification_not_started(error: HTTPException) -> bool:
    """Is this 404 the handler saying "verification has not started"?

    ``get_verification_progress`` answers ``HTTPException(404, {"error":
    "verification not started"})`` when the executor has not written a
    progress state yet — the normal state of every plan that is still
    executing.  So it is an **answer**, not a fault, and the snapshot
    has to treat it as one.

    The check is exact on purpose.  A 404 whose detail says something
    else (the plan is not in the tree, a plain string) is a different
    answer, and it keeps being logged as the failure it is — the
    blanket ``except`` below stays in charge of everything this does
    not recognise.
    """
    return (
        error.status_code == 404
        and isinstance(error.detail, dict)
        and error.detail.get("error") == "verification not started"
    )


def _plan_status_payload(plan_id: str) -> Dict[str, Any]:
    """``PlanStatus`` + the three detail sections, read once.

    The detail sections are read through the same handler functions the
    standalone endpoints serve, so there is one implementation of each;
    a failure in any one of them degrades that section rather than
    failing the whole snapshot (an operator asking "where is this plan"
    should still get an answer).

    The degradation is partial, not total.  A section that reads
    ``None`` says "this section is unreadable" — but the execution
    section used to swallow the plan's execution verdict along with
    it, and "the execution section is unreadable" is not the same
    answer as "the plan's state is unknown".  So the execution section
    falls back to the durable row's verdict (see
    :func:`_degraded_execution_section`) and the task counts are always
    present (see :func:`_base_status_payload`).
    """
    payload = _base_status_payload(plan_id)
    # The phase under the key the plans listing uses (``current_phase``)
    # plus its stable label. The two interfaces name the same facts the
    # same way — a consumer reading both must not have to know which key
    # holds the phase in which one — and they read the label from the
    # same table (``plan_state.PHASE_LABELS``), so the same phase can
    # never be labelled two ways.
    #
    # 2026-10-26 (merge): 快照本体仍走本分支的 :func:`_base_status_payload`
    # （它多做一件事：任务计数永远在场），main 的两个键加在上面。
    payload["current_phase"] = payload.get("phase", "")
    payload["current_phase_label"] = _phase_label(payload.get("phase"))
    # 2026-11-09: 产物判定的事实也进载荷 —— **主分支**与**盘上实际存在的两份
    # 产物的相对名**。共存不是冲突：主分支是 dev（任务优先），计划书是附加
    # 产物。计划列表给的是同一份判定（:func:`plan_artifacts.detect`），两个
    # 读者不许对同一条 plan 给出两个答案。
    artifacts = detect(_server._plan_dir(plan_id))
    payload["branch"] = artifacts.branch
    payload["artifacts"] = _artifact_names(artifacts)
    for key, handler in (
        ("summary", _plan_summary_section),
        ("execution", _server.get_execution_progress),
        ("verification", _server.get_verification_progress),
    ):
        try:
            payload[key] = handler(plan_id)
        except Exception as error:
            if (
                key == "verification"
                and isinstance(error, HTTPException)
                and _is_verification_not_started(error)
            ):
                # 2026-11-04: "verification not started" is a normal
                # answer — the plan is still executing and the
                # verification round has not begun.  It used to fall
                # into the blanket handler below and be reported as a
                # broken section: a full traceback at ERROR level,
                # 2393 times in two hours, for every plan still
                # running.  The section stays ``None`` (there is
                # nothing to show yet) and the reason is said once,
                # quietly, so an operator can still tell "not started
                # yet" from "the section broke".
                payload[key] = None
                _server.logger.debug(
                    "[plan_status] verification not started plan=%s", plan_id,
                )
                continue
            _server.logger.exception(
                "[plan_status] %s section failed plan=%s", key, plan_id,
            )
            payload[key] = (
                _degraded_execution_section(plan_id)
                if key == "execution" else None
            )
    return payload


def _base_status_payload(plan_id: str) -> Dict[str, Any]:
    """The snapshot itself, with the task counts guaranteed present.

    The counts are the numbers every consumer asks for ("how much is
    left to do"), so they are never optional: even a snapshot that
    could not be assembled at all answers with zeros rather than with a
    missing key.  ``ensure_task_counts`` is what turns "the counts
    section could not be read" into "the counts are zero" without
    discarding the counts that were read fine.
    """
    from plan_status import ensure_task_counts

    try:
        payload = _build_plan_status(plan_id).to_dict()
    except PlanDataMissing:
        # A refusal is not a failure to degrade.  The phase rule said
        # "I cannot say" because the plan's rows are not there, and
        # degrading it into a default phase would turn that refusal into
        # a claim — the exact thing the refusal exists to prevent.  So
        # it travels up to the route, which answers 503 and names the
        # store that could not answer.
        raise
    except HTTPException:
        # A deliberate refusal is not a failure to degrade either.  If
        # the snapshot ever refuses on purpose, the refusal travels up
        # unchanged — degrading it into the empty default would turn
        # "I cannot say" into a claim.
        #
        # 2026-11-09: 这条分支曾经专门接住 409 ``artifact_conflict``
        # （目录里两类产物同时存在 → 状态快照不给）。共存合法之后，那个
        # 拒绝已经不存在了：主分支由「任务优先」定下来，状态快照给得出来。
        raise
    except Exception:
        # The snapshot is built from several stores and one of them may
        # be unreadable in a way its own handler did not anticipate.
        # The plan's phase and counts are then unknown — but "unknown"
        # is an answer (zeros, an empty phase), not a 500 for the
        # operator who asked.
        _server.logger.exception(
            "[plan_status] snapshot build failed plan=%s", plan_id,
        )
        payload = {
            "plan_id": plan_id,
            "phase": "",
            # The snapshot could not be assembled at all, so the phase was
            # not read from any record — it is the resting fallback, and
            # it is empty because there is nothing to report a phase for.
            "phase_source": "default",
            "verification_status": "",
            "verification_round": 0,
            "verification_max_rounds": 0,
            "verification_stop_reason": None,
            "execution_in_flight": False,
            "verification_in_flight": False,
            "tasks": {},
            "current_task": None,
            "next_task": None,
            "current_activity": None,
            "divergences": [],
        }
    payload["tasks"] = ensure_task_counts(payload.get("tasks"))
    return payload


def _execution_row_from_conn(conn: Any, plan_id: str) -> Optional[Dict[str, Any]]:
    """The durable ``plan_execution`` row, read on an open connection.

    The row is the plan's own record of how its last run ended, and it
    outlives the process that wrote it — which is why the phase rule
    reads it rather than the in-memory execution map.

    It is read on a connection the caller already holds: the status
    endpoint is polled several times a minute per plan, and a second
    connection to the same SQLite file buys nothing.

    ``None`` means "no row to read" — no row, or a row that could not
    be read.  It is not a failure of the plan, and it is not a failure
    of the read: the caller answers "the phase was not read from here".
    """
    try:
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )

        row = ExecutionRepository(conn).summary(plan_id)
    except Exception:
        _server.logger.exception(
            "[plan_status] execution row read failed plan=%s", plan_id,
        )
        return None
    return row if isinstance(row, dict) else None


def _persisted_execution_row(plan_id: str) -> Optional[Dict[str, Any]]:
    """The durable ``plan_execution`` row for ``plan_id``, on its own
    connection.

    The standalone read, for the callers that have no connection open
    (the degraded execution section).  ``None`` means "no row to read".
    """
    try:
        sm = _server._open_state_machine()
    except Exception:
        # A state.db that cannot even be opened is a missing row, not a
        # failed request — the caller degrades to "the phase was not
        # read from the execution row".
        _server.logger.exception(
            "[plan_status] execution row read failed plan=%s", plan_id,
        )
        return None
    if sm is None:
        # No state.db yet — nothing was ever recorded for this plan.
        return None
    try:
        conn, _routing, _execution, _verification, _artifact = sm
        return _execution_row_from_conn(conn, plan_id)
    finally:
        _server._close_state_machine(sm)


def _execution_row_status(plan_id: str) -> Optional[str]:
    """``plan_execution.exec_status`` — the execution row's own verdict.

    Read straight from the state-machine row, because that row is the
    only place an execution verdict lives: ``waiting_for_quota`` is
    written there and nowhere else.

    ``None`` means "there is no verdict to read" — no row, an empty
    ``exec_status``, or an unreadable database.  It never means "the
    plan failed", and it is not a failure of the read: the caller uses
    it to decide whether a degraded section has anything to preserve.
    """
    row = _persisted_execution_row(plan_id)
    if row is None:
        return None
    status = str(row.get("exec_status") or "").strip()
    return status or None


def _persisted_routing_phase(plan_id: str) -> Optional[str]:
    """``plan_routing.current_phase`` — the workflow's own answer.

    Read on its own connection, for the callers that have no handle
    open.  ``None`` means "there is no phase to read" — no routing row,
    or an unreadable database.

    It is the counterpart of :func:`_execution_row_status`: the row says
    how the LAST run ended, the routing row says where the plan IS.
    A reader that shows one without the other shows a stale verdict
    (see :func:`plan_status.normalize_exec_status`).
    """
    try:
        sm = _server._open_state_machine()
    except Exception:
        return None
    if sm is None:
        return None
    try:
        conn, routing, _execution, _verification, _artifact = sm
        row = routing.current(plan_id)
        if not isinstance(row, dict):
            return None
        phase = str(row.get("current_phase") or "").strip()
        return phase or None
    except Exception:
        return None
    finally:
        _server._close_state_machine(sm)


def _degraded_execution_section(plan_id: str) -> Optional[Dict[str, Any]]:
    """The execution section of a plan whose section read failed.

    A section that cannot be read must not take the plan's state with
    it.  The execution verdict is the one field a consumer cannot do
    without — it is the difference between "parked, waiting for
    capacity" and "failed" — and it is durable: it lives in the
    ``plan_execution`` row, not in the handler that just failed.  So
    the degraded section carries that verdict and blanks everything
    else.

    ``None`` when the row carries no verdict: there is nothing to
    preserve, and inventing one would be a lie.  A plan with no
    execution record has no execution status, and a reader is better
    off knowing that than being handed a made-up one.
    """
    status = _execution_row_status(plan_id)
    if status is None:
        return None
    # The verdict is reconciled against the workflow's answer, exactly as
    # the full section does it: a plan that finished must not go on
    # reporting the verdict of a run that was interrupted long before.
    status = normalize_exec_status(_persisted_routing_phase(plan_id), status)
    return {
        "plan_id": plan_id,
        "pid": None,
        "project_dir": "",
        "execution_status": status,
        "tasks": [],
        "current": None,
        "next": None,
        "counts": {
            "total": 0, "completed": 0, "failed": 0,
            "in_progress": 0, "pending": 0,
        },
        "current_activity": None,
        "stop_reason": None,
        "stop_detail": None,
        "api_error": None,
        "started_at": None,
        "ended_at": None,
        "execution_stop_reason": None,
        # A reader has to be able to tell a reduced answer from a
        # complete one: everything except the verdict is missing here.
        "degraded": True,
    }


def _plan_summary_section(plan_id: str) -> Optional[Dict[str, Any]]:
    """``/summary``'s body, built without a FastAPI route context.

    ``_get_plan_summary_body`` declares a ``request`` parameter but never
    reads it, so the section can be assembled outside a request — which
    is the point: one read, one moment.
    """
    sm = _server._open_state_machine()
    try:
        return _get_plan_summary_body(None, plan_id, sm)
    finally:
        if sm is not None:
            _server._close_state_machine(sm)


def _paused_executions() -> List[Dict[str, Any]]:
    """Every plan carrying an un-consumed pause checkpoint.

    The counterpart to the two in-memory registries below, and the one
    that survives a restart: ``_execution_state`` /
    ``_verification_state`` die with the process that populated them,
    while a checkpoint on disk is exactly the record a resume needs.
    So this is read from the plan directories, not from memory.

    Every entry has the same four keys — a consumer should never have
    to branch on which kind of interruption it is looking at. A plan
    with no checkpoint is simply absent from the list; a checkpoint
    that cannot be read is treated as absent too, so a torn file can
    never turn this read-only listing into an error.
    """
    from pause_state import checkpoint_path, checkpoint_summary

    plans_dir = _server.PLANS_DIR
    entries: List[Dict[str, Any]] = []
    try:
        plan_dirs = sorted(plans_dir.iterdir())
    except OSError:
        # No plans directory yet (fresh install), or one that vanished
        # under us. Either way there is nothing paused in it.
        return entries

    for plan_dir in plan_dirs:
        if not plan_dir.is_dir():
            continue
        # Existence first: this listing is polled by the supervisor on
        # every tick, and the common answer is "no checkpoint".
        try:
            if not checkpoint_path(plan_dir).exists():
                continue
            summary = checkpoint_summary(plan_dir)
        except OSError:
            _server.logger.warning(
                "[system_active] could not read a pause checkpoint dir=%s",
                plan_dir, exc_info=True,
            )
            continue
        if not summary:
            continue
        entries.append({
            "plan_id": plan_dir.name,
            "kind": summary.get("kind"),
            "paused_at": summary.get("paused_at"),
            "interrupted_task_count": int(
                summary.get("interrupted_task_count") or 0
            ),
        })
    return entries


@router.get("/api/system/active")
def get_active_tasks():
    """
    Quick snapshot of how many the workflow tasks are currently running.

    Inspects:
      - _execution_state: per-plan execution subprocess status
      - _verification_state: per-plan verification cycle status
      - Heartbeat check on subprocess poll() to detect "running" state
      - pause checkpoints on disk: plans interrupted but not resumed

    Returns:
      {
        "execution_running": <int>,
        "verification_running": <int>,
        "total_active": <int>,
        "details": [
          {"plan_id": ..., "type": "execution"|"verification", "status": ..., ...},
          ...
        ],
        "paused_executions": [
          {"plan_id": ..., "kind": ..., "paused_at": ..., "interrupted_task_count": ...},
          ...
        ]
      }

    ``paused_executions`` is computed BEFORE the empty-state
    short-circuit below and returned from every path. The short-circuit
    is exactly the state of a freshly-restarted server — which is the
    one moment the pause list matters most, since a plan that was
    paused before the restart has no in-memory state left to report.
    A response that carried the list only on the non-empty path would
    answer "nothing is paused" at precisely the moment a resume is
    waiting to be found.
    """
    details: list = []
    paused_executions = _paused_executions()
    # Hot-path optimization B-03: short-circuit when both state dicts are
    # empty. This is the common case in a freshly-started server and
    # after every completed cycle; iterating two dicts + building two
    # detail dicts to return "0 / 0 / 0 / []" is wasted work on every
    # supervisor poll. The pause list is read above it and travels with
    # the short-circuit's answer.
    if not _server._execution_state and not _server._verification_state:
        return {
            "execution_running": 0,
            "verification_running": 0,
            "total_active": 0,
            "details": [],
            "paused_executions": paused_executions,
        }
    # Check execution_state for running subprocesses. ``_is_execution_in_flight``
    # rather than a bare membership test — the dict also holds records for
    # plans that finished long ago (see the predicate's docstring).
    for plan_id, st in _server._execution_state.items():
        if not _server._is_execution_in_flight(st):
            continue
        proc = st.get("process")
        details.append({
            "plan_id": plan_id,
            "type": "execution",
            "status": st.get("status", "unknown"),
            "started_at": st.get("started_at"),
            "project_dir": st.get("project_dir"),
            "pid": proc.pid if proc else None,
        })
    # Check verification_state for in-progress verification cycles
    for plan_id, st in _server._verification_state.items():
        if not _server._is_verification_in_flight(st):
            continue
        details.append({
            "plan_id": plan_id,
            "type": "verification",
            "status": st.get("verification_status", ""),
            "round": st.get("verification_round"),
            "started_at": st.get("started_at"),
            "updated_at": st.get("updated_at"),
        })

    execution_running = sum(1 for d in details if d["type"] == "execution")
    verification_running = sum(1 for d in details if d["type"] == "verification")
    return {
        "execution_running": execution_running,
        "verification_running": verification_running,
        "total_active": len(details),
        "details": details,
        "paused_executions": paused_executions,
    }


# ---------------------------------------------------------------------------
# System-wide stop / start
# ---------------------------------------------------------------------------
#
# Restarting the backend is a two-step operation: stop everything that is
# running, then start everything that was paused. Done per plan, that is N
# calls and N chances to forget one — and the plan that was forgotten
# either keeps a subprocess alive across the restart or is never resumed,
# leaving the operator with a plan that "quietly stopped doing something".

#: The two kinds of work the system-wide endpoints know how to stop and
#: start. ``get_active_tasks`` names them in ``details[*]["type"]`` and a
#: pause checkpoint names them in ``kind`` — the same two words, which is
#: what lets one dispatcher serve both endpoints.
_EXECUTION_TYPE = "execution"
_VERIFICATION_TYPE = "verification"

#: The file the global stop writes and the global start consumes. It
#: lives next to the state database — the one directory the backend
#: already owns — and its name says what it is.
_BATCH_FILENAME = "stop_all_batch.json"

#: Bumped only on an incompatible change to the batch document's shape.
#: A record written by a newer PDT is ignored rather than half-read.
_BATCH_VERSION = 1

#: The kind reported for a batch member that has nothing to resume. It
#: is a marker, not a real kind: no start route owns it.
_NO_CHECKPOINT_KIND = "no_checkpoint"


def _failure_text(exc: BaseException) -> str:
    """A short reason for a plan that could not be stopped or started.

    ``HTTPException`` carries a ``detail`` that is often a dict whose
    ``error`` key is the machine-readable reason ("conflict",
    "not_found"). That dict is the operator's own answer, so it is what
    the result row repeats.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        error = detail.get("error")
        if error:
            return str(error)
        return str(detail.get("detail") or detail)
    if detail:
        return str(detail)
    text = str(exc).strip()
    return text or type(exc).__name__


def _response_error(outcome: Any) -> Optional[str]:
    """The error an outcome carries, or ``None`` when it carries none.

    Not every failure raises: the verification routes answer a CAS
    conflict with a 409 *response*. Both shapes have to reach the
    operator as the same ``ok: false`` row, or a conflicting plan would
    be reported as a successful stop — the one answer that must never be
    wrong.
    """
    status = getattr(outcome, "status_code", None)
    if status is None:
        return None
    try:
        status = int(status)
    except (TypeError, ValueError):
        return None
    if status < 400:
        return None
    body = getattr(outcome, "body", None)
    if body:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if error:
                return str(error)
            reason = payload.get("reason")
            if reason:
                return str(reason)
    return f"http_{status}"


def _run_for_plan(plan_id: str, kind: str, action: Any) -> Dict[str, Any]:
    """One plan's stop/start, isolated: a failure is a row, not a 500.

    A global stop that gives up half way through is worse than one that
    reports what it could not do — the operator has to be able to see
    which plans are still running.
    """
    try:
        outcome = action(plan_id)
    except Exception as exc:  # noqa: BLE001 — one plan must not stop the rest
        _server.logger.warning(
            "[system_stop_start] %s failed plan=%s", kind, plan_id,
            exc_info=True,
        )
        return {
            "plan_id": plan_id,
            "type": kind,
            "ok": False,
            "error": _failure_text(exc),
        }
    error = _response_error(outcome)
    if error:
        return {
            "plan_id": plan_id,
            "type": kind,
            "ok": False,
            "error": error,
        }
    return {"plan_id": plan_id, "type": kind, "ok": True}


def _batch_path() -> Optional[Path]:
    """Where the stop_all batch record lives.

    Next to the state database — the directory the backend already
    owns — resolved through ``_server._state_db_path`` so a test (or a
    deployment that moves the database) gets the right answer instead
    of a hard-coded ``.pdt``.

    ``None`` when the path cannot be resolved at all: the record is a
    convenience, and a stop that cannot place it must still stop.
    """
    try:
        return Path(_server._state_db_path()).parent / _BATCH_FILENAME
    except Exception:  # noqa: BLE001 — the path is not worth a 500
        return None


def _write_stop_all_batch(plan_ids: List[str]) -> bool:
    """Record the plans this stop stopped, for the next start to resume.

    The record is the *batch*: the set of plans one global stop
    touched. It is written atomically, so a stop killed mid-write
    leaves the previous record intact rather than a torn one.

    A stop that stopped nothing still writes a record — an empty one.
    "Nothing was running" and "nothing was ever stopped" are different
    answers, and only the first one may resume a plan.
    """
    from utils.atomic_io import atomic_write_json

    path = _batch_path()
    if path is None:
        _server.logger.warning(
            "[system_stop_start] cannot resolve the state db path; "
            "no stop_all batch was recorded"
        )
        return False
    payload = {
        "version": _BATCH_VERSION,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "plan_ids": list(plan_ids),
    }
    try:
        atomic_write_json(path, payload, logger=_server.logger, reraise=True)
    except OSError as exc:
        _server.logger.warning(
            "[system_stop_start] could not write the stop_all batch %s (%s); "
            "the next start_all will have nothing to resume",
            path, type(exc).__name__,
        )
        return False
    return True


def _read_stop_all_batch() -> Optional[Dict[str, Any]]:
    """The recorded stop_all batch, or ``None`` when there is not one.

    Every unusable record answers ``None``: absent, unreadable, not
    JSON, JSON but not an object, a version this code does not know, or
    a ``plan_ids`` that is not a list. A damaged record costs a
    resume — never an exception, and never a wrong resume.
    """
    path = _batch_path()
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # ``ValueError`` covers ``json.JSONDecodeError``.
        _server.logger.warning(
            "[system_stop_start] ignoring an unreadable stop_all batch %s (%s)",
            path, type(exc).__name__,
        )
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != _BATCH_VERSION:
        _server.logger.warning(
            "[system_stop_start] ignoring a stop_all batch %s with version %r",
            path, data.get("version"),
        )
        return None
    if not isinstance(data.get("plan_ids"), list):
        return None
    return data


def _batch_plan_ids(batch: Optional[Dict[str, Any]]) -> List[str]:
    """The plan ids of a batch record, in the order they were stopped."""
    if not batch:
        return []
    plan_ids: List[str] = []
    seen: set = set()
    for raw in batch.get("plan_ids") or []:
        plan_id = str(raw or "").strip()
        if not plan_id or plan_id in seen:
            continue
        # A plan id is a directory name, never a path: refuse anything
        # that could point outside PLANS_DIR.
        if plan_id in (".", "..") or "/" in plan_id or "\\" in plan_id:
            continue
        seen.add(plan_id)
        plan_ids.append(plan_id)
    return plan_ids


def _consume_stop_all_batch() -> None:
    """Delete the batch record: the global start consumes it.

    A failure here is a warning, not an error: the resume already
    happened, and a record that could not be deleted only means a
    second start_all would try the same batch again.
    """
    path = _batch_path()
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _server.logger.warning(
            "[system_stop_start] could not delete the stop_all batch %s (%s); "
            "a second start_all would try the same batch again",
            path, type(exc).__name__,
        )


@router.post("/api/system/stop_all")
def stop_all_tasks():
    """Stop every plan that is running, in one call.

    The first half of a backend restart, and the counterpart of
    :func:`start_all_tasks`. The operator asks for one stop instead of
    one stop per plan, and the answer says what happened to each of
    them.

    The list of plans comes from :func:`get_active_tasks` — the same
    read the supervisor polls — so "what is running" has one answer in
    this codebase and not two that can disagree.

    Each plan is stopped by the route that owns its kind: an execution
    through ``stop_execution``, a verification through
    ``stop_verification``. Both are imported *inside* the function: the
    route modules import ``server``, which imports this one, so a
    module-level import here would be circular.

    One plan's failure is one row. The others are still stopped.

    The plans this call actually stopped are recorded as a *batch* (see
    :func:`_write_stop_all_batch`), and the batch is the only thing
    the global start will resume. Without it, a restart would pick up
    every checkpoint left on disk — including the ones a previous,
    unrelated stop left days ago.
    """
    from routes.execution import stop_execution
    from routes.verification import stop_verification

    actions = {
        _EXECUTION_TYPE: stop_execution,
        _VERIFICATION_TYPE: stop_verification,
    }

    results: List[Dict[str, Any]] = []
    handled: set = set()
    for detail in (get_active_tasks() or {}).get("details") or []:
        plan_id = str(detail.get("plan_id") or "")
        kind = str(detail.get("type") or "")
        action = actions.get(kind)
        # A plan is handled once: the two registries are independent, so
        # the same plan can be listed as an execution *and* as a
        # verification, and stopping it twice would report a conflict
        # for a plan that is in fact fine.
        if not plan_id or action is None or plan_id in handled:
            continue
        handled.add(plan_id)
        results.append(_run_for_plan(plan_id, kind, action))

    # Only the plans that were really stopped belong to the batch: a
    # plan that refused to stop has nothing to resume, and listing it
    # would make the next start report a phantom failure.
    _write_stop_all_batch([
        row["plan_id"] for row in results if row.get("ok") is True
    ])
    return {"results": results}


def _checkpoint_kind(plan_id: str) -> Tuple[bool, str]:
    """``(a checkpoint was there, the kind it names)`` for one plan.

    A checkpoint that cannot be read counts as absent — the same
    degradation every other reader of it makes, so a torn file can
    never turn this listing into an error.
    """
    from pause_state import checkpoint_path, read_pause_checkpoint

    try:
        plan_dir = _server.PLANS_DIR / plan_id
        if not checkpoint_path(plan_dir).exists():
            return False, ""
        checkpoint = read_pause_checkpoint(plan_dir)
    except OSError:
        return False, ""
    if not isinstance(checkpoint, dict):
        return False, ""
    return True, str(checkpoint.get("kind") or "")


def _start_targets(batch_plan_ids: List[str]) -> List[Tuple[str, str]]:
    """Every plan in the batch that has something to resume, and how.

    The batch is the *only* source of candidates. A plan this stop did
    not stop is not resumed, however old its checkpoint is: a restart
    must not wake up the leftovers of an unrelated stop from days ago.

    Within the batch, two things can make a plan resumable:

    * a **pause checkpoint on disk** — the durable half of a stop, and
      the one that survives the restart this endpoint exists for. The
      checkpoint's ``kind`` says which start owns the plan.
    * a **recovered verification whose thread is gone** — the backend
      was killed mid-round, so the round is in the database but nothing
      is running it. Such a plan has no checkpoint (it was killed, not
      stopped), and without this source it could never be started
      again: its recovered state still says "running", so the start
      endpoint would answer 409 forever.

    A batch member with neither is reported as ``_NO_CHECKPOINT_KIND``
    rather than dropped: silence would leave the operator believing the
    whole batch was resumed.
    """
    targets: List[Tuple[str, str]] = []
    for plan_id in batch_plan_ids:
        has_checkpoint, kind = _checkpoint_kind(plan_id)
        if has_checkpoint:
            # A checkpoint with an unknown (or empty) kind is still a
            # checkpoint: the caller reports it as unsupported rather
            # than as "nothing to resume".
            targets.append((plan_id, kind))
            continue
        state = (_server._verification_state or {}).get(plan_id)
        if (
            isinstance(state, dict)
            and state.get("recovered") is True
            and state.get("thread") is None
        ):
            # The recovered marker is what makes a hard-killed plan
            # resumable; a round that is genuinely running owns a live
            # thread and must be left alone, or two rounds would run
            # over one plan.
            targets.append((plan_id, _VERIFICATION_TYPE))
            continue
        targets.append((plan_id, _NO_CHECKPOINT_KIND))
    return targets


@router.post("/api/system/start_all")
def start_all_tasks():
    """Resume the batch this stop stopped, in one call.

    The second half of a backend restart, and the counterpart of
    :func:`stop_all_tasks`.

    The targets are the plans named by the batch the global stop
    recorded — and only those. A checkpoint left on disk by an earlier,
    unrelated stop is not resumed: the restart belongs to the batch,
    not to every pause that ever happened.

    Within the batch, a plan is resumed from its pause checkpoint on
    disk, or — for a verification whose thread a hard kill took away —
    from its recovered state. Each is started by the route that owns
    its kind.

    The execution start is handed an explicitly empty request: the
    checkpoint carries the project directory the work was happening in,
    and a request that named one would redirect the resumed work to a
    directory it was never written for.

    One plan's failure is one row. The others are still started.

    The batch is consumed: the record is deleted once the resume has
    been attempted, whatever the per-plan outcome, so a second
    ``start_all`` is a no-op instead of a second resume.

    The answer carries the batch it acted on (``"batch"``), and
    ``"detail"`` when there was none to act on.
    """
    from routes.execution import start_execution
    from routes.verification import start_verification

    def _start_execution(plan_id: str) -> Any:
        return start_execution(plan_id, _server.StartExecutionRequest())

    def _start_verification(plan_id: str) -> Any:
        return start_verification(plan_id)

    actions = {
        _EXECUTION_TYPE: _start_execution,
        _VERIFICATION_TYPE: _start_verification,
    }

    batch = _read_stop_all_batch()
    batch_plan_ids = _batch_plan_ids(batch)

    results: List[Dict[str, Any]] = []
    for plan_id, kind in _start_targets(batch_plan_ids):
        if kind == _NO_CHECKPOINT_KIND:
            # A plan of the batch that has nothing to resume. Reported
            # as one row: the other plans of the batch are still
            # started.
            results.append({
                "plan_id": plan_id,
                "type": "unknown",
                "ok": False,
                "error": "no_checkpoint",
            })
            continue
        action = actions.get(kind)
        if action is None:
            # A checkpoint nobody can resume. Reported rather than
            # dropped: silence would leave the operator believing the
            # whole fleet was resumed.
            results.append({
                "plan_id": plan_id,
                "type": kind or "unknown",
                "ok": False,
                "error": "unsupported_kind",
            })
            continue
        results.append(_run_for_plan(plan_id, kind, action))

    # The batch is consumed whatever the rows say: a batch that
    # survived its own start would be resumed a second time.
    _consume_stop_all_batch()

    if batch is None:
        return {
            "results": results,
            "batch": None,
            "detail": "no stop_all batch on record",
        }
    return {"results": results, "batch": batch}


@router.get("/api/plans")
def list_plans(
    request: Request,
    response: Response,
    include_terminal: bool = False,
):
    """List all plans.

    By default, plans in terminal states (``stopped``, ``completed``,
    ``failed``, ``verification_loop_stopped``, ``verification_passed``,
    ``verification_failed``) are filtered out — they are historical
    artifacts that no longer need a sidebar entry. Pass
    ``?include_terminal=true`` to see the full list (e.g. for
    audit / cleanup scripts).

    ``waiting_for_quota`` is NOT one of the terminal phases: a plan
    parked for capacity is alive and will resume when the window opens,
    so it stays in the sidebar.

    The terminal decision reads TWO verdicts, not one: the routing
    phase (the workflow's answer) and the persisted execution row (the
    run's answer).  A run that failed or was stopped is over whatever
    the workflow phase says — see :func:`_plan_is_terminal`.

    Provenance
    ----------
    The answer says which tree it read: ``data_source``
    (``state_db`` when the phases came from the state database,
    ``directory`` when the listing degraded to a directory scan) and
    ``plans_dir`` (the root it read).  Both appear on every row and on
    the response headers, so an empty list from a process pointed at
    another worktree's plans root cannot be mistaken for "this
    instance has no plans".

    The ``data_source`` of a row is a BLOCK, not a word: it carries the
    kind (``state_db`` / ``directory``) together with the facts about
    the data plane the rows came from — the database by path and by
    inode, the process that answered (pid + boot id) and how many rows
    it found.  The kind is what the response header carries; the block
    is what a reader needs to tell this answer apart from another
    instance's answer about the same URL.

    Refusal
    -------
    A plans tree and a state database are two halves of one instance's
    world.  When the tree holds plans and the database knows NONE of
    them, the two halves belong to different instances — and the
    listing built from that database is empty, which is exactly what
    "this instance has no plans" looks like on the wire.

    So the listing refuses in that case: ``503``, with the data plane
    that could not answer named in the body.  An empty array is a
    coincidence of shape that a reader takes for a fact; a refusal is
    an answer about what this instance could and could not read.

    Degradation
    -----------
    The terminal filter needs the phase, and the phase lives in the
    state database.  When that database cannot be opened, the listing
    degrades to a directory scan: it answers 200 with every plan it can
    see and it CANNOT filter the terminal ones.  Such an answer carries
    the degradation marker — on every row
    (``list_degraded``) and on the response
    (``X-PDT-Plans-List-Degraded``) — because "200 + a full list" is
    what a reader takes for "the terminal plans were filtered out".
    See :data:`LIST_DEGRADED_KEY`.

    Reads
    -----
    * RoutingRepository.list_all(data_dir=PLANS_DIR) — single
      aggregate call returning BOTH new (SQLite) and archived
      (directory-derived) rows. Archived rows carry
      ``archived=True`` and DO NOT carry stage / current_phase
      fields.
    * ExecutionRepository.snapshot_for_list(plan_ids) — the execution
      row of every listed plan, read from the state database.  It
      carries the run's own verdict (``exec_status``), which is part of
      the terminal decision, plus the run-state fields
      (``exec_pid`` / ``project_dir`` …); the workflow phase comes from
      the routing row above, not from here (schema v6).  Archived rows
      have no execution rows so they get ``None``.

    Architecture decision point 5 forbids an in-memory aggregation
    facade: this route composes the two repositories directly
    (no intermediate facade object).  Human-readable fields
    (``requirement``, ``created_at``, ``steps``) are still derived
    from the on-disk JSON files because those fields are slated
    for a separate "interview_repository" / "artifact metadata"
    task and are out of scope for this refactor.

    Caching
    -------
    The rendered list is cached in :data:`_PLANS_CACHE` for
    :data:`_PLANS_CACHE_TTL_SECONDS` seconds, keyed on the
    ``include_terminal`` query parameter. Mutation endpoints call
    :func:`invalidate_plans_cache` to drop the snapshot the moment
    they know it is stale; absent explicit invalidation, the TTL
    guarantees self-healing within a small bounded delay.

    The degradation verdict and the provenance are read off the cached
    rows, so a cache hit reports the same verdict and the same tree the
    cache miss did.
    """
    cached = _server._read_plans_cache(include_terminal)
    if cached is not None:
        _mark_listing_degraded(response, cached)
        _mark_listing_provenance(response, cached, include_terminal)
        return cached
    # Fast-path: empty PLANS_DIR — skip the state-machine open + DB scan +
    # bulk snapshot entirely. A fresh install with no plans (or a runtime
    # where PLANS_DIR hasn't been populated yet) hits this on every
    # sidebar poll; the cache-miss path would otherwise pay for an
    # empty RoutingRepository.list_all() + ExecutionRepository.snapshot
    # even though both return no rows.
    if not _server.PLANS_DIR.exists():
        empty: List[Dict[str, Any]] = []
        # The state database was NOT read on this path, so the answer
        # comes from the plans directory — the root that does not
        # exist.  Say so: an empty list that names no tree cannot be
        # told apart from "another instance's tree, and it has no
        # plans".
        _remember_listing_provenance(
            include_terminal,
            _listing_provenance(
                _data_source_block(
                    LIST_DATA_SOURCE_DIRECTORY,
                    _resolved_state_db_path(request),
                    0,
                )
            ),
        )
        _server._write_plans_cache(include_terminal, empty)
        _mark_listing_provenance(response, empty, include_terminal)
        return empty
    try:
        result, provenance = _list_plans_impl(request, include_terminal)
    except PlansDataPlaneMismatch as mismatch:
        # The state database this instance reads does not describe the
        # tree it serves.  Refuse, and name the data plane that could
        # not answer — an empty array here would read as "there is
        # nothing here", which is a fact about the WRONG instance.
        return _plans_data_plane_mismatch_response(mismatch)
    _remember_listing_provenance(include_terminal, provenance)
    _server._write_plans_cache(include_terminal, result)
    _mark_listing_degraded(response, result)
    _mark_listing_provenance(response, result, include_terminal)
    return result


def _list_plans_impl(
    request: Request, include_terminal: bool
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Compute the ``/api/plans`` payload from disk.

    Returns the rendered rows AND the provenance of that rendering —
    the data source and the plans root.  The rows cannot carry it on
    their own when the listing is empty, and an empty listing is
    exactly the answer that needs it: "no plans here" and "another
    instance's plans root" look alike without it.

    This is the cache-miss path; ``list_plans`` wraps it with the
    TTL + explicit-invalidation layer above.

    2026-09-23: the state-machine connection is opened here and
    released in a ``finally``, so the descriptor's lifetime does not
    depend on which of the 200 lines below happens to return. The body
    lives in :func:`_list_plans_body` and receives the handle instead
    of opening its own.

    Raises :class:`PlansDataPlaneMismatch` when the state database
    does not describe the plans tree this instance serves — the caller
    turns that into a refusal rather than an empty answer.
    """
    sm = _server._open_state_machine(request)
    try:
        return _list_plans_body(request, include_terminal, sm)
    finally:
        _server._close_state_machine(sm)


def _list_plans_body(
    request: Request,
    include_terminal: bool,
    sm: Optional[Tuple[Any, ...]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build the plan list using an already-open state-machine handle.

    ``sm`` is injected rather than opened here: the caller owns its
    lifetime. It may legitimately be ``None`` (no ``state.db`` yet), in
    which case the legacy directory-listing path below still answers.

    Returns the rows and their provenance — see :func:`_list_plans_impl`.
    """
    # A listing without a state machine has no phase to filter on, so
    # it is a DEGRADED listing: the terminal-phase filter below cannot
    # be applied to it.  The rows it produces say so — see
    # :data:`LIST_DEGRADED_KEY` for why the answer must not be silent
    # about it.
    degraded = sm is None

    # -- the data plane, resolved BEFORE anything is rendered ----------
    # The answer has to be able to say which database it read, and it
    # has to say it even when the answer is empty: an empty array is
    # exactly what an instance serving another tree's data plane
    # produces, and it cannot say so by itself.
    db_path = _resolved_state_db_path(request)

    # 2026-10-26 (merge): main 的运行相位反驳与终局相位集合，两边都要。
    from plan_status import resolve_phase

    # The terminal phases live at module level: :data:`TERMINAL_PHASES`
    # (the whole phase vocabulary that ends a plan) and
    # :data:`WORKFLOW_TERMINAL_PHASES` (the subset that is the *workflow's*
    # own verdict, as opposed to the run's).  The distinction matters for a
    # plan that carries a verification row — see
    # :func:`_workflow_phase_is_terminal`.
    #
    # There is deliberately **no second copy** of the set here: the
    # listing has ONE terminal gate, and that gate asks which vocabulary
    # applies (see :func:`_plan_is_terminal`).  A local set beside it
    # would be a second, contradicting answer to the same question.

    plans: List[Dict[str, Any]] = []

    if sm is None:
        # Fresh install (no state.db yet) — fall back to directory
        # listing only so the route still returns SOMETHING.  This
        # preserves the legacy on-disk-JSON path for the migration
        # window.
        routing_rows: List[Dict[str, Any]] = []
        if _server.PLANS_DIR.exists():
            for d in sorted(_server.PLANS_DIR.iterdir()):
                if d.is_dir():
                    routing_rows.append(
                        {
                            "plan_id": d.name,
                            "current_phase": None,
                            "substage": None,
                            "version": None,
                            "updated_at": None,
                            "archived": False,
                        }
                    )
    else:
        _, routing, execution, _verification, _artifact = sm
        routing_rows = routing.list_all(data_dir=_server.PLANS_DIR)

    # -- where this listing comes from ---------------------------------
    # The state database was read when there is one — the phases are
    # authoritative and the terminal filter was applied.  Without one
    # the listing is a directory scan, and the answer has to say so.
    # The block carries the facts about the data plane as well as the
    # kind: the database by path and by inode, the process that
    # answered, and how many rows the answer was built from.
    data_source = _data_source_block(
        LIST_DATA_SOURCE_STATE_DB if sm is not None
        else LIST_DATA_SOURCE_DIRECTORY,
        db_path,
        len(routing_rows),
    )
    provenance = _listing_provenance(data_source)

    # -- the data plane must describe the tree this instance serves ----
    # A database that knows NONE of the plans in the tree is somebody
    # else's database, and the listing built from it is empty — which
    # is exactly what "this instance has no plans" looks like.  Refuse
    # instead of answering that.
    if sm is not None:
        conn = sm[0]
        known_plan_ids = _plan_ids_known_to_db(conn)
        # The archived plans are read out of the tree itself, so they
        # are known to this instance even when no table holds them.
        known_plan_ids |= {
            str(row.get("plan_id"))
            for row in routing_rows
            if isinstance(row, dict) and row.get("archived") and row.get("plan_id")
        }
        _refuse_when_data_source_does_not_describe_the_tree(
            known_plan_ids, data_source,
        )

    # ``plan_routing`` can hold ids that predate the plan-id validator
    # (a state.db can carry a ``'..'`` row from
    # the traversal era). Every per-row access below funnels through
    # ``_plan_dir``, so the first such row would raise and take down the
    # whole listing — one legacy row hiding every real plan. Skip and
    # log instead; URL routes still 400 a bad id, only this full-table
    # rendering is resilient.
    renderable_rows: List[Dict[str, Any]] = []
    for row in routing_rows:
        if _server._is_renderable_plan_id(row.get("plan_id")):
            renderable_rows.append(row)
        else:
            _server.logger.warning(
                "Skipping plan_routing row with unrenderable id %r in "
                "plans listing (row predates the plan-id validator)",
                row.get("plan_id"),
            )
    routing_rows = renderable_rows

    # -- the row order is the plan id, and only the plan id --------------
    # The listing's order is a **contract**: the frontend renders the rows
    # the way they arrive and never sorts them itself ("the order is the
    # listing's own" — ``frontend/app.js``).  A contract needs a stable
    # basis, and the database's row order is not one: ``plan_routing`` is
    # read back ``updated_at`` DESC, so the same set of plans written in a
    # different order produces a different listing, and a caller that
    # addresses a row by index addresses the wrong plan.
    #
    # Sorting here — after the renderable-id filter, before any row is
    # rendered — makes the order a function of the plan ids alone, and it
    # makes the two read paths (the state database and the directory
    # fallback) agree: the fallback already walks the directory in name
    # order, so both now answer with the same listing.
    routing_rows = sorted(
        routing_rows, key=lambda row: str(row.get("plan_id") or "")
    )

    # Bulk-fetch execution rows for every non-archived plan_id.
    new_plan_ids = [
        r["plan_id"] for r in routing_rows if not r.get("archived")
    ]
    execution_by_plan: Dict[str, Optional[Dict[str, Any]]] = {}
    if sm is not None and new_plan_ids:
        _, _, execution, _, _ = sm
        execution_by_plan = execution.snapshot_for_list(new_plan_ids)

    # Bulk-fetch the verification rows for the same plan_ids.  They are the
    # third verdict this listing needs: a plan that carries one is in — or
    # has been in — a verification loop, and the run's verdict does not
    # speak for that loop.  Only the ids that HAVE a row come back, so
    # membership in this mapping is the evidence.
    verification_by_plan: Dict[str, Dict[str, Any]] = {}
    if sm is not None and new_plan_ids:
        _, _, _, verification, _ = sm
        verification_by_plan = verification.snapshot_for_list(new_plan_ids)

    for row in routing_rows:
        plan_id = row["plan_id"]
        plan_dir = _server._plan_dir(plan_id)
        is_archived = bool(row.get("archived"))

        # 产物判定：这条 plan 走哪条分支、盘上有哪些产物。判定来自
        # :func:`plan_artifacts.detect`（唯一入口）。
        #
        # 2026-11-09: 共存（两类产物同时存在）**不是冲突** —— 主分支是 dev
        # （任务优先），计划书是附加产物。列表曾经把这种 plan 丢进 ``conflict``
        # 桶，现在没有了：条目给出主分支与两份产物的相对名。
        artifacts = detect(plan_dir)

        has_interview = (plan_dir / "interview.json").exists() if plan_dir.exists() else False
        has_prd = (
            (plan_dir / "prd.json").exists() or (plan_dir / "prd.md").exists()
        ) if plan_dir.exists() else False
        has_review = (plan_dir / "review.json").exists() if plan_dir.exists() else False
        has_tasks = (plan_dir / "tasks.json").exists() if plan_dir.exists() else False
        has_arch = (plan_dir / "arch-design.md").exists() if plan_dir.exists() else False
        has_test = (plan_dir / "test-design.md").exists() if plan_dir.exists() else False

        # For new (non-archived) plans: derive current_phase from
        # the plan_execution row, fall back to status heuristic.
        # For archived plans: skip the heuristic; archive_scan
        # has already said everything that's relevant.
        if is_archived:
            current_phase = None
            steps_dict: Dict[str, bool] = {
                "interview": has_interview,
                "prd": has_prd,
                "review": has_review,
                "arch": has_arch,
                "test": has_test,
                "tasks": has_tasks,
            }
            exec_pid = None
            status = "archived"
        else:
            status = _artifact_status(
                has_tasks=has_tasks,
                has_review=has_review,
                has_prd=has_prd,
                has_interview=has_interview,
            )

            # ``current_phase`` used to be read from the ``plan_execution``
            # row — the "display-only mirror" of the routing phase — with
            # ``status`` (a heuristic guessed from which documents happen
            # to exist on disk) as the fallback for plans that had no
            # execution row at all.  Schema v6 removed that column, so
            # this reads the authority: ``plan_routing.current_phase``,
            # the same column ``_build_plan_status`` and every card
            # already use.  One phase column, one answer.
            #
            # The heuristic survives ONLY as the fallback for a plan with
            # no routing row, which is the archived / no-state.db path
            # where there is no recorded phase to report at all.  Note
            # that its vocabulary is NOT the phase vocabulary
            # (``reviewed`` / ``interviewed`` / ``new`` are not valid
            # phases), so preferring it would reintroduce exactly the
            # two-vocabulary split v5 removed.
            exec_row = execution_by_plan.get(plan_id)
            # The execution record is read BEFORE the phase is decided,
            # because whether the recorded phase can be believed depends
            # on it (see the runtime-phase check below).
            routing_phase = str(row.get("current_phase") or "")
            # 2026-10-25 (R10): ``ready`` 是 ``queued`` 的历史别名。存量
            # ``plan_routing`` 行里还存着它，而启动时的迁移（``phase_migration``）
            # 可能还没跑过、或跑在另一个库上 —— **读路径必须自己兜住**，否则
            # 列表会把旧词原样透出给前端与调度器。
            #
            # 归一化只动**相位词汇**，不动这一行的其它语义：这一行仍然返回
            # （不跳过、不 500），桶（``status``）仍按下面的既有规则算。命中
            # 别名时留一条 warning —— 旧数据要清得掉，先得知道它在哪儿。
            routing_phase, aliased = normalize_waiting_phase(routing_phase)
            if aliased:
                log_deprecated_ready(_server.logger, plan_id, "plans.list")
            current_phase = routing_phase or status
            # 启发式兜底也可能说出旧词：等待桶在等待相位合并之前叫
            # ``ready``。相位字段同样不能说旧词汇，所以这里 **静默** 归一
            # 化：启发式是当场算出来的，不是存量数据，不值得为它刷一条
            # 弃用日志（真正的旧数据来自上面那一行）。
            current_phase, _ = normalize_waiting_phase(current_phase)

            # 2026-10-26 (merge): 终局判定**两边都要**。
            #
            # 本分支的 :func:`_plan_is_terminal` 看的是两个裁决：路由相位
            # （工作流的答案）与持久化的执行行（这一轮运行的答案）。第二个是
            # 单看相位给不出来的 —— 进程死掉时工作流相位留在原地，裁决留在行
            # 上；只读相位的列表会把一条死掉的计划继续摆在列表里，仿佛它还在
            # 跑。它还明确把 ``waiting_for_quota`` 排除在终局之外：等配额的
            # 计划是活的，窗口一开就继续。
            #
            # 2026-11-02: 第三个裁决进来了 —— **验证行**（``plan_verification``）。
            # 有验证行的计划是在（或进过）一轮验证里的，那轮验证是另一件活：
            # 执行的裁决说的是**进程**的死活，对还开着的验证循环什么也证明不了。
            # 所以有验证行时只看**工作流**的终局相位
            # （:data:`WORKFLOW_TERMINAL_PHASES`）：执行裁决（``failed`` /
            # ``stopped``）单独一条不许把这样的计划藏起来 —— 验证中、能启动
            # 验证、验证循环停下来的结果，都是面板必须看得见的东西。没有验证
            # 行的失败计划保持原样：它就是结束了。
            persisted_exec_status = _persisted_exec_status(exec_row)
            has_verification_row = plan_id in verification_by_plan
            # The stop reason is the third verdict: it tells a *stopped*
            # run apart from a *dead plan*.  A plan the scheduler
            # evicted was put back in the queue — it is waiting for its
            # next turn, not gone — so the listing must not filter it
            # out on the strength of the execution row alone.
            persisted_stop_reason = _persisted_stop_reason(exec_row)
            # The ONE terminal gate of this listing.
            #
            # Two verdicts, one rule: the phase verdict and the run's
            # verdict.  :func:`_plan_is_terminal` owns both — and it owns
            # the question of WHICH phase vocabulary applies.  With a
            # verification row only the workflow's own verdict counts
            # (:func:`_workflow_phase_is_terminal`): the run's verdict is
            # about a *process*, and says nothing about a verification
            # loop that is still going.  Without one the full phase
            # vocabulary applies, so a failed plan that carries no
            # verification row stays hidden exactly as before.
            #
            # There is deliberately no second, parallel phase test beside
            # this one: it would answer the same question a second time
            # with the *other* vocabulary — which is exactly how a plan
            # whose verification loop is still going disappeared from the
            # default listing once already.  The redundant parallel clause
            # came back in a merge-conflict resolution (cfba0c53) although
            # the code before it (e5a725d8) never had it.  One gate, one
            # rule.
            if not include_terminal and _plan_is_terminal(
                current_phase,
                persisted_exec_status,
                persisted_stop_reason,
                has_verification=has_verification_row,
            ):
                continue

            # An **explicit bucket**: a phase that has a stable label
            # (``plan_state.PHASE_LABELS``) is a phase the listing must
            # not guess — the guess is made from which documents happen
            # to exist on disk, and it does not know the plan-doc review
            # phase, so that phase was labelled ``reviewed`` (a phase
            # that never happened). The bucket is named after the phase,
            # and the phase column is the authority.
            #
            # Only a phase that actually has a stable label is bucketed
            # this way. Every other phase keeps the historical guess, so
            # the listing's bucket vocabulary does not change under the
            # reader — and a plan with NO routing row keeps the guess as
            # its only answer, because there is no recorded phase to
            # bucket by.
            #
            # The bucket is decided LAST (see below): for a phase with a
            # stable label the phase column is the authority on "where is
            # this plan", so the bucket is named after the phase even if
            # an execution happens to be live — the entry still carries
            # ``pid`` for that.
            explicit_bucket = (
                routing_phase
                if _stable_phase_label(routing_phase) is not None
                else None
            )

            exec_pid = None
            exec_status = None
            exec_state = _server._execution_state.get(plan_id)
            if isinstance(exec_state, dict):
                exec_status = exec_state.get("status")
                exec_pid = exec_state.get("pid")
            if isinstance(exec_row, dict):
                if exec_status is None:
                    exec_status = exec_row.get("exec_status")
                if exec_pid is None:
                    pid_from_repo = exec_row.get("exec_pid")
                    if isinstance(pid_from_repo, int):
                        exec_pid = pid_from_repo

            # ``executing`` is the one phase in the vocabulary that does
            # not describe a plan's ARTEFACTS — it describes a PROCESS:
            # "the executor is running this plan right now".  So it is the
            # one phase the execution store can refute, and the store is
            # read here for that reason.
            #
            # The routing row can hold such a phase with nothing running
            # behind it.  The way it happens: a plan directory carrying a
            # legacy ``plan_state.json`` is imported once by
            # ``plan_state.PlanState._load_state``, which copies the
            # JSON's ``current_phase`` into ``plan_routing`` verbatim.  A
            # fixture left behind by a test run — or a plan whose executor
            # was killed and whose row was never reconciled — then carries
            # "executing" forever.
            #
            # The listing answered ``status=ready`` (no pid, no execution
            # row) with ``current_phase=executing`` for those: two answers
            # for one plan, and anything that counts ``current_phase`` to
            # answer "how many plans are running?" over-counts by every
            # one of them.
            #
            # A plan the executor never touched cannot be executing, so
            # with no execution record at all the runtime claim is not
            # merely stale — it is impossible, and the phase falls back to
            # the same artifact-derived ``status`` this route already
            # falls back to when a plan has no routing row.  A plan that
            # IS running keeps its phase: the in-memory
            # ``_execution_state`` entry exists from before the phase is
            # stamped, and the persisted row from before the subprocess
            # is spawned.
            #
            # ``verification_running`` is deliberately NOT included: a
            # verification round records its liveness in
            # ``plan_verification`` / ``_verification_state``, not in
            # ``plan_execution``, so "no execution row" says nothing
            # about it.
            #
            # The judgement itself lives in
            # ``plan_status.resolve_phase`` — the single-plan status
            # endpoint calls the SAME function with the same fallback, so
            # the two readers cannot answer differently about one plan.
            current_phase = resolve_phase(
                current_phase,
                execution_recorded=(
                    exec_row is not None or exec_state is not None
                ),
                fallback=status,
            )

            # The second terminal gate: the phase AFTER the runtime claim
            # was refuted.  Same rule as the first one — a plan that
            # carries a verification row is judged by the workflow's
            # verdict alone.
            if not include_terminal and _workflow_phase_is_terminal(
                current_phase, has_verification=has_verification_row
            ):
                continue

            if (
                exec_status == "running"
                and isinstance(exec_pid, int)
                and exec_pid > 0
            ):
                status = "running"
            else:
                exec_pid = None

            # 2026-11-22: 桶由**相位 / 验证行**推出，不再由「目录里有没有
            # tasks.json」猜。
            #
            # 猜出来的桶把**所有**有 ``tasks.json``、执行行又没说 running 的
            # 计划都报成 ``queued`` —— 包括失败的和停止的。线上实测就是一条
            # 已经停下的计划在列表里显示「排队中」（前端 ``phaseLabel`` 把它
            # 翻成中文）。相位列才是「这条计划现在是什么」的权威：桶跟着它走，
            # 启发式只在**没有相位**（空串 / 没有 routing 行）时兜底。
            #
            # 验证行要一起读：修复相位（``verification_repairing``）自己不说是
            # 死是活 —— 它只说「修复任务在等着跑」，是「操作者停过」还是
            # 「修复没跑成」只有验证行知道（见
            # :func:`plan_status.listing_status_bucket`）。
            #
            # 读验证行不许把列表搞挂：读不到就按「没有验证行」算，桶退回
            # 启发式，接口照常 200。
            verification_status = ""
            verification_stop_reason = ""
            try:
                verification_row = verification_by_plan.get(plan_id) or {}
                verification_status = str(
                    verification_row.get("verification_status") or ""
                )
                verification_stop_reason = str(
                    verification_row.get("verification_stop_reason") or ""
                )
            except Exception:  # noqa: BLE001 — 一个列表不许因为一行而 500
                _server.logger.exception(
                    "[plans.list] could not read the verification row "
                    "plan=%s", plan_id,
                )

            phase_bucket = listing_status_bucket(
                current_phase,
                verification_status=verification_status,
                verification_stop_reason=verification_stop_reason,
            )
            if phase_bucket is not None:
                status = phase_bucket
            elif explicit_bucket is not None:
                status = explicit_bucket

            steps_dict = {
                "interview": has_interview,
                "prd": has_prd,
                "review": has_review,
                "arch": has_arch,
                "test": has_test,
                "tasks": has_tasks,
            }

        # Human-readable fields.  For archived plans use the
        # requirement_first_line captured at archive-scan time;
        # for new plans, derive from the on-disk interview.json
        # — the same path the legacy implementation used.
        requirement_text = ""
        created_at_text = ""
        if is_archived:
            requirement_text = str(row.get("requirement_first_line") or "")[:100]
            created_at_text = str(row.get("created_at") or "")
        else:
            interview_data = {}
            if has_interview:
                try:
                    # 2026-09-23: reuse the connection the wrapper
                    # opened. This used to call _open_state_machine()
                    # per plan, inside the loop, and drop the handle —
                    # one leaked SQLite connection per plan with an
                    # interview artifact, on every GET /api/plans.
                    if sm is not None:
                        _, _, _, _, artifact_repo = sm
                        interview_data = artifact_repo.query_interview(plan_id) or {}
                    else:
                        interview_data = {}
                except Exception:
                    interview_data = {}
            requirement_text = str(
                interview_data.get("dimensions", {}).get("goals", "")
            )[:100] if interview_data else ""
            created_at_text = (
                interview_data.get("created_at", "") if interview_data else ""
            )

        flags = (
            {"arch_enabled": False, "test_enabled": False}
            if is_archived
            else {"arch_enabled": False, "test_enabled": False}
        )
        if not is_archived:
            # ``flags`` used to live in ``plan_state.json``; it is
            # now a JSON column on the plan_execution row (see
            # state_machine.repositories.execution_repository).  Read
            # it via the bulk-snapshot already populated above rather
            # than opening the legacy JSON file directly.
            exec_row = execution_by_plan.get(plan_id)
            if isinstance(exec_row, dict):
                row_flags = exec_row.get("flags")
                if isinstance(row_flags, dict):
                    flags = dict(row_flags)
                else:
                    flags = {"arch_enabled": False, "test_enabled": False}
            else:
                flags = {"arch_enabled": False, "test_enabled": False}

        entry: Dict[str, Any] = {
            "id": plan_id,
            "status": status,
            "requirement": requirement_text,
            "created_at": created_at_text,
            "flags": flags,
            "steps": steps_dict,
            "pid": exec_pid,
            "archived": is_archived,
        }
        if is_archived:
            # Archived rows expose only the public identifiers; they
            # MUST NOT carry stage / current_phase / substage /
            # version keys (those are SQLite state-machine semantics
            # that do not apply to archived plans).
            entry["requirement_first_line"] = requirement_text
        else:
            entry["current_phase"] = current_phase
            # The same stable label the status interface returns, read
            # from the same table — one phase, one label, two endpoints.
            entry["current_phase_label"] = _phase_label(current_phase)
        if degraded:
            # This row's phase is NOT authoritative: it came from the
            # directory fallback, where no phase could be read and the
            # terminal filter could not be applied.  Say so on the row
            # itself, so the answer cannot be read as "the terminal
            # plans were filtered out".
            entry[LIST_DEGRADED_KEY] = True
            entry[f"{LIST_DEGRADED_KEY}_reason"] = LIST_DEGRADED_REASON
            # The same verdict under the name the execution section
            # already uses, so a consumer that reads one vocabulary
            # finds it either way.
            entry["degraded"] = True
        # Every row carries the provenance of the listing it came from:
        # which data source produced it, and which plans root that
        # source read.
        entry.update(provenance)
        # 产物判定的事实也进条目：**主分支**与**盘上实际存在的产物的相对名**。
        # 共存不是冲突 —— 主分支是 dev（任务优先），计划书是附加产物，两份产物
        # 的相对名都在这里。状态接口给的是同一份判定，两个读者不许对同一条
        # plan 给出两个答案。
        entry["branch"] = artifacts.branch
        entry["artifacts"] = _artifact_names(artifacts)
        plans.append(entry)
    return plans, provenance


def _seed_plan_routing_phase(plan_id: str, phase: str) -> None:
    """Create the plan's routing row, holding ``current_phase=phase``.

    Task #3.9: the interview phase is now tracked in
    ``plan_routing`` so the routing table is the single source of
    truth for the current workflow phase. Before this change,
    ``PlanState._load_state`` fell through to ``_default_state`` →
    ``_infer_phase`` and derived the phase from file-existence
    (e.g. ``interview.json`` present ⇒ ``interview_complete``),
    which mis-classified a fresh plan whose first
    ``interviewer.start()`` call had already written
    ``interview.json``.

    The write is **create-only**: the row is created when it is
    missing, and when it is already there *not one column* is
    written.  A plan whose row has already advanced past ``phase``
    (e.g. ``prd_review``) is therefore never regressed by a re-run —
    that is what keeps a restart on a live plan safe.  Failures are
    non-fatal — the legacy file fallback still works.

    The write goes through the routing repository
    (:meth:`RoutingRepository.insert_if_missing`) rather than through
    a statement spelled out here.  ``current_phase`` is the single
    workflow-state column — the DDL comment in
    ``backend/state_machine/db/schema.py`` says so — and the routing
    repository is its only writer; a second, hand-written write of
    the row would be a second writer of the phase.  The repository's
    create-only method has exactly the semantics this helper needs:
    the row is written when it is missing and nothing at all is
    written when it is there.

    The repo + connection follow the same resolution as the rest
    of the server (see ``_state_db_path``): ``PDT_STATE_DB_PATH``
    env override first, then the canonical repo-root ``state.db``.
    """
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )
        db_path = _server._state_db_path()
        conn = _open_db(db_path)
        try:
            _migrate(conn)
            repo = RoutingRepository(conn)
            # Create-only: the row is written when it is missing and
            # left alone when it is there.  An existing row whose phase
            # has advanced past ``phase`` (e.g. ``prd_review``) is NOT
            # rolled back — this keeps a restart safe.
            repo.insert_if_missing(plan_id, phase)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: legacy file fallback still works. Log via the
        # server logger so an operator can diagnose persistent
        # SQLite failures (e.g. migration schema drift).
        _server.logger.warning(
            "_seed_plan_routing_phase(%s, %s) failed: %s",
            plan_id, phase, exc,
        )


def _pending_decisions_block(plan_id: str) -> Dict[str, Any]:
    """The plan's outstanding human decisions.

    This is the block the three projections that answer "how is this
    plan doing" carry: the plan summary, the execution progress and
    the verification progress.  They share it so a consumer that has
    learned to read one has learned to read the other two — a plan
    waiting for a human has to look the same wherever it is looked
    at.

    Only ``pending`` decisions are listed.  An answered, defaulted or
    unroutable decision is settled: it is history, not work, and
    listing it would send the operator to answer a question nobody is
    waiting for any more.

    A decision table that cannot be read degrades to an empty block
    rather than a 500.  These endpoints answer "how is this plan
    doing", and a locked or missing decision table must not turn that
    question into a failed request — the honest answer is "nothing is
    outstanding", and the log says what actually happened.
    """
    try:
        from hitl.service import list_decisions

        decisions = list_decisions(plan_id, "pending")
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad: the import, the database and the row
        # decoder can each fail, and none of them is a reason to
        # refuse the whole projection.
        _server.logger.warning(
            "pending_decisions_read_failed plan=%s: %s", plan_id, exc,
        )
        return {"count": 0, "items": []}

    items = [
        {
            "decision_id": decision.decision_id,
            "question": decision.question,
            "source": decision.source,
            "created_at": decision.created_at,
        }
        for decision in decisions
    ]
    return {"count": len(items), "items": items}


@router.get("/api/plan/{plan_id}/summary")
def get_plan_summary(request: Request, plan_id: str):
    """Aggregated plan summary — route wrapper owning the DB handle.

    The payload is built by :func:`_get_plan_summary_body`, which
    receives an already-open state-machine handle. Splitting them lets
    the connection be released in a ``finally`` even though the body
    has a single terminal ``return`` deep inside its own branches.
    """
    sm = _server._open_state_machine(request)
    try:
        return _get_plan_summary_body(request, plan_id, sm)
    finally:
        _server._close_state_machine(sm)


def _normalize_summary_verification_status(raw_status: Any) -> str:
    """Map an orchestrator status onto the public status set.

    The table is :data:`server._VERIFICATION_STATUS_NORMALIZATION` — the
    same one ``routes.verification._normalize_verification_status`` uses
    for ``GET /api/verification/{id}/status``.  Both endpoints read the
    same row, so both must speak the same vocabulary.

    An unrecognised value is returned **verbatim**: it is a value this
    code does not understand, and reporting it as ``not_started`` would
    claim a plan never ran a verification round when the table says
    otherwise.
    """
    if not raw_status:
        return "not_started"
    return _server._VERIFICATION_STATUS_NORMALIZATION.get(raw_status, raw_status)


def _summary_current_phase(routing_repo: Any, plan_id: str) -> Optional[str]:
    """The plan's current phase, read from ``plan_routing``.

    Read from the routing repository (not from the already-loaded
    ``state``) so the summary and ``/api/verification/{id}/status``
    consult the same row for the same answer.
    """
    if routing_repo is None:
        return None
    try:
        route = routing_repo.current(plan_id)
    except Exception:  # noqa: BLE001 — an unreadable row is "no phase"
        return None
    if not isinstance(route, dict):
        return None
    return route.get("current_phase")


def _authoritative_verification_block(
    plan_id: str,
    routing_repo: Any,
    verification_repo: Any,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """The ``state.verification`` block, sourced from ``plan_verification``.

    2026-11-04: the summary reported ``plan_routing.verification`` — a
    *mirror* column the verification loop used to write on its way past.
    The authoritative record is the ``plan_verification`` row, the same
    table ``GET /api/verification/{id}/status`` reads.  The two had
    drifted apart on live plans (two plans reported a verdict the
    authoritative table contradicted), so the summary answered "how did
    verification go?" with a value nobody else agreed with.

    The four lifecycle keys (``status`` / ``round`` / ``max_rounds`` /
    ``stop_reason``) are replaced by the row's values.  Everything else
    in the block is carried over untouched — in particular the frozen
    ``baseline`` / ``baseline_doc_sha256``, which the modules deciding
    whether a plan may enter verification read from there.

    A plan that has no ``plan_verification`` row has never run a
    round, and says so with the documented defaults.  A plan whose phase
    is ``verification_running`` IS running, even when the row has not
    been stamped yet — the same rule the status endpoint applies.
    """
    # Copy first: the baseline keys live in the same dict and must
    # survive the override of the four lifecycle keys.
    block = dict(state.get("verification") or {})

    record: Optional[Dict[str, Any]] = None
    if verification_repo is not None:
        try:
            record = verification_repo.current(plan_id)
        except Exception:  # noqa: BLE001 — an unreadable row is "no row"
            record = None

    if record is None:
        status = "not_started"
        round_n = 0
        max_rounds = _server.DEFAULT_MAX_VERIFICATION_ROUNDS
        stop_reason: Optional[str] = None
    else:
        status = _normalize_summary_verification_status(
            record.get("verification_status")
        )
        round_n = record.get("round", 0)
        max_rounds = record.get(
            "max_rounds", _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
        )
        stop_reason = record.get("verification_stop_reason")

    if _summary_current_phase(routing_repo, plan_id) == "verification_running":
        status = "running"

    block["status"] = status
    block["round"] = round_n
    block["max_rounds"] = max_rounds
    block["stop_reason"] = stop_reason
    return block


def _get_plan_summary_body(
    request: Request, plan_id: str, sm: Optional[Tuple[Any, ...]],
) -> Dict[str, Any]:
    """Aggregated plan summary for AI agents — state, flags, available docs, task counts.

    Reads
    -----
    * :class:`PlanState`  — current state-machine JSON snapshot
      (kept for the ``state`` key, which is a fat object beyond
      what the four repositories cover — verification sub-state,
      completed_phases, etc.).
    * :class:`ArtifactRepository.list_for_plan` — the new
      ``artifacts`` manifest; replaces the implicit "does the file
      exist?" check that used to live in the ``docs`` block.
    * :class:`ExecutionRepository.summary` — execution metadata
      (status, project_dir, pid, ...) sourced from plan_execution.

    Architecture decision point 5: no in-memory aggregation facade.
    Each value the response carries is sourced from a repository
    or kept on disk intentionally (legacy JSON fields that the
    state-machine refactor has not yet ingested).
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    state = _server.PlanState(plan_dir).get_state()

    docs = {
        "interview": (plan_dir / "interview.json").exists(),
        "prd": (plan_dir / "prd.md").exists() or (plan_dir / "prd.json").exists(),
        "arch": (plan_dir / "arch-design.md").exists(),
        "test": (plan_dir / "test-design.md").exists(),
        "tasks": (plan_dir / "tasks.json").exists(),
    }

    task_counts = {"total": 0, "completed": 0, "failed": 0, "pending": 0, "in_progress": 0}
    tasks_file = plan_dir / "tasks.json"
    if tasks_file.exists():
        try:
            data = json.loads(tasks_file.read_text())
            tasks = data.get("tasks", [])
            # Track the original disk count so we can decide whether
            # to extend ``total`` later when orphans are appended (see
            # the 2026-09-10 v4 follow-up hydrate step below).  If
            # hydrate is skipped (no runtime DB), the original
            # ``len(tasks)`` is still the right total.
            disk_task_count = len(tasks)
            task_counts["total"] = disk_task_count

            # Overlay SQLite runtime state onto each task before counting.
            # Without this overlay, task counts reflect the static
            # tasks.json (all status=None / pending) and Telegram
            # progress cards show "进行中 0  待执行 50" even when 43
            # tasks have actually completed.  Mirrors the runtime
            # overlay applied in /api/execution/{id}/progress below.
            try:
                from state_machine.db.connection import open as open_db
                from state_machine.db.schema import migrate
                from state_machine.repositories.plan_task_repository import (
                    PlanTaskRepository,
                )

                runtime_conn = open_db(_server._state_db_path())
                try:
                    migrate(runtime_conn)
                    repo = PlanTaskRepository(runtime_conn)
                    runtime = repo.load_all(plan_id)
                    # 2026-09-10 (v4 follow-up): hydrate DB-only orphan
                    # rows so the summary / Feishu card see the same
                    # task set the dispatcher does. Without this step,
                    # ``plan_tasks`` carries 79 rows but ``tasks.json``
                    # only carries 63 (v3 → v4 R3-1 race stripped
                    # static fields from 16 rows before the executor
                    # re-saved ``tasks.json``). The agent's hot path
                    # already does this via ``agent.py:_load_tasks``
                    # orphan reconcile; mirror that here so the
                    # summary endpoint doesn't lie about progress.
                    disk_ids = {t.get("id") for t in tasks if isinstance(t, dict)}
                    _TERMINAL = {"completed", "failed", "skipped", "superseded", "voided"}
                    for tid, rt in runtime.items():
                        if tid in disk_ids:
                            continue
                        status = rt.get("status") or "pending"
                        if status in _TERMINAL:
                            # Already-finished orphan — count it but
                            # don't surface a placeholder (the
                            # dispatcher never re-runs terminal rows).
                            # 2026-09-12 plan v14 follow-up: also pull
                            # the real title so the failure card shows
                            # which task failed, not the bare id.
                            _orphan_rt_term = rt  # alias for symmetry
                            _real_title_term = (
                                _orphan_rt_term.get("title")
                                or _orphan_rt_term.get("description")
                                or ""
                            ).strip()
                            tasks.append({
                                "id": tid,
                                "title": _real_title_term or f"Task {tid} (DB-only)",
                                "status": status,
                                # 2026-09-14: copy failure_reason /
                                # end_ts through so the card's "❌
                                # 失败任务详情" section can render the
                                # real reason instead of "未知原因".
                                # The progress-endpoint hydrate
                                # (db_orphan_terminal, ~server.py:8240)
                                # already carries these fields; the
                                # summary hydrate had drifted and
                                # dropped them.
                                "failure_reason": rt.get("failure_reason"),
                                "end_ts": rt.get("end_ts"),
                                "_origin": "db_orphan_terminal",
                            })
                            continue
                        # Non-terminal orphan — surface as a
                        # placeholder so the Feishu progress bar
                        # reflects the work the dispatcher will
                        # actually pick up. Prefer the runtime row's
                        # title over the placeholder so the operator
                        # sees the real task identity (2026-09-12).
                        _real_title = (rt.get("title") or rt.get("description") or "").strip()
                        tasks.append({
                            "id": tid,
                            "title": _real_title or f"Task {tid} (DB-only)",
                            "description": (
                                rt.get("description")
                                or f"recovered from plan_tasks DB (no static "
                                   f"fields on disk; status={status!r}; see "
                                   f"plan_tasks row for the real task identity)"
                            ),
                            "status": status,
                            "updated_time": rt.get("end_ts"),
                            "_origin": "db_orphan",
                        })
                    for t in tasks:
                        if not isinstance(t, dict):
                            continue
                        # Only overlay disk-originated tasks — the
                        # placeholders we just appended already carry
                        # the right status.
                        if t.get("_origin") in ("db_orphan", "db_orphan_terminal"):
                            continue
                        rt = runtime.get(t.get("id"))
                        if not rt:
                            continue
                        if "status" in rt:
                            t["status"] = rt["status"]
                finally:
                    runtime_conn.close()
            except (sqlite3.OperationalError, OSError):
                # State-machine not on disk yet — tasks.json carries
                # the runtime fields directly (legacy / pre-migration).
                pass

            for t in tasks:
                st = t.get("status", "pending")
                if st in task_counts:
                    task_counts[st] += 1

            # 2026-09-10 v4 follow-up: re-sync ``total`` so the
            # progress bar reflects the *hydrated* task set, not just
            # disk.  Without this, a plan whose DB has 16 orphans
            # would show ``total=63`` while ``completed=75`` —
            # i.e. "完成 119%".  After hydrate, ``len(tasks)`` includes
            # every disk row + every DB-only orphan we appended above.
            task_counts["total"] = len(tasks)

        except Exception:
            pass

    project_dir = _server._get_project_dir(plan_id)
    exec_status = "not_started"
    started_at = None
    ended_at = None
    stop_reason = None
    sync_targets = None
    pid = None
    s = _server._execution_state.get(plan_id)
    if s:
        exec_status = s.get("status", "not_started")
        started_at = s.get("started_at")
        ended_at = s.get("ended_at")
        stop_reason = s.get("stop_reason")
        sync_targets = s.get("sync_targets")
        pid = s.get("pid")
    # ``sync_targets`` lives in the in-memory ``_execution_state`` dict
    # (it is a runtime-only field, not persisted to SQLite).  The
    # legacy ``execution.json`` fallback was removed because writes
    # already flow through ``_execution_state``; on cross-restart,
    # ``sync_targets`` defaults to ``None`` (the executor re-syncs it
    # the first time it touches the per-plan record).

    # ---- state-machine enrichment ----
    # The handle arrives from get_plan_summary, which closes it.
    artifacts_manifest: List[Dict[str, Any]] = []
    execution_row_from_sm: Optional[Dict[str, Any]] = None
    routing_phase: Optional[str] = None
    if sm is not None:
        _, routing_repo, execution_repo, _verification_repo, artifact_repo = sm
        try:
            artifacts_manifest = artifact_repo.list_for_plan(plan_id)
        except Exception:
            artifacts_manifest = []
        try:
            execution_row_from_sm = execution_repo.summary(plan_id)
        except Exception:
            execution_row_from_sm = None
        # The workflow's answer about this plan — read on the same handle
        # as the execution row, so the status shown below can be
        # reconciled against it.
        try:
            routing_row = routing_repo.current(plan_id)
            if isinstance(routing_row, dict):
                routing_phase = str(
                    routing_row.get("current_phase") or ""
                ).strip() or None
        except Exception:
            routing_phase = None

        # 2026-11-04: the verification block is owned by
        # ``plan_verification`` — the table ``/api/verification/{id}/
        # status`` reads — not by the ``plan_routing.verification``
        # mirror column.  The two had drifted apart on live plans, so
        # the summary reported a verdict the authoritative table
        # contradicted.  The keys do not change, so
        # the consumers (feishu_notifier / cards) need no change; they
        # just start reading the authoritative value.
        #
        # Without a state machine (``sm is None``) there is nothing to
        # read the authoritative value from, so the plan state's own
        # value stands — no key added, none dropped.
        state["verification"] = _authoritative_verification_block(
            plan_id=plan_id,
            routing_repo=routing_repo,
            verification_repo=_verification_repo,
            state=state,
        )

        # When the state-machine has a row for this plan, it is
        # authoritative for ``exec_status`` / ``pid`` / ``project_dir``
        # etc. — replacing the legacy direct read of execution.json.
        if isinstance(execution_row_from_sm, dict):
            if execution_row_from_sm.get("exec_status") and not s:
                exec_status = execution_row_from_sm["exec_status"]
            if execution_row_from_sm.get("exec_pid") and not pid:
                pid = execution_row_from_sm["exec_pid"]
            if (
                execution_row_from_sm.get("project_dir")
                and project_dir is None
            ):
                project_dir = Path(execution_row_from_sm["project_dir"])
            # 2026-09-13: fill started_at / stop_reason from the row too
            # when there is no in-memory record (post-restart readback).
            # Without this, /summary reported None for both on every plan
            # whose execution was not live in memory — the plan card then
            # showed blank status fields after an backend server restart.
            if not s:
                if started_at is None:
                    started_at = execution_row_from_sm.get("started_at")
                if stop_reason is None:
                    stop_reason = execution_row_from_sm.get("stop_reason")

    # The status shown is reconciled against the WORKFLOW's answer before
    # it leaves this handler: the execution row records the LAST run, and
    # a plan that finished keeps the verdict of a run that was interrupted
    # long before it finished.  Showing that verdict is how a completed
    # plan reads as "failed" here.  See
    # ``plan_status.normalize_exec_status``.
    exec_status = normalize_exec_status(routing_phase, exec_status)

    return {
        "plan_id": plan_id,
        "state": state,
        "review_rounds": state.get("review_rounds", {"prd": 0, "arch": 0, "test": 0}),
        "docs": docs,
        "tasks": task_counts,
        "execution": {
            "status": exec_status,
            "project_dir": str(project_dir) if project_dir else None,
            "started_at": started_at,
            "ended_at": ended_at,
            "pid": pid,
            "stop_reason": stop_reason,
            "sync_targets": sync_targets,
        },
        "artifacts": artifacts_manifest,
        # The decisions that are still waiting for a human.  The same
        # block the two progress projections carry, so a consumer that
        # polls any of the three reads the same shape.  Always present
        # — an empty block means "nothing is outstanding", which is an
        # answer, not a missing key.
        "pending_decisions": _pending_decisions_block(plan_id),
        # Compact per-plan LLM usage (2026-09-21). Cached report only —
        # /summary is polled frequently, and re-aggregating would take a
        # CC Switch DB snapshot on every poll. Full report:
        # GET /api/plan/{plan_id}/usage
        "usage": _server._usage_summary_block(plan_id),
    }


@router.get("/api/plan/{plan_id}/usage")
def get_plan_usage(plan_id: str, refresh: bool = False):
    """Per-plan LLM usage, aggregated from CC Switch's own ledger.

    Answers "what did this plan actually cost" — new input / output /
    cache-read / cache-creation tokens plus the billed USD, broken down
    by ledger source, provider, model, scene, task and day, with a
    cross-check against the backend's own first-hand accounting.

    Query params
    ------------
    ``refresh=true``
        Re-aggregate before answering. Otherwise the persisted
        ``plans/<id>/usage_report.json`` is returned when present, and
        the report is computed on demand when it is not (so the endpoint
        is useful before the first execution finishes).
    """
    if not _server._plan_dir(plan_id).exists():
        raise HTTPException(404, "Plan not found")
    if refresh:
        _server._refresh_usage_report(plan_id, block=True)
    report = _server._read_cached_usage_report(plan_id)
    if report is None:
        try:
            report = _server.plan_usage.build_usage_report(plan_id)
        except Exception as exc:
            _server.logger.warning("usage_report_build_failed plan=%s", plan_id,
                           exc_info=True)
            raise HTTPException(
                503, f"usage report unavailable for {plan_id}: {exc}"
            )
    return report


@router.get("/api/plan/{plan_id}/tasks")
def get_plan_tasks(plan_id: str):
    """Return a summary of the plan's tasks with lifecycle counts."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    tasks_file = plan_dir / "tasks.json"
    tasks: list = []
    if tasks_file.exists():
        data = json.loads(tasks_file.read_text())
        if isinstance(data, dict):
            tasks = data.get("tasks", [])
        elif isinstance(data, list):
            tasks = data

    # SQLite runtime overlay so per-task ``status`` reflects what the
    # executor wrote, not the static tasks.json.  See the matching
    # overlay above in get_plan_summary for the rationale.
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        runtime_conn = open_db(_server._state_db_path())
        try:
            migrate(runtime_conn)
            runtime = PlanTaskRepository(runtime_conn).load_all(plan_id)
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                rt = runtime.get(t.get("id"))
                if not rt:
                    continue
                if "status" in rt:
                    t["status"] = rt["status"]
        finally:
            runtime_conn.close()
    except (sqlite3.OperationalError, OSError):
        pass

    summary = {
        "total": len(tasks),
        "completed": 0,
        "failed": 0,
        "pending": 0,
        "in_progress": 0,
    }
    for task in tasks:
        if isinstance(task, dict):
            status = task.get("status")
            if status in summary:
                summary[status] += 1

    return {"plan_id": plan_id, "tasks": summary}


# ---------------------------------------------------------------------------
# 框架修复条目：受守卫的删除（dismiss）
# ---------------------------------------------------------------------------
#
# 调度页的框架修复分区要给出一个「删除 / 忽略」动作。这里是它的受守卫
# 形态：**还有修复计划在跑、或者修复 PR 还占着某个仓库的合并槽的条目删
# 不了** —— 删掉它等于把还在飞的工作藏起来，而所有等着这个修复的计划会
# 继续挂着，且没有人再看着它们。所以「拒绝」是默认，「删除」是例外。
#
# 删除要落两处，两处都不能少：
#
# * 计划的 ``framework_fix`` 块 —— 条目住在那里。块里保留读模型需要的
#   事实（``fingerprint``、修复计划号），多出删除记录，并**释放挂起**：
#   这个计划不再等这个缺陷了。
# * 只追加的审计链 —— 一条 ``framework_fix_dismissed`` 事件，带着原因。
#   审计链活得比写下它的那次进程久，所以「为什么删的」在块没了之后仍然
#   答得出来。

#: 删除在审计链上留下的事件类型。
FRAMEWORK_FIX_DISMISSED_EVENT = "framework_fix_dismissed"

#: 拒绝删除时的错误码。
FRAMEWORK_FIX_ACTIVE_ERROR = "framework_fix_active"

#: 没带原因时的错误码 —— 没人能解释的删除不是删除。
FRAMEWORK_FIX_MISSING_REASON_ERROR = "missing_reason"

#: 计划不存在时的错误码。
FRAMEWORK_FIX_PLAN_NOT_FOUND_ERROR = "plan_not_found"

#: 状态库没跑框架修复迁移时的错误码 —— 没人能说出正在修什么，那就谁都
#: 不许删。
FRAMEWORK_FIX_UNAVAILABLE_ERROR = "framework_fix_registry_unavailable"


def _optional_text(value: Any) -> Optional[str]:
    """A non-empty string, or ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _framework_fix_registry() -> Optional[Any]:
    """The framework-fix registry, or ``None`` when there is none to talk to.

    The registry refuses to create its own schema, so a state database
    that never ran the framework-fix migration has no registry at all.
    The endpoint says so (503) instead of pretending the lane is
    empty: "there is nothing to dismiss" and "we cannot tell what is
    being worked on" are two different answers, and only one of them is
    safe to act on.
    """
    from framework_fix.registry import FrameworkFixRegistry

    try:
        return FrameworkFixRegistry()
    except Exception:  # noqa: BLE001
        # SchemaNotMigratedError (the migration never ran) or a database
        # that cannot be opened — both mean "there is no registry to
        # ask", and both are the caller's 503.
        return None


def _routing_row_exists(plan_id: str) -> bool:
    """Does this plan have a ``plan_routing`` row?

    A read and nothing else — no migration, no row creation.  The
    answer decides whether the dismissal may be written into the plan's
    framework-fix block: the block lives in that row, and writing one
    for a plan that has no row would plant a row nobody asked for.
    """
    from state_machine.db.connection import open as open_db

    try:
        conn = open_db(_server._state_db_path())
    except Exception:  # noqa: BLE001
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM plan_routing WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def _entry_fingerprint(
    registry: Any, plan_id: str, block: Dict[str, Any]
) -> Optional[str]:
    """Which defect is this plan's framework-fix entry about?

    Two ways to name a defect.  A held business plan names it in its
    block (``fingerprint``).  A repair plan does not hold anything — it
    *is* the repair — so its entry is the defect the registry says it
    is repairing (the registry row carries ``fix_plan_id``).  The block
    is asked first and the registry second: a block can be missing for
    reasons that have nothing to do with what kind of plan this is.
    """
    named = _optional_text(block.get("fingerprint"))
    if named:
        return named
    try:
        rows = registry.list_registry()
    except (sqlite3.Error, ValueError):
        return None
    for row in rows:
        if str(row.get("fix_plan_id") or "") != plan_id:
            continue
        candidate = _optional_text(row.get("fingerprint"))
        if candidate:
            return candidate
    return None


def _defect_is_open(registry: Any, fingerprint: Optional[str]) -> bool:
    """Is somebody still repairing this defect?

    ``open`` (and its synonym ``active``) means the repair is in
    flight; ``reopened`` means the defect came back and is being
    repaired again.  Both are work that is in flight.  A status word
    outside the closed set is **not** read as "not open": it means the
    data is broken, and broken data is not evidence that the repair
    finished — so the dismissal is refused and an operator can look at
    it.
    """
    if not fingerprint:
        return False
    from framework_fix.registry import (
        REGISTRY_STATUS_OPEN,
        REGISTRY_STATUS_REOPENED,
        normalize_status,
    )

    try:
        row = registry.lookup(fingerprint)
    except (sqlite3.Error, ValueError):
        return True
    if row is None:
        return False
    try:
        status = normalize_status(row.get("status"))
    except ValueError:
        return True
    return status in (REGISTRY_STATUS_OPEN, REGISTRY_STATUS_REOPENED)


def _merge_lease_holders(plan_id: str, fix_plan_id: Optional[str]) -> List[str]:
    """Which of these plans still hold a repository's merge slot?

    A lease row means a repair PR for that repository is open and has
    not been merged yet — the repair is still in flight, so the entry
    must not be dismissed.  The lease is held by the **repair** plan:
    for a repair plan that is the plan itself, for a held business plan
    it is the plan it waits on.
    """
    from state_machine.db.connection import open as open_db

    try:
        conn = open_db(_server._state_db_path())
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = conn.execute(
            "SELECT fix_plan_id FROM framework_fix_merge_lease "
            "WHERE fix_plan_id = ? OR fix_plan_id = ?",
            (plan_id, fix_plan_id or ""),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def _dismissal_recorded(plan_id: str, fingerprint: Optional[str]) -> bool:
    """Has this plan's entry already been dismissed?

    Two records of the same fact: the plan's block (where the entry
    lives) and the append-only audit chain (where the dismissal was
    written down).  A plan without a routing row has no block, so the
    chain is the only place that can answer — and the chain is exactly
    the place that must answer, because the chain is what outlives the
    process that dismissed the entry.
    """
    from state_machine.db.connection import open as open_db

    try:
        conn = open_db(_server._state_db_path())
    except Exception:  # noqa: BLE001
        return False
    try:
        rows = conn.execute(
            "SELECT payload_json FROM framework_fix_event "
            "WHERE event_type = ? AND (fingerprint = ? OR fingerprint IS NULL)",
            (FRAMEWORK_FIX_DISMISSED_EVENT, fingerprint or ""),
        ).fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    for row in rows:
        try:
            payload = json.loads(row[0]) if row[0] else {}
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("plan_id") == plan_id:
            return True
    return False


def _attached_plan_ids(registry: Any, fingerprint: Optional[str]) -> List[str]:
    """The plans that are waiting on this defect's repair.

    They are recorded in the dismissal event, not because the dismissal
    is theirs to make, but because "who was still waiting when this was
    dismissed" is a fact the audit chain should hold.
    """
    if not fingerprint:
        return []
    try:
        rows = registry.list_attach(fingerprint)
    except (sqlite3.Error, ValueError):
        return []
    seen: List[str] = []
    for row in rows:
        plan_id = _optional_text(row.get("plan_id"))
        if plan_id and plan_id not in seen:
            seen.append(plan_id)
    return sorted(seen)


def _record_dismissal(
    plan_id: str,
    block: Dict[str, Any],
    reason: str,
    fingerprint: Optional[str],
    fix_plan_id: Optional[str],
    registry: Any,
) -> None:
    """Write the dismissal down.

    The block goes through the production writer
    (``plan_state._write_routing_framework_fix``) so the column is
    encoded the way every other writer encodes it — and it is written
    *additively*: the block keeps the facts the read model needs, and
    only the four keys this dismissal is about change.  The writer is
    a private helper on purpose: ``PlanState``'s public block API can
    enter a block and clear one, but it cannot record "this entry was
    dismissed, and here is why".

    A plan with no routing row gets no block: writing one would plant
    a row nobody asked for — the audit chain is the record in that
    case.
    """
    from plan_state import _write_routing_framework_fix

    if _routing_row_exists(plan_id):
        updated = dict(block)
        updated["dismissed"] = True
        updated["dismiss_reason"] = reason
        updated["dismissed_at"] = datetime.now(timezone.utc).isoformat()
        # The hold is released: the plan is not waiting on this defect
        # any more.  Everything else the block says stays as it is.
        updated["hold"] = False
        _write_routing_framework_fix(plan_id, updated)

    registry.append_event(
        FRAMEWORK_FIX_DISMISSED_EVENT,
        {
            "plan_id": plan_id,
            "reason": reason,
            "fingerprint": fingerprint,
            "fix_plan_id": fix_plan_id,
            "attached_plans": _attached_plan_ids(registry, fingerprint),
        },
        fingerprint=fingerprint,
        fix_plan_id=fix_plan_id,
    )


@router.post("/api/plan/{plan_id}/framework-fix/dismiss")
def dismiss_framework_fix_entry(
    plan_id: str,
    payload: Optional[Dict[str, Any]] = Body(default=None),
) -> Any:
    """Dismiss this plan's framework-fix entry — guarded.

    Answers
    -------
    * **200** ``{"dismissed": true}`` — the entry is dismissed: the
      plan's framework-fix block records it and releases the hold, and
      the audit chain carries one ``framework_fix_dismissed`` event
      with the reason.  Dismissing twice is the same as dismissing
      once — the second call answers 200 and writes nothing.
    * **400** ``{"error": "missing_reason"}`` — no reason.  A
      dismissal nobody can explain is not a dismissal.
    * **404** ``{"error": "plan_not_found"}`` — no such plan.
    * **409** ``{"error": "framework_fix_active", "fingerprint": ...}``
      — the defect is still being repaired, or a repair PR still holds
      the repository's merge slot.  Nothing is dismissed: the entry
      stays exactly as it was, and the plans waiting on that repair
      stay blocked with somebody still looking after them.
    * **503** ``{"error": "framework_fix_registry_unavailable"}`` — the
      state database has not run the framework-fix migration, so nobody
      can tell what is being worked on.
    """
    reason = payload.get("reason") if isinstance(payload, dict) else None
    reason = _optional_text(reason)
    if reason is None:
        return JSONResponse(
            status_code=400,
            content={"error": FRAMEWORK_FIX_MISSING_REASON_ERROR},
        )

    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists() and not _routing_row_exists(plan_id):
        return JSONResponse(
            status_code=404,
            content={"error": FRAMEWORK_FIX_PLAN_NOT_FOUND_ERROR},
        )

    registry = _framework_fix_registry()
    if registry is None:
        return JSONResponse(
            status_code=503,
            content={"error": FRAMEWORK_FIX_UNAVAILABLE_ERROR},
        )

    from plan_state import PlanState

    plan_state = PlanState(plan_dir)
    kind = plan_state.get_plan_kind()
    block = plan_state.get_framework_fix()

    # The repair plan that owns the entry: a repair plan owns itself, a
    # held business plan owns the plan that is repairing its defect.
    fix_plan_id = (
        plan_id if kind == "framework_fix"
        else (_optional_text(block.get("fix_plan_id")) or plan_id)
    )

    fingerprint = _entry_fingerprint(registry, plan_id, block)

    # Already dismissed → the same answer, and no second event.
    if block.get("dismissed") is True or _dismissal_recorded(
        plan_id, fingerprint
    ):
        return {"dismissed": True}

    # Guard one: the defect is still being repaired.
    if _defect_is_open(registry, fingerprint):
        return JSONResponse(
            status_code=409,
            content={
                "error": FRAMEWORK_FIX_ACTIVE_ERROR,
                "fingerprint": fingerprint,
            },
        )

    # Guard two: a repair PR still holds the repository's merge slot.
    holders = _merge_lease_holders(plan_id, fix_plan_id)
    if holders:
        return JSONResponse(
            status_code=409,
            content={
                "error": FRAMEWORK_FIX_ACTIVE_ERROR,
                "fingerprint": fingerprint,
            },
        )

    _record_dismissal(
        plan_id, block, reason, fingerprint, fix_plan_id, registry
    )

    return {"dismissed": True}

