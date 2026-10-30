"""Verification API: start / reset / status / progress / repair / stop.

Extracted from ``server.py`` on 2026-09-25. The background machinery this
surface drives (watchdog ticks, the auto-repair loop, terminal persistence)
lives in ``verification_loop.py``. See ``routes/phases.py`` for the
late-binding rule that governs every module in this package.
"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Dict, List, Optional, Tuple, Any
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path, PureWindowsPath
from datetime import datetime
import json
import os
import signal
import sqlite3
import threading

# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server

# 2026-10-23: 读侧与写侧共用同一条「verdicts → 索引桶」规则。
# ``verification_buckets`` 是那条规则的**唯一**实现：写侧
# ``VerificationExecutor._backfill_index_lists_from_verdicts`` 与读侧
# ``_derive_vp_buckets`` 都从它取，两边不可能再漂移。它是叶子模块
# （只依赖 typing），所以这里可以安全地模块级导入。
from verification_buckets import (
    BUCKET_COMPLETED,
    BUCKET_FAILED,
    BUCKET_SKIPPED,
    RETIRED_OBSOLETE,
    RETIRED_SUPERSEDED_BY,
    derive_index_buckets,
)

# ``plan_artifacts`` 是叶子模块（只依赖 pathlib / typing），所以这里可以安全地
# 模块级导入 —— 判定「这条 plan 走哪条分支」只有一个入口，见
# :func:`plan_artifacts.detect`。
from plan_artifacts import BRANCH_CONFLICT, detect

router = APIRouter()


class ArchivedPlanError(Exception):
    """Raised when a verification mutation targets an archived plan."""


def _normalize_verification_status(raw_status: str) -> str:
    """Map orchestrator internal values to the public status set."""
    if not raw_status:
        return "not_started"
    return _server._VERIFICATION_STATUS_NORMALIZATION.get(raw_status, raw_status)


def _open_verification_state():
    """Open the SQLite state-machine connection and verification repos."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import VerificationRepository

    conn = open_db(_server._state_db_path())
    migrate(conn)
    return conn, RoutingRepository(conn), VerificationRepository(conn)


def _archived_plan_response(plan_dir: Path):
    """Return a ``410 {"error": "archived"}`` response, or ``None``.

    VP-020 contract (``tests/integration/api/test_archived_plan_write_returns_410.py``):
    a plan whose directory name sorts at or before the archive cutoff
    is READ-ONLY. Every write endpoint that targets it must answer 410
    and leave all four ``plan_*`` tables untouched.

    Returning the response instead of raising lets each handler place
    the guard at the point where it knows its own 404 has already
    passed — and keeps the function usable from handlers that return
    ``JSONResponse`` directly rather than raising ``HTTPException``.

    2026-09-14: the guard existed only on the two *start* endpoints.
    ``/execution/{id}/stop``, ``/verification/{id}/stop``,
    ``/plan/{id}/state`` and ``/plan/{id}/phase`` all mutated archived
    plans, so the parametrised VP-020 matrix had 5 red cases.
    """
    from state_machine.db.archive_scan import CUTOFF_2026_08_05, classify_plan

    if classify_plan(Path(plan_dir), CUTOFF_2026_08_05) == "archived":
        return JSONResponse(status_code=410, content={"error": "archived"})
    return None


def _ensure_verification_plan(plan_id: str) -> None:
    """Reject archived plans before any SQLite mutation."""
    from state_machine.db.archive_scan import CUTOFF_2026_08_05, classify_plan

    plan_dir = _server._plan_dir(plan_id)
    if classify_plan(plan_dir, CUTOFF_2026_08_05) == "archived":
        raise ArchivedPlanError(plan_id)


def _conflict_reason(exc: Exception) -> str:
    """Map repository conflict details to the public API reason."""
    return "stage_mismatch" if "predicate mismatch" in str(exc) else "version_mismatch"


def _read_verification_checkpoint(plan_dir: Path) -> Optional[dict]:
    """The plan's pause checkpoint, when it is a *verification* one.

    ``pause_state`` owns the file's shape; this reader only answers the
    question the start endpoint asks: "is there an interrupted
    verification round that this start should resume?".

    A checkpoint of another kind — an interrupted *execution* — is not
    ours. It must survive untouched, or the execution resume would lose
    the trace it was written for.
    """
    try:
        from pause_state import read_pause_checkpoint

        checkpoint = read_pause_checkpoint(plan_dir)
    except Exception:  # noqa: BLE001 — an unreadable checkpoint is "no checkpoint"
        return None
    if not isinstance(checkpoint, dict):
        return None
    if checkpoint.get("kind") != "verification":
        return None
    return checkpoint


def _resolve_start_round(
    current_row: Optional[dict], checkpoint: Optional[dict],
) -> Tuple[int, bool]:
    """The round this start runs, and whether it resumes an interrupted one.

    An interrupted round must be **resumed, not replaced**: the round
    number is the contract a plan's verdicts, report and repair tasks
    are written against, and starting "round N+1" for work that was
    already half-done in round N loses that work and mislabels it.

    Two independent traces say "there is an interrupted round":

    * a ``kind="verification"`` checkpoint — the operator pressed stop,
      and the stop wrote the round, the verdicts and the VPs it cut
      short (``_write_verification_checkpoint``);
    * a ``plan_verification`` row whose status is still ``running`` or
      ``loop_stopped`` with a round > 0 — the process was killed before
      it could write anything, so the row is all that is left.

    Either one is enough. When neither is there the plan's last round
    really did finish, and the next round is the next one.

    Returns ``(next_round, resumed)``.
    """
    row = current_row or {}
    current_round = int(row.get("round") or 0)
    status = str(row.get("verification_status") or "").lower()
    checkpoint_round = int((checkpoint or {}).get("verification_round") or 0)

    interrupted = checkpoint is not None or (
        status in ("running", "loop_stopped") and current_round > 0
    )
    if not interrupted:
        return current_round + 1, False

    # The DB row is the authority on which round was in flight; the
    # checkpoint is the fallback for the case where the row was never
    # written (or was written with round 0).
    resume_round = current_round or checkpoint_round
    if resume_round <= 0:
        # Nothing identifies the interrupted round — there is no round
        # to resume, so this is an ordinary fresh start.
        return current_round + 1, False
    return resume_round, True


def _rebuild_round_report(
    plan_id: str, plan_dir: Path, round_n: int, verification_repo,
) -> None:
    """Rebuild ``verification_report.json`` from the DB verdicts.

    A round that was killed never closed, so it never wrote a report —
    the file the repair chain reads (``verification_results``) simply
    was not there, and the plan looked as if it had never been
    verified. The verdicts themselves did survive: the executor appends
    them to the ``plan_verification`` row as it goes, one VP at a time.

    So the report is rebuilt from that row. The write merges into
    whatever the report already holds (see ``_persist_round_snapshot``),
    which makes this safe to run on a plan whose report is already
    there.
    """
    try:
        row = verification_repo.current(plan_id) or {}
    except Exception:  # noqa: BLE001 — a report we cannot rebuild is not fatal
        _server.logger.warning(
            "[verification_start] could not read the round's verdicts "
            "plan=%s — the report is left as it is", plan_id,
        )
        return
    verdicts = [
        verdict for verdict in (row.get("verdicts") or [])
        if isinstance(verdict, dict)
    ]
    if not verdicts:
        return
    try:
        _persist_round_snapshot(plan_id, plan_dir, round_n, verdicts)
    except Exception:  # noqa: BLE001 — a report we cannot write is not fatal
        _server.logger.warning(
            "[verification_start] could not rebuild the round report "
            "plan=%s round=%s", plan_id, round_n,
        )


def _consume_verification_checkpoint(plan_dir: Path) -> None:
    """Retire the verification checkpoint this start has honoured.

    Only a ``kind="verification"`` checkpoint is consumed, and only
    after it has been read again: a checkpoint of another kind (an
    interrupted execution) belongs to a different resume and must not
    be deleted by a verification start.
    """
    try:
        from pause_state import consume_pause_checkpoint, read_pause_checkpoint

        checkpoint = read_pause_checkpoint(plan_dir)
        if not isinstance(checkpoint, dict):
            return
        if checkpoint.get("kind") != "verification":
            return
        consume_pause_checkpoint(plan_dir)
    except Exception:  # noqa: BLE001 — a checkpoint we cannot retire is not fatal
        _server.logger.warning(
            "[verification_start] could not consume the verification "
            "checkpoint — it stays on disk and may be consumed again",
        )


def _verification_status_from_db(plan_id: str) -> dict:
    """Build status solely from the routing and verification tables."""
    conn, routing, verification = _server._open_verification_state()
    try:
        # Repair any drift between ``results.recorded_by`` and
        # ``verification_status`` BEFORE we read the row. This is a
        # read-through auto-heal — callers see consistent data even if
        # a prior ``_persist_verification_terminal`` step 1 ran with
        # a partial write (audit 2026-09-09 hit this on
        # ``2026-09-04 plan``).
        try:
            previous = verification.repair_stale_terminal_state(plan_id)
            if previous is not None:
                # repair ran; log so the operator sees a self-heal event
                import logging
                logging.getLogger(__name__).info(
                    "[verification_status_read] auto-repair plan=%s "
                    "previous_status=%s",
                    plan_id, previous,
                )
        except Exception as exc:
            # Repair is best-effort; if it fails the read proceeds
            # with whatever was in the row.
            import logging
            logging.getLogger(__name__).warning(
                "[verification_status_read] repair failed plan=%s: %s",
                plan_id, exc,
            )
        route = routing.current(plan_id)
        record = verification.current(plan_id)
    finally:
        conn.close()

    if route is None:
        raise HTTPException(404, {"error": "Plan not found", "detail": "The requested plan does not exist."})

    plan_dir = _server._plan_dir(plan_id)
    verification_points = []
    plan_file = plan_dir / "verification_plan.json"
    if plan_file.exists():
        try:
            verification_points = json.loads(plan_file.read_text(encoding="utf-8")).get("verification_points", [])
        except (OSError, json.JSONDecodeError):
            verification_points = []

    record = record or {
        "verification_status": "not_started", "round": 0,
        "max_rounds": _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
        "results": None, "started_at": None, "updated_at": None,
        "verification_stop_reason": None,
    }
    status = _normalize_verification_status(record.get("verification_status", "not_started"))
    if route["current_phase"] == "verification_running":
        status = "running"

    # ``execution_profile`` (budget projection: group list, per-method
    # timeouts, parallelism cap, current group index). The hardcoded
    # ``{}`` this replaces was a casualty of the SQLite-first rewrite —
    # the field is part of the documented /status contract but has no
    # column of its own, so it silently flattened to empty for every
    # plan and the budget consumers lost their data.
    #
    # It is a LIVE payload by nature: ``current_group_index`` points at
    # the group executing right now, which cannot be reconstructed from
    # the round row after the fact. So read the running round's copy
    # from the in-memory state first (published by the orchestrator as
    # it progresses, server.py:6168), then the durable copy in the
    # verification report, then ``{}`` — the field is optional for
    # clients that don't render a budget card.
    execution_profile: dict = {}
    _live_profile = (_server._verification_state.get(plan_id) or {}).get("execution_profile")
    if isinstance(_live_profile, dict) and _live_profile:
        execution_profile = _live_profile
    else:
        _report_file = plan_dir / "verification_report.json"
        if _report_file.exists():
            try:
                execution_profile = (
                    json.loads(_report_file.read_text(encoding="utf-8"))
                    .get("execution_profile")
                ) or {}
            except (OSError, json.JSONDecodeError, AttributeError):
                execution_profile = {}

    return {
        "plan_id": plan_id,
        "current_phase": route["current_phase"],
        "verification_status": status,
        "verification_round": record.get("round", 0),
        "verification_max_rounds": record.get(
            "max_rounds", _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
        ),
        "results": record.get("results") or {"pytest_summary": "", "llm_findings": "", "performance_metrics": {}},
        "repair_tasks": [],
        "execution_profile": execution_profile,
        "verification_points": verification_points,
        "started_at": record.get("started_at"),
        "updated_at": _status_updated_at(plan_dir, record),
        "stop_reason": record.get("verification_stop_reason"),
    }


def _status_updated_at(plan_dir: Path, record: dict) -> Optional[str]:
    """Return the ``updated_at`` for a /status response.

    A plan that has never run a verification round has no
    ``plan_verification`` row, so there is no verification timestamp to
    report. Falling back to the workflow's own ``last_updated`` keeps the
    field non-null for those plans — the documented contract for
    ``/api/verification/{id}/status`` (``test_status_not_started_schema``:
    "falls back to plan_state last_updated"), which was lost when the
    endpoint moved to a SQLite-only read.

    Returns ``None`` only when neither source has a timestamp.
    """
    updated_at = record.get("updated_at")
    if updated_at is not None:
        return updated_at
    try:
        from plan_state import PlanState

        return _server.PlanState(plan_dir).get_state().get("last_updated")
    except Exception:  # noqa: BLE001 — a missing state must not 500 the read
        return None


def _get_verification_status(plan_id: str) -> dict:
    """Build verification status response from in-memory state or plan_state fallback."""
    state = _server._verification_state.get(plan_id)
    plan_dir = _server._plan_dir(plan_id)
    verification_points = []
    if plan_dir.exists():
        plan_file = plan_dir / "verification_plan.json"
        if plan_file.exists():
            try:
                with open(plan_file, "r", encoding="utf-8") as f:
                    plan_data = json.load(f)
                    verification_points = plan_data.get("verification_points", [])
            except Exception:
                pass
    if state:
        return {
            "plan_id": plan_id,
            "verification_status": _normalize_verification_status(state.get("verification_status", "not_started")),
            "verification_round": state.get("verification_round", 0),
            "verification_max_rounds": state.get(
                "verification_max_rounds", _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
            ),
            "results": state.get("results", {"pytest_summary": "", "llm_findings": "", "performance_metrics": {}}),
            "repair_tasks": state.get("repair_tasks", []),
            "execution_profile": state.get("execution_profile", {}),
            "verification_points": verification_points,
            "started_at": state.get("started_at"),
            "updated_at": state.get("updated_at"),
        }

    # Fallback to plan_state
    if plan_dir.exists():
        ps = _server.PlanState(plan_dir)
        v = ps.get_state().get("verification", {})
        return {
            "plan_id": plan_id,
            "verification_status": _normalize_verification_status(v.get("status", "not_started")),
            "verification_round": v.get("round", 0),
            "verification_max_rounds": v.get(
                "max_rounds", _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
            ),
            "results": {"pytest_summary": "", "llm_findings": "", "performance_metrics": {}},
            "repair_tasks": [],
            "execution_profile": {},
            "verification_points": verification_points,
            "started_at": None,
            "updated_at": ps.get_state().get("last_updated"),
        }

    return {
        "plan_id": plan_id,
        "verification_status": "not_started",
        "verification_round": 0,
        "verification_max_rounds": _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
        "results": {"pytest_summary": "", "llm_findings": "", "performance_metrics": {}},
        "repair_tasks": [],
        "execution_profile": {},
        "verification_points": [],
        "started_at": None,
        "updated_at": None,
    }


def _live_non_repair_execution(plan_id: str) -> bool:
    """True when a NON-repair executor is running this plan right now.

    The repair executor and the main executor share
    ``_execution_state``; only the repair one stamps
    ``_source="repair_execution"``. A live MAIN execution owns the
    plan's task queue — the pending repair tasks are its work, not a
    repair round's — so a start must not spawn a second executor over
    the same plan.

    A plan whose in-memory record is empty (the backend restarted) is
    NOT a live main execution: the process that owned it is gone, and
    that is exactly the case the repair resume exists for.
    """
    try:
        state = _server._execution_state.get(plan_id) or {}
    except Exception:  # noqa: BLE001 — an unreadable record is not a live one
        return False
    if state.get("_source") == "repair_execution":
        return False
    return state.get("status") == "running"


