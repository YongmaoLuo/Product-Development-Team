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
from pathlib import Path
from datetime import datetime
import json
import sqlite3
import threading

# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server

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
    if _live_status in ("running", "verification_running"):
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

    from state_machine.repositories.routing_repository import ConflictError, PlanNotFoundError

    conn = None
    try:
        conn, routing, verification = _server._open_verification_state()
    except (sqlite3.OperationalError, OSError) as exc:
        raise HTTPException(404, {"error": "Plan not found", "detail": str(exc)})

    try:
        current = verification.current(plan_id)
        next_round = (current.get("round", 0) if current else 0) + 1

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
            ),
            "verification_running",
        )
        verification.init_round(plan_id, round_n=next_round, max_rounds=max_rounds)
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
            coding_tool = _server.create_coding_tool(tool_type, cwd=str(project_dir), scene="verification")
            max_parallel = req.max_parallel if req and req.max_parallel is not None else _server._resolve_max_parallel(coding_tool)
            # Wire the SQLite VerificationRepository so the agent can
            # persist Phase 1 execution envelopes to
            # ``plan_verification.execution_results`` (task #3.5). The
            # repo was already opened earlier in this function via
            # ``_open_verification_state``.
            orch = _server.VerificationOrchestrator(
                plan_dir, project_dir, coding_tool=coding_tool,
                max_parallel=max_parallel, verif_repo=verification,
            )
            _server._verification_state[plan_id]["orchestrator"] = orch
            ps = _server.PlanState(plan_dir)
            ps.set_verification_max_rounds(max_rounds)
            current = ps.get_current_phase()
            if current not in (
                "verification", "verification_running",
                "verification_repairing", "verification_rerunning",
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
            report = orch.start_verification_cycle(
                round_number=next_round,
                force=force_verify,
                resume=(next_round > 1),
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
    verification_thread = threading.Thread(
        target=_server._run_in_plan_ctx, args=(plan_id, _run), daemon=True
    )
    verification_thread.start()
    _server._verification_state[plan_id]["thread"] = verification_thread
    return {"plan_id": plan_id, "status": "started"}


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
    ``plan_tasks``               pending ``RP-*`` → ``superseded`` (never
                                 deleted: task data may only be voided or
                                 created, never rewritten)
    ``plans/<id>/`` on disk      per-round verification artifacts, when
                                 ``clear_artifacts``; ``verification_plan.json``
                                 only when ``clear_verification_plan``
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
        superseded: List[str] = []
        if req.supersede_repair_tasks:
            pending = _server._get_pending_repair_tasks(plan_id)
            if pending:
                ids = [t.get("id") for t in pending if t.get("id")]
                with conn:
                    conn.executemany(
                        "UPDATE plan_tasks SET status = 'superseded', "
                        "failure_reason = ?, updated_at = ? "
                        "WHERE plan_id = ? AND task_id = ?",
                        [
                            (
                                "[reset] superseded by verification reset — "
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

    # --- current_phase mirror --------------------------------------------
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository as _ExecRepo,
        )

        _conn = _open_db(_server._state_db_path())
        try:
            _migrate(_conn)
            _ExecRepo(_conn).update_phase(
                plan_id, "verification", create_if_missing=True,
            )
        finally:
            _conn.close()
        summary["cleared"]["current_phase_mirror"] = True
    except Exception:  # noqa: BLE001 — display-only mirror
        _server.logger.exception("[reset] current_phase mirror failed plan=%s", plan_id)
        summary["cleared"]["current_phase_mirror"] = False

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
    """
    logs_dir = plan_dir / "logs"
    if not logs_dir.exists():
        return {}
    log_files = sorted(
        logs_dir.glob("verification_*.log"),
        key=lambda p: p.stat().st_mtime,
    )
    if not log_files:
        return {}
    activity: Dict[str, Dict[str, str]] = {}
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
                if et not in ("vp_start", "vp_complete"):
                    continue
                vp_id = entry.get("verification_point_id")
                ts = entry.get("timestamp")
                if not isinstance(vp_id, str) or not isinstance(ts, str):
                    continue
                slot = activity.setdefault(vp_id, {"start": "", "complete": ""})
                slot["start" if et == "vp_start" else "complete"] = ts
    except OSError:
        return {}
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


def _verification_round_activity(
    plan_dir: Path,
    running: set,
    activity: Dict[str, Dict[str, str]],
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
    about which VPs are live.
    """
    logs_dir = plan_dir / "logs"
    log_files = (
        sorted(logs_dir.glob("verification_*.log"), key=lambda p: p.stat().st_mtime)
        if logs_dir.exists() else []
    )

    if running:
        # A VP is genuinely in flight. The caller already names it via
        # ``current_vp``; stay out of the way rather than emitting a
        # second, vaguer line about the same thing.
        return None

    if not log_files:
        return {"kind": "planning", "label": _VERIFICATION_ACTIVITY_LABELS["planning"],
                "vp_id": None, "since": None}

    last_event_type: Optional[str] = None
    last_vp_id: Optional[str] = None
    last_ts: Optional[str] = None
    started_any = False
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
                if et == "vp_start":
                    started_any = True
                if et == "judgment_heartbeat" and isinstance(ts, str):
                    last_event_type = "judgment_heartbeat"
                    last_ts = ts
                    last_vp_id = vp_id if isinstance(vp_id, str) and vp_id else None
                elif et in ("vp_start", "vp_complete") and isinstance(ts, str):
                    last_event_type = et
                    last_ts = ts
                    last_vp_id = vp_id if isinstance(vp_id, str) and vp_id else None
    except OSError:
        return None

    if last_event_type == "judgment_heartbeat":
        label = _VERIFICATION_ACTIVITY_LABELS["judging"]
        if last_vp_id:
            label += f" `[{last_vp_id}]`"
        return {"kind": "judging", "label": label,
                "vp_id": last_vp_id, "since": last_ts}
    if not started_any:
        return {"kind": "planning", "label": _VERIFICATION_ACTIVITY_LABELS["planning"],
                "vp_id": None, "since": last_ts}
    # Every VP in the round has completed and no judgment has started:
    # the orchestrator is aggregating verdicts into the report.
    return {"kind": "summarizing",
            "label": _VERIFICATION_ACTIVITY_LABELS["summarizing"],
            "vp_id": None, "since": last_ts}


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

    completed_vps: List[str] = list(progress.get("completed_vps") or [])
    failed_vps: List[str] = list(progress.get("failed_vps") or [])
    skipped_vps: List[str] = list(progress.get("skipped_vps") or [])

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
    _latest_activity = _read_latest_round_vp_activity(plan_dir)
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
                    _ar = (_vr.get("actual_result") or "").strip()
                    if _ar:
                        failure_details_by_id[_vid] = {
                            "actual_result": _ar[:500],
                            "evidence_tail": (
                                (_vr.get("evidence") or "")[:300]
                            ),
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
        # Attach failure details for failed VPs (matches the
        # ``failure_details_by_id`` dict above).
        if status == "failed":
            details = failure_details_by_id.get(vp_id, {})
            if details.get("actual_result"):
                entry["actual_result"] = details["actual_result"]
            if details.get("evidence_tail"):
                entry["evidence_tail"] = details["evidence_tail"]
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
                plan_dir, running_vp_ids, _latest_activity,
            )
        except Exception:
            _server.logger.exception(
                "[verification_progress] round_activity failed plan=%s", plan_id,
            )

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


@router.post("/api/verification/{plan_id}/stop")
def stop_verification(plan_id: str):
    """Stop a verification cycle by CASing the routing row to idle."""
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
        routing.try_mark_phase(plan_id, ("verification_running",), "verification")
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
    # Only close the fallback connection; thread-owned conn lives
    # until the verification thread terminates.
    if not owned_conn:
        conn.close()

    state = _server._verification_state.get(plan_id)
    if state:
        state.update({"verification_status": "loop_stopped", "stop_reason": "user_stopped", "updated_at": datetime.now().isoformat()})

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
        _running_vp_ids = [
            getattr(_h, "vp_id", "") or ""
            for _h in _server.sub_agent_registry.all_handles(plan_id)
        ]
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
        "current_round": (state or {}).get("verification_round", 0),
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
            repo.try_mark_phase(
                plan_id,
                (
                    "verification_running",
                    "verification_rerunning",
                    "verification_repairing",
                ),
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

