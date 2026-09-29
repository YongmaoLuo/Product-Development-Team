"""Plan inventory: listing, status, summary, usage, task rows.

Extracted from ``server.py`` on 2026-09-25. See ``routes/phases.py`` for the
late-binding rule that governs every module in this package.
"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Dict, List, Optional, Tuple, Any
from fastapi import HTTPException, Request
from pathlib import Path
from datetime import datetime
import json
import sqlite3

# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server

router = APIRouter()


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


def _read_disk_task_ids(plan_id: str) -> Optional[set]:
    """Task ids in ``plans/<id>/tasks.json``; ``None`` when unreadable.

    ``None`` and ``set()`` mean different things: ``None`` is "no static
    DAG to compare against, so do not judge orphans", ``set()`` is "the
    static DAG is empty, so every DB row is an orphan".
    """
    try:
        data = json.loads((_server._plan_dir(plan_id) / "tasks.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
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

    Two things are not work and must not reach ``total``:

    * ``superseded`` rows — the dispatcher refuses to schedule them.
    * **terminal** rows that exist only in ``plan_tasks`` — "DB-only
      orphans". The card already drops these (``drop_terminal_db_orphans``);
      counting them here would put the status object's ``total`` out of
      step with the progress bar rendered right below the same header.
    """
    counts = {key: 0 for key in _server._STATUS_TASK_KEYS}
    counts["total"] = 0
    for task_id, row in rows.items():
        status = str(row.get("status") or "").strip().lower() or "pending"
        if status == "superseded":
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


def _build_plan_status(plan_id: str) -> Any:
    """Assemble the one plan-status snapshot every reader renders from.

    One read, one answer. The stores that used to answer separately —
    ``plan_routing.current_phase``, ``plan_routing.verification``,
    ``plan_verification``, ``plan_tasks``, and the two in-memory
    liveness records — are read here, together, and reconciled once.
    See :mod:`plan_status` for why that matters.
    """
    from dataclasses import replace

    from plan_status import PlanStatus, find_divergences

    phase = ""
    v_status = ""
    v_round = 0
    v_max = 0
    v_stop: Optional[str] = None
    try:
        conn, routing, verification = _server._open_verification_state()
        try:
            route = routing.current(plan_id)
            record = verification.current(plan_id)
        finally:
            conn.close()
        if route:
            phase = str(route.get("current_phase") or "")
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

    status_snapshot = PlanStatus(
        plan_id=plan_id,
        phase=phase,
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
    )

    divergences = find_divergences(status_snapshot)
    if divergences:
        status_snapshot = replace(status_snapshot, divergences=divergences)
        _log_divergences(status_snapshot)
    return status_snapshot


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
    """
    if not _server._plan_dir(plan_id).exists():
        raise HTTPException(404, "Plan not found")
    return _plan_status_payload(plan_id)


def _plan_status_payload(plan_id: str) -> Dict[str, Any]:
    """``PlanStatus`` + the three detail sections, read once.

    The detail sections are read through the same handler functions the
    standalone endpoints serve, so there is one implementation of each;
    a failure in any one of them degrades that section to ``None``
    rather than failing the whole snapshot (an operator asking "where is
    this plan" should still get an answer).
    """
    payload = _build_plan_status(plan_id).to_dict()
    for key, handler in (
        ("summary", _plan_summary_section),
        ("execution", _server.get_execution_progress),
        ("verification", _server.get_verification_progress),
    ):
        try:
            payload[key] = handler(plan_id)
        except Exception:
            _server.logger.exception(
                "[plan_status] %s section failed plan=%s", key, plan_id,
            )
            payload[key] = None
    return payload


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


@router.get("/api/system/active")
def get_active_tasks():
    """
    Quick snapshot of how many the workflow tasks are currently running.

    Inspects:
      - _execution_state: per-plan execution subprocess status
      - _verification_state: per-plan verification cycle status
      - Heartbeat check on subprocess poll() to detect "running" state

    Returns:
      {
        "execution_running": <int>,
        "verification_running": <int>,
        "total_active": <int>,
        "details": [
          {"plan_id": ..., "type": "execution"|"verification", "status": ..., ...},
          ...
        ]
      }
    """
    details: list = []
    # Hot-path optimization B-03: short-circuit when both state dicts are
    # empty. This is the common case in a freshly-started server and
    # after every completed cycle; iterating two dicts + building two
    # detail dicts to return "0 / 0 / 0 / []" is wasted work on every
    # supervisor poll.
    if not _server._execution_state and not _server._verification_state:
        return {
            "execution_running": 0,
            "verification_running": 0,
            "total_active": 0,
            "details": [],
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
    }


@router.get("/api/plans")
def list_plans(
    request: Request,
    include_terminal: bool = False,
):
    """List all plans.

    By default, plans in terminal states (``stopped``, ``completed``,
    ``failed``, ``verification_loop_stopped``, ``verification_passed``,
    ``verification_failed``) are filtered out — they are historical
    artifacts that no longer need a sidebar entry. Pass
    ``?include_terminal=true`` to see the full list (e.g. for
    audit / cleanup scripts).

    Reads
    -----
    * RoutingRepository.list_all(data_dir=PLANS_DIR) — single
      aggregate call returning BOTH new (SQLite) and archived
      (directory-derived) rows. Archived rows carry
      ``archived=True`` and DO NOT carry stage / current_phase
      fields.
    * ExecutionRepository.snapshot_for_list(plan_ids) — bulk
      enrichment for the SQLite (new) rows; archived rows have no
      execution rows so they get ``None``.

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
    """
    cached = _server._read_plans_cache(include_terminal)
    if cached is not None:
        return cached
    # Fast-path: empty PLANS_DIR — skip the state-machine open + DB scan +
    # bulk snapshot entirely. A fresh install with no plans (or a runtime
    # where PLANS_DIR hasn't been populated yet) hits this on every
    # sidebar poll; the cache-miss path would otherwise pay for an
    # empty RoutingRepository.list_all() + ExecutionRepository.snapshot
    # even though both return no rows.
    if not _server.PLANS_DIR.exists():
        empty: List[Dict[str, Any]] = []
        _server._write_plans_cache(include_terminal, empty)
        return empty
    result = _list_plans_impl(request, include_terminal)
    _server._write_plans_cache(include_terminal, result)
    return result


def _list_plans_impl(request: Request, include_terminal: bool) -> List[Dict[str, Any]]:
    """Compute the ``/api/plans`` payload from disk.

    This is the cache-miss path; ``list_plans`` wraps it with the
    TTL + explicit-invalidation layer above.

    2026-09-23: the state-machine connection is opened here and
    released in a ``finally``, so the descriptor's lifetime does not
    depend on which of the 200 lines below happens to return. The body
    lives in :func:`_list_plans_body` and receives the handle instead
    of opening its own.
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
) -> List[Dict[str, Any]]:
    """Build the plan list using an already-open state-machine handle.

    ``sm`` is injected rather than opened here: the caller owns its
    lifetime. It may legitimately be ``None`` (no ``state.db`` yet), in
    which case the legacy directory-listing path below still answers.
    """
    terminal_phases = {
        "completed", "failed", "stopped",
        "verification_passed",
        "verification_failed",
    }

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

    # Bulk-fetch execution rows for every non-archived plan_id.
    new_plan_ids = [
        r["plan_id"] for r in routing_rows if not r.get("archived")
    ]
    execution_by_plan: Dict[str, Optional[Dict[str, Any]]] = {}
    if sm is not None and new_plan_ids:
        _, _, execution, _, _ = sm
        execution_by_plan = execution.snapshot_for_list(new_plan_ids)

    for row in routing_rows:
        plan_id = row["plan_id"]
        plan_dir = _server._plan_dir(plan_id)
        is_archived = bool(row.get("archived"))

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
            status = "new"
            if has_tasks:
                status = "ready"
            elif has_review:
                status = "reviewed"
            elif has_prd:
                status = "prd"
            elif has_interview:
                status = "interviewed"

            # ``current_phase`` and ``flags`` used to live in the
            # legacy ``plan_state.json`` file; both are now in the
            # state-machine SQLite row.  Resolve them via the
            # ExecutionRepository's bulk-snapshot (already populated
            # above) rather than reading the JSON file directly.
            exec_row = execution_by_plan.get(plan_id)
            if isinstance(exec_row, dict):
                current_phase = exec_row.get("current_phase") or status
            else:
                current_phase = status
            if not include_terminal and current_phase in terminal_phases:
                continue

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

            if (
                exec_status == "running"
                and isinstance(exec_pid, int)
                and exec_pid > 0
            ):
                status = "running"
            else:
                exec_pid = None

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
        plans.append(entry)
    return plans