def _sigterm_repair_executor(plan_id: str) -> Optional[int]:
    """SIGTERM the repair executor subprocess; return the pid signalled.

    2026-10-22: the repair executor is a SEPARATE subprocess
    (``cli.py --recover``) and ``stop_verification`` used to leave it
    running — the stop killed the VERIFICATION sub-agents (the
    ``_running_vp_ids`` registry), never the repair executor, so an
    operator's stop did not stop the repair.

    The pid comes from ``plan_execution.exec_pid``: it is the DURABLE
    record (the in-memory ``_execution_state`` entry is gone after a
    backend restart, and the pid is exactly what has to survive that).
    The kill is the plain ``os.kill(pid, SIGTERM)`` the rest of the
    codebase uses; the executor's own watcher thread notices the exit.

    Returns the pid that was signalled, or ``None`` when there was
    nothing to signal. Never raises — a stop must not fail on the kill.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    pid = None
    try:
        conn = open_db(_server._state_db_path())
        try:
            migrate(conn)
            row = ExecutionRepository(conn).summary(plan_id) or {}
            pid = row.get("exec_pid")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a stop must not fail on a read
        _server.logger.exception(
            "[stop_verification] could not read the repair executor pid "
            "plan=%s", plan_id,
        )
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return None
    except Exception:  # noqa: BLE001
        _server.logger.exception(
            "[stop_verification] could not SIGTERM the repair executor "
            "plan=%s pid=%s", plan_id, pid,
        )
        return None
    return pid


def _repair_resume_response(plan_id, plan_dir, conn, req):
    """Resume the REPAIR half of the loop instead of starting a round.

    2026-10-22. The repair executor is a stateless scanner: it
    re-reads ``tasks.json`` + ``state.db`` and runs whatever is still
    pending. So "continue the repair" is "spawn it once more" — there
    is nothing to attach to.

    Two shapes, decided by the routing row's phase:

    * ``verification_repairing`` — the round finished and its repair
      tasks are waiting to be confirmed. The start routes the plan
      into ``executing`` (round+1) and spawns the executor.
    * ``executing`` with pending repair tasks — the executor was
      already spawned (or died mid-flight) and the phase is already
      ``executing``. The round was already counted, so this start only
      spawns the executor again; bumping the round a second time would
      count the same repair twice.

    Returns the response body, or ``None`` when this start is an
    ordinary verification start (the caller then runs the normal
    path).
    """
    from plan_state import normalize_phase as _normalize_phase
    from state_machine.repositories.routing_repository import RoutingRepository

    try:
        row = RoutingRepository(conn).current(plan_id)
    except Exception:  # noqa: BLE001 — no routing row means no repair shape
        row = None
    phase = _normalize_phase((row or {}).get("current_phase"))
    if phase not in ("verification_repairing", "executing"):
        return None

    tool = req.tool if req else None
    bump_round = True

    if phase == "executing":
        # Branch B — a pure resume. The pending-repair-task probe is
        # the condition: without queued repair work there is nothing
        # to resume, and this start is an ordinary verification start.
        pending = _server._get_pending_repair_tasks(plan_id)
        if not pending:
            return None
        if _live_non_repair_execution(plan_id):
            # A main executor owns this plan right now — its queue
            # already holds the repair tasks.
            return None
        # The phase is already ``executing`` and the round was already
        # counted; ``start_repair_execution`` would add a second round.
        bump_round = False

    project_dir = _server._get_project_dir(plan_id)
    if not project_dir:
        raise HTTPException(
            400,
            {
                "error": "Missing project directory",
                "detail": "No project directory configured for this plan.",
            },
        )

    # 2026-09-15: a fresh start clears any stop flag left by a
    # previous ``/stop`` — otherwise the new executor would treat
    # every task as "cancelled".
    try:
        import verification_cancel
        verification_cancel.clear(plan_id)
    except Exception:  # noqa: BLE001
        pass

    ps = _server.PlanState(plan_dir)
    ps.reload()
    if bump_round:
        try:
            ps.start_repair_execution()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=409,
                content={
                    "error": "conflict",
                    "reason": "repair_resume_failed",
                    "detail": str(exc),
                },
            )
    round_n = ps.get_verification_round()

    _server._run_repair_execution_async(
        plan_id, project_dir, tool=tool,
        on_complete=_server._make_repair_on_complete(
            plan_id, project_dir, tool,
        ),
    )
    return {
        "status": "started",
        "mode": "repair_resume",
        "round": round_n,
    }


def _verification_review_ready(plan_dir: Path) -> bool:
    """这个 plan 目录是否已经就绪，可以从「计划书待审」进入验证。

    2026-10-25。``/start`` 的唯一放行判定是 ``try_mark_phase`` 的**源阶段
    集合**，而阶段词汇表变大不会让新阶段自动被接纳——所以「验证分支已就绪」
    这个事实必须由 :func:`verification_gate.verification_branch_ready` 回答，
    答案只以「多一个源阶段」的形式进入 CAS。

    谓词异常一律折叠为假：读不懂的计划与没准备好的计划，对调用方是同一件事
    ——**还没到能跑的时候**。折叠之后仍然走既有的 409 ``stage_mismatch``：
    不新增错误码，也不产生 500。

    Args:
        plan_dir: plan 目录。

    Returns:
        就绪为 ``True``；不就绪（含异常）为 ``False``。本函数**不抛异常**。
    """
    try:
        from verification_gate import verification_branch_ready

        return bool(verification_branch_ready(plan_dir))
    except Exception:  # noqa: BLE001 - 读不懂 = 还没到能跑的时候
        return False


# ---------------------------------------------------------------------------
# 验证分支的前奏（2026-10-25）
# ---------------------------------------------------------------------------
#
# ``/start`` 原来只做两件事：CAS（阶段放行）与启动验证循环。验证分支在这两件
# 事之前还缺一整条管线：
#
# * 机器计划得与审核过的计划书**逐条相等** —— 审过的与实际被验的必须是同一批
#   验证点；
# * 基线得还是审核时冻结的那个 commit —— PR head 移动过，要验的东西就换了；
# * 验证得在**被验证对象自己的 worktree** 里跑，而不是在 plan 的旧目录里；
# * ``project_dir`` 得指向那个 worktree —— 执行链只读这一列。
#
# 顺序是固定的，而且顺序本身就是契约：
#
#     产物判定 → 翻译 + 等价校验 → 基线漂移校验 → 物化 worktree → 注册
#     project_dir → 既有循环
#
# 任何一步失败都**中止**，且中止之前不产生副作用：worktree 没建、project_dir
# 没改写、循环没启动。
#
# 为什么「翻译」排在「漂移校验」前面：翻译要问一次模型，那是要花时间的，而
# PR head 在模型思考期间移动是完全现实的事。所以漂移校验有两次 —— 放行前那次
# （不白花一次模型调用）与物化前那次（权威的那次）。两次之间隔着的就是那次
# 模型调用。

#: 验证分支的机器计划：验证回路读的那一份（与验证 agent 写出的同名同格式）。
MACHINE_PLAN_FILENAME = "verification_plan.json"


def _verification_branch_prelude(
    plan_id: str,
    plan_dir: Path,
    *,
    review_ready: bool,
) -> Optional[JSONResponse]:
    """验证分支的前奏：把「能开始验证」这件事做完，再让循环跑起来。

    Args:
        plan_id: plan id。
        plan_dir: plan 目录。
        review_ready: 「验证分支已就绪」（审核完备 + 基线与 ref 一致）。不就绪
            的计划不进这条管线 —— 它的答案由既有的 409 ``stage_mismatch`` 给出，
            不新增错误码。

    Returns:
        ``None`` 表示放行（继续既有循环）；``JSONResponse`` 表示中止（4xx）。
    """
    from plan_artifacts import BRANCH_VERIFICATION, detect

    artifacts = detect(plan_dir)
    if artifacts.branch != BRANCH_VERIFICATION:
        # 开发分支（以及产物冲突、目录为空）完全走原路径：不建 worktree、
        # 不改写 project_dir、也不问模型。产物冲突的 409 映射由产物判定那条
        # 线负责 —— 本任务只按 ``detect`` 返回的 branch 分流。
        return None

    if not review_ready:
        # 审核不完备 / 基线与 ref 对不上 → 交给既有的 409 ``stage_mismatch``。
        return None

    failure = _prepare_machine_plan(plan_id, plan_dir)
    if failure is not None:
        return failure

    return _materialise_verification_workspace(plan_id, plan_dir)


def _prepare_machine_plan(plan_id: str, plan_dir: Path) -> Optional[JSONResponse]:
    """机器计划：盘上已有的那一份，或模型现译的那一份；两者都要与计划书相等。

    **先校验，后落盘**：等价性门禁报出差异时什么都不写 —— 一份与审核过的
    计划书对不上的机器计划留在盘上，只会成为下一个读它的人踩的坑。

    盘上已有机器计划时直接用它（不重复问一次模型）：它是上一轮管线写下的
    产物，而门禁会拿它与**当前**计划书逐条比对 —— 计划书改了，比对就会报出
    差异。

    Returns:
        ``None`` 表示机器计划就绪（已落盘）；``JSONResponse`` 表示中止。
    """
    from plan_artifacts import PLAN_DOC_FILENAME
    from verification_plan_doc import StructureError, parse_plan_doc
    from verification_plan_translator import validate_equivalence

    doc_path = plan_dir / PLAN_DOC_FILENAME
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "verification_plan_invalid",
                "detail": f"验证计划书读不出来：{exc}",
                "plan_id": plan_id,
            },
        )

    try:
        parsed = parse_plan_doc(text)
    except StructureError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "verification_plan_invalid",
                "detail": "验证计划书不满足十章节契约。",
                "plan_id": plan_id,
                "section": exc.section,
                "line": exc.line,
                "code": exc.code,
                "message": exc.message,
            },
        )

    machine_plan_path = plan_dir / MACHINE_PLAN_FILENAME
    existed = machine_plan_path.exists()
    machine_plan, failure = _machine_plan_for(plan_id, plan_dir, parsed, machine_plan_path)
    if failure is not None:
        return failure

    divergences = validate_equivalence(parsed, machine_plan)
    if divergences:
        return JSONResponse(
            status_code=422,
            content={
                "error": "verification_plan_equivalence_failed",
                "detail": (
                    "机器计划与审核过的验证计划书对不上（验证点集合 / 判据 / "
                    "执行范围 / 证据编号），不落盘。"
                ),
                "plan_id": plan_id,
                "divergences": [
                    {
                        "dimension": item.dimension,
                        "item_id": item.item_id,
                        "detail": item.detail,
                    }
                    for item in divergences
                ],
            },
        )

    if not existed:
        # 只有管线**自己**译出来的那一份才写盘：盘上已有的那一份原样留着。
        machine_plan_path.write_text(
            json.dumps(machine_plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return None


def _machine_plan_for(
    plan_id: str,
    plan_dir: Path,
    parsed: Any,
    machine_plan_path: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
    """机器计划的来源：盘上已有的那一份，或模型现译的那一份。

    Returns:
        ``(机器计划, 失败响应)``。两者恰有一个是 ``None``。
    """
    from verification_plan_translator import TranslationError, translate

    if machine_plan_path.exists():
        try:
            loaded = json.loads(machine_plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, JSONResponse(
                status_code=422,
                content={
                    "error": "verification_plan_invalid",
                    "detail": f"盘上的机器计划读不出来：{exc}",
                    "plan_id": plan_id,
                },
            )
        if not isinstance(loaded, dict):
            return None, JSONResponse(
                status_code=422,
                content={
                    "error": "verification_plan_invalid",
                    "detail": "盘上的机器计划不是一个对象。",
                    "plan_id": plan_id,
                },
            )
        return loaded, None

    # 盘上没有 → 现译。模型只补**执行细节**，验证点集合以文档为准（翻译阶段
    # 零漂移：多一条、少一条、改 id，一律抛错）。
    tool = _server.create_coding_tool(cwd=str(plan_dir), scene="verification")
    try:
        translated = translate(parsed, _repo_facts(plan_dir), tool)
    except TranslationError as exc:
        return None, JSONResponse(
            status_code=422,
            content={
                "error": "verification_plan_translation_failed",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )
    return _with_evidence_refs(parsed, translated), None


def _repo_facts(plan_dir: Path) -> Dict[str, Any]:
    """仓库级事实：模型补执行细节时要用的那些。

    ``services`` 是空数组 —— 验证分支没有 ``tasks.json``，也就没有服务声明；
    端口那种事实不在这条分支上出现。

    ``test_files`` 是仓库里的测试文件清单：模型要写的是「一条可执行命令」，
    而命令指向哪个测试文件只有仓库知道。把清单给它，它就不必猜。
    """
    facts: Dict[str, Any] = {"services": [], "test_files": []}
    baseline = _recorded_baseline(plan_dir)
    if baseline is None:
        return facts
    repo_dir = Path(str(baseline.get("repo_dir") or "")).expanduser()
    if not repo_dir.is_dir():
        return facts
    facts["repo_root"] = str(repo_dir)
    tests_root = repo_dir / "tests"
    if not tests_root.is_dir():
        return facts
    found = sorted(
        str(path.relative_to(repo_dir))
        for path in tests_root.rglob("*.py")
        if path.is_file()
    )
    facts["test_files"] = found[:200]
    return facts


def _recorded_baseline(plan_dir: Path) -> Optional[Dict[str, Any]]:
    """计划状态里记录下来的基线；没有记录时返回 ``None``。

    与 :mod:`verification_gate` 读的是同一处 —— 基线只有这一个来源。
    """
    from plan_state import PlanState

    verification = PlanState(plan_dir).get_state().get("verification") or {}
    recorded = verification.get("baseline")
    return recorded if isinstance(recorded, dict) else None


def _with_evidence_refs(parsed: Any, translated: Dict[str, Any]) -> Dict[str, Any]:
    """给每条验证点的 ``expected_result`` 配上证据编号。

    机器格式的规范形式是 ``E1,E2 | 正文``：编号在前，正文在后。翻译搬的是
    文档里的**正文**，编号得由管线自己配上 —— 编号的来源是文档（审核过的那
    一份），不是模型。
    """
    by_id = {item.id: item for item in parsed.items}
    points: List[Dict[str, Any]] = []
    for entry in translated.get("verification_points") or []:
        vp = dict(entry)
        item = by_id.get(str(vp.get("id") or ""))
        if item is not None and item.evidence:
            refs = ",".join(sorted(item.evidence))
            vp["expected_result"] = f"{refs} | {item.expected_result}"
        points.append(vp)
    result = dict(translated)
    result["verification_points"] = points
    return result


def _materialise_verification_workspace(
    plan_id: str,
    plan_dir: Path,
) -> Optional[JSONResponse]:
    """基线 → 漂移校验 → 物化 worktree → 注册 ``project_dir``。

    漂移校验排在翻译**之后**是有意的：翻译要问一次模型，那是要花时间的，而
    PR head 在模型思考期间移动是完全现实的事。这次校验是权威的那一次 ——
    放行前那次只是不白花一次模型调用。

    Returns:
        ``None`` 表示工作区已就绪；``JSONResponse`` 表示中止。
    """
    from framework.ids import InvalidPlanIdError
    from verification_workspace import (
        BaselineError,
        RefResolutionError,
        WorkspaceConflictError,
        WorkspaceRootError,
        ensure_workspace,
        register_project_dir,
        resolve_baseline,
        resolve_ref_commit,
    )

    try:
        baseline = resolve_baseline(plan_dir)
    except BaselineError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "baseline_missing",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )

    try:
        current = resolve_ref_commit(baseline.repo_dir, baseline.ref)
    except RefResolutionError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "baseline_unresolvable",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )

    if current != baseline.commit_sha:
        # 与既有的 409 同一个形状：不新增错误码。
        return JSONResponse(
            status_code=409,
            content={
                "error": "conflict",
                "reason": "stage_mismatch",
                "detail": (
                    f"基线漂移：{baseline.ref} 现在解析到 {current}，"
                    f"而审核冻结的是 {baseline.commit_sha}。"
                ),
                "plan_id": plan_id,
            },
        )

    try:
        workspace = ensure_workspace(plan_id, baseline)
    except WorkspaceConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "conflict",
                "reason": "workspace_conflict",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )
    except (RefResolutionError, BaselineError) as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "baseline_unresolvable",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )
    except (WorkspaceRootError, InvalidPlanIdError) as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "workspace_root_invalid",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )

    try:
        register_project_dir(plan_id, workspace.path)
    except (WorkspaceRootError, InvalidPlanIdError) as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "workspace_root_invalid",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )

    return None


@router.post("/api/verification/{plan_id}/start")
def start_verification(plan_id: str, req: _server.StartVerificationRequest = None):
    """Start verification via the routing/verification repositories."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, {"error": "Plan not found", "detail": "The requested plan does not exist."})

    try:
        _ensure_verification_plan(plan_id)
    except ArchivedPlanError:
        return JSONResponse(status_code=410, content={"error": "archived"})

    # 产物门禁（五个调用点之一）。
    #
    # 两类产物（``tasks.json`` / ``verification-plan.md``）同时存在时，这条 plan
    # 到底走哪条分支**还没人裁决**。这时候**不能进 CAS**：CAS 成功就是阶段推进，
    # 阶段一动就把人的裁决权抹掉了 —— 框架替人择一，正是这条门禁要防的事。
    #
    # 判定来自 :func:`plan_artifacts.detect`（唯一入口）；错误语义与其它四个
    # 调用点逐字相同。载荷构造器住在 ``routes.plans``（状态接口与列表的所在地），
    # 那里是**函数内**导入的：路由模块之间在模块级互相导入会绕成环（``server``
    # 在文件底部才导入它们），函数内导入是在请求到达时求值，那时一切都已加载。
    from routes.plans import artifact_conflict_payload

    artifacts = detect(plan_dir)
    if artifacts.branch == BRANCH_CONFLICT:
        return JSONResponse(
            status_code=409,
            content=artifact_conflict_payload(plan_id, artifacts),
        )

    # --- round budget resolution (2026-09-20) ---------------------------
    # An explicit ``max_rounds`` in the body wins — that is the supported
    # way to give a task its own iteration budget ("/start 是可以设置
    # 不同的预算的"). Only the *implicit* path defers to the plan.
    #
    # The default used to be resolved right here, which meant a plain
    # ``POST /start`` stamped ``DEFAULT_MAX_VERIFICATION_ROUNDS`` over
    # whatever the plan had been set up with — so bumping that constant
    # silently re-budgeted long-running plans in either direction (a
    # plan set up at 5 would be shrunk to 3; and after the 3→4 bump, a
    # plan set up at 3 would have been widened to 4). The
    # implicit path now falls back below, once the row has been read.
    requested_max_rounds = (
        req.max_rounds if req and req.max_rounds is not None else None
    )
    if requested_max_rounds is not None and not 1 <= requested_max_rounds <= 1000:
        raise HTTPException(400, {"error": "Invalid max_rounds", "detail": f"max_rounds must be between 1 and 1000, got {requested_max_rounds}."})

    # ---------------------------------------------------------------
    # 2026-09-14: deliberately NO workflow-phase precondition here.
    #
    # A pre-SQLite version of this endpoint read ``current_phase`` out
    # of ``plan_state.json`` and answered 400 "Invalid phase" for a
    # phase that does not permit starting. That check was dropped by
    # the SQLite-first rewrite, which made the routing CAS below
    # (``try_mark_phase``) the single decider: a plan whose routing
    # phase is not one of the accepted source phases gets
    # ``409 {"error": "conflict", "reason": "stage_mismatch"}``.
    #
    # 2026-09-17 (schema v5): this precondition used to be *unsound*,
    # because "routing stage" and "workflow phase" were two columns
    # that could disagree. They are one column now, so the CAS is a
    # total decider by construction.
    #
    # Re-adding the 400 was tried and reverted: it masked the CAS for
    # every plan whose routing state had drifted from its workflow
    # phase, which is the exact case VP-018 pins as a conflict. See
    # ``tests/integration/api/test_api_error_matrix.py``
    # (``verify_start x cas_predicate_fail`` → 409) and
    # ``state_machine/tests/integration/test_verification_routes.py``
    # (``test_start_verification_409_when_stage_mismatch``, seeded
    # with phase ``interview`` → 409). The five response-side tests
    # that still expected 400 were relics of the pre-SQLite endpoint.
    # ---------------------------------------------------------------

    # The *live thread* guard below is a different concern and stays:
    # ``_verification_state`` is the only record that a verification
    # thread owns this plan, so without it a caller racing the first
    # ``/start`` gets a second orchestrator thread spawned over the
    # same plan. See ``tests/test_verification_state_machine.py``
    # (``...rejects_already_running_with_409``).
    _live = _server._verification_state.get(plan_id) or {}
    _live_status = str(_live.get("verification_status") or "").lower()
    # 2026-10-20: a RECOVERED entry is not a running round. The process
    # that owned it is gone — the backend was killed — so the in-memory
    # state still says "running" while nothing is running, and the
    # guard turned every subsequent ``/start`` into a permanent 409.
    # The ``recovered`` marker (set by ``_recover_verification_states``,
    # in memory only, never persisted) is what tells the two apart, and
    # the missing thread is the second half of the proof: a live round
    # always owns one.
    _recovered = (
        _live.get("recovered") is True and _live.get("thread") is None
    )
    if _live_status in ("running", "verification_running") and not _recovered:
        return JSONResponse(
            status_code=409,
            content={
                "error": "Already running",
                "detail": (
                    "A verification round is already running for this "
                    "plan; stop it before starting a new one."
                ),
            },
        )

    # 2026-10-20: the pause checkpoint, read before anything is
    # written. It is one of the two traces of an interrupted round (the
    # other is the ``plan_verification`` row's own status) and it is
    # what this start retires once it has honoured it.
    _checkpoint = _read_verification_checkpoint(plan_dir)

    from state_machine.repositories.routing_repository import ConflictError, PlanNotFoundError

    conn = None
    try:
        conn, routing, verification = _server._open_verification_state()
    except (sqlite3.OperationalError, OSError) as exc:
        raise HTTPException(404, {"error": "Plan not found", "detail": str(exc)})

    # -----------------------------------------------------------------
    # 2026-10-22: a plan parked in the REPAIR half of the loop is
    # RESUMED, not re-verified.
    #
    # The repair executor is a stateless scanner: it re-reads
    # tasks.json + state.db and runs whatever is still pending. So
    # "continue the repair" is "spawn it once more" — no attach, no
    # replay of what already ran. Starting a VERIFICATION round here
    # instead would measure code the repair never touched.
    #
    # Two shapes, both decided by the routing row's phase:
    #   A. ``verification_repairing`` — the round finished and its
    #      repair tasks are waiting to be confirmed. The start routes
    #      the plan into ``executing`` (round+1) and spawns the
    #      executor.
    #   B. ``executing`` with pending repair tasks — the executor was
    #      already spawned (or died mid-flight) and the phase is
    #      already ``executing``. The round was already counted, so
    #      this start only spawns the executor again.
    # -----------------------------------------------------------------
    _repair_resume = _repair_resume_response(plan_id, plan_dir, conn, req)
    if _repair_resume is not None:
        return _repair_resume

    # 2026-10-25: the verification branch's own entry ticket.
    #
    # A plan on the verification branch sits at ``verification_plan_review``
    # — the plan doc exists and waits for a human to accept / skip its
    # items. That phase is NOT one of the accepted source phases below,
    # so the CAS refuses the start with ``409 stage_mismatch`` — the very
    # "the state machine let it through, the API says 409 forever"
    # shape the shared predicate exists to prevent.
    #
    # The predicate answers "is the verification branch ready?" — the
    # same question the state machine asks, so the two gates cannot
    # disagree. It enters the CAS as **one more source phase**, and only
    # when it holds: a plan that is not ready keeps the old source set
    # and the old 409. No new error code is introduced.
    _review_ready = _verification_review_ready(plan_dir)

    # 2026-10-25: the verification branch's pipeline, **before** the CAS.
    #
    # The order is the contract: the machine plan has to match the reviewed
    # document, the baseline still has to point at the reviewed commit, and
    # the round has to run inside the worktree of the object under
    # verification — all of that *before* the phase moves, so a failure
    # leaves nothing behind: the phase stays where it was, no worktree was
    # created, ``project_dir`` was not rewritten and no loop was started.
    #
    # The development branch goes through the original path untouched: no
    # worktree, no rewritten ``project_dir``, no model call.
    _prelude = _verification_branch_prelude(
        plan_id, plan_dir, review_ready=_review_ready,
    )
    if _prelude is not None:
        try:
            conn.close()
        except Exception:
            pass
        return _prelude

    try:
        current = verification.current(plan_id)
        # 2026-10-20: an interrupted round is RESUMED, not replaced.
        # ``_resolve_start_round`` answers "is there a round to pick up?"
        # from the two traces that survive an interruption — the
        # pause checkpoint and the row's own status — and returns the
        # round to run plus whether this start is a resume.
        next_round, resumed = _resolve_start_round(current, _checkpoint)

        # Implicit budget: keep the plan's own, else the project default.
        max_rounds = requested_max_rounds
        if max_rounds is None:
            _plan_budget = current.get("max_rounds") if current else None
            max_rounds = (
                int(_plan_budget)
                if isinstance(_plan_budget, int) and _plan_budget > 0
                else _server.DEFAULT_MAX_VERIFICATION_ROUNDS
            )
        # 2026-08-25: ceiling check. Without this guard, repeated
        # ``POST /start`` invocations (or a single retry after the
        # engine silently bumped the round inside ``_run_auto_verification_loop``)
        # can drive ``next_round`` past ``max_rounds`` and start
        # rounds that should already be considered terminal.
        # Operator escape hatch is the new
        # ``POST /api/verification/{plan_id}/reset_rounds`` endpoint.
        if next_round > max_rounds:
            try:
                conn.close()
            except Exception:
                pass
            return JSONResponse(
                status_code=409,
                content={
                    "error": "max_rounds_exceeded",
                    "detail": (
                        f"current round {next_round - 1} has already met "
                        f"max_rounds={max_rounds}; refusing to start round {next_round}"
                    ),
                    "action": (
                        "POST /api/verification/{plan_id}/reset_rounds "
                        "to reset the round counter (max_rounds is immutable, "
                        "so the plan gets a fresh batch of max_rounds rounds)."
                    ),
                },
            )
        routing.try_mark_phase(
            plan_id,
            # 2026-08-26 audit: previously only ``executing`` and the
            # two terminal routing values were valid source states for
            # re-entering verification. This blocked Round 2 / Round 3
            # re-triggers after a verification round completed (PASSED,
            # FAILED, or loop-stopped) — those ARE the legitimate
            # starting points for the next round (the auto-loop inside
            # ``_run_auto_verification_loop`` already chains round →
            # round; the manual ``/start`` path needs the same
            # source reachability so an operator can drive Round 2 /
            # Round 3 manually after a repair commit). The downstream
            # orchestrator's ``force_set_phase("verification")``
            # already handles the in-PlanState transition from these
            # terminal states; only the routing CAS gate needed
            # widening.
            #
            # 2026-09-17 (schema v5): the three old entries
            # (``terminal_failed``, ``terminal_done``,
            # ``verification_idle``) are spelled out as the phases they
            # projected from. The routing vocabulary collapsed
            # four non-pass terminals into ``terminal_failed`` and two
            # pass terminals into ``terminal_done``, so the pre-v5 gate
            # accepted all six; listing them individually preserves
            # that reachability exactly instead of silently narrowing it
            # to ``failed`` / ``completed``.
            #
            # ``verification`` (formerly ``verification_idle``) is
            # accepted because ``POST /api/verification/{id}/stop`` CASes
            # the phase back to ``verification`` when the operator
            # interrupts a round mid-flight. After the stop, the next
            # ``/start`` call must be able to re-enter the verification
            # flow without a manual routing patch.
            (
                "executing",
                "verification",
                "verification_failed",
                "verification_loop_stopped",
                "verification_passed",
                "completed",
                "failed",
                "stopped",
                # 2026-10-22: a plan parked in the REPAIR half of the
                # loop is resumed by the branches above (the repair
                # executor is a stateless scanner — "continue" is
                # "spawn it once more"). This entry is the safety net
                # for the case where those branches do not apply: the
                # plan is still re-enterable as a verification round
                # instead of being refused with 409 stage_mismatch.
                "verification_repairing",
            )
            # 2026-10-20: a recovered plan is still parked at
            # ``verification_running`` — the process died before it
            # could move the routing row. Its own recovered in-memory
            # state is the only evidence that nothing is running, so
            # in exactly that case the current phase is an acceptable
            # source. Every other plan keeps the old source set: a
            # round that is genuinely running must not be re-entered.
            + (("verification_running",) if _recovered else ())
            # 2026-10-25: the verification branch adds ONE more source
            # phase, and only when the branch is ready (see
            # ``_verification_review_ready`` above). The shape is
            # deliberately the same as the ``_recovered`` entry above:
            # the development branch's source set stays item-for-item
            # identical, and a verification branch that is not ready
            # keeps the old source set — so it still answers the
            # existing ``409 stage_mismatch`` instead of a new error
            # code.
            + (("verification_plan_review",) if _review_ready else ()),
            "verification_running",
        )
        verification.init_round(plan_id, round_n=next_round, max_rounds=max_rounds)
        if resumed:
            # 2026-10-20: the interrupted round's work is in the DB
            # row, not in the report (a killed round never wrote one)
            # and the checkpoint is still on disk. Rebuild the report
            # from the row, then retire the checkpoint — this start
            # has honoured it, and a resume must be single-shot.
            _rebuild_round_report(plan_id, plan_dir, next_round, verification)
            _consume_verification_checkpoint(plan_dir)
    except ConflictError as exc:
        try:
            conn.rollback()
        except sqlite3.OperationalError:
            pass
        return JSONResponse(status_code=409, content={"error": "conflict", "reason": _conflict_reason(exc)})
    except (PlanNotFoundError, KeyError):
        return JSONResponse(status_code=404, content={"error": "Plan not found", "detail": "The requested plan does not exist."})
    except sqlite3.OperationalError:
        return JSONResponse(status_code=409, content={"error": "conflict", "reason": "version_mismatch"})
    # 2026-08-25 audit: do NOT close ``conn`` here. The
    # ``verification`` (VerificationRepository) instance is bound to
    # this connection and is handed to the VerificationOrchestrator
    # below, which passes it through VerificationAgent ->
    # VerificationExecutor. The executor's per-VP ``_save_progress``
    # and ``_record_result`` paths call ``verif_repo.update_progress_state``
    # / ``verif_repo.append_verdict`` long AFTER this HTTP request
    # returns — they run in the verification thread that lives across
    # many minutes. Closing ``conn`` here used to surface as
    # ``ProgrammingError('Cannot operate on a closed database.')`` the
    # first time the executor tried to persist a verdict, which
    # silently broke the cross-process progress view. The connection
    # is now bound to ``_verification_state[plan_id]`` under
    # ``_state_db_conn`` and closed when the verification thread
    # terminates (see stop_verification / the verification thread
    # cleanup path).
    project_dir = _server._get_project_dir(plan_id)
    if not project_dir:
        return JSONResponse(status_code=400, content={"error": "Missing project directory", "detail": "No project directory configured for this plan."})

    tool_type = req.tool if req else None
    force_verify = req.force if req else False
    # 2026-08-26 audit: ``req.auto_fix`` was defined in the schema
    # (``StartVerificationRequest.auto_fix = True``) but never read.
    # The auto-repair→rerun loop was only triggered from the post-execution
    # path (server.py:4674); manual ``POST /api/verification/{id}/start``
    # only ran one round. With ``auto_fix=True`` (the default), delegate
    # to ``_run_auto_verification_loop`` so the manual ``/start`` endpoint
    # chains round → round automatically. ``auto_fix=False`` preserves the
    # legacy single-round path so operators can pause between rounds.
    auto_fix = bool(req.auto_fix) if req else True
    with _server._verification_lock:
        if auto_fix:
            # Defer ``_init_verification_state`` to the auto-loop
            # (server.py:4048) — otherwise the auto-loop's
            # "already running" early-return at server.py:4045
            # would short-circuit the auto-fix path. The CAS at
            # server.py:5272 already advanced ``plan_routing.stage``
            # to ``verification_running`` and ``init_round`` at
            # server.py:5295 already wrote the round to
            # ``plan_verification.round``, so the auto-loop's
            # own init_round call will idempotently overwrite
            # with the same round_n on its first iteration.
            #
            # We still need to register the long-lived SQLite
            # connection (used by ``/api/verification/{id}/progress``
            # and ``HeartbeatMonitor``) so the verification
            # thread can be observed and stopped.
            _server._verification_state.setdefault(plan_id, {})
            _server._verification_state[plan_id]["_state_db_conn"] = conn
            # 2026-09-15: a fresh start clears any stop flag left by a
            # previous ``/stop`` — otherwise the new round's executor
            # would immediately skip every VP as "cancelled".
            try:
                import verification_cancel
                verification_cancel.clear(plan_id)
            except Exception:  # noqa: BLE001
                pass
            # 2026-09-14: scrub per-cycle fields left over from a
            # RECOVERED entry (see ``_recover_verification_states`` —
            # even with the recovery-time scrub, belt-and-suspenders
            # here because ``setdefault`` preserves whatever the dict
            # already held). A fresh verification run must not render
            # the previous cycle's repair_tasks / stop_reason on the
            # new round's card (cross-round bleed); the round-close
            # handler repopulates them for the CURRENT round.
            _fresh_vs = _server._verification_state[plan_id]
            _fresh_vs["repair_tasks"] = []
            _fresh_vs.pop("stop_reason", None)
            _fresh_vs.pop("repair_generation_error", None)
            # 2026-10-20: a RECOVERED entry still says "running" — the
            # process that was running it is gone, but the field did
            # not change with it. The auto-loop's duplicate-start guard
            # reads exactly that field, so leaving it would make the
            # resume a no-op: the loop would see a running round,
            # skip, and the plan would stay stuck where the crash left
            # it. The round this start is about to run IS the running
            # one, so the field is demoted to "pending" — the state
            # the plan is in between the request and the loop's own
            # initialisation.
            if _recovered:
                _fresh_vs["verification_status"] = "pending"
            try:
                _round_row = verification.current(plan_id)
                if _round_row is not None:
                    _server._verification_state[plan_id]["verification_round"] = int(_round_row.get("round", 0))
            except Exception:
                pass
        else:
            _server._init_verification_state(plan_id, max_rounds)
            # Seed the in-memory round counter from the SQLite row that
            # ``init_round`` just wrote; without this, the
            # ``/api/verification/{id}/progress`` endpoint reads
            # ``verification_round`` from the in-memory dict (which
            # defaults to 0) and reports a stale value even though the
            # underlying ``plan_verification.round`` column is correct.
            # 2026-08-25 audit.
            try:
                _round_row = verification.current(plan_id)
                if _round_row is not None:
                    _server._verification_state[plan_id]["verification_round"] = int(_round_row.get("round", 0))
            except Exception:
                pass
            # Bind the SQLite connection backing ``verification`` to the
            # in-memory state so the verification thread can keep using
            # the repository until the thread terminates. The
            # connection is closed in three places: (1)
            # stop_verification user stop, (2) the verification thread
            # cleanup path, (3) server shutdown.
            _server._verification_state[plan_id]["_state_db_conn"] = conn

    def _run():
        # 2026-08-26 audit: under ``auto_fix=True`` (default), delegate
        # to ``_run_auto_verification_loop`` so a manual ``/start`` after
        # a failed round automatically chains into repair → re-execute
        # → next round (until passed / max_rounds_reached /
        # same_failure_repeated / no_repair_tasks / user_stopped).
        # Under ``auto_fix=False``, keep the legacy single-round
        # semantics so operators can manually drive each round.
        if auto_fix:
            try:
                _server._run_auto_verification_loop(
                    plan_id, plan_dir, project_dir,
                    max_rounds=max_rounds,
                    tool=tool_type,
                    start_round=next_round,
                )
            except Exception as exc:
                _server.logger.exception("verification auto-loop crashed for %s", plan_id)
                state = _server._verification_state.get(plan_id)
                if state:
                    state.update({
                        "verification_status": "failed",
                        "stop_reason": f"auto_loop_crash: {exc}",
                        "updated_at": datetime.now().isoformat(),
                    })
            return

        try:
            _server._verification_state[plan_id]["updated_at"] = datetime.now().isoformat()
            coding_tool = _server.create_coding_tool(
                tool_type, cwd=str(project_dir), scene="verification",
                logger=_server._verification_event_logger(plan_dir),
            )
            # ``req.max_parallel`` is accepted and **ignored** — see
            # ``StartVerificationRequest.max_parallel``. The round's
            # width is derived from the live provider fleet, so there
            # is no longer a number here for a request body to set.
            # Wire the SQLite VerificationRepository so the agent can
            # persist Phase 1 execution envelopes to
            # ``plan_verification.execution_results`` (task #3.5). The
            # repo was already opened earlier in this function via
            # ``_open_verification_state``.
            orch = _server.VerificationOrchestrator(
                plan_dir, project_dir, coding_tool=coding_tool,
                verif_repo=verification,
            )
            _server._verification_state[plan_id]["orchestrator"] = orch
            ps = _server.PlanState(plan_dir)
            ps.set_verification_max_rounds(max_rounds)
            current = ps.get_current_phase()
            if current not in (
                "verification", "verification_running",
                "verification_repairing",
                "verification_passed", "verification_failed",
                "verification_loop_stopped",
            ):
                try:
                    ps.begin_verification()
                except ValueError:
                    pass
            # 2026-08-25: pass ``resume=True`` for round > 1 so the
            # executor reuses the verdict cache (already-PASSED
            # VPs are filtered out by ``BaseExecutor.run``'s
            # ``completed_set`` check) instead of re-running every
            # VP from scratch. Round 1 still starts with a clean
            # slate (resume=False) so a freshly-edited
            # ``verification_plan.json`` always gets re-evaluated
            # from scratch.
            #
            # Pre-2026-08-25 behaviour: every /start passed
            # ``resume=False`` (the default), which triggered
            # ``_clear_verification_state_files`` and re-ran all
            # 28 VPs each round even though only 1 VP actually
            # failed. Round 2 burned ~5 minutes and ~600k tokens
            # re-running 20 already-passed VPs before the user
            # noticed.
            #
            # 2026-10-20: a RESUMED round asks for the verdict cache
            # too, even when it is round 1. The verdicts in the cache
            # are the interrupted round's own — the VPs it had already
            # judged — so keeping them is what makes the resume pick
            # up where the interruption left off instead of redoing
            # the round from scratch. A round that is NOT a resume
            # keeps the old rule: only round > 1 inherits the cache.
            report = orch.start_verification_cycle(
                round_number=next_round,
                force=force_verify,
                resume=(resumed or next_round > 1),
            )
            result = orch.check_cycle_conditions(report, round_number=next_round)
            v_state = _server._verification_state[plan_id]
            v_state.update({
                "verification_status": result.get("status", "failed"),
                "verification_round": next_round,
                "repair_tasks": result.get("repair_tasks", []),
                "stop_reason": result.get("stop_reason"),
                "updated_at": datetime.now().isoformat(),
            })
            verification.complete_round(
                plan_id,
                {"status": result.get("status", "failed")},
                status=result.get("status", "failed"),
            )
            # 2026-08-25: also persist the terminal status to
            # ``plan_verification.verification_status`` AND advance
            # ``plan_routing.stage`` out of
            # ``verification_running``. The previous
            # implementation only called ``complete_round`` here,
            # which writes to ``plan_verification.results`` but
            # never updated the canonical status column or the
            # routing row. After this thread exits, the next
            # ``/start`` call hits the ``verification_running``
            # branch in ``_verification_status_from_db``
            # (server.py:4984) and forces ``status="running"`` on
            # the response. Mirrors the helper called from
            # ``_run_auto_verification_loop``'s closure.
            _server._persist_verification_terminal(
                plan_id,
                result.get("status", "failed"),
                result.get("stop_reason"),
            )
        except ValueError as exc:
            # 2026-08-19 audit: the previous blanket ``except Exception``
            # silently swallowed Illegal-transition errors raised when
            # the orchestrator's PlanState instance was stale (its
            # cached current_phase did not yet reflect a parallel
            # writer's update). Narrow the catch to ValueError so the
            # caller knows the phase did not advance — previously the
            # verdict PASSED but the stage stayed at verification_running.
            _server.logger.warning(
                "verification worker ValueError for %s: %s",
                plan_id, exc,
            )
            state = _server._verification_state.get(plan_id)
            if state:
                state.update({"verification_status": "failed", "stop_reason": f"value_error: {exc}", "updated_at": datetime.now().isoformat()})
        except Exception as exc:
            _server.logger.exception("Verification worker crashed for %s", plan_id)
            state = _server._verification_state.get(plan_id)
            if state:
                state.update({"verification_status": "failed", "stop_reason": str(exc), "updated_at": datetime.now().isoformat()})
        finally:
            # 2026-08-25 audit: close the long-lived SQLite
            # connection that ``verif_repo`` was bound to. Without
            # this the connection leaks per verification cycle and
            # would eventually exhaust sqlite handles. The conn
            # lives in ``_verification_state[plan_id]["_state_db_conn"]``
            # from start_verification until the thread terminates.
            _state_for_cleanup = _server._verification_state.get(plan_id)
            if _state_for_cleanup:
                _conn_to_close = _state_for_cleanup.pop("_state_db_conn", None)
                if _conn_to_close is not None:
                    try:
                        _conn_to_close.close()
                    except sqlite3.OperationalError:
                        pass

    # Usage-registry attribution: the verification worker makes LLM
    # calls (VP audits, repair generation) — bind plan_id for its thread.
    # The thread is named so the thread can be told apart in a log and
    # in a thread dump — and so a test double that runs threads inline
    # can tell THIS thread (the verification loop) apart from the
    # background workers the loop itself starts (the lease heartbeats):
    # running a heartbeat inline deadlocks, because its body waits for
    # an event only the caller can set.
    verification_thread = threading.Thread(
        target=_server._run_in_plan_ctx,
        args=(plan_id, _run),
        name=f"verification-loop-{plan_id}",
        daemon=True,
    )
    verification_thread.start()
    _server._verification_state[plan_id]["thread"] = verification_thread
    # 2026-10-20: the recovery marker has done its job. From here on
    # the live thread is what keeps a second ``/start`` out — and the
    # marker must not survive, or a later start would treat a plan
    # whose thread has died as a recoverable one.
    _server._verification_state[plan_id].pop("recovered", None)
    return {
        "plan_id": plan_id,
        "status": "started",
        # 2026-10-25: where the round actually runs. On the verification
        # branch that is the worktree of the object under verification (the
        # pipeline registered it), not the plan's old directory — so the
        # caller sees the same directory the executor will.
        "project_dir": str(project_dir),
        # 2026-10-20: the round this start runs and whether it picked
        # an interrupted round up (``resumed``) instead of starting
        # the next one. The round number is the contract a plan's
        # verdicts and report are written against, so a client that
        # polls the status must be able to see which round it is on.
        "round": next_round,
        "resumed": resumed,
    }


#: The root-cause chain's ledger file, relative to a plan directory.
#:
#: The same name the orchestrator writes it under
#: (``verification/orchestrator.py`` ``_ROOT_CAUSE_CHAIN_FILENAME``): the
#: reset has to delete exactly the file a round reads.
_ROOT_CAUSE_CHAIN_FILENAME = "verification_root_cause_chain.json"


def _plan_relative_report_path(
    plan_id: str, report_path: Any,
) -> Optional[str]:
    """The plan-relative path of a report the chain names — or ``None``.

    The chain's ``report_path`` is **repo-relative**: it is the value
    :func:`root_cause_task.root_cause_report_path` returns, i.e.
    ``plans/<plan_id>/reports/root-cause-<vp>.md``.

    Three shapes are refused, and the reason is the same for all of them:
    **the chain is data, and data does not get to name a file anywhere on
    the disk.**

    * an absolute path (``/etc/passwd``, ``C:\\...``) — it names a file
      outside the plan tree entirely;
    * a path with a ``..`` component — the same thing, one level up;
    * a path that is not under ``plans/<plan_id>/`` — it names some other
      plan's file, or a file outside the plans tree.

    A refused path is skipped, not an error: one bad entry in the ledger
    must not stop the reset from clearing the rest of it.
    """
    raw = str(report_path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    # ``PureWindowsPath`` catches the drive-letter form that
    # ``Path.is_absolute`` does not on a POSIX host.
    if candidate.is_absolute() or PureWindowsPath(raw).is_absolute():
        return None
    if ".." in candidate.parts:
        return None
    prefix = f"plans/{plan_id}/"
    if not raw.startswith(prefix):
        return None
    relative = raw[len(prefix):]
    if not relative:
        return None
    return relative


def _load_root_cause_chain(chain_path: Path) -> Dict[str, Any]:
    """The chain's ledger — an empty chain when it cannot be read.

    A ledger that is missing, unreadable or malformed is **not** an error:
    the reset's job is to clear it, and there is nothing to clear when
    there is nothing there. Raising would turn "the ledger was already
    gone" into a 500.
    """
    try:
        raw = chain_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        _server.logger.warning(
            "[reset] root-cause chain ledger is not valid JSON: %s",
            chain_path,
        )
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _clear_root_cause_chain(plan_id: str, plan_dir: Path) -> List[str]:
    """Delete the chain's ledger and the reports it names.

    Returns the plan-relative paths that were actually deleted.

    The order is the ledger first: the ledger is the record of *which*
    reports the chain owned, and once it is gone those reports are
    orphans. The other order leaves a ledger pointing at files that are
    already gone, and the next round would re-derive "the diagnosis was
    delivered" from a file it can no longer read.

    Only the files the ledger names are deleted — **never** a glob of the
    reports directory: that directory also holds the reports of points
    that are no longer on the chain, and a reset has no business taking
    those with it.

    A file that cannot be deleted is logged and skipped: one unwritable
    file must not turn the whole reset into a 500.
    """
    chain_path = plan_dir / _ROOT_CAUSE_CHAIN_FILENAME
    cleared: List[str] = []

    chain = _load_root_cause_chain(chain_path)
    vps = chain.get("vps")
    targets: List[str] = []
    if isinstance(vps, dict):
        for vp_id in sorted(vps):
            entry = vps.get(vp_id)
            if not isinstance(entry, dict):
                continue
            relative = _plan_relative_report_path(
                plan_id, entry.get("report_path"),
            )
            if relative is None:
                _server.logger.warning(
                    "[reset] skipping out-of-plan root-cause report_path "
                    "plan=%s vp=%s path=%s",
                    plan_id, vp_id, entry.get("report_path"),
                )
                continue
            if relative not in targets:
                targets.append(relative)

    if chain_path.exists():
        try:
            chain_path.unlink()
            cleared.append(_ROOT_CAUSE_CHAIN_FILENAME)
        except OSError:
            _server.logger.exception("[reset] unlink failed %s", chain_path)

    for relative in targets:
        target = plan_dir / relative
        # Only *files* are deleted. A ``report_path`` that names a
        # directory is not a report — and the contract says "delete the
        # files the ledger names", not "delete what the ledger points at".
        if not target.exists() or target.is_dir():
            continue
        try:
            target.unlink()
            cleared.append(relative)
        except OSError:
            _server.logger.exception("[reset] unlink failed %s", target)

    return cleared


@router.post("/api/verification/{plan_id}/reset")
def reset_verification(
    plan_id: str,
    req: Optional[_server.ResetVerificationRequest] = None,
):
    """Hard-reset a plan's verification state — direct writes, no transitions.

    2026-09-17::

        "手动重启的时候，不是让状态机自动的从一个点变到另外一个点，而是
         直接去重置这个状态……只要把状态搞定了，把应该要清除掉的东西清除
         掉，它应该是一个比较固定的流程。"

    Why "direct" and not the state machine: a restart is not a workflow
    step, and routing it through ``transition_to`` means every restart
    has to satisfy whatever transition table the current stage happens
    to allow — which is where the drift the user is describing comes
    from. A reset knows exactly what the state should be afterwards, so
    it writes that state.

    What a reset leaves behind (the "fixed procedure"):

    ===========================  ===============================================
    ``_verification_state``      in-memory entry dropped (status, results,
                                 stop_reason, orchestrator, thread — all gone)
    ``plan_verification``        ``pending``, round ← ``restart_at_round-1``,
                                 ``results``/``verdicts``/``runtime_state``/
                                 ``executor_state``/``progress_state``/
                                 ``execution_results`` cleared,
                                 ``verification_stop_reason`` cleared
    ``plan_routing``             ``current_phase='verification'`` (a phase
                                 ``/start`` accepts), ``current_phase``
                                 mirrored, ``version`` bumped
    ``plan_tasks``               pending ``RP-*`` → ``voided`` (never
                                 deleted: task data may only be voided or
                                 created, never rewritten)

    2026-10-16: the status written here was ``superseded`` and is now
    ``voided``. These repair rows had no work handed to breakdown
    children — their *premise* (the round's verdicts) was thrown away —
    so ``voided`` is the status whose resolver behaviour matches
    ("this will never run"), and ``superseded`` is now reserved for
    "go look at my children". The request field
    (``supersede_repair_tasks``) and the response key
    (``cleared.superseded_repair_tasks``) deliberately keep their old
    names: they are an external API contract with a test pinning them,
    and renaming a response key to match an internal status word is a
    breaking change for no functional gain.
    ``plans/<id>/`` on disk      per-round verification artifacts, when
                                 ``clear_artifacts``; ``verification_plan.json``
                                 only when ``clear_verification_plan``;
                                 the root-cause chain's ledger and the
                                 reports it names only when
                                 ``clear_root_cause_chain``
    ===========================  ===============================================

    ``max_rounds`` is NOT touched — it is the plan's immutable budget
    (see :class:`ResetRoundsRequest`).

    Refuses with 409 while a verification thread is live: that thread
    owns the orchestrator and keeps writing state, so a reset under it
    would be immediately overwritten. ``force=true`` cancels the round
    first and then resets.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, {"error": "Plan not found"})

    _archived = _archived_plan_response(plan_dir)
    if _archived is not None:
        return _archived

    req = req or _server.ResetVerificationRequest()

    # --- live-round guard -------------------------------------------------
    summary: Dict[str, Any] = {
        "plan_id": plan_id,
        "cancelled_live_round": False,
        "cleared": {},
    }
    live = _server._verification_state.get(plan_id) or {}
    live_status = str(live.get("verification_status") or "").lower()
    live_thread = live.get("thread")
    thread_alive = bool(live_thread is not None and live_thread.is_alive())
    if (live_status in ("running", "verification_running") or thread_alive):
        if not req.force:
            raise HTTPException(
                409,
                {
                    "error": "Already running",
                    "detail": (
                        "A verification round is live for this plan; it holds "
                        "the orchestrator and keeps writing state, so a reset "
                        "now would be overwritten. Stop it first, or pass "
                        '{"force": true} to cancel the round and reset.'
                    ),
                },
            )
        # Force: ask the round to unwind before we pull the floor out.
        # The cancel marker is checked at every round boundary and by the
        # sub-agent teardown, so this is the same path /stop uses.
        #
        # We deliberately do NOT clear the marker afterwards: the round
        # we just cancelled may still be mid-VP, and clearing it would
        # let that thread sail past its own next boundary check. The next
        # ``/start`` clears it (server.py, the ``verification_cancel.clear``
        # line just before the auto-loop dispatch), so the plan is not
        # left permanently blocked.
        try:
            import verification_cancel
            verification_cancel.request_cancel(plan_id)
        except Exception:  # noqa: BLE001 — best effort; the reset matters more
            _server.logger.exception(
                "[reset] cancel signal failed plan=%s", plan_id,
            )
        summary["cancelled_live_round"] = True

    # --- in-memory --------------------------------------------------------
    dropped = _server._verification_state.pop(plan_id, None)
    if dropped is not None:
        # The entry may hold an open SQLite connection (the /start path
        # stashes one). Dropping the dict without closing it leaks the
        # handle for the life of the process.
        _conn_to_close = dropped.pop("_state_db_conn", None)
        if _conn_to_close is not None:
            try:
                _conn_to_close.close()
            except sqlite3.OperationalError:
                pass
    summary["cleared"]["in_memory_state"] = dropped is not None

    # --- SQLite (direct writes) ------------------------------------------
    conn = None
    try:
        try:
            conn, routing, verification = _server._open_verification_state()
        except (sqlite3.OperationalError, OSError) as exc:
            raise HTTPException(
                503, {"error": "state_db_unavailable", "detail": str(exc)},
            )

        row = verification.current(plan_id) or {}
        max_rounds = int(
            row.get("max_rounds") or _server.DEFAULT_MAX_VERIFICATION_ROUNDS
        )
        if req.restart_at_round > max_rounds:
            raise HTTPException(
                400,
                {
                    "error": "restart_round_above_cap",
                    "detail": (
                        f"restart_at_round={req.restart_at_round} exceeds the "
                        f"plan's immutable max_rounds={max_rounds}"
                    ),
                },
            )

        # ``reset_round_counter`` already does the transition-free write of
        # the counter + status + results + the plan_routing.verification
        # mirror (2026-09-15 fix: without the envelope clear,
        # ``repair_stale_terminal_state`` re-derives the old terminal from
        # it on the very next /progress read and un-resets the row).
        verification.reset_round_counter(
            plan_id,
            round_n=req.restart_at_round - 1,
            max_rounds=max_rounds,
        )

        # The rest of the verdict/runtime columns are not covered by the
        # helper. One direct UPDATE, no transition table involved.
        now = datetime.now().isoformat()
        with conn:
            conn.execute(
                "UPDATE plan_verification SET "
                "runtime_state = NULL, executor_state = NULL, "
                "progress_state = NULL, verdicts = NULL, "
                "execution_results = NULL, verification_stop_reason = NULL, "
                "started_at = NULL, updated_at = ? "
                "WHERE plan_id = ?",
                (now, plan_id),
            )
            # Direct phase write. ``verification`` is the honest
            # "in the verification phase, nothing running" value AND one
            # of the phases the verify ``/start`` CAS admits, so the plan
            # is immediately restartable without another transition.
            # Bumping ``version`` keeps the CAS readers that snapshot it
            # consistent.
            #
            # 2026-09-17 (schema v5): this used to write the routING-only
            # spelling ``verification_idle`` and leave ``current_phase``
            # at ``verification_repairing`` — so a reset plan read as
            # "repairing" to the API and "idle" to the scheduler. One
            # column, one value now.
            conn.execute(
                "UPDATE plan_routing SET current_phase = ?, substage = NULL, "
                "verification = NULL, version = version + 1, updated_at = ? "
                "WHERE plan_id = ?",
                ("verification", now, plan_id),
            )
        summary["cleared"]["verification_row"] = True
        summary["max_rounds"] = max_rounds
        summary["restart_at_round"] = req.restart_at_round

        # --- pending repair tasks ----------------------------------------
        # Local name kept aligned with the response key below (which is
        # part of the pinned API surface) even though the status written
        # is now ``voided``.
        superseded: List[str] = []
        if req.supersede_repair_tasks:
            pending = _server._get_pending_repair_tasks(plan_id)
            if pending:
                ids = [t.get("id") for t in pending if t.get("id")]
                with conn:
                    conn.executemany(
                        "UPDATE plan_tasks SET status = 'voided', "
                        "failure_reason = ?, updated_at = ? "
                        "WHERE plan_id = ? AND task_id = ?",
                        [
                            (
                                "[reset] voided by verification reset — "
                                "generated from verdicts that were discarded",
                                now,
                                plan_id,
                                tid,
                            )
                            for tid in ids
                        ],
                    )
                superseded = ids
        summary["cleared"]["superseded_repair_tasks"] = superseded
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.OperationalError:
                pass

    # 2026-10-16 (schema v6): the ``current_phase`` mirror that used to
    # live here is GONE — and its ``except`` clause with it.  That block
    # wrote ``plan_execution.current_phase='verification'`` on a SEPARATE
    # connection after the routing UPDATE above had already committed, and
    # swallowed any failure into a ``cleared.current_phase_mirror: false``
    # response key that nothing has ever read.  So the failure mode was
    # invisible in both directions: the response said "fine" to a caller
    # that never checked the key, and the mirror silently kept a stale
    # phase.  There is no second column left to disagree with.

    # --- disk artifacts ---------------------------------------------------
    removed: List[str] = []
    if req.clear_artifacts:
        # NOTE: ``verification_plan.json`` is deliberately absent. Its VP
        # ids are positional, so regenerating recycles them onto different
        # verification points and every VP-id-keyed artefact across the
        # batch boundary gets re-attributed. It is gated by its own flag
        # below — do not re-add it here.
        for rel in (
            "verification_report.json",
            "verification_execution_results.json",
            "verification_repair_tasks.json",
            # 2026-09-19: the cross-round same-failure tracking
            # (``previous_failed_ids`` + the repeat counter). It is what
            # makes the loop stop early, and it must NOT survive a reset:
            # the whole point of a reset is to discard the verdict set
            # being counted, so inheriting it would stop the very next
            # round on a comparison against a plan generation that no
            # longer exists.
            "verification_loop_tracking.json",
        ):
            target = plan_dir / rel
            if target.exists():
                try:
                    target.unlink()
                    removed.append(rel)
                except OSError:
                    _server.logger.exception("[reset] unlink failed %s", target)
        for sub, pattern in (
            ("logs", "verification_*"),
            ("screenshots", "verification_*"),
        ):
            directory = plan_dir / sub
            if not directory.is_dir():
                continue
            for target in sorted(directory.glob(pattern)):
                if target.is_file():
                    try:
                        target.unlink()
                        removed.append(f"{sub}/{target.name}")
                    except OSError:
                        _server.logger.exception("[reset] unlink failed %s", target)

    # Gated on its own flag rather than ``clear_artifacts``: regenerating
    # the VP list is a different decision from discarding the round's
    # outputs, and it is the one that recycles VP ids. Default off.
    if req.clear_verification_plan:
        target = plan_dir / "verification_plan.json"
        if target.exists():
            try:
                target.unlink()
                removed.append("verification_plan.json")
            except OSError:
                _server.logger.exception("[reset] unlink failed %s", target)

    # Gated on its own flag, like ``clear_verification_plan``: the chain
    # is the plan's own history of where each point's root cause stood,
    # and clearing it is a decision — not a side effect of a reset. The
    # key is always present so a caller can read it without guarding for
    # its absence.
    chain_cleared: List[str] = []
    if req.clear_root_cause_chain:
        chain_cleared = _clear_root_cause_chain(plan_id, plan_dir)
    summary["cleared"]["root_cause_chain"] = chain_cleared

    summary["cleared"]["disk_artifacts"] = removed

    # 2026-09-18 C3: a reset ends the current cycle, so the services it
    # started are reaped here rather than left holding their ports for
    # the operator's next ``/start``. The ledger itself is *not* in the
    # ``clear_artifacts`` list on purpose — it is the input to this
    # reap, so deleting it first would strand the processes it records.
    _server._reap_managed_services(plan_id, plan_dir, "verification_reset")

    _server.logger.warning(
        "[reset] plan=%s hard-reset to verification, restart at round %d "
        "(artifacts removed=%d, superseded tasks=%d, force=%s)",
        plan_id, req.restart_at_round, len(removed), len(superseded), req.force,
    )
    return summary


@router.post("/api/verification/{plan_id}/reset_rounds")
def reset_rounds(plan_id: str, req: _server.ResetRoundsRequest):
    """Operator escape hatch — give the plan another batch of rounds.

    Name (2026-09-16)
    -----------------
    Renamed from ``reset_max_rounds`` because it never touched
    ``max_rounds``: it resets the round **counter**. See
    :class:`ResetRoundsRequest`.

    Semantics (2026-09-14 — inverted from the original)
    -----------------------------------------------------------------
    * The plan's ``max_rounds`` is **immutable**: it is the budget the
      plan was set up with. Raising it makes rounds unbounded (the
      2026-08-25 incident: an operator lifted the cap and the counter
      together and round 6 ran for 47 minutes); lowering it silently
      rewrites the plan's setup contract. Any ``new_max_rounds`` that
      differs from the current cap is refused.
    * What this endpoint DOES is reset the round **counter** — the thing
      the ``/start`` ceiling check compares against the cap. With user
      authorization the operator can hand the plan a fresh batch so the
      auto-loop iterates up to ``max_rounds`` more times.

    Body::

        {}                       # restart at round 1 (default; full batch)
        {"reset_round_to": 1}    # next /start iterates rounds 1, 2, 3
        {"reset_round_to": 3}    # next /start runs round 3 only (1 left)

    ``reset_round_to`` is the round number the iteration **restarts at**
    (1-based, the same numbering the card shows): after the cap is hit,
    the counter resets to 1 and the next ``/start`` iterates from there.
    It is stored internally as completed-rounds =
    ``value - 1`` so ``/start`` begins exactly at that round, and it may
    not exceed the immutable cap (there is no round 4 of a 3-round
    budget).

    Side effects: the counter is written to ``plan_verification.round``
    (verdicts / runtime state survive — a reset is about budget, not
    about wiping progress), the row is left ``pending`` with no stop
    reason, and the in-memory state is re-synced so the next ``/start``
    is admitted instead of 409-ing on a stale "already running" flag.

    Operator-only by convention (matches ``/start`` and ``/stop`` — no
    extra auth gate). Returns
    ``{"plan_id", "round" (stored counter), "restart_at_round",
    "rounds_remaining", "max_rounds", "new_max_rounds"}``.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, {"error": "Plan not found"})

    if req is None:
        raise HTTPException(
            400,
            {"error": "missing_body",
             "detail": "send {} (defaults) or {\"reset_round_to\": <int>"},
        )

    # 2026-08-25: hard cap — refuse new_max_rounds above the
    # plan's *original* cap. We read the current value out of the
    # SQLite state-machine row (the canonical source of truth post
    # the refactor) before applying the change so an operator can't
    # escalate past the plan's setup cap by repeatedly calling this
    # endpoint. If the row has no max_rounds yet, fall back to the
    # plan_state.json value.
    #
    # The cap read and the actual update share a single
    # ``_open_verification_state`` connection — closing it between
    # the two calls would invalidate the ``VerificationRepository``
    # bound to it (it holds a reference to the closed connection
    # and the next call raises ``Cannot operate on a closed
    # database.``).
    try:
        conn, _routing, verification = _server._open_verification_state()
    except (sqlite3.OperationalError, OSError) as exc:
        raise HTTPException(404, {"error": "Plan not found", "detail": str(exc)})

    try:
        original_max_rounds: int
        try:
            existing_row = verification.summary(plan_id) or {}
            existing = existing_row.get("max_rounds")
            if isinstance(existing, int) and existing > 0:
                original_max_rounds = existing
            else:
                # Fall back to plan_state.json (legacy source).
                try:
                    ps_fb = _server.PlanState(plan_dir)
                    original_max_rounds = int(
                        ps_fb.get_state().get("verification", {}).get(
                            "max_rounds", _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
                        ) or _server.DEFAULT_MAX_VERIFICATION_ROUNDS
                    )
                except Exception:
                    original_max_rounds = _server.DEFAULT_MAX_VERIFICATION_ROUNDS
        except Exception:
            original_max_rounds = _server.DEFAULT_MAX_VERIFICATION_ROUNDS

        # --- 2026-09-14: the cap is immutable; the COUNTER is what we reset.
        if (
            req.new_max_rounds is not None
            and req.new_max_rounds != original_max_rounds
        ):
            raise HTTPException(
                400,
                {
                    "error": "max_rounds_immutable",
                    "detail": (
                        f"max_rounds is the plan's budget and cannot be "
                        f"changed (requested {req.new_max_rounds}, plan's is "
                        f"{original_max_rounds}). This endpoint resets the "
                        f"round COUNTER instead — send "
                        f"{{\"reset_round_to\": 1}} for a fresh batch of "
                        f"{original_max_rounds} rounds."
                    ),
                    "max_rounds": original_max_rounds,
                },
            )

        # 2026-09-15: ``reset_round_to`` is the
        # 1-based round number the iteration RESTARTS at (a reset to 1 →
        # rounds 1..max run). The store keeps completed-rounds, so a
        # restart at round N is stored as N-1 and /start's
        # ``next_round = counter + 1`` lands on N exactly.
        restart_at_round = (
            req.reset_round_to if req.reset_round_to is not None else 1
        )
        if restart_at_round > original_max_rounds:
            raise HTTPException(
                400,
                {
                    "error": "reset_round_to_exceeds_cap",
                    "detail": (
                        f"cannot restart at round {restart_at_round}: the "
                        f"plan's budget is {original_max_rounds} rounds and "
                        f"the cap is immutable. Send "
                        f"{{\"reset_round_to\": {original_max_rounds}}} to "
                        f"use the last round of the budget."
                    ),
                    "max_rounds": original_max_rounds,
                },
            )
        round_target = max(0, restart_at_round - 1)
        # Reset the counter (not the cap) and leave the row ``pending``:
        # nothing runs until ``/start`` admits the next round.
        try:
            verification.reset_round_counter(
                plan_id, round_n=round_target, max_rounds=original_max_rounds,
            )
        except AttributeError:
            # Older repository (pre-2026-09-14): fall back to the two
            # steps the new method wraps.
            verification.init_round(
                plan_id, round_n=round_target, max_rounds=original_max_rounds,
            )

        # Re-sync the in-memory view: without this the next ``/start`` is
        # rejected by the "already running" guard (a recovered entry can
        # carry ``verification_status='running'``) and the dashboard keeps
        # showing the previous round's stop reason.
        v_state = _server._verification_state.get(plan_id)
        if v_state is not None:
            v_state["verification_round"] = round_target
            v_state["verification_max_rounds"] = original_max_rounds
            v_state["verification_status"] = "pending"
            v_state["stop_reason"] = None
            v_state["updated_at"] = datetime.now().isoformat()
        # The plan-state mirror keeps its own copy for the card builder.
        try:
            _server.PlanState(plan_dir).set_verification_max_rounds(
                original_max_rounds
            )
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _server.logger.info(
        "[reset_round_counter] plan=%s restart at round %d (stored counter %d; "
        "cap %d unchanged by operator)",
        plan_id, restart_at_round, round_target, original_max_rounds,
    )
    return {
        "plan_id": plan_id,
        # Stored completed-rounds counter (what /start reads).
        "round": round_target,
        # 1-based round number the next /start begins iterating at —
        # the operator-facing number ("重置回 N").
        "restart_at_round": restart_at_round,
        "rounds_remaining": original_max_rounds - round_target,
        "max_rounds": original_max_rounds,
        # Backward-compat field: older callers read this key. It is the
        # plan's cap, i.e. unchanged.
        "new_max_rounds": original_max_rounds,
    }