def _seed_plan_routing_phase(plan_id: str, phase: str) -> None:
    """INSERT OR IGNORE a ``plan_routing`` row with ``current_phase=phase``.

    Task #3.9: the interview phase is now tracked in
    ``plan_routing`` so the routing table is the single source of
    truth for the current workflow phase. Before this change,
    ``PlanState._load_state`` fell through to ``_default_state`` →
    ``_infer_phase`` and derived the phase from file-existence
    (e.g. ``interview.json`` present ⇒ ``interview_complete``),
    which mis-classified a fresh plan whose first
    ``interviewer.start()`` call had already written
    ``interview.json``.

    The call is idempotent (``INSERT OR IGNORE``) so a re-run on
    a plan that has already advanced past ``phase`` does not
    regress the row. Failures are non-fatal — the legacy file
    fallback still works.

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
            # INSERT OR IGNORE: only insert if the row does not
            # already exist. An existing row whose ``current_phase``
            # is past ``phase`` (e.g. ``prd_review``) is NOT
            # overwritten — this keeps a restart safe.
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO plan_routing "
                    "(plan_id, current_phase, version, updated_at) "
                    "VALUES (?, ?, 0, ?)",
                    (
                        plan_id,
                        phase,
                        datetime.now().isoformat(),
                    ),
                )
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
                    _TERMINAL = {"completed", "failed", "skipped", "superseded"}
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
    if sm is not None:
        _, _, execution_repo, _verification_repo, artifact_repo = sm
        try:
            artifacts_manifest = artifact_repo.list_for_plan(plan_id)
        except Exception:
            artifacts_manifest = []
        try:
            execution_row_from_sm = execution_repo.summary(plan_id)
        except Exception:
            execution_row_from_sm = None

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