@router.get("/api/verification/{plan_id}/status")
def get_verification_status(plan_id: str):
    """Return verification status sourced from the SQLite state machine."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, {"error": "Plan not found", "detail": "The requested plan does not exist."})
    try:
        return _verification_status_from_db(plan_id)
    except HTTPException:
        raise
    except (sqlite3.OperationalError, OSError) as exc:
        raise HTTPException(404, {"error": "Plan not found", "detail": str(exc)})


def _read_latest_vp_start(plan_dir: Path, vp_id: str) -> Optional[str]:
    """Scan the round log files for the most recent ``vp_start`` event
    whose ``verification_point_id`` matches ``vp_id``.

    Returns the ``timestamp`` field of that event (``"YYYY-MM-DDTHH:MM:SS.fff"``)
    or ``None`` if no matching event is found. Used to populate
    ``current_vp.started_at`` on the progress endpoint without
    re-parsing the full JSON state file.

    The log files are JSON-lines at
    ``plans/{id}/logs/verification_{round}_{timestamp}.log``; the
    file with the largest round number contains the most recent
    run, so we sort by round (numeric) and return the first match
    we find. Round 0 is treated as a real round.
    """
    logs_dir = plan_dir / "logs"
    if not logs_dir.exists():
        return None

    log_files = sorted(logs_dir.glob("verification_*.log"))
    if not log_files:
        return None

    # Sort by file mtime DESCENDING so the most-recently-written log is
    # scanned first and the first match returned IS the latest run.
    # 2026-09-07 fix (twice): the original ascending round sort returned
    # the OLDEST round's vp_start (e.g. yesterday's), which is what the
    # Feishu card rendered as "current VP started at" — making an
    # actively-running VP look hours old. A descending round sort is
    # still wrong because the round counter RESETS on restart (an old
    # round-3 file from yesterday outranks a fresh round-2 file from
    # today). mtime is the only monotonic signal: the in-flight round's
    # log is being actively appended, so it always sorts first.
    # (The sibling ``_read_all_vp_starts`` scans every file and lets
    # the LAST write win keyed by round — that one also needs the same
    # treatment, see its comment.)
    log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for log_file in log_files:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    # See _read_all_vp_starts — log entries use
                    # ``event_type`` (verification_persistence.py:88),
                    # not ``event``. Same 2026-08-25 audit fix.
                    if entry.get("event_type") == "vp_start" and entry.get("verification_point_id") == vp_id:
                        ts = entry.get("timestamp")
                        if isinstance(ts, str):
                            return ts
        except OSError:
            continue
    return None


def _read_all_vp_starts(plan_dir: Path) -> Dict[str, str]:
    """Return ``{vp_id: latest_started_at_iso}`` for every VP with a
    ``vp_start`` event in the verification round logs.

    Mirrors :func:`_read_latest_vp_start` but scans all VPs in a single
    log pass (and the innermost loop returns on the first match, so the
    same per-file cost stays bounded). Used by
    :func:`_build_verification_progress` to derive the
    per-VP ``running`` status without changing the executor's
    single-``current_vp`` persistence shape — the executor only
    remembers the "primary" current VP via a semaphore-bounded
    scheduler, but the logs already record every parallel ``vp_start``
    so the dashboard can display all in-flight VPs at once.
    """
    logs_dir = plan_dir / "logs"
    if not logs_dir.exists():
        return {}

    log_files = sorted(logs_dir.glob("verification_*.log"))
    if not log_files:
        return {}

    # Sort by file mtime ASCENDING so older logs are scanned first and
    # the LATEST file's vp_start wins the overwrite. 2026-09-07 fix:
    # the round-number key is NOT monotonic — the round counter resets
    # on verification restart, so yesterday's round-3 file outranked
    # today's round-2 file and supplied stale timestamps (same root
    # cause as the _read_latest_vp_start fix).
    log_files.sort(key=lambda p: p.stat().st_mtime)

    starts: Dict[str, str] = {}
    for log_file in log_files:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    # Audit 2026-08-25: log entries use ``event_type``,
                    # not ``event`` (see verification_persistence.py
                    # line 88). The old ``event`` key never matched
                    # anything in production logs, so ``running_vp_ids``
                    # was always empty and the operator never saw a
                    # non-zero ``in_progress`` count in the card even
                    # while VPs were actively running.
                    if entry.get("event_type") != "vp_start":
                        continue
                    vp_id = entry.get("verification_point_id")
                    ts = entry.get("timestamp")
                    if isinstance(vp_id, str) and isinstance(ts, str):
                        # Logs are processed round-ascending so the
                        # LATEST ``vp_start`` wins — overwrites any
                        # older entry from an earlier round.
                        starts[vp_id] = ts
        except OSError:
            continue
    return starts


def _read_latest_round_state(
    plan_dir: Path,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Any], Dict[str, Any]]:
    """One pass over the NEWEST round log →
    ``(vp_activity, judgment, last_vp_event)``.

    ``vp_activity`` is ``{vp_id: {"start": iso|"", "complete": iso|""}}``.
    ``judgment`` is the most recent ``judgment_heartbeat``
    (``{"stage", "vp_id", "ts"}``), or ``{}`` if there was none.
    ``last_vp_event`` is the most recent ``vp_start`` / ``vp_complete``
    (``{"event_type", "vp_id", "ts"}``), or ``{}``.

    All three come from the same file in the same pass on purpose. The
    VP lifecycle and the judgment phase are two views of one timeline; a
    reader that returns them separately has to re-parse the file for the
    second, and this runs on ``/api/verification/{id}/progress``, which
    the notifier polls continuously while a round is live. Round logs
    reach tens of megabytes on a long run, so the second pass is not
    free. The caller also needs ``last_vp_event`` to decide which of the
    two is genuinely the most recent thing that happened — that
    comparison cannot be made from either half alone.
    """
    logs_dir = plan_dir / "logs"
    if not logs_dir.exists():
        return {}, {}, {}
    log_files = sorted(
        logs_dir.glob("verification_*.log"),
        key=lambda p: p.stat().st_mtime,
    )
    if not log_files:
        return {}, {}, {}
    activity: Dict[str, Dict[str, str]] = {}
    judgment: Dict[str, Any] = {}
    last_vp: Dict[str, Any] = {}
    try:
        with open(log_files[-1], "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                et = entry.get("event_type")
                ts = entry.get("timestamp")
                vp_id = entry.get("verification_point_id")
                if et == "judgment_heartbeat":
                    # Keep the LAST one, and only accept a usable
                    # timestamp — a heartbeat with a malformed ts is not
                    # a position in the timeline.
                    if isinstance(ts, str):
                        judgment = {
                            "stage": (entry.get("data") or {}).get("stage")
                            if isinstance(entry.get("data"), dict) else None,
                            "vp_id": vp_id if isinstance(vp_id, str) else None,
                            "ts": ts,
                        }
                    continue
                if et not in ("vp_start", "vp_complete"):
                    continue
                if not isinstance(vp_id, str) or not isinstance(ts, str):
                    continue
                slot = activity.setdefault(vp_id, {"start": "", "complete": ""})
                slot["start" if et == "vp_start" else "complete"] = ts
                last_vp = {"event_type": et, "vp_id": vp_id, "ts": ts}
    except OSError:
        return {}, {}, {}
    return activity, judgment, last_vp


def _read_latest_round_vp_activity(plan_dir: Path) -> Dict[str, Dict[str, str]]:
    """Return ``{vp_id: {"start": iso|"", "complete": iso|""}}`` from
    the NEWEST verification round log only.

    2026-09-14: the persisted
    ``plan_verification.progress_state`` carries terminal verdicts
    from the LAST round that wrote it. When a VP is re-run in the
    CURRENT round (e.g. round 2 re-running a VP that round 1's
    watchdog stamped failed), the stale verdict shadowed the live
    state — the progress payload showed the VP as ``failed`` while
    the orchestrator was actively re-verifying it, and
    ``current_vp`` came back ``None`` because the executor only
    persists its "primary" semaphore VP.

    The newest round log is the live truth: a ``vp_start`` with no
    subsequent ``vp_complete`` in that file means the VP is running
    RIGHT NOW, regardless of what the persisted verdicts say.

    Thin wrapper over :func:`_read_latest_round_state` so both views of
    the round cost one pass. The judgment half is discarded here because
    every caller of this function wants only the VP lifecycle.
    """
    activity, _judgment, _last_vp = _read_latest_round_state(plan_dir)
    return activity


def _running_vps_from_activity(
    activity: Dict[str, Dict[str, str]],
) -> set:
    """VP ids with a ``vp_start`` but no ``vp_complete`` in the
    activity map — i.e. mid-flight right now."""
    return {
        vp_id
        for vp_id, act in activity.items()
        if act.get("start") and not act.get("complete")
    }


#: Display labels for the round sub-steps, keyed by the ``kind`` this
#: module derives. Kept next to the derivation so adding a kind forces a
#: decision about how it reads; the card receives the finished string and
#: never has to know these names.
_VERIFICATION_ACTIVITY_LABELS = {
    "planning": "📋 正在编排本轮验证点",
    "running": "🔍 正在验证",
    "judging": "⚖️ 正在复核验证结论",
    "summarizing": "📊 正在汇总本轮结果",
}

#: How much of a VP title survives into an activity label. Titles run to
#: several clauses, and the label is rendered as one card row: an
#: untruncated title wraps until it is no longer a scannable status
#: line. Truncation is cosmetic — the full title is still available on
#: the VP's own row further down the card.
_VERIFICATION_ACTIVITY_TITLE_MAX = 60


def _flatten_vp_title(title: Any) -> str:
    """Collapse a VP title to a single bounded line for a label.

    Plan titles are prose and may contain newlines; the label is a
    one-line status string, so every whitespace run (including the
    newlines) becomes a single space before truncation.
    """
    if not isinstance(title, str):
        return ""
    flat = " ".join(title.split())
    if len(flat) <= _VERIFICATION_ACTIVITY_TITLE_MAX:
        return flat
    return flat[:_VERIFICATION_ACTIVITY_TITLE_MAX] + "…"


def _verification_round_activity(
    running: set,
    activity: Dict[str, Dict[str, str]],
    judgment: Dict[str, Any],
    last_vp: Dict[str, Any],
    vp_titles: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Describe what the verification round is doing when no VP is in flight.

    Returns ``{"kind", "label", "vp_id", "since"}`` or ``None``.

    Why this exists: ``current_vp`` is populated by the executor's
    semaphore-primary slot, so it is only set while a VP is executing.
    A round spends most of its wall-clock time elsewhere — planning the
    VP set, judging the verdicts, writing the report — and in those
    windows the card fell through to a bare "🔄 验证中" naming nothing.
    The round's two boundaries produce a nameless card each: one at
    round start before the first ``vp_start``, one at round close after
    the last ``vp_complete``.

    The judgment phase is the expensive case, and the reason it needs
    naming rather than merely tolerating. It walks the VPs one at a time
    and can run for a long stretch. Its heartbeats publish
    ``KIND_VP_STATE_CHANGED``, so the card IS rebuilt throughout — but
    nothing the card renders moves, so ``card_fingerprint`` matches and
    every one of those rebuilds is deduped away. Without this the card
    freezes for the whole phase showing a verdict from a phase that has
    already ended. Naming the sub-step makes the content differ, which
    is what lets the dedup do its job.

    ``running`` (the in-flight VP set) is passed in rather than re-derived
    so this and the ``current_vp`` fallback in the caller cannot disagree
    about which VPs are live. ``activity`` is the caller's already-parsed
    view of the same log — re-reading it here would double the cost of
    the poll this runs inside.

    ``vp_titles`` is the caller's already-loaded ``vp_id -> title`` map,
    passed in for the same reason: naming a VP is only useful if the name
    says WHAT is being reviewed, and a bare id (``[VP-006]``) does not.
    Reading the plan here instead would add a disk read to every poll of
    an endpoint that is already poll-driven. A missing entry degrades to
    the id-only label rather than failing the payload.
    """
    if running:
        # A VP is genuinely in flight. The caller already names it via
        # ``current_vp``; stay out of the way rather than emitting a
        # second, vaguer line about the same thing.
        return None

    if not activity:
        # No VP has started in this round yet. Either the round is being
        # planned, or the log is unreadable — both mean the same thing to
        # the card, which is "not far enough along to name anything".
        return {"kind": "planning",
                "label": _VERIFICATION_ACTIVITY_LABELS["planning"],
                "vp_id": None, "since": None}

    # ``judgment`` wins only if it is genuinely the most recent thing
    # that happened. A VP that started after the last heartbeat means the
    # round moved on (a re-check, or a second pass), and calling that
    # "judging" would name a phase that already ended.
    if judgment and (not last_vp or judgment["ts"] >= last_vp["ts"]):
        label = _VERIFICATION_ACTIVITY_LABELS["judging"]
        vp_id = judgment.get("vp_id")
        if vp_id:
            label += f" `[{vp_id}]`"
            # The id alone does not say what is under review. The
            # title is appended here (rather than at the render
            # sites) so every consumer of this label — card body,
            # unified header, any future caller — names the same
            # thing, and so the card can never double-append it.
            title = _flatten_vp_title((vp_titles or {}).get(vp_id))
            if title:
                label += f" {title}"
        return {"kind": "judging", "label": label,
                "vp_id": vp_id, "since": judgment["ts"]}

    # Every VP in the round has reached a terminal state and nothing is
    # being judged: the orchestrator is aggregating verdicts.
    return {"kind": "summarizing",
            "label": _VERIFICATION_ACTIVITY_LABELS["summarizing"],
            "vp_id": None, "since": last_vp.get("ts")}


def _build_verification_progress(plan_id: str) -> dict:
    """Verification progress — wrapper owning the state-machine handle.

    The payload is built by :func:`_verification_progress_body`, which
    receives the open handle. The split exists so the connection is
    released in a ``finally``; the body is a 300-line read-through with
    a single terminal ``return`` and several error branches.
    """
    sm = _server._open_state_machine()
    try:
        return _verification_progress_body(plan_id, sm)
    finally:
        _server._close_state_machine(sm)


def _retired_vps_from_plan(plan_vps: List[dict]) -> Dict[str, str]:
    """The plan's retired VPs → the marker that retired each one.

    Mirrors ``VerificationExecutor._vp_retired_marker``: ``obsolete``
    wins over ``superseded_by`` — both mean "this VP is not run any
    more", and only the FAILED handling differs between them (obsolete
    → no bucket at all, superseded_by → skipped).
    """
    retired: Dict[str, str] = {}
    for vp in plan_vps or []:
        if not isinstance(vp, dict):
            continue
        if vp.get("obsolete"):
            marker = RETIRED_OBSOLETE
        elif vp.get("superseded_by"):
            marker = RETIRED_SUPERSEDED_BY
        else:
            continue
        vp_id = vp.get("id")
        if vp_id:
            retired[str(vp_id)] = marker
    return retired


def _verdicts_by_vp_id(
    verification_row: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """The row's verdict table → ``{vp_id: verdict}`` (last write wins).

    ``plan_verification.verdicts`` is a **list** of ``{"vp_id", "status",
    ...}`` dicts — the executor appends one per judged VP. The legacy
    on-disk shape (``{vp_id: verdict}``) is accepted too.

    Duplicate verdicts for one VP are normalised here: the **last** one
    wins, mirroring the write side (the verdict map is a dict, so a
    later write overwrites the earlier one). Entries that cannot be
    attributed to a VP (no ``vp_id``) are dropped — a verdict without a
    subject says nothing about any bucket.
    """
    raw = (verification_row or {}).get("verdicts")
    entries: List[Tuple[Any, Any]] = []
    if isinstance(raw, dict):
        entries = list(raw.items())
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                entries.append((entry.get("vp_id"), entry))
    out: Dict[str, Dict[str, Any]] = {}
    for vp_id, verdict in entries:
        if not isinstance(verdict, dict):
            continue
        key = str(vp_id) if vp_id is not None else ""
        if not key:
            continue
        out[key] = verdict
    return out


#: ``actual_result`` / ``evidence_tail`` 的长度上限（飞书卡片的契约 ——
#: 卡片把原因渲染成一行引文，太长会把卡片撑爆）。
MAX_ACTUAL_RESULT_LEN = 500
MAX_EVIDENCE_TAIL_LEN = 300


def _stringify_evidence(evidence: Any) -> str:
    """把 ``evidence`` 统一成一段文本（它的形状不固定）。

    裁决侧写的是 ``dict``（``{"pytest": "..."}``），报告侧常见 ``str``，
    偶尔是 ``list[str]``。直接切片（``s[:300]``）对 ``dict`` 会抛
    ``TypeError`` —— 那会让整份报告的失败原因被 ``except`` 吞掉，
    操作员看到的是「有失败数、没原因」。
    """
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        return evidence
    if isinstance(evidence, dict):
        try:
            return json.dumps(evidence, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(evidence)
    if isinstance(evidence, (list, tuple)):
        return "；".join(str(item) for item in evidence)
    return str(evidence)


def _verdict_failure_reason(
    verdict: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """一条裁决 → ``(失败原因, 证据尾巴)``，都按契约截断。

    原因优先取 ``actual_result``；没有就取 ``reasons`` 列表，用 ``；``
    拼接（裁决表里通常只有 ``reasons``）。两个都没有 → 返回空串，由调用方
    回退到 ``verification_report.json``。
    """
    if not isinstance(verdict, dict):
        return "", ""

    reason = ""
    actual = verdict.get("actual_result")
    if isinstance(actual, str):
        reason = actual.strip()
    if not reason:
        reasons = verdict.get("reasons")
        if isinstance(reasons, str):
            reason = reasons.strip()
        elif isinstance(reasons, (list, tuple)):
            reason = "；".join(
                str(item).strip() for item in reasons if str(item).strip()
            )

    evidence = _stringify_evidence(verdict.get("evidence")).strip()
    return reason[:MAX_ACTUAL_RESULT_LEN], evidence[:MAX_EVIDENCE_TAIL_LEN]


def _derive_vp_buckets(
    verification_row: Optional[Dict[str, Any]],
    plan_vps: List[dict],
    progress: Dict[str, Any],
) -> Dict[str, List[str]]:
    """The three terminal buckets — derived from the verdict table.

    ``progress_state``'s ``completed_vps`` / ``failed_vps`` /
    ``skipped_vps`` are a **cache**, not the truth. A historical bad row
    (one VP listed as both failed *and* completed) is served to the card
    verbatim, and the repair loop then re-runs a VP that is actually
    SKIPPED. The truth is the verdict table
    (``plan_verification.verdicts``) — the write side has derived its
    index lists from it since 2026-08-25
    (``VerificationExecutor._backfill_index_lists_from_verdicts``), and
    the rule itself lives in :mod:`verification_buckets`.

    The rules:

      * **verdicts 非空** → the three lists are derived from the
        verdicts and ``progress_state``'s lists are ignored
        **entirely** (a row that has any verdict never falls back to
        the cached lists);
      * **verdicts 为空**（真正的旧行）→ fall back to the stored lists.
        That fallback exists for the migration window only;
      * DEFERRED lands in no terminal bucket — it stays *pending*
        (Phase 2 will re-run it);
      * the plan's retired VPs follow the write side's rules (see
        :mod:`verification_buckets`).

    Deliberately **not** done here: judging whether a verdict is stale.
    ``_record_result`` writes the DB row without the command
    fingerprint, so ``verdict_provenance.is_stale`` would call every
    verdict stale and empty every bucket.
    """
    raw_verdicts = (verification_row or {}).get("verdicts")
    if not raw_verdicts:
        # 真正的旧行：只有缓存的三个列表。迁移窗口内保持旧行为。
        return {
            "completed_vps": list(progress.get("completed_vps") or []),
            "failed_vps": list(progress.get("failed_vps") or []),
            "skipped_vps": list(progress.get("skipped_vps") or []),
        }

    buckets = derive_index_buckets(
        _verdicts_by_vp_id(verification_row),
        retired=_retired_vps_from_plan(plan_vps),
    )
    return {
        "completed_vps": list(buckets[BUCKET_COMPLETED]),
        "failed_vps": list(buckets[BUCKET_FAILED]),
        "skipped_vps": list(buckets[BUCKET_SKIPPED]),
    }


def _verification_progress_body(
    plan_id: str, sm: Optional[Tuple[Any, ...]],
) -> dict:
    """Build the response payload for ``/api/verification/{plan_id}/progress``.

    Reads two on-disk artifacts (cross-server-restart compatible):

      * ``plan_verification.progress_state`` (via VerificationRepository)
       — the executor's scheduler view: ``current_vp``,
        ``completed_vps``, ``failed_vps``, ``skipped_vps``,
        ``updated_at``.  This replaces the legacy
        ``plan_dir/verification_progress_state.json`` read.
      * ``plan_dir/verification_plan.json`` — the VP list with
        titles (used to compute ``pending_vps`` and to populate
        ``current_vp.title``).

    The L1/L2/L3 layer concept was removed in 2026-06-13. For
    backward-compat with older API consumers the response still
    carries ``current_layer: None`` and ``layer_summaries: {}``;
    the per-VP ``current_vp.layer`` field is removed entirely.

    In-memory ``_verification_state`` is consulted only for the
    cheap summary fields (``verification_status``,
    ``verification_round``); the per-VP scheduler state lives in
    the state-machine SQLite row's ``progress_state`` column
    (parsed JSON via VerificationRepository.summary) so the
    response is identical whether the in-memory dict is populated
    or the server was just restarted.
    """
    plan_dir = _server._plan_dir(plan_id)

    # The handle was opened by _build_verification_progress, which
    # closes it; both the read-through auto-heal below and the
    # progress_state read further down share it.
    #
    # 2026-09-09 (unified card phase):
    # read-through auto-heal before serving /progress. If a prior
    # ``_persist_verification_terminal`` step 1 partial-wrote
    # ``results`` without updating ``verification_status``, the
    # card generator would otherwise still see ``pending`` and
    # render "🔄 验证中" forever. The repair mirrors the verdict to
    # ``plan_routing.verification`` JSON column too, which is the
    # source this endpoint actually reads via PlanState.
    if sm is not None:
        _, _routing, _execution, verification_repo, _artifact = sm
        if verification_repo is not None and hasattr(
            verification_repo, "repair_stale_terminal_state"
        ):
            try:
                previous = verification_repo.repair_stale_terminal_state(plan_id)
                if previous is not None:
                    import logging
                    logging.getLogger(__name__).info(
                        "[verification_progress_read] auto-repair plan=%s "
                        "previous_status=%s",
                        plan_id, previous,
                    )
            except Exception as exc:
                # Repair is best-effort; the read continues with the
                # row as-is and PlanState's "pending" default will
                # only matter if no verdict is recoverable.
                import logging
                logging.getLogger(__name__).warning(
                    "[verification_progress_read] repair failed plan=%s: %s",
                    plan_id, exc,
                )

    # ``progress_state`` is stored as a parsed JSON column on the
    # ``plan_verification`` SQLite row (via VerificationRepository).
    # The legacy on-disk JSON file is no longer written — reads must
    # go through the repository layer.
    progress: Dict[str, Any] = {}
    verification_row: Optional[Dict[str, Any]] = None
    if sm is not None:
        _, _routing, _execution, verification_repo, _artifact = sm
        if verification_repo is not None:
            try:
                verification_row = verification_repo.summary(plan_id)
            except Exception:
                verification_row = None
            if isinstance(verification_row, dict):
                column_value = verification_row.get("progress_state")
                if isinstance(column_value, dict):
                    progress = dict(column_value)

    if not progress:
        # Either the state-machine has no row for this plan yet, or
        # the executor hasn't written any progress state.  Return
        # the legacy 404 so the dashboard falls back to /status.
        raise HTTPException(404, {"error": "verification not started"})

    # ``verification_plan.json`` uses the envelope key
    # ``verification_points`` (the agent writes this); the executor
    # itself uses ``vps`` in its in-memory plan. Accept both so the
    # endpoint works for plans from either pipeline.
    plan_vps: List[dict] = []
    plan_file = plan_dir / "verification_plan.json"  # noqa: F841 — used below
    if plan_file.exists():
        try:
            with open(plan_file, "r", encoding="utf-8") as f:
                plan_data = json.load(f)
            if isinstance(plan_data, dict):
                if isinstance(plan_data.get("verification_points"), list):
                    plan_vps = list(plan_data["verification_points"])
                elif isinstance(plan_data.get("vps"), list):
                    plan_vps = list(plan_data["vps"])
        except (OSError, json.JSONDecodeError):
            plan_vps = []

    # Index by id for O(1) title lookup.
    plan_index: Dict[str, dict] = {}
    for vp in plan_vps:
        if isinstance(vp, dict):
            vp_id = vp.get("id")
            if isinstance(vp_id, str):
                plan_index[vp_id] = vp

    # 2026-10-23: the three terminal lists are **derived** from the
    # verdict table, not read from ``progress_state``'s cache — a
    # historical bad row (one VP listed as both failed and completed)
    # used to be served to the card verbatim. See
    # :func:`_derive_vp_buckets`.
    _buckets = _derive_vp_buckets(verification_row, plan_vps, progress)
    completed_vps: List[str] = list(_buckets["completed_vps"])
    failed_vps: List[str] = list(_buckets["failed_vps"])
    skipped_vps: List[str] = list(_buckets["skipped_vps"])

    current_vp_id = progress.get("current_vp")
    in_flight: Optional[str] = (
        current_vp_id if isinstance(current_vp_id, str) and current_vp_id else None
    )

    # ``pending_vps`` is everything in the plan that hasn't reached a
    # terminal state and isn't currently in flight. The in-flight VP
    # has its own field (``current_vp``) and would otherwise
    # double-count with ``counts.in_progress``. Derive from the plan
    # (source of truth for *all* VPs) rather than from the
    # executor's in-memory pending list, which is private to the
    # executor instance and not persisted.
    terminal = set(completed_vps) | set(failed_vps) | set(skipped_vps)

    # Scan logs once for every VP's ``vp_start`` timestamp. Used
    # below to (a) expose a per-VP ``running`` status (the executor's
    # ``current_vp`` field only mirrors the semaphore's "primary"
    # current VP, so VPs running in parallel would otherwise show as
    # ``pending``) and (b) correct ``in_progress_count`` to match the
    # actual number of concurrent VPs the dashboard cares about.
    vp_starts: Dict[str, str] = _read_all_vp_starts(plan_dir)
    running_vp_ids: set = {
        vp_id
        for vp_id in vp_starts
        if vp_id not in terminal
    }

    # 2026-09-14: the persisted terminal
    # verdicts may belong to a PREVIOUS round that was interrupted
    # (e.g. the watchdog's stale-stamp). A VP with a vp_start but no
    # vp_complete in the CURRENT round's log is running RIGHT NOW —
    # it must show as running, not inherit the stale failed/completed
    # verdict. Drop it from the persisted terminal lists and add it
    # to the running set; the round's own vp_complete will re-write
    # the verdict when it lands.
    _latest_activity, _latest_judgment, _latest_vp_event = (
        _read_latest_round_state(plan_dir)
    )
    _running_now = _running_vps_from_activity(_latest_activity)
    if _running_now:
        completed_vps = [v for v in completed_vps if v not in _running_now]
        failed_vps = [v for v in failed_vps if v not in _running_now]
        skipped_vps = [v for v in skipped_vps if v not in _running_now]
        running_vp_ids |= _running_now
        # The persisted ``current_vp`` only mirrors the executor's
        # "primary" semaphore VP and is often None/stale; fall back
        # to the earliest-started currently-running VP so the card
        # can name what is being verified.
        if not in_flight:
            in_flight = sorted(
                _running_now,
                key=lambda v: (_latest_activity.get(v) or {}).get("start") or "",
            )[0]

    # 2026-10-23: the verdict table is the **primary** source of the
    # failure reason (same source as the failure bucket). The report
    # below is only the fallback.
    verdicts_by_id: Dict[str, Dict[str, Any]] = _verdicts_by_vp_id(
        verification_row
    )

    # 2026-08-26: surface failed-VP ``actual_result`` so the Feishu
    # card can render a red reason box per failing VP. The plan-index
    # only carries the static plan metadata (title / method / layer);
    # the actual failure text lives in ``verification_report.json``
    # which is written by the orchestrator at round close. We scan
    # the latest report once and attach ``actual_result`` (truncated
    # to 500 chars so the card stays compact) + a short evidence
    # tail to each failed VP entry below. The file may be missing
    # mid-round (orchestrator hasn't flushed yet) — that's expected
    # and ``failure_details_by_id`` simply stays empty.
    #
    # 2026-10-23: the report is now only a **fallback**. The failure
    # reason must come from the same source as the failure bucket —
    # the verdict table (``plan_verification.verdicts``). The two
    # sources drift apart: the report is rewritten by later judging
    # rounds (a plan whose report ended up "17 PASSED" still had 11
    # failures in the bucket), so a report-derived reason matched
    # nothing and the card showed a bare ``🔴 [VP-001] · 标题``.
    failure_details_by_id: Dict[str, Dict[str, str]] = {}
    try:
        report_files = sorted(
            plan_dir.glob("verification_report*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if report_files:
            with report_files[0].open("r", encoding="utf-8") as _rf:
                _report = json.loads(_rf.read())
            # 2026-09-12: the top-level
            # ``verification_results`` may be empty when
            # ``snapshot_round_results`` + ``clear_round_results`` ran
            # at round start and the new round hasn't re-populated the
            # top-level field yet. Fall back to ``rounds[-1].results``
            # so the Feishu card still surfaces actual_result text
            # for failed VPs that live in the previous round snapshot.
            _results_lists: List[List[Dict[str, Any]]] = []
            _top = _report.get("verification_results", []) or []
            if _top:
                _results_lists.append(_top)
            _rounds_field = _report.get("rounds") or []
            if (
                not _top
                and isinstance(_rounds_field, list)
                and _rounds_field
            ):
                _last = _rounds_field[-1] if isinstance(_rounds_field[-1], dict) else {}
                _last_results = _last.get("results") or []
                if isinstance(_last_results, list) and _last_results:
                    _results_lists.append(_last_results)
            for _results_list in _results_lists:
                for _vr in _results_list:
                    _vid = _vr.get("id")
                    # 2026-09-06 audit: surface FAILED AND SKIPPED entries
                    # that carry a non-empty actual_result. SKIPPED entries
                    # that came from a hard BLOCK (e.g. binary_freshness
                    # pre-flight) carry their reason in actual_result —
                    # without this branch the operator sees the count but
                    # not the reason, and the Feishu card "⏭ 跳过 VP"
                    # section only renders id+title.
                    if not _vid or _vr.get("status") not in ("FAILED", "SKIPPED", "BLOCKED"):
                        continue
                    _ar = _stringify_evidence(
                        _vr.get("actual_result")
                    ).strip()
                    if _ar:
                        failure_details_by_id[_vid] = {
                            "actual_result": _ar[:MAX_ACTUAL_RESULT_LEN],
                            "evidence_tail": _stringify_evidence(
                                _vr.get("evidence")
                            )[:MAX_EVIDENCE_TAIL_LEN],
                        }
    except Exception:
        # Defensive: a corrupt report file must not break /progress.
        pass

    pending_vps: List[str] = [
        vp_id
        for vp_id in plan_index.keys()
        if vp_id not in terminal and vp_id not in running_vp_ids
    ]

    current_vp_payload: Optional[dict] = None
    if in_flight:
        vp_meta = plan_index.get(in_flight, {})
        current_vp_payload = {
            "id": in_flight,
            "title": vp_meta.get("title", ""),
            "started_at": _read_latest_vp_start(plan_dir, in_flight),
        }

    # Counts: the dashboard keys on ``in_progress`` (1 if and only
    # if a VP is mid-flight) and on the lengths of the index lists
    # for the rest. ``skipped`` is exposed separately so the
    # progress bar can show a distinct segment without re-deriving
    # it from the plan.
    completed_set = set(completed_vps)
    failed_set = set(failed_vps)
    skipped_set = set(skipped_vps)
    vps: List[Dict[str, Any]] = []
    for vp_id, vp_meta in plan_index.items():
        if vp_id in completed_set:
            status = "completed"
        elif vp_id in failed_set:
            status = "failed"
        elif vp_id in skipped_set:
            status = "skipped"
        elif vp_id in running_vp_ids:
            status = "running"
        else:
            status = "pending"
        entry: Dict[str, Any] = {
            "id": vp_id,
            "title": vp_meta.get("title", ""),
            "status": status,
            "started_at": vp_starts.get(vp_id),
            "method": vp_meta.get("method", ""),
            "layer": vp_meta.get("layer", ""),
        }
        # Attach the failure reason for failed VPs.
        #
        # 2026-10-23: the reason comes from the **same source as the
        # bucket** — the verdict table (``plan_verification.verdicts``,
        # read through ``_verdicts_by_vp_id``). ``verification_report.
        # json`` is only a fallback for the rows that predate the
        # verdict table (and for verdicts that carry no reason at
        # all). The two used to be independent, and the report is
        # rewritten by later judging rounds — a plan whose report had
        # been rewritten to "17 PASSED" still had 11 failures in the
        # bucket, so the card showed a bare ``🔴 [VP-001] · 标题``
        # with no reason at all.
        if status == "failed":
            reason, evidence_tail = _verdict_failure_reason(
                verdicts_by_id.get(vp_id)
            )
            details = failure_details_by_id.get(vp_id, {})
            if not reason:
                reason = details.get("actual_result", "")
            if not evidence_tail:
                evidence_tail = details.get("evidence_tail", "")
            if reason:
                entry["actual_result"] = reason
            if evidence_tail:
                entry["evidence_tail"] = evidence_tail
        vps.append(entry)

    in_progress_count = len(running_vp_ids)
    counts = {
        "total": len(plan_index),
        "completed": len(completed_vps),
        "failed": len(failed_vps),
        "skipped": len(skipped_vps),
        "in_progress": in_progress_count,
        "pending": len(pending_vps),
    }

    # Cheap summary fields from the in-memory dict first (matches
    # /status), then fall back to plan_state.json so the endpoint
    # returns the right answer after a server restart.
    mem_state = _server._verification_state.get(plan_id, {}) or {}
    verification_status = mem_state.get("verification_status")
    verification_round = mem_state.get("verification_round")
    # 2026-09-19: ``stop_reason`` joined the payload so clients can tell
    # *why* a loop ended. The notifier's header resolver has a dedicated
    # "⏹ 循环停止(round 已达上限)" branch keyed on
    # ``stop_reason == "max_rounds_reached"``, but it read this field off
    # ``/progress`` — which never carried it — and off
    # ``summary.state.verification.stop_reason``, which is a third stale
    # copy. The authoritative value is the
    # ``plan_verification.verification_stop_reason`` column.
    stop_reason: Optional[str] = mem_state.get("stop_reason")
    if verification_status is None or verification_round is None or stop_reason is None:
        try:
            ps = _server.PlanState(plan_dir)
            v = ps.get_state().get("verification", {}) or {}
            if verification_status is None:
                verification_status = v.get("status", "not_started")
            if verification_round is None:
                verification_round = v.get("round", 0)
            if stop_reason is None:
                stop_reason = v.get("stop_reason")
        except Exception:
            verification_status = verification_status or "not_started"
            verification_round = verification_round if verification_round is not None else 0
    if stop_reason is None and sm is not None:
        try:
            _, _rr, _ee, _vr, _aa = sm
            stop_reason = (_vr.summary(plan_id) or {}).get(
                "verification_stop_reason"
            )
        except Exception:
            stop_reason = None

    # ``current_vp`` above answers "which VP is executing". This answers
    # "what is the round doing when none is" — planning, judging,
    # summarizing. Additive on purpose: several tests pin ``current_vp``'s
    # existing semantics (including its being None outside a VP), and the
    # card needs both. Gated on the round being live so a terminal plan's
    # payload is byte-for-byte what it was before.
    round_activity: Optional[dict] = None
    if str(verification_status or "").lower() in ("running", "in_progress"):
        try:
            round_activity = _verification_round_activity(
                running_vp_ids, _latest_activity, _latest_judgment,
                _latest_vp_event,
                # ``plan_index`` was read from disk earlier in this
                # same request and already carries each VP's title
                # (it is what ``current_vp_payload`` reads at
                # line ~1828). Handing it over keeps the sub-step as
                # namable as the in-flight VP without a second read.
                {
                    vid: (vp or {}).get("title") or ""
                    for vid, vp in plan_index.items()
                },
            )
        except Exception:
            _server.logger.exception(
                "[verification_progress] round_activity failed plan=%s", plan_id,
            )

    # The plan's outstanding human decisions.  The same block the plan
    # summary and the execution progress carry, so a consumer that
    # polls the progress endpoints reads the same shape it reads from
    # the summary.  Imported here rather than at module scope:
    # ``routes.plans`` reaches back into the sibling route modules,
    # and a module-level import between them would be a cycle.
    from routes.plans import _pending_decisions_block

    return {
        "plan_id": plan_id,
        "verification_status": verification_status,
        "verification_round": verification_round,
        "stop_reason": stop_reason,
        "current_vp": current_vp_payload,
        "activity": round_activity,
        "completed_vps": completed_vps,
        "failed_vps": failed_vps,
        "skipped_vps": skipped_vps,
        "pending_vps": pending_vps,
        "vps": vps,
        # Always present — an empty block means "nothing is
        # outstanding", which is an answer, not a missing key.
        "pending_decisions": _pending_decisions_block(plan_id),
        # 2026-09-11 plan v12 (cards.py repair_tasks):
        # ``repair_tasks`` comes from two layers — first the in-memory
        # ``_verification_state`` (live, may be empty after server
        # restart), then the on-disk
        # ``plans/{id}/verification_repair_tasks.json`` snapshot (set
        # by :func:`_run_auto_verification_loop` after each round).
        # The disk version wins when present because it survives
        # server restarts; the in-memory fallback covers the brief
        # window between orchestrator write and disk flush.
        "repair_tasks": _load_repair_tasks_for_progress(plan_id),
        # 2026-09-14: parents the repair-phase judge
        # split into sub-VPs. Rendered on the card so a round with no
        # repair tasks reads as "拆分了 VP" rather than as a silent
        # no-op; durable because it is derived from
        # ``verification_plan.json`` (``superseded_by``).
        "vp_splits": _load_vp_splits_for_progress(plan_id),
        # L1/L2/L3 layer concept was removed 2026-06-13. These
        # two keys are kept in the response shape for backward
        # compat with older API consumers (the bridge card builder
        # ignores them; dashboards that read them get empty values).
        "current_layer": None,
        "layer_summaries": {},
        "counts": counts,
        "last_updated_at": progress.get("updated_at"),
    }


def _load_vp_splits_for_progress(plan_id: str) -> List[Dict[str, Any]]:
    """Return the VP splits visible on the progress payload / Feishu card.

    2026-09-14: when the repair-phase judge decides a
    VP is too big, the VP is *split* rather than repaired — the card must
    show which VPs were split, into what, and why. Otherwise
    the round's missing repair tasks look like a silent no-op.

    Primary source is ``verification_plan.json`` itself: the splitter
    stamps the parent with ``superseded_by`` + ``split_reason`` and
    inserts the children right after it, so the split is durable across
    restarts without a new artifact. The in-memory
    ``_verification_state[plan_id]["vp_splits"]`` record (written by the
    auto-loop right after the round) fills in the judge's ``hint`` and is
    the only source while a plan file write is still in flight.

    Returns ``[]`` when nothing was split — the common case.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    plan_file = _server._plan_dir(plan_id) / "verification_plan.json"
    try:
        if plan_file.exists():
            with open(plan_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data
            if isinstance(data, dict):
                entries = (
                    data.get("verification_points")
                    or data.get("vps")
                    or []
                )
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                children = entry.get("superseded_by")
                if not isinstance(children, list) or not children:
                    continue
                parent_id = str(entry.get("id", "") or "")
                if not parent_id or parent_id in seen:
                    continue
                seen.add(parent_id)
                out.append({
                    "vp_id": parent_id,
                    "title": entry.get("title", ""),
                    "child_vp_ids": [str(c) for c in children],
                    "reason": entry.get("split_reason", ""),
                    "hint": entry.get("split_hint", ""),
                })
    except (OSError, json.JSONDecodeError):
        out = []
    mem_state = _server._verification_state.get(plan_id, {}) or {}
    for record in mem_state.get("vp_splits") or []:
        if not isinstance(record, dict):
            continue
        vp_id = str(record.get("vp_id", "") or "")
        if not vp_id:
            continue
        if vp_id in seen:
            # Enrich the plan-derived record with the judge's hint.
            for existing in out:
                if existing["vp_id"] == vp_id and not existing.get("hint"):
                    existing["hint"] = record.get("hint", "")
                    existing["reason"] = (
                        existing.get("reason") or record.get("reason", "")
                    )
            continue
        seen.add(vp_id)
        out.append({
            "vp_id": vp_id,
            "title": "",
            "child_vp_ids": [
                str(c) for c in (record.get("child_vp_ids") or [])
            ],
            "reason": record.get("reason", ""),
            "hint": record.get("hint", ""),
        })
    return out


def _load_repair_tasks_for_progress(plan_id: str) -> List[Dict[str, Any]]:
    """Return the accumulated repair_tasks for ``verification_progress``.

    Three-layer read (2026-09-11 plan v12 + 2026-09-12 RP-* persistence
    fix):

    1. In-memory ``_verification_state[plan_id]["repair_tasks"]`` (live,
       may be empty after server restart or when the round generated
       zero tasks).
    2. On-disk ``plans/{id}/verification_repair_tasks.json`` snapshot.
       Supports BOTH the legacy v1 schema (``{round, tasks}``) and
       the new v2 round-accumulation schema (``{rounds: [...]}``);
       v2 reads aggregate tasks from EVERY recorded round, deduping
       by ``id`` and preferring the latest copy so an empty round N
       does not erase round N-1's pending RP-* tasks.
    3. State-machine ``plan_tasks`` rows whose ``task_group`` starts
       with ``repair`` (the orchestrator's authoritative write path —
       survives even when the on-disk JSON file is missing or
       truncated). This is the layer that answers the open question
       directly: prior repair tasks are NEVER deleted or overwritten.

    Layer 3 wins because it is the only source that always reflects
    the truth in the framework's primary state-machine store.
    Layers 1 + 2 fill gaps before the state.db row is committed.

    Returns an empty list when no source has data.
    """
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()

    def _append_tasks(tasks: Any) -> None:
        if not isinstance(tasks, list):
            return
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id", ""))
            if tid and tid in seen_ids:
                continue
            if tid:
                seen_ids.add(tid)
            out.append(t)

    # 1) Disk snapshot — preferred over in-memory.
    try:
        rt_path = _server._plan_dir(plan_id) / "verification_repair_tasks.json"
        if rt_path.exists():
            with rt_path.open("r", encoding="utf-8") as rf:
                rt_data = json.load(rf)
            if isinstance(rt_data, dict):
                # v2 schema — accumulate across rounds so an empty
                # round N does NOT erase round N-1's RP-* entries.
                rounds = rt_data.get("rounds")
                if isinstance(rounds, list):
                    # Walk rounds in order so the LATEST copy of a
                    # task wins (a later round may have refreshed
                    # status, title, or acceptance_criteria).
                    for entry in rounds:
                        if not isinstance(entry, dict):
                            continue
                        _append_tasks(entry.get("tasks"))
                else:
                    # v1 legacy schema — single top-level ``tasks``.
                    _append_tasks(rt_data.get("tasks"))
    except Exception:
        # Corrupt file → fall through to in-memory.
        pass

    # 2) In-memory — fills gaps when disk snapshot was for a prior
    # round and the live round has generated a fresh list.
    try:
        mem = _server._verification_state.get(plan_id, {}) or {}
        mem_tasks = mem.get("repair_tasks")
        if isinstance(mem_tasks, list):
            _append_tasks(mem_tasks)
    except Exception:
        pass

    # 3) state.db.plan_tasks rows whose task_group starts with
    # "repair" — authoritative, survives any disk-side wipe or
    # orchestrator write to the wrong DB. The orchestrator stamps
    # ``task_group="repair-round-N"`` (v9 refactor) on every repair
    # task written via ``add_task``; legacy RP-* rows from pre-v9
    # also match because they share the same prefix convention.
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        sm_conn = _open_db(_server._state_db_path())
        try:
            _migrate(sm_conn)
            for row in PlanTaskRepository(sm_conn).iter_by_task_group_prefix(
                plan_id, "repair",
            ):
                if not isinstance(row, dict):
                    continue
                # Normalise to the same shape the readers expect:
                # {id, title, description, status, task_group, round,
                # failed_vp_id, ...}.
                _append_tasks([row])
        finally:
            try:
                sm_conn.close()
            except Exception:
                pass
    except Exception:
        # state.db unavailable — layers 1 + 2 are still authoritative
        # for the round that just ran.
        pass

    return out


@router.get("/api/verification/{plan_id}/progress")
def get_verification_progress(plan_id: str):
    """Get VP-granularity + layer-granularity verification progress.

    Returns 404 if the executor hasn't written a progress state
    file yet (``verification_progress_state.json`` absent under
    ``plan_dir``) — the dashboard falls back to ``/status`` in that
    case. Otherwise the response is the same shape whether the
    in-memory ``_verification_state`` is populated or the server
    was just restarted: the live state lives on disk in
    ``verification_progress_state.json`` (one write per
    ``vp_status_changed`` event), so cross-process recovery is
    automatic.

    Schema::

        {
          "plan_id": str,
          "verification_status": str,           # "running" | "passed" | ...
          "verification_round": int,
          "stop_reason": Optional[str],         # why the loop ended, e.g.
                                                # "max_rounds_reached" /
                                                # "same_failure_repeated_after_max_attempts"
                                                # (2026-09-19)
          "current_vp": {                       # None when no VP in flight
            "id": str,
            "title": str,
            "started_at": Optional[str]
          },
          "completed_vps": [str, ...],
          "failed_vps": [str, ...],
          "skipped_vps": [str, ...],
          "pending_vps": [str, ...],
          "current_layer": None,                # legacy field, always None
          "layer_summaries": {},                # legacy field, always empty
          "counts": {
            "total": int, "completed": int, "failed": int,
            "skipped": int, "in_progress": int, "pending": int
          },
          "last_updated_at": Optional[str]
        }
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, {"error": "Plan not found", "detail": "The requested plan does not exist."})
    return _build_verification_progress(plan_id)


@router.get("/api/verification/{plan_id}/repair_tasks")
def get_verification_repair_tasks(plan_id: str):
    """Get repair tasks generated by the verification cycle.

    2026-09-11 plan v12 (cards.py repair_tasks): reads the on-disk
    ``plans/{id}/verification_repair_tasks.json`` snapshot first
    (survives server restart), then falls back to the in-memory
    ``_verification_state[plan_id]["repair_tasks"]`` for the live
    round's freshly generated list.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, {"error": "Plan not found", "detail": "The requested plan does not exist."})

    tasks: List[Dict[str, Any]] = []
    seen_ids: set = set()

    def _append_tasks(items: Any) -> None:
        if not isinstance(items, list):
            return
        for t in items:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id", ""))
            if tid and tid in seen_ids:
                continue
            if tid:
                seen_ids.add(tid)
            tasks.append(t)

    # 1) Disk snapshot (preferred — survives restart). 2026-09-12
    # plan: support v2 round-accumulation schema (aggregate across
    # rounds, dedupe by id) AND legacy v1 single-round schema.
    try:
        rt_path = plan_dir / "verification_repair_tasks.json"
        if rt_path.exists():
            with rt_path.open("r", encoding="utf-8") as rf:
                rt_data = json.load(rf)
            if isinstance(rt_data, dict):
                rounds = rt_data.get("rounds")
                if isinstance(rounds, list):
                    for entry in rounds:
                        if not isinstance(entry, dict):
                            continue
                        _append_tasks(entry.get("tasks"))
                else:
                    _append_tasks(rt_data.get("tasks"))
    except Exception:
        pass

    # 2) In-memory (fills gaps for live round).
    state = _server._verification_state.get(plan_id, {})
    _append_tasks(state.get("repair_tasks") or [])

    # 3) state.db.plan_tasks rows whose task_group starts with
    # "repair" — authoritative layer that survives disk wipes
    # (mirrors the third layer in
    # :func:`_load_repair_tasks_for_progress`).
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        sm_conn = _open_db(_server._state_db_path())
        try:
            _migrate(sm_conn)
            for row in PlanTaskRepository(sm_conn).iter_by_task_group_prefix(
                plan_id, "repair",
            ):
                if isinstance(row, dict):
                    _append_tasks([row])
        finally:
            try:
                sm_conn.close()
            except Exception:
                pass
    except Exception:
        # state.db unavailable — layers 1 + 2 still authoritative
        # for the round that just ran.
        pass

    formatted = []
    for task in tasks:
        formatted.append({
            "id": task.get("id", ""),
            "title": task.get("title", ""),
            "description": task.get("description", ""),
            "test_command": task.get("test_command", ""),
            "failure_reason": task.get("failure_reason", ""),
        })

    return {"tasks": formatted}


# ---------------------------------------------------------------------------
# What a stop leaves behind (2026-10-20)
# ---------------------------------------------------------------------------

#: The report the repair chain reads. Its per-VP key is
#: ``verification_results`` — NOT ``verification_points``, which belongs
#: to ``verification_plan.json`` (see
#: ``verification/verification_report_reader.py``, which reads this key
#: and nothing else).
_REPORT_FILENAME = "verification_report.json"

#: The plan's VP list — the universe a round's verdicts are drawn from.
_VP_PLAN_FILENAME = "verification_plan.json"


def _load_report(plan_dir: Path) -> Dict[str, Any]:
    """The plan's verification report, or ``{}`` when it is unusable.

    A missing report is the NORMAL case for a round that has not closed
    yet (the report is written at round close); a corrupt one is the
    abnormal case. Both degrade to ``{}`` — the snapshot's job is to
    ADD the round's verdicts, and it can do that on top of an empty
    document. A torn report must not cost the round its verdicts.
    """
    report_path = Path(plan_dir) / _REPORT_FILENAME
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError):
        # ``ValueError`` covers ``json.JSONDecodeError``.
        return {}
    return data if isinstance(data, dict) else {}


def _verdict_vp_id(verdict: Any) -> str:
    """The VP a verdict belongs to (``""`` when it names none).

    The executor writes ``vp_id`` (``verification_executor.py``);
    older rows and hand-written fixtures use ``id``. Both spellings are
    accepted so a snapshot never drops a verdict over a key name.
    """
    if not isinstance(verdict, dict):
        return ""
    for key in ("vp_id", "id"):
        value = verdict.get(key)
        if value:
            return str(value)
    return ""


def _round_vp_ids(
    plan_dir: Path, extra: Optional[List[str]] = None,
) -> List[str]:
    """Every VP the round was working on, in plan order.

    The VP list in ``verification_plan.json`` is the universe; the
    in-flight sub-agents (``extra``) are folded in because a plan whose
    VP list has not been written yet can still have VPs running.
    """
    ids: List[str] = []
    seen = set()
    try:
        with open(
            Path(plan_dir) / _VP_PLAN_FILENAME, "r", encoding="utf-8",
        ) as f:
            plan_data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        plan_data = {}
    if isinstance(plan_data, dict):
        for vp in plan_data.get("verification_points") or []:
            vp_id = str(vp.get("id") or "") if isinstance(vp, dict) else ""
            if vp_id and vp_id not in seen:
                seen.add(vp_id)
                ids.append(vp_id)
    for vp_id in extra or []:
        if vp_id and vp_id not in seen:
            seen.add(vp_id)
            ids.append(vp_id)
    return ids


def _persist_round_snapshot(
    plan_id: str,
    plan_dir: Path,
    round_n: int,
    verdicts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Write the round's verdicts into ``verification_report.json``.

    Why this exists
    ---------------
    The report was written only when a round closed NORMALLY. A round
    that the operator stopped never wrote one, so the two consumers of
    the report saw nothing: the repair chain
    (``verification/verification_report_reader.py`` → ``extract_failed_vps``)
    found no failures to repair, and the plan's directory looked exactly
    like a plan that had never been verified. The stop threw the round's
    verdicts away.

    The snapshot closes that gap by writing the verdicts the round had
    already reached — the ones the executor persisted per VP — into the
    report, under the key the reader actually reads.

    It MERGES, it does not rewrite:

    * ``verification_results`` is merged by VP id. An entry that is
      already there keeps its place and its ``title`` (the report's own
      copy of the VP title is better than anything this function could
      invent) while its verdict is refreshed; a VP with no entry yet is
      appended.
    * ``execution_profile`` / ``rounds`` / ``requirement_deviations`` /
      ``round1_stable_vps`` are left exactly as they were — they belong
      to the round that wrote them, and the snapshot knows nothing about
      them.
    * ``_metadata`` is refreshed: ``current_round`` names the round the
      verdicts belong to, ``generated_at`` says when the snapshot was
      taken.

    The write is atomic (tempfile + ``os.replace``), so a reader never
    sees a half-written report — and a write that fails leaves the
    previous report intact rather than a truncated one.
    """
    report = _load_report(plan_dir)

    existing = report.get("verification_results")
    results: List[Any] = list(existing) if isinstance(existing, list) else []
    index_by_id: Dict[str, int] = {}
    for position, entry in enumerate(results):
        if isinstance(entry, dict) and entry.get("id"):
            index_by_id[str(entry["id"])] = position

    for verdict in verdicts or []:
        vp_id = _verdict_vp_id(verdict)
        if not vp_id:
            continue
        reasons = verdict.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        evidence = verdict.get("evidence")
        entry: Dict[str, Any] = {
            "id": vp_id,
            "status": str(verdict.get("status") or ""),
            "title": "",
            "actual_result": "; ".join(str(reason) for reason in reasons),
            "evidence": json.dumps(
                evidence if evidence is not None else {},
                ensure_ascii=False,
            ),
        }
        position = index_by_id.get(vp_id)
        if position is None:
            index_by_id[vp_id] = len(results)
            results.append(entry)
            continue
        previous = results[position]
        if isinstance(previous, dict):
            entry["title"] = str(previous.get("title") or "")
        results[position] = entry

    report["verification_results"] = results

    metadata = report.get("_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata["current_round"] = int(round_n)
    metadata["generated_at"] = datetime.now().isoformat()
    report["_metadata"] = metadata

    # ``verification_persistence.update_report`` injects an empty
    # ``execution_profile`` when a writer omits it, because the bridge
    # UI keys on the field being present. A snapshot that creates the
    # report from scratch must not produce a shape the other writer
    # would never produce.
    if not isinstance(report.get("execution_profile"), dict):
        report["execution_profile"] = {}

    from utils.atomic_io import atomic_write_json
    atomic_write_json(
        Path(plan_dir) / _REPORT_FILENAME, report, logger=_server.logger,
    )
    _server.logger.info(
        "[stop_verification] persisted round snapshot plan=%s round=%s "
        "verdicts=%d", plan_id, round_n, len(results),
    )
    return report


def _write_verification_checkpoint(
    plan_dir: Path,
    *,
    project_dir: Optional[str],
    round_n: int,
    verdicts: List[Dict[str, Any]],
    interrupted_tasks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Record the interrupted verification round in ``pause_state.json``.

    ``pause_state.write_pause_checkpoint`` owns the document shape (and
    the fixed key set of an interrupted-task entry); the verdict list is
    verification-specific, so it is added on top of the document the
    module wrote and the file is rewritten atomically. Two writes, one
    of them a superset: a crash between them leaves a checkpoint
    without the verdicts — still a valid checkpoint, just a poorer one.

    The verdict list is what makes the pause worth anything: it is the
    round's work, already done, that a resume must not redo.
    """
    from pause_state import checkpoint_path, write_pause_checkpoint
    from utils.atomic_io import atomic_write_json

    checkpoint = write_pause_checkpoint(
        plan_dir,
        kind="verification",
        project_dir=project_dir,
        reason="user_stopped",
        interrupted_tasks=interrupted_tasks,
        unfinished_tasks=[],
        verification_round=int(round_n),
    )
    checkpoint["verdicts"] = list(verdicts or [])
    atomic_write_json(
        checkpoint_path(plan_dir), checkpoint, logger=_server.logger,
    )
    return checkpoint


@router.post("/api/verification/{plan_id}/stop")
def stop_verification(plan_id: str):
    """Stop the running verification round and leave a resumable pause behind.

    The order is load-bearing:

    1. mark the routing row (verification_running -> verification) and the
       plan_verification row (loop_stopped / user_stopped);
    2. read the round's verdicts before the connection is released -- they
       live only in that row;
    3. snapshot the interrupted round (_persist_round_snapshot);
    4. write the pause checkpoint: _write_verification_checkpoint records
       the round, its verdicts and the interrupted VPs in pause_state.json;
    5. cancel and kill the in-flight VP sub-agents, then reap their services.

    The response is a pause, not a discard: it reports paused, round and
    interrupted_vp_count, and a later /start reads that checkpoint through
    _resolve_start_round and resumes this round. The plan is left stopped.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, {"error": "Plan not found", "detail": "The requested plan does not exist."})

    # VP-020: archived plans are read-only, and this must be decided
    # before the "not running" precondition below — otherwise an
    # archived plan with no live round reports 400 instead of 410 and
    # the operator reads it as an ordinary state error.
    archived = _archived_plan_response(plan_dir)
    if archived is not None:
        return archived

    from state_machine.repositories.routing_repository import ConflictError, PlanNotFoundError
    from state_machine.repositories.verification_repository import VerificationRepository

    # 2026-08-25 audit: prefer the thread-owned connection so the
    # orchestrator's ``verif_repo`` stays usable. Fall back to a
    # fresh short-lived connection if no thread-owned one exists
    # (server restart between start and stop).
    _stop_state = _server._verification_state.get(plan_id)
    owned_conn = bool(_stop_state and _stop_state.get("_state_db_conn") is not None)

    # 2026-09-14: "not running" is decided by the routing CAS below,
    # not by ``_verification_state``. A pre-SQLite version of this
    # endpoint answered 400 here; the SQLite rewrite made the CAS the
    # single decider, so a plan whose stage is not
    # ``verification_running`` gets
    # ``409 {"error": "conflict", "reason": "stage_mismatch"}``.
    # Re-adding the 400 was tried and reverted — it masked the conflict
    # signal VP-018 pins for every write consumer. See
    # ``tests/integration/api/test_api_error_matrix.py``
    # (``verify_stop x normal_plan`` / ``verify_stop x cas_predicate_fail``
    # → 409) and ``state_machine/tests/integration/test_verification_routes.py``
    # (``test_stop_verification_409_when_not_running``).

    try:
        conn, routing, verification = _server._open_verification_state()
    except (sqlite3.OperationalError, OSError) as exc:
        raise HTTPException(404, {"error": "Plan not found", "detail": str(exc)})

    if owned_conn:
        conn = _stop_state["_state_db_conn"]
        from state_machine.repositories.routing_repository import RoutingRepository
        verification = VerificationRepository(conn)
        routing = RoutingRepository(conn)

    try:
        # 2026-10-21: the source set is the phases a verification
        # round can actually be *in flight* at — not just
        # ``verification_running``. A round that reached
        # ``verification_repairing`` still has a running
        # ``plan_verification`` row, and the operator's stop is
        # exactly as legitimate there. With only
        # ``verification_running`` the CAS raises ``ConflictError`` on
        # that phase and the endpoint answers 409, leaving the
        # routing row parked where it was; ``/start`` (:597-614) does
        # not accept it as a source either, so it answers 409
        # ``stage_mismatch`` too. Both doors refuse, so the plan was
        # stuck forever — unrecoverable through the API. (Measured, not
        # inferred: ``tests/integration/api/
        # test_verification_stop_from_repair_phase.py`` pins both.) The
        # same set is already used by ``force_verification_terminal``
        # (:3070) and by the dead-end rollback in
        # ``verification_loop.py`` (:2993), so this is the recognised
        # vocabulary for "verification work stopped halfway", not a new
        # one. The TARGET phase stays ``verification``: the round is
        # paused, not finished. ``/start``'s own source whitelist is
        # deliberately left alone — a plan that is genuinely still
        # running must not become re-enterable.
        #
        # 2026-10-22: the source set no longer spells out
        # ``verification_rerunning`` — that phase left the vocabulary
        # (it was a dead state). A row that still holds the historical
        # value is matched through the alias table, so a stop on such a
        # row still lands; the literal itself is not in the source set
        # any more.
        from plan_state import LEGACY_PHASE_ALIASES

        _source_phases = ("verification_running", "verification_repairing")
        _row = routing.current(plan_id) or {}
        _row_phase = _row.get("current_phase")
        if (
            isinstance(_row_phase, str)
            and _row_phase in LEGACY_PHASE_ALIASES
        ):
            _source_phases = _source_phases + (_row_phase,)

        # 2026-10-22: the REPAIR EXECUTOR is a separate subprocess
        # (``cli.py --recover``) and this stop used to leave it
        # running: the hard kill below only reaches the VERIFICATION
        # sub-agents registered in ``sub_agent_registry``. So a stop
        # request parked the plan at ``verification`` while the repair
        # kept running — the plan kept changing code after the stop was
        # asked for.
        #
        # The repair executor is identified by ``_source ==
        # "repair_execution"`` on the shared ``_execution_state``
        # record; the MAIN executor never sets ``_source``, so an
        # ``executing`` plan with any other source is NOT a repair
        # execution and keeps its old behaviour (409 — this stop only
        # ever owned verification work).
        _exec_state = _server._execution_state.get(plan_id) or {}
        _repair_execution_stop = (
            _row_phase == "executing"
            and _exec_state.get("_source") == "repair_execution"
        )
        if _repair_execution_stop:
            # The plan is parked in the REPAIR half of the loop, not
            # in the verification half: the target phase is
            # ``verification_repairing`` (the repair tasks are waiting
            # to be confirmed / resumed), NOT ``verification`` (which
            # means "a verification round was paused").
            routing.try_mark_phase(
                plan_id, ("executing",), "verification_repairing",
            )
        else:
            routing.try_mark_phase(
                plan_id,
                _source_phases,
                "verification",
            )
            verification.mark_stopped(plan_id, "user_stopped")
    except ConflictError as exc:
        try:
            conn.rollback()
        except sqlite3.OperationalError:
            pass
        if not owned_conn:
            conn.close()
        return JSONResponse(status_code=409, content={"error": "conflict", "reason": _conflict_reason(exc)})
    except (PlanNotFoundError, KeyError):
        if not owned_conn:
            conn.close()
        return JSONResponse(status_code=404, content={"error": "Plan not found", "detail": "The requested plan does not exist."})

    if _repair_execution_stop:
        # 2026-10-22: what is being stopped here is the REPAIR
        # EXECUTOR, not a verification round. The round already ended
        # (it failed and produced the repair tasks); the executor
        # subprocess is what is still running.
        #
        # So this branch does NOT write a verification checkpoint: a
        # checkpoint says "this verification round was interrupted,
        # resume it", and no round was interrupted here. A stale
        # checkpoint would make the NEXT ordinary ``/start`` resume
        # that old round instead of starting a new one.
        #
        # The cancel flag is requested BEFORE the kill: the killed
        # executor fires ``on_complete`` immediately, and that
        # callback re-enters verification unless the flag is already
        # set.
        try:
            import verification_cancel
            verification_cancel.request_cancel(plan_id)
        except Exception:  # noqa: BLE001
            _server.logger.exception(
                "[stop_verification] cancel flag failed plan=%s", plan_id,
            )
        killed_pid = _sigterm_repair_executor(plan_id)
        _server.logger.warning(
            "[stop_verification] stopped the repair executor plan=%s "
            "pid=%s", plan_id, killed_pid,
        )
        _repair_row = verification.current(plan_id) or {}
        round_n = int(_repair_row.get("round") or 0)
        if not owned_conn:
            conn.close()
        _server._reap_managed_services(plan_id, plan_dir, "verification_stopped")
        return {
            "stopped_at": datetime.now().isoformat(),
            "reason": "user_stopped_at_repair_execution",
            "current_round": round_n,
            "status": "stopped",
            "paused": True,
            "round": round_n,
            "interrupted_vp_count": 0,
        }

    # 2026-10-20 — read the round BEFORE the connection is closed.
    # The row is the only place the round's verdicts live: the executor
    # appends them per VP, and nothing else keeps them. A stop that
    # does not read them here throws the round's work away (this is
    # what made a stopped round indistinguishable from a plan that had
    # never been verified).
    row = verification.current(plan_id) or {}
    verdicts = [
        verdict for verdict in (row.get("verdicts") or [])
        if isinstance(verdict, dict)
    ]

    # Only close the fallback connection; thread-owned conn lives
    # until the verification thread terminates.
    if not owned_conn:
        conn.close()

    state = _server._verification_state.get(plan_id)
    if state:
        state.update({"verification_status": "loop_stopped", "stop_reason": "user_stopped", "updated_at": datetime.now().isoformat()})

    # The round the stop interrupted. The DB row is authoritative (it
    # survives a backend restart); the in-memory state is the fallback
    # for a plan whose row has no round yet.
    round_n = int(row.get("round") or 0) or int(
        (state or {}).get("verification_round") or 0
    )

    # The in-flight VPs are read BEFORE the snapshot: the stop's own
    # hard kill removes them, and the snapshot must describe the round
    # as it was when the operator pressed stop.
    try:
        _running_vp_ids = [
            getattr(_h, "vp_id", "") or ""
            for _h in _server.sub_agent_registry.all_handles(plan_id)
        ]
    except Exception:  # noqa: BLE001
        _running_vp_ids = []

    # The VPs the stop cut short: every VP of the round that never
    # reached a verdict. This is what a resume still owes, so it is
    # what the checkpoint records.
    _judged_vp_ids = {vid for vid in (_verdict_vp_id(v) for v in verdicts) if vid}
    _interrupted_vp_ids = [
        vp_id for vp_id in _round_vp_ids(plan_dir, _running_vp_ids)
        if vp_id not in _judged_vp_ids
    ]

    # 2026-10-20 — the snapshot is written BEFORE the process cleanup
    # below. The cleanup kills the in-flight sub-agents, and a verdict
    # that lands after this point would never reach the report; a
    # snapshot taken after the kill would describe a round that no
    # longer exists.
    _persist_round_snapshot(plan_id, plan_dir, round_n, verdicts)

    # Where the round was verifying. Best-effort: the checkpoint is
    # still a valid checkpoint without it, and a project_dir lookup
    # that fails (a state.db that cannot be opened) must not take the
    # stop down with it.
    _project_dir: Optional[str] = None
    try:
        _resolved_project_dir = _server._get_project_dir(plan_id)
    except Exception:  # noqa: BLE001
        _resolved_project_dir = None
    if _resolved_project_dir:
        _project_dir = str(_resolved_project_dir)

    _write_verification_checkpoint(
        plan_dir,
        project_dir=_project_dir,
        round_n=round_n,
        verdicts=verdicts,
        interrupted_tasks=[
            {
                "task_id": vp_id,
                "status": "interrupted",
                "phase": "verification",
                "session_id": None,
                "strategy": "verification_round_resume",
                "reason": "user_stopped",
            }
            for vp_id in _interrupted_vp_ids
        ],
    )

    # 2026-09-15 — a stop must actually STOP the work.
    #
    # The CAS above only parks the routing row; the verification thread
    # kept running: the in-flight VP sub-agent stayed alive (tokens + CPU)
    # and the thread carried on into the next round, re-stamping the stage
    # the operator had just parked (which then blocked the next ``/start``
    # with ``409 stage_mismatch``). Two layers close that gap:
    #
    #   1. hard kill — SIGTERM the registered sub-agent subprocesses and
    #      sweep the orphaned pytest/bash children they disowned;
    #   2. cooperative cancel — the executor skips every not-yet-started
    #      VP (and reports an interrupted attempt as SKIPPED), and the
    #      auto-loop exits instead of running judgment → repair → next
    #      round. ``/start`` clears the flag.
    try:
        import verification_cancel
        verification_cancel.request_cancel(plan_id)
    except Exception:  # noqa: BLE001
        _server.logger.exception(
            "[stop_verification] cancel flag failed plan=%s", plan_id,
        )
    try:
        _server._cleanup_dead_verification_processes(
            plan_id, [vp for vp in _running_vp_ids if vp],
        )
        _server.logger.warning(
            "[stop_verification] killed in-flight verification work "
            "plan=%s vps=%s", plan_id, _running_vp_ids,
        )
    except Exception:  # noqa: BLE001
        _server.logger.exception(
            "[stop_verification] process kill failed plan=%s", plan_id,
        )

    # 2026-09-18 C3: the round is over, so the services it started are
    # no longer needed. This is an operator-initiated exit that does
    # NOT go through ``_run_auto_verification_loop`` (which joins the
    # loop only after the next checkpoint), so it needs its own reap.
    _server._reap_managed_services(plan_id, plan_dir, "verification_stopped")

    return {
        "stopped_at": datetime.now().isoformat(),
        "reason": "user_stopped",
        "current_round": round_n,
        # 2026-10-20 — the stop is a PAUSE: the round, the verdicts it
        # reached and the VPs it cut short are on disk (see
        # ``_persist_round_snapshot`` / ``_write_verification_checkpoint``),
        # so the response says so. ``paused`` is the flag a caller
        # polls for; ``round`` and ``interrupted_vp_count`` say what
        # the resume would pick up.
        "status": "stopped",
        "paused": True,
        "round": round_n,
        "interrupted_vp_count": len(_interrupted_vp_ids),
    }


@router.post("/api/verification/{plan_id}/force_terminal")
def force_verification_terminal(plan_id: str):
    """Operator escape hatch — synchronously CAS a plan out of any
    ``verification_*`` stage (running / rerunning / repairing) into
    ``failed`` and persist the verification_status update.

    2026-09-06:
    Use this for plans that the new watchdog hasn't yet recovered
    (e.g. the 12-hour-stuck ``2026-09-04 plan``
    before the backend picks up the new code). Returns 200 on success
    with the new stage in the response body so callers can verify the
    transition before issuing follow-up requests; returns 409 if the
    plan is already in an incompatible stage (already terminal, or in a
    non-verification stage like ``executing``); returns 404 if the plan
    does not exist in ``plan_routing``.

    Side effects:
      1. ``RoutingRepository.try_mark_phase`` SQL CAS — fires
         ``KIND_PLAN_PHASE_CHANGED`` on the state bus.
      2. ``_persist_verification_terminal`` — fires ``KIND_PLAN_CLOSED``
         via ``VerificationRepository.complete_round``.
      3. Feishu notifier (already subscribed to both events) rebuilds
         the card within seconds: status flips from "still verifying"
         to "failed".

    The endpoint is synchronous on purpose — operators who manually
    call this for a stuck plan want to see the change in
    ``GET /api/plan/{id}/summary`` immediately, not after a background
    task completes. The CAS itself is one SQL UPDATE so latency is
    dominated by SQLite write time (sub-millisecond on local disk).
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate as migrate_schema
    from state_machine.repositories.routing_repository import (
        ConflictError as RoutingConflictError,
        PlanNotFoundError as RoutingPlanNotFoundError,
        RoutingRepository,
    )

    conn = open_db(_server._state_db_path())
    try:
        migrate_schema(conn)
        repo = RoutingRepository(conn)
        try:
            # 2026-10-22: ``verification_rerunning`` left the phase
            # vocabulary (it was a dead state), so it is no longer
            # spelled out here. A row that still holds the historical
            # value is matched through the alias table, so the escape
            # hatch still reaches it.
            from plan_state import LEGACY_PHASE_ALIASES

            _source_phases = (
                "verification_running",
                "verification_repairing",
            )
            _raw_phase = (repo.current(plan_id) or {}).get("current_phase")
            if (
                isinstance(_raw_phase, str)
                and _raw_phase in LEGACY_PHASE_ALIASES
            ):
                _source_phases = _source_phases + (_raw_phase,)
            repo.try_mark_phase(
                plan_id,
                _source_phases,
                "failed",
            )
        except RoutingPlanNotFoundError:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "plan_not_found",
                    "plan_id": plan_id,
                    "detail": "No plan_routing row for this plan_id.",
                },
            )
        except RoutingConflictError as exc:
            # 409 means the plan is not currently in any of the
            # verification_* source-stages — could already be terminal,
            # could be in executing / ready, etc. Don't transition
            # anything. Caller can decide what to do based on
            # current_phase in the response.
            return JSONResponse(
                status_code=409,
                content={
                    "error": "stage_mismatch",
                    "reason": _conflict_reason(exc),
                    "current_phase": exc.current_phase,
                    "plan_id": plan_id,
                },
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Persist the verification_status column + fire KIND_PLAN_CLOSED.
    # ``_persist_verification_terminal`` is best-effort — it has its
    # own try/except around the routing CAS (which we already won
    # above), so on the second CAS attempt (it'll see
    # failed → not in source-phases) it just swallows the
    # ConflictError. ``complete_round`` will still fire
    # KIND_PLAN_CLOSED so the Feishu card updates regardless.
    _server._persist_verification_terminal(plan_id, "failed", "user_force_terminal")

    # Update in-memory state so /api/verification/{id}/status reflects it
    # on the very next call (without waiting for the next heartbeat
    # tick to re-read the DB).
    state = _server._verification_state.get(plan_id)
    forced_at = datetime.now().isoformat()
    if state:
        state["verification_status"] = "failed"
        state["stop_reason"] = "user_force_terminal"
        state["updated_at"] = forced_at
        state["ended_at"] = forced_at

    # Update the ``plan_routing.verification`` JSON column that
    # ``/api/plan/{id}/summary`` reads via ``PlanState._sqlite_row_to_state``.
    # Without this, ``state.verification.status`` would still show
    # ``running`` even though ``plan_routing.stage`` is now
    # ``failed`` — the Feishu notifier (which reads via
    # ``/api/plan/{id}/summary``) would then render a stale "still
    # verifying" card despite the CAS having succeeded. See the helper's
    # docstring for why we use a raw SQL UPDATE instead of
    # ``PlanState._save_state`` (the latter would clobber ``stage``).
    _server._update_plan_state_to_terminal(plan_id, "user_force_terminal")

    # Record in watchdog stats so the operator can see this was a
    # user-driven force-terminal, distinct from the three automatic
    # stop_reasons emitted by ``_lazy_check_verification``.
    _server._record_watchdog_action(plan_id, "user_force_terminal")

    return {
        "plan_id": plan_id,
        "current_phase": "failed",
        "stop_reason": "user_force_terminal",
        "forced_at": forced_at,
    }

