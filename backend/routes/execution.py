"""Execution lifecycle: start / status / progress / stop / artifacts.

Extracted from ``server.py`` on 2026-09-25. See ``routes/phases.py`` for the
late-binding rule that governs every module in this package.
"""

from __future__ import annotations

from utils.atomic_io import atomic_write_json
from fastapi import APIRouter

from typing import Dict, List, Optional, Tuple, Any
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path
from datetime import datetime
from collections import deque
import json
import os
import sqlite3
import subprocess
import sys
import threading

# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server

router = APIRouter()


def _recover_execution_states(plans_dir: Path) -> None:
    """On startup, scan plan directories and restore execution state.

    - Read the plan_execution row from the state-machine SQLite
      (ExecutionRepository.summary) into _execution_state.
    - For 'running' entries, probe PID with os.kill(pid, 0).
    - Dead or missing PID → mark failed + sync plan_state.
    - Corrupt / missing row → skip with a log warning.
    """
    if not plans_dir.exists():
        return
    # Open a single SQLite connection for the lifetime of the
    # recovery scan; the state-machine layer is the canonical source
    # of execution metadata (architecture decision point 5).
    sm = _server._open_state_machine()
    if sm is None:
        # Fresh install — no state.db yet — nothing to recover.
        return
    _conn, _routing_repo, execution_repo, _verification_repo, _artifact_repo = sm
    try:
        for plan_dir in plans_dir.iterdir():
            if not plan_dir.is_dir():
                continue
            plan_id = plan_dir.name
            try:
                row = execution_repo.summary(plan_id)
            except Exception:
                _server.logger.warning(
                    "Skipping execution row for plan %s (state-machine read failed)",
                    plan_id,
                    exc_info=True,
                )
                continue
            if row is None:
                continue
            status = row.get("exec_status") or "not_started"
            pid = row.get("exec_pid")
            # Restore in-memory state (handle None values gracefully)
            from collections import deque
            _server._execution_state[plan_id] = {
                "status": status,
                "started_at": row.get("started_at"),
                "ended_at": None,
                "pid": pid,
                "stop_reason": row.get("stop_reason"),
                "project_dir": row.get("project_dir"),
                "logs": deque(maxlen=2000),
                "process": None,
            }

            # If still running, check if the process is alive
            if status == "running":
                if not pid:
                    _server._mark_failed_dead(plan_id)
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    _server._mark_failed_dead(plan_id)
                except PermissionError:
                    pass
    finally:
        # 2026-09-23: this connection is opened once for the whole scan
        # and must be released even if the traversal raises. Its sibling
        # _recover_verification_states used to leak one handle per plan;
        # see tests/unit/test_recovery_closes_its_connections.py.
        _server._close_state_machine(sm)


def _adopt_run_project_dir(
    plan_dir: Path, project_dir: Path, tasks_file: Path
) -> Optional[int]:
    """Bind a declared-nothing plan's tasks to this run's ``project_dir``.

    Contract: where work happens is the plan's
    call — ``target_project_dir`` / ``primary_project_dir`` in the PRD or
    interview constraints. When the plan declared none, the framework does
    NOT guess (no filesystem scan, no LLM router, no ``-dev`` sibling
    heuristic — see ``TasksGenerator.validate_workspace``). The path the
    operator passes here is then the only declaration in the system, so the
    run adopts it.

    Why this lives at start rather than at generation: at generation time
    the operator has not stated anything yet. ``start`` is the first and
    only moment the framework learns the working directory for a plan that
    never named one.

    Args:
        plan_dir: ``plans/<id>/`` — where the declaration is read from.
        project_dir: the resolved ``project_dir`` from the request.
        tasks_file: ``plans/<id>/tasks.json``, rewritten in place.

    Returns:
        How many tasks were re-pointed, or ``None`` when the plan DID
        declare a workspace (generation already placed every task, and this
        must not touch them).

    Never raises. This is bookkeeping, and it must not be able to turn a
    startable run into a failed one — every failure mode (unreadable plan
    doc, malformed tasks.json, read-only disk) degrades to "leave the
    tasks alone", which is the pre-existing behaviour.
    """
    try:
        from tasks_generator import declared_workspaces_for_plan

        if declared_workspaces_for_plan(plan_dir):
            return None
        if not tasks_file.exists():
            return None
        with open(tasks_file, "r", encoding="utf-8") as f:
            tasks_data = json.load(f)
        tasks = tasks_data.get("tasks") if isinstance(tasks_data, dict) else None
        if not isinstance(tasks, list):
            return None

        target = str(project_dir)
        changed = 0
        for task in tasks:
            if not isinstance(task, dict) or task.get("project_dir") == target:
                continue
            task["project_dir"] = target
            task["_workspace_reason"] = (
                "adopted from the project_dir this run was started with "
                "(the plan declared no workspace)"
            )
            changed += 1
        if changed:
            # Unique temp name — see ``task_manager.save_tasks``: a
            # fixed ``tasks.json.tmp`` races any concurrent writer.
            atomic_write_json(tasks_file, tasks_data, indent=2, reraise=True)
            _server.logger.info(
                "execution_project_dir_adopted plan=%s dir=%s tasks=%d",
                plan_dir.name, target, changed,
            )
        return changed
    except Exception:  # noqa: BLE001 — bookkeeping must never break a start
        _server.logger.exception(
            "execution_project_dir_adopt_failed plan=%s", plan_dir.name
        )
        return None


def _spawn_executor_subprocess(
    plan_id: str,
    backend_dir: Path,
    project_dir: Path,
    tasks_file: Path,
    tool: Optional[str],
    extra_args: Optional[List[str]] = None,
    log_path: Optional[Path] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[subprocess.Popen, Path]:
    """Spawn ``cli.py --recover`` as a subprocess with stdout drained to
    a log file. Returns ``(process, log_path)``.

    2026-09-06:

    Both ``start_execution`` (server.py:5258) and the previous
    ``_run_repair_execution`` (server.py:4483) called
    ``subprocess.Popen(stdout=subprocess.PIPE, ...)`` and then either
    drained the pipe via a background ``for line in process.stdout``
    loop (start_execution) or NOT at all (_run_repair_execution).
    The drain pattern is the working one — without it the OS pipe
    buffer fills (64KB on macOS) and the subprocess blocks forever
    on its next ``write()``, making ``process.wait()`` hang. R1-5
    (a full ``pytest backend/tests/ -q`` run) was the first task to
    exceed 64KB of stdout, and the repair subprocess hung silently
    for 24 hours until an operator noticed the task_progress row
    was stuck in ``in_progress``.

    A comparable Go implementation uses
    ``cmd.WaitDelay = 3 * time.Second`` plus a parallel read loop
    to ensure the I/O goroutine never blocks longer than the wait.
    The Python equivalent: redirect stdout to a file (no pipe
    capacity limit at all) and append lines as they arrive. The
    subprocess can't deadlock regardless of how much it writes, and
    ``process.wait()`` returns as soon as the child exits.

    Both ``start_execution`` (HTTP request path) and
    ``_run_repair_execution`` (verification auto-loop path) now go
    through this helper so they share one spawn pattern, one
    drain strategy, and one log-file naming convention.

    Args:
        plan_id: identifies the plan — used for log file naming and
            the ``PDT_PLAN_ID`` env var that the executor reads.
        backend_dir: path to ``backend/`` (parent of cli.py).
        project_dir: forwarded as ``-w`` to cli.py.
        tasks_file: forwarded as ``--tasks-file`` to cli.py.
        tool: optional ``--tool <claude|...>`` arg.
        extra_args: extra CLI args appended before spawn (e.g.
            ``["--verification-tasks", str(...)]`` from the repair
            path).
        log_path: where to write subprocess stdout+stderr. Defaults
            to ``plan_dir/logs/executor_<timestamp>.log`` so the
            existing log rotation convention is preserved. Parent
            directories are created automatically.
        extra_env: extra env vars to merge into the subprocess env
            (PYTHONUNBUFFERED=1 and PDT_PLAN_ID are always set).

    Returns:
        ``(process, log_path)`` so the caller can decide whether to
        block on ``process.wait()`` (synchronous repair flow) or
        thread-drain it (HTTP execution flow).
    """
    plan_dir = _server._plan_dir(plan_id)

    if log_path is None:
        log_dir = plan_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"executor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    cmd = [
        sys.executable, "-u", str(backend_dir / "cli.py"),
        "--recover", "-w", str(project_dir),
        "--tasks-file", str(tasks_file),
    ]
    if tool:
        cmd.extend(["--tool", tool])
    if extra_args:
        cmd.extend(extra_args)

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PDT_PLAN_ID": plan_id}
    venv_bin = str(backend_dir / ".venv" / "bin")
    if os.path.isdir(venv_bin):
        env["PATH"] = venv_bin + ":" + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")

    # stdout/stderr → file (not PIPE) — avoids the 64KB pipe-buffer
    # deadlock that hung R1-5 for 24h. ``bufsize=0`` + ``flush=True``
    # on the log file ensures tail -f sees output in real time.
    log_file.reconfigure(line_buffering=True)
    process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        bufsize=0,
        cwd=str(backend_dir),
        env=env,
        start_new_session=True,
    )
    return process, log_path


@router.post("/api/execution/{plan_id}/start")
def start_execution(plan_id: str, req: _server.StartExecutionRequest = _server.StartExecutionRequest()):
    """Start autonomous task execution as a background subprocess.

    State-machine contract (architecture decision point 3):

      1. Reject archived plans with HTTP 410.
      2. CAS ``RoutingRepository.try_mark_phase(pid,
         _EXECUTION_START_SOURCE_PHASES, 'executing')`` — the
         only legitimate states from which ``start`` may succeed.
         A concurrent second ``start`` (or a state we don't expect
         to start from) raises :class:`ConflictError` which we
         surface as HTTP 409.
      3. Persist ``current_phase=executing``, ``project_dir``,
         ``exec_pid`` via :meth:`ExecutionRepository.update_phase`.
         The transition is wrapped in the repository's own
         ``BEGIN IMMEDIATE`` transaction so partial writes are
         impossible.
    """
    from datetime import datetime
    from state_machine.db.archive_scan import CUTOFF_2026_08_05, classify_plan
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
        PlanNotFoundError as ExecPlanNotFoundError,
    )
    from state_machine.repositories.routing_repository import (
        ConflictError,
        PlanNotFoundError as RoutingPlanNotFoundError,
    )

    plan_dir = _server._plan_dir(plan_id)
    tasks_file = plan_dir / "tasks.json"
    if not tasks_file.exists():
        raise HTTPException(404, "Tasks not found — generate tasks first")

    # VP-006: block execution while the PRD is a placeholder.
    _server._reject_if_placeholder_prd(plan_dir, plan_id)

    # --- Step 1: archived-plan guard (pre-CAS, returns 410) ------------
    if classify_plan(plan_dir, CUTOFF_2026_08_05) == "archived":
        return JSONResponse(
            status_code=410,
            content={"error": "archived"},
        )

    # project_dir is REQUIRED (audit 2026-07-16): the previous
    # default of ``<plan_dir>/project`` silently created empty
    # commits in this repository, because that subdirectory lives
    # inside this git repo while the actual work went elsewhere — the
    # operator's real project working tree. The default + relative-path
    # fallback meant ``{"project_dir": "frontend-app"}`` (or an
    # empty body) would commit into a "ghost" project subdirectory that
    # the executor's git operations then picked up — but the work itself
    # was in a different working tree, so the commits came out empty.
    #
    # Failure-mode audit (2026-07-16): eleven empty ``[task-N]`` commits
    # replayed onto this repo's main branch. All carried the
    # "Files changed: (No files changed)" marker because the
    # frontend code lived in a separate working tree.
    #
    # Fix: refuse the request when ``project_dir`` is missing. The
    # operator must explicitly point at the real project location.
    if not req.project_dir or not req.project_dir.strip():
        raise HTTPException(
            400,
            "project_dir is required. Provide the absolute path to "
            "the project directory where the executor should "
            "checkout / commit. Defaulting to a relative path "
            "silently produces empty commits in the wrong git "
            "repo (audit 2026-07-16)."
        )
    _raw_project_dir = req.project_dir.strip()
    project_dir = Path(_raw_project_dir).expanduser().resolve()

    # Refuse to execute inside the backend/ directory to prevent modifying
    # running service code. Both roots come from ``server``: derived from
    # ``__file__`` here they would point at ``backend/routes/`` instead.
    _self_root = _server._PROJECT_ROOT
    _backend_dir = _server._BACKEND_DIR
    if project_dir == _backend_dir or _backend_dir in project_dir.parents:
        raise HTTPException(
            400,
            f"拒绝在 backend/ 目录内执行（{_backend_dir}）。"
            " 请指定其他目录，避免执行过程修改正在运行的服务代码。",
        )
    # Only ``backend/`` is refused above; the repository root itself is
    # allowed, since a plan may legitimately create new subdirectories
    # there.

    project_dir.mkdir(parents=True, exist_ok=True)

    # Kill any existing execution for this plan
    existing = _server._execution_state.get(plan_id, {})
    proc = existing.get("process")
    if proc and proc.poll() is None:
        _server._kill_process_tree(proc)

    # State is persisted directly in plans/{plan_id}/tasks.json via --tasks-file

    # Per-PRD-DP-1 + arch-DP-7: resolve the effective sync_targets list.
    # ``None`` (or a missing field from old callers) falls back to the
    # Telegram default; ``[]`` is an explicit "no sinks" value and is
    # preserved verbatim.
    effective_sync_targets: List[str] = (
        ["telegram"] if req.sync_targets is None else list(req.sync_targets)
    )

    logs: deque = deque(maxlen=2000)
    state = {
        "status": "running",
        "logs": logs,
        "project_dir": str(project_dir),
        "process": None,
        "started_at": datetime.now().isoformat(),
        "ended_at": None,
        "pid": None,
        "stop_reason": None,
        "sync_targets": effective_sync_targets,
    }
    _server._execution_state[plan_id] = state

    # --- Step 2: routing CAS (the start-execution mutex) ---------------
    # The CAS rejects concurrent double-start (bug 2): the second
    # caller sees ``current_phase='executing'`` which is NOT in
    # ``expected_phases``, so ``try_mark_phase`` raises
    # :class:`ConflictError` and we return HTTP 409.
    backend_dir = _server._BACKEND_DIR
    conn = None
    exec_pid_for_repo: Optional[int] = None
    try:
        conn = open_db(_server._state_db_path())
        migrate(conn)
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )
        routing = RoutingRepository(conn)
        execution = ExecutionRepository(conn)
        # We don't yet know the subprocess pid; we'll insert/update
        # plan_execution AFTER spawn so exec_pid is non-null.  The
        # CAS row update can proceed without exec_pid.
        routing.try_mark_phase(
            plan_id,
            # 2026-09-15: ``queued`` is the phase a
            # queued plan carries — the scheduler starts it from here, so
            # the CAS must accept it alongside the manual ``ready``
            # gate and the terminal re-entry phases.
            _server._EXECUTION_START_SOURCE_PHASES,
            "executing",
        )
    except ConflictError as exc:
        try:
            conn.rollback()  # type: ignore[union-attr]
        except sqlite3.OperationalError:
            pass
        if conn is not None:
            conn.close()
        # Surface 409 with the public reason mapping.
        return JSONResponse(
            status_code=409,
            content={"error": "conflict", "reason": _server._conflict_reason(exc)},
        )
    except (RoutingPlanNotFoundError, ExecPlanNotFoundError):
        if conn is not None:
            conn.close()
        # The plan is unknown to the state-machine (never went
        # through bootstrap).  Fall back to the legacy transition so
        # # the executor can still run for legacy plans whose routing
        # row was never inserted.  We then transition PlanState below
        # exactly as before.
        ps = _server.PlanState(plan_dir)
        try:
            ps.transition_to("executing")
        except ValueError:
            current = ps.get_current_phase()
            if current in ("completed", "verification_passed", "verification_loop_stopped"):
                ps.force_set_phase("ready")
                ps.transition_to("executing")
            else:
                raise
    else:
        # CAS succeeded — close the routing-only connection so we
        # don't leak it through the long-running subprocess.
        if conn is not None:
            conn.close()

        # Mirror the legacy PlanState transition so consumers that
        # still read plan_state.json (and downstream routes that
        # derive status from it) see ``current_phase == 'executing'``.
        # The state-machine row is the source of truth for the CAS;
        # the JSON mirror keeps the legacy consumers happy.
        ps = _server.PlanState(plan_dir)
        try:
            ps.transition_to("executing")
        except ValueError:
            # Re-execution from terminal / post-verification states
            # should not require manual state repair.  Force the plan
            # back to a state from which execution can start, then
            # proceed normally.
            current = ps.get_current_phase()
            if current in ("completed", "verification_passed", "verification_loop_stopped"):
                ps.force_set_phase("ready")
                ps.transition_to("executing")
            else:
                raise

    # Spawn the subprocess synchronously so the pid is known at return time
    # 2026-09-06:
    # Route this through the shared :func:`_spawn_executor_subprocess`
    # helper so ``start_execution`` and ``_run_repair_execution`` share
    # one spawn pattern (stdout → log file, no PIPE-buffer deadlock).
    #
    # 2026-09-22: for a plan that declared no workspace, ``project_dir``
    # (above) is the only declaration in the system — write it onto the
    # tasks before the executor reads them. See ``_adopt_run_project_dir``.
    _adopt_run_project_dir(plan_dir, project_dir, tasks_file)
    try:
        process, log_path = _server._spawn_executor_subprocess(
            plan_id=plan_id,
            backend_dir=backend_dir,
            project_dir=project_dir,
            tasks_file=tasks_file,
            tool=req.tool,
        )
        state["log_path"] = str(log_path)
    except OSError as exc:
        # Process spawn failed (binary missing, exec permission, etc.).
        # The HTTP request should still return 200 — the failure is
        # recorded into execution.json with status='failed' so the
        # caller can poll /api/execution/{id}/status.
        _server.logger.exception(
            "subprocess.Popen failed for plan %s: %s", plan_id, exc
        )
        state["status"] = "failed"
        state["stop_reason"] = "process_died_unexpectedly"
        state["ended_at"] = datetime.now().isoformat()
        # 2026-09-13: use ``create_if_missing=True`` (same rationale as the
        # Step-3 persist below): when spawn fails, Step 3 never ran, so no
        # ``plan_execution`` row exists and the ``update_status`` call used
        # here raised ``ExecPlanNotFoundError`` and silently persisted
        # nothing — the failed run left zero trace in SQLite. ``stop_reason``
        # is persisted too since this branch creates the row fresh.
        try:
            persist_conn = open_db(_server._state_db_path())
            try:
                migrate(persist_conn)
                ExecutionRepository(persist_conn).update_phase(
                    plan_id,
                    current_phase="executing",
                    create_if_missing=True,
                    exec_status="failed",
                    stop_reason="process_died_unexpectedly",
                )
            finally:
                persist_conn.close()
        except ExecPlanNotFoundError:
            pass
        try:
            _server.PlanState(plan_dir).transition_to("failed")
        except Exception:
            pass
        return {"status": state["status"], "pid": None}
    state["process"] = process
    state["pid"] = process.pid
    # NOTE: the immediate "running" persist that used to live here is
    # redundant with the ``update_phase(exec_status="running", exec_pid=...)``
    # call below — that single write covers both fields atomically. The
    # legacy JSON-only fallback (ExecPlanNotFoundError) is handled by
    # the same exception branch.

    # --- Step 3: persist project_dir + exec_pid into plan_execution ----
    # We persist AFTER spawn so exec_pid is the real subprocess pid
    # rather than None.  The write uses ExecutionRepository's own
    # BEGIN IMMEDIATE txn so the read-modify-write is race-free.
    #
    # 2026-09-13: ``create_if_missing=True`` so a plan whose
    # ``plan_execution`` row was never created by the bootstrap
    # (the legacy path tolerated above via RoutingPlanNotFoundError /
    # ExecPlanNotFoundError) still persists its run state. Without
    # this the ExecPlanNotFoundError branch below silently skipped the
    # write, leaving ``exec_pid`` / ``exec_status`` / ``project_dir``
    # in-memory only — a server restart lost them and
    # ``_recover_execution_states`` could not restore the plan. That is
    # the same crash-recovery hole the now-removed ``execution.json``
    # write used to cover, reintroduced when the JSON file was retired.
    try:
        persist_conn = open_db(_server._state_db_path())
        migrate(persist_conn)
        ExecutionRepository(persist_conn).update_phase(
            plan_id,
            current_phase="executing",
            create_if_missing=True,
            project_dir=str(project_dir),
            exec_pid=process.pid,
            exec_status="running",
            started_at=state["started_at"],
        )
        persist_conn.close()
    except ExecPlanNotFoundError:
        # Unreachable with create_if_missing=True (the upsert never
        # raises PlanNotFoundError); kept as a defensive no-op so a
        # future re-read does not mistake it for the authoritative path.
        if persist_conn is not None:  # type: ignore[possibly-undefined]
            try:
                persist_conn.close()
            except Exception:
                pass

    def _run():
        try:
            # 2026-09-06:
            # stdout/stderr are now redirected to a log file by
            # :func:`_spawn_executor_subprocess` (not PIPE), so the
            # drain loop is unnecessary — the subprocess can write
            # unbounded amounts without blocking. We only wait for
            # it to exit, then read the log file on demand from
            # ``GET /api/execution/{id}/status`` (via state["log_path"]).
            process.wait()
            # 2026-09-11 plan v11 (watchdog race fix):
            # Mark executor as "finished cleanly" IMMEDIATELY after
            # ``process.wait()`` returns, BEFORE any in-memory state
            # mutation (line 5976+). The HeartbeatMonitor's 30s
            # ``_lazy_check_execution`` tick probes the PID via
            # ``os.kill(pid, 0)``; if the subprocess died in the
            # ~30ms window between ``process.wait()`` returning and
            # the success path writing ``state["status"]="completed"``
            # (line 6163), the watchdog fires ``_mark_failed_dead``
            # and races the success path, leaving the state machine
            # stuck at ``terminal_failed`` even though the executor
            # returned 0. Setting the flag here lets the watchdog
            # distinguish "PID gone but success path is processing
            # the result" from "PID gone because executor truly
            # crashed". See ``test_execution_watchdog_race`` for
            # the contract.
            state["executor_finished_cleanly"] = True
            state["executor_returncode"] = process.returncode
            if state["status"] == "running":
                if process.returncode == 0:
                    # 2026-08-24: the original code
                    # unconditionally marked the executor "completed"
                    # whenever its subprocess returned 0. That masked
                    # a real failure mode in the executor's
                    # self-scheduling loop: ``agent.run`` exits 0 even
                    # when ``_load_tasks`` filtered every task as
                    # terminal before the executor got to run any of
                    # them, or when the no-schedulable-micro-layer
                    # branch in agent.py:2635 fired with downstream
                    # tasks still pending. The downstream state machine
                    # then proceeded to launch the verification round
                    # against a half-finished plan: tasks that had never
                    # started were reported as "all done" and the round
                    # ran against the completed subset.
                    #
                    # The corrected contract: a 0 return code means
                    # "the subprocess didn't crash". The semantic
                    # completion state must be derived from the
                    # authoritative per-task runtime overlay (SQLite
                    # ``plan_execution.task_progress``) cross-checked
                    # against the static ``tasks.json`` task ids.
                    # Anything that exists in tasks.json but has no
                    # ``status`` field in the runtime overlay is
                    # "never started" and the plan cannot be
                    # considered terminal.
                    unfinished = _server._count_unfinished_tasks(plan_id, tasks_file)
                    if unfinished["pending_or_unstarted"] > 0:
                        # 2026-09-11 plan v9 (Bug 2 fix):
                        # Executor gave up with deferred/blocked tasks.
                        # Two sub-cases:
                        #
                        # (a) ALL unfinished tasks are blocked by a
                        #     failed upstream → recover via
                        #     partial-completion: mark the deferred
                        #     tasks ``skipped`` and enter auto-
                        #     verification on the completed subset.
                        #     The previous code went straight to
                        #     ``failed`` here, leaving a plan with most
                        #     of its tasks complete but stuck at
                        #     ``routing.stage='tasks_ready'`` until
                        #     someone unblocked it by hand.
                        #
                        # (b) At least one unfinished task has no
                        #     failed upstream (executor crashed
                        #     before running it, or it's a leaf
                        #     never reached) → this IS a real
                        #     failure; preserve the legacy
                        #     ``state["status"]="failed"`` +
                        #     ``transition_to("failed")`` path.
                        unfinished_ids = unfinished.get(
                            "unfinished_ids", []
                        )
                        # ``_count_unfinished_tasks`` does not
                        # currently return the id list — derive it
                        # here from disk + runtime overlay.
                        if not unfinished_ids:
                            try:
                                _tdata = json.loads(tasks_file.read_text())
                                _ttasks = (
                                    _tdata
                                    if isinstance(_tdata, list)
                                    else _tdata.get("tasks", [])
                                )
                                _static_ids = {
                                    str(t.get("id"))
                                    for t in _ttasks
                                    if isinstance(t, dict) and t.get("id")
                                }
                                try:
                                    # ``open_db`` and ``migrate`` are
                                    # deliberately NOT imported here.
                                    # ``start_execution`` already binds
                                    # them as enclosing-function locals
                                    # (6626-6627) and ``_run`` closes over
                                    # them. Re-importing them inside this
                                    # branch re-binds them as ``_run``
                                    # locals for the *whole* function, so
                                    # every other ``open_db(...)`` /
                                    # ``migrate(...)`` call in ``_run``
                                    # raised UnboundLocalError whenever
                                    # this branch was skipped (i.e. on the
                                    # normal all-tasks-terminal success
                                    # path) — which silently skipped the
                                    # auto-verification hand-off. See
                                    # 2026-09-13.
                                    from state_machine.repositories.plan_task_repository import (  # noqa: E501
                                        PlanTaskRepository,
                                    )
                                    _conn = open_db(_server._state_db_path())
                                    try:
                                        migrate(_conn)
                                        _runtime = (
                                            PlanTaskRepository(_conn)
                                            .load_all(plan_id)
                                            or {}
                                        )
                                    finally:
                                        _conn.close()
                                except Exception:
                                    _runtime = {}
                                _TERMINAL_OK = {"completed", "skipped"}
                                for _tid in _static_ids:
                                    _entry = _runtime.get(_tid)
                                    if _entry is None:
                                        unfinished_ids.append(_tid)
                                        continue
                                    if (
                                        _entry.get("status")
                                        not in _TERMINAL_OK
                                    ):
                                        unfinished_ids.append(_tid)
                            except Exception:
                                pass
                        blocked_only = (
                            _server._are_all_unfinished_blocked_by_failed_upstream(
                                plan_id, unfinished_ids, tasks_file,
                            )
                        )
                        if blocked_only:
                            # (a) — partial-completion path.
                            for tid in unfinished_ids:
                                _server._mark_task_skipped(
                                    plan_id,
                                    tid,
                                    reason="blocked_by_failed_upstream",
                                )
                            state["status"] = "completed"
                            state["stop_reason"] = (
                                f"partial_completion:"
                                f"{len(unfinished_ids)}_blocked_tasks_skipped"
                            )
                            state["ended_at"] = datetime.now().isoformat()
                            try:
                                persist_conn = open_db(_server._state_db_path())
                                try:
                                    migrate(persist_conn)
                                    ExecutionRepository(persist_conn).update_status(
                                        plan_id, "completed"
                                    )
                                finally:
                                    persist_conn.close()
                            except ExecPlanNotFoundError:
                                pass
                            except Exception:
                                # Best-effort persist — see the twin branch
                                # on the success path below. A failure here
                                # must not skip the auto-verification
                                # hand-off.
                                _server.logger.exception(
                                    "execution_status_persist_failed plan=%s",
                                    plan_id,
                                )
                            # Fall through to the success path —
                            # enter auto-verification on the
                            # completed subset. ``_run_auto_verification_loop``
                            # will CAS ``plan_routing.stage`` to
                            # ``verification`` and call
                            # ``transition_to("completed")`` once
                            # the round finishes.
                            loop_outcome = _server._run_auto_verification_loop(
                                plan_id,
                                plan_dir,
                                project_dir,
                                tool=req.tool,
                            )
                            # 2026-09-23: only a TERMINAL loop exit may be
                            # promoted to ``completed``. A hand-off — the
                            # loop spawned the repair execution and will
                            # resume from its ``on_complete`` callback —
                            # leaves the plan mid-repair, and the
                            # ``executing`` the dispatcher just wrote is
                            # the truth. Promoting it anyway stamps the
                            # plan ``completed`` while its repair task is
                            # still starting, and makes the card read
                            # "⏸ 暂停（上游阻塞）" with a live ⏳ on it.
                            _server._settle_phase_after_verification_loop(
                                plan_dir, loop_outcome,
                            )
                            return
                        # (b) — truly unfinished → failed
                        # (preserved old behaviour).
                        state["status"] = "failed"
                        state["stop_reason"] = (
                            f"executor_exited_with_unfinished_tasks"
                            f":{unfinished['pending_or_unstarted']}"
                        )
                        state["ended_at"] = datetime.now().isoformat()
                        try:
                            persist_conn = open_db(_server._state_db_path())
                            try:
                                migrate(persist_conn)
                                ExecutionRepository(persist_conn).update_status(
                                    plan_id, "failed"
                                )
                            finally:
                                persist_conn.close()
                        except ExecPlanNotFoundError:
                            pass
                        try:
                            _server.PlanState(plan_dir).transition_to("failed")
                        except ValueError:
                            # Plan might already be in a terminal
                            # phase (e.g. from a previous interrupted
                            # run); the executor status update is
                            # still authoritative via the DB row.
                            pass
                        _server.logger.warning(
                            "executor_exited_0_with_unfinished_tasks plan=%s "
                            "unfinished=%s",
                            plan_id,
                            unfinished,
                        )
                        return
                    state["status"] = "completed"
                    state["stop_reason"] = None
                    state["ended_at"] = datetime.now().isoformat()
                    try:
                        persist_conn = open_db(_server._state_db_path())
                        try:
                            migrate(persist_conn)
                            ExecutionRepository(persist_conn).update_status(
                                plan_id, "completed"
                            )
                        finally:
                            persist_conn.close()
                    except ExecPlanNotFoundError:
                        pass
                    except Exception:
                        # 2026-09-13: this persist is
                        # best-effort, but before this branch existed a
                        # transient SQLite error (e.g. ``database is
                        # locked`` while the plan-card / task-sync poller
                        # held a writer) propagated into the outer
                        # ``except Exception`` at the bottom of ``_run``,
                        # which silently swallowed it *and* skipped the
                        # auto-verification hand-off below. Symptom: the
                        # plan stranded at ``routing.stage='executing'``
                        # with a stale ``plan_execution.exec_status='running'``
                        # and nothing in the server log. The status write
                        # must never decide whether verification runs.
                        _server.logger.exception(
                            "execution_status_persist_failed plan=%s", plan_id
                        )
                    # --- Usage accounting ---
                    # Execution has finished, so the plan's LLM sessions
                    # are complete; re-aggregate them against CC Switch's
                    # ledger now. Backgrounded + best-effort: accounting
                    # must never delay or break the verification hand-off.
                    _server._refresh_usage_report(plan_id)
                    # --- Auto verification loop ---
                    # 2026-09-23: default to "terminal" so this branch keeps
                    # its pre-existing behaviour if the loop raises before
                    # returning an outcome (the exception is logged just
                    # below and the plan still needs a resting phase).
                    loop_outcome = _server._VerificationLoopOutcome()
                    try:
                        loop_outcome = _server._run_auto_verification_loop(plan_id, plan_dir, project_dir, tool=req.tool)
                    except Exception:
                        _server.logger.exception(
                            "auto_verification_launch_failed plan=%s", plan_id
                        )
                    # If verification loop didn't transition plan_state (e.g. skipped
                    # or mocked), ensure it reaches a terminal state.
                    # 2026-09-23: but only when the loop really ended. A
                    # hand-off exit leaves a repair execution running, and
                    # ``executing`` is then the truthful phase — see
                    # :class:`_VerificationLoopOutcome`.
                    _server._settle_phase_after_verification_loop(plan_dir, loop_outcome)
                elif process.returncode > 0:
                    state["status"] = "failed"
                    state["stop_reason"] = "non_zero_exit"
                    state["ended_at"] = datetime.now().isoformat()
                    _server.PlanState(plan_dir).transition_to("failed")
                else:
                    state["status"] = "failed"
                    state["stop_reason"] = "process_died_unexpectedly"
                    state["ended_at"] = datetime.now().isoformat()
                    _server.PlanState(plan_dir).transition_to("failed")
            try:
                persist_conn = open_db(_server._state_db_path())
                try:
                    migrate(persist_conn)
                    ExecutionRepository(persist_conn).update_status(
                        plan_id, state.get("status", "running")
                    )
                finally:
                    persist_conn.close()
            except ExecPlanNotFoundError:
                pass
        except Exception:
            # Never swallow this silently: an exception raised anywhere in
            # the watcher body used to leave ``plan_execution.exec_status``
            # stale and (when it fired before the auto-verification call)
            # stranded the plan with no trace in the log.
            _server.logger.exception("executor_watcher_crashed plan=%s", plan_id)
            if state["status"] == "running":
                state["status"] = "failed"
                state["stop_reason"] = "process_died_unexpectedly"
                state["ended_at"] = datetime.now().isoformat()
                _server.PlanState(plan_dir).transition_to("failed")
                try:
                    persist_conn = open_db(_server._state_db_path())
                    try:
                        migrate(persist_conn)
                        ExecutionRepository(persist_conn).update_status(
                            plan_id, "failed"
                        )
                    finally:
                        persist_conn.close()
                except ExecPlanNotFoundError:
                    pass

    # Usage-registry attribution: bind plan_id for the watcher thread
    # (auto-verification triggers can call the LLM from here).
    threading.Thread(
        target=_server._run_in_plan_ctx, args=(plan_id, _run), daemon=True
    ).start()
    return {
        "plan_id": plan_id,
        "status": "started",
        "pid": state["pid"],
        "project_dir": str(project_dir),
    }


@router.get("/api/execution/{plan_id}/status", response_model=_server.ExecutionStatusResponse)
def get_execution_status(plan_id: str):
    """Poll execution status and logs.

    Reads flow through the in-memory ``_execution_state`` dict (which
    is restored on startup by :func:`_recover_execution_states` from
    the ExecutionRepository SQLite row).  The legacy direct read of
    ``execution.json`` was removed because writes already flow through
    the repository layer — there is no canonical on-disk JSON to
    read from at this point.
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function
    _server._lazy_check_execution(plan_id)
    s = _server._execution_state.get(plan_id)
    if not s:
        # No row in the state-machine — surface an empty "not started"
        # response rather than 500.  This matches the legacy behaviour
        # when ``execution.json`` was absent.
        return {
            "plan_id": plan_id,
            "status": "not_started",
            "logs": [],
            "project_dir": "",
            "pid": None,
            "started_at": None,
            "ended_at": None,
            "stop_reason": None,
            "sync_targets": None,
        }
    return {
        "plan_id": plan_id,
        "status": s.get("status", "not_started"),
        "logs": list(s.get("logs", [])),
        "project_dir": s.get("project_dir", ""),
        "pid": s.get("pid"),
        "started_at": s.get("started_at"),
        "ended_at": s.get("ended_at"),
        "stop_reason": s.get("stop_reason"),
        "sync_targets": s.get("sync_targets"),
    }


@router.post("/api/execution/{plan_id}/task_delta")
def apply_execution_task_delta(plan_id: str, req: _server.TaskDeltaRequest):
    """Apply an operator task-list delta (``add`` / ``obsolete``).

    Why this exists: a failed execution task previously had only two
    outcomes — get repaired, or sit on the progress card as a permanent
    red entry. Clearing them would hide the problem; this endpoint adds
    the ability to **void** a task with a recorded reason and **add** a
    replacement, without editing the original.

    Semantics live in ``backend/task_plan_delta.py``; this handler owns
    the I/O (load current tasks, open the state-machine connection).

    ``obsolete`` writes ``status="superseded"`` (already terminal for the
    dispatcher) plus an ``[obsolete] …`` marker in ``failure_reason`` —
    ``plan_tasks`` has no dedicated ``obsolete_reason`` column. The
    authoritative audit record (actor, round, reasons for both actions,
    and everything that was rejected) goes to
    ``plans/{plan_id}/task_delta_log.json``.
    """
    import json as _json

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from task_plan_delta import (
        apply_task_delta,
        is_obsolete_reason,
        parse_task_delta_payload,
    )

    archived = _server._archived_plan_response(_server._plan_dir(plan_id))
    if archived is not None:
        return archived

    plan_dir = _server._plan_dir(plan_id)
    tasks_file = plan_dir / "tasks.json"
    if not tasks_file.exists():
        raise HTTPException(404, f"tasks.json not found for plan {plan_id!r}")

    try:
        raw = _json.loads(tasks_file.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Failed to read tasks.json: {exc}")
    disk_tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(disk_tasks, list):
        disk_tasks = []

    conn = open_db(_server._state_db_path())
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        runtime = repo.load_all(plan_id)

        # "Existing" spans both truth sources: the task may be on disk
        # only (never dispatched) or in the DB only (an orphan).
        existing_ids = {
            str(t.get("id")) for t in disk_tasks if isinstance(t, dict)
        } | {str(tid) for tid in runtime}

        already_obsolete = {
            str(tid)
            for tid, row in runtime.items()
            if str(row.get("status") or "") == "superseded"
            or is_obsolete_reason(row.get("failure_reason"))
        }

        delta = parse_task_delta_payload(
            req.model_dump(), existing_ids, already_obsolete,
        )
        if delta.is_empty and not (
            delta.rejected_additions
            or delta.rejected_obsoletes
            or delta.rejected_modifications
        ):
            raise HTTPException(
                400, "task_delta requires a non-empty 'add' or 'obsolete' list"
            )

        summary = apply_task_delta(
            repo=repo,
            plan_id=plan_id,
            plan_dir=plan_dir,
            existing_tasks=disk_tasks,
            delta=delta,
            round_number=int(req.round or 0),
            actor=str(req.actor or "operator"),
        )
    finally:
        conn.close()

    return {
        "plan_id": plan_id,
        **summary,
        "is_empty": delta.is_empty,
    }


@router.post("/api/execution/{plan_id}/stop")
def stop_execution(plan_id: str):
    """Terminate running execution."""
    from datetime import datetime
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
        PlanNotFoundError as ExecPlanNotFoundError,
    )

    # VP-020: an archived plan is read-only — refuse before touching
    # ``_execution_state`` or any SQLite row. This endpoint had no
    # archived guard at all, so a stop against a pre-2026-08-05 plan
    # answered 200 and mutated the row.
    archived = _server._archived_plan_response(_server._plan_dir(plan_id))
    if archived is not None:
        return archived

    s = _server._execution_state.get(plan_id)
    if s:
        proc = s.get("process")
        if proc and proc.poll() is None:
            _server._kill_process_tree(proc)
        # Only update if not already in a terminal state from the _run thread
        if s.get("status") == "running":
            s["status"] = "stopped"
            s["stop_reason"] = "user_requested"
            s["ended_at"] = datetime.now().isoformat()
            try:
                persist_conn = open_db(_server._state_db_path())
                try:
                    migrate(persist_conn)
                    ExecutionRepository(persist_conn).update_status(
                        plan_id, "stopped"
                    )
                finally:
                    persist_conn.close()
            except ExecPlanNotFoundError:
                pass
            try:
                _server.PlanState(_server._plan_dir(plan_id)).transition_to("stopped")
            except ValueError:
                pass
    # 2026-09-18 C3: stopping the execution phase is also an exit from
    # the workflow. The execution phase does not itself start declared
    # services (only verification's preflight does), but a plan that
    # was re-executed after a verification cycle can still have a
    # ledger from that cycle, and this is the last exit point that
    # would otherwise leave it behind.
    _server._reap_managed_services(plan_id, _server._plan_dir(plan_id), "execution_stopped")

    return {"status": "stopped"}


@router.get("/api/execution/{plan_id}/progress")
def get_execution_progress(plan_id: str):
    """Real-time task progress.

    Resolution order (architecture decision point 3):

      1. ``plan_execution.task_progress`` (SQLite) — the
         ``ExecutionRepository.progress()`` snapshot is the
         authoritative counts when present.
      2. ``<PLANS_DIR>/<plan_id>/tasks.json`` — legacy fallback for
         plans that haven't yet been migrated to the state-machine
         task_progress write path.  ``cli.py --recover`` writes here
         (via ``--tasks-file``), so we still consult it to derive
         counts when the SQLite column is empty.
      3. ``<project_dir>/tasks.json`` — pre-refactor fallback.

    The ``current`` / ``next`` / ``api_error`` fields are still
    derived from the on-disk tasks.json (the per-task rich
    metadata that ``task_progress`` does not carry).  The
    ``pid`` / ``project_dir`` / ``execution_status`` fields are
    derived from the state-machine row + on-disk ``execution.json``
    mirror, NOT from ``_execution_state`` (no in-memory aggregation).
    """
    _server._lazy_check_execution(plan_id)
    project_dir = _server._get_project_dir(plan_id)
    if not project_dir:
        raise HTTPException(404, "Execution not configured for this plan — call /api/execution/{plan_id}/start first")

    plan_dir = _server._plan_dir(plan_id)
    plan_tasks_file = plan_dir / "tasks.json"
    legacy_tasks_file = project_dir / "tasks.json"  # 404 above guarantees project_dir is set

    # Hot-path optimization B-05: short-circuit candidate files with a
    # single tuple of Path.exists() probes. The previous code did two
    # sequential .exists() calls even on the common single-source
    # layout (only plan_tasks_file present). Single tuple evaluation
    # avoids re-walking the path resolution for the legacy fallback.
    candidates = (plan_tasks_file, legacy_tasks_file)
    tasks_file = next((c for c in candidates if c.exists()), None)

    # 2026-09-06 — also consult the latest ``tasks_with_repair_round_*.json``
    # if one exists, since the executor's repair pipeline (cli.py --recover)
    # writes merged task lists to that filename rather than mutating the
    # original ``tasks.json``. Without this, the per-task progress overlay
    # and the Feishu/Telegram cards show 62/62 "completed" while the plan
    # is actually mid-repair with R1-* tasks queued/in-flight. Pick the
    # highest-numbered round so a round-2 repair supersedes a round-1.
    #
    # 2026-09-08 (single-writer refactor): the new repair pipeline
    # does NOT write ``tasks_with_repair_round_*.json`` files anymore
    # — repair tasks live in state.db (via PlanTaskRepository.add_task)
    # and the executor's :meth:`AutonomousAgent._load_tasks` Phase 2
    # reconciles them on read. For backwards compatibility we still
    # consult any legacy merged file that may exist on disk from a
    # pre-2026-09-08 run; new runs simply don't write such a file.
    # Additionally, after the canonical ``tasks.json`` is loaded we
    # merge in any repair tasks recorded in state.db whose id is NOT
    # in ``tasks.json`` so the API response shape stays consistent
    # (no missing R1-* entries in the progress card).
    repair_tasks_file: Optional[Path] = None
    if plan_dir.exists():
        candidates = sorted(
            plan_dir.glob("tasks_with_repair_round_*.json"),
            key=lambda p: int(
                # filename is ``tasks_with_repair_round_<N>.json``
                p.stem.rsplit("_", 1)[-1]
            ) if p.stem.rsplit("_", 1)[-1].isdigit() else -1,
            reverse=True,
        )
        if candidates:
            repair_tasks_file = candidates[0]

    # The legacy merged file used to WIN unconditionally whenever it
    # existed. That is a split-brain for any plan still running today:
    # the executor reads ``plans/<id>/tasks.json`` (``cli.py
    # --tasks-file``) while this endpoint read the legacy file, so the
    # card and the executor disagreed about the task list.
    #
    # A legacy ``tasks_with_repair_round_3.json`` (the pre-2026-09-08
    # layout) can still carry a repair task that is absent from
    # state.db and from ``tasks.json``, therefore unrunnable — so the
    # progress card shows it as ``pending``
    # forever, while ``R1-5`` (re-queued in ``tasks.json`` on 09-14)
    # was invisible because the legacy file predates it.
    #
    # The legacy path exists for plans whose repair pipeline ran
    # *before* 2026-09-08 (see above) — those plans have no newer
    # ``tasks.json`` to prefer. So decide by mtime: the newer file is
    # the one that reflects the plan's current task list, whichever
    # filename it happens to have. Ties keep ``tasks.json``.
    if repair_tasks_file is not None and tasks_file is not None:
        try:
            use_repair_file = (
                repair_tasks_file.stat().st_mtime > tasks_file.stat().st_mtime
            )
        except OSError:
            use_repair_file = False
        if not use_repair_file:
            _server.logger.debug(
                "progress: preferring %s over legacy %s for plan=%s",
                tasks_file.name, repair_tasks_file.name, plan_id,
            )
            repair_tasks_file = None
    if repair_tasks_file is not None:
        tasks_file = repair_tasks_file

    tasks: list = []
    data: Any = {}
    if tasks_file is not None:
        try:
            data = json.loads(tasks_file.read_text())
            tasks = data if isinstance(data, list) else data.get("tasks", [])
        except Exception as e:
            raise HTTPException(500, f"Failed to read tasks.json: {e}")

    # --- Runtime overlay from plan_execution.task_progress.tasks (task #3.8) -
    # ``tasks.json`` is now static-only; the per-task runtime state
    # (``status`` / ``end_ts`` / ``commit_sha`` / ``attempt`` /
    # ``schedule_ts`` / ``failure_reason``) lives in the
    # ``plan_execution.task_progress`` SQLite column.  We overlay it
    # onto each task dict in place so the API response shape stays
    # identical to the pre-#3.8 contract (frontend gets the same
    # per-task fields it always has).
    _RUNTIME_OVERLAY_FIELDS = (
        "status",
        "end_ts",
        "commit_sha",
        "attempt",
        "schedule_ts",
        "failure_reason",
    )
    # --- Single state-machine connection ----------------------------------
    # ``plan_task_repository.load_all`` (per-task runtime overlay) and
    # ``execution_repository.progress`` (aggregated ``task_progress``
    # column used as a sanity-check hint) both read from the same
    # SQLite file. We open the connection ONCE and pass the handle
    # to both repositories so:
    #
    #   * the WAL / busy_timeout / synchronous PRAGMAs are applied
    #     once per request instead of twice,
    #   * the read-modify-write view is consistent — a writer that
    #     commits between two opens cannot show the overlay and
    #     the aggregated column out-of-order,
    #   * the second ``migrate()`` call (a no-op once the schema is
    #     current but still a DDL round-trip) is gone.
    runtime: dict = {}
    db_progress: Optional[dict] = None
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )

        sm_conn = open_db(_server._state_db_path())
        try:
            migrate(sm_conn)
            # Per-task runtime overlay (current schema; see
            # ``_RUNTIME_OVERLAY_FIELDS`` above).
            runtime = PlanTaskRepository(sm_conn).load_all(plan_id)
            # Aggregated ``task_progress`` column — the sanity-check
            # hint consumed by the drift log below.
            db_progress = ExecutionRepository(sm_conn).progress(plan_id)
        finally:
            sm_conn.close()
    except (sqlite3.OperationalError, OSError):
        # State-machine not on disk yet — tasks.json carries the
        # runtime fields directly (legacy / pre-migration plan).
        runtime = {}
        db_progress = None

    for t in tasks:
        rt = runtime.get(t.get("id")) if isinstance(t, dict) else None
        if not rt:
            continue
        for k in _RUNTIME_OVERLAY_FIELDS:
            if k in rt:
                t[k] = rt[k]

    # 2026-09-10 v4 follow-up: hydrate DB-only orphan rows into
    # ``tasks`` so the executor-progress endpoint / Feishu / Telegram
    # cards can surface in-progress orphans.  Without this, ``tasks``
    # only carries disk rows and an orphan whose status flips to
    # ``in_progress`` would silently disappear from the response
    # (and therefore from "当前任务" / "🔄 正在执行" sections).
    # Same shape as ``get_plan_summary``'s hydrate; non-terminal
    # orphans get a placeholder, terminal orphans are dropped
    # because the dispatcher never re-runs them anyway.
    try:
        _TERMINAL = {"completed", "failed", "skipped", "superseded"}
        disk_ids = {t.get("id") for t in tasks if isinstance(t, dict)}
        for tid, rt in runtime.items():
            if tid in disk_ids:
                continue
            status = (rt.get("status") or "").lower() or "pending"
            if status in _TERMINAL:
                # 2026-09-11 plan v9 (Bug 1): terminal orphans
                # (failed / completed / skipped) MUST be appended as
                # ``db_orphan_terminal`` placeholders so the per-task
                # counts loop at line 6181+ reflects them. The previous
                # implementation dropped terminal orphans here, which
                # meant a plan with 2 failed orphans showed
                # ``counts.failed=0`` even though the summary endpoint
                # (which has the correct logic at line 4457-4471) showed
                # ``failed=2``. Mirror ``/api/plan/.../summary`` so both
                # endpoints agree on terminal orphan accounting.
                # ``failure_reason`` and ``end_ts`` are copied through so
                # the card builder can render the failure detail.
                # 2026-09-12 plan v14 follow-up: also pull the real
                # title from the runtime overlay so the failure card
                # shows the operator which task failed (not the bare
                # "Task RP-1 (DB-only)" placeholder).
                _orphan_rt_term = runtime.get(tid) or {}
                _real_title_term = (
                    _orphan_rt_term.get("title")
                    or _orphan_rt_term.get("description")
                    or ""
                ).strip()
                tasks.append({
                    "id": tid,
                    "title": _real_title_term or f"Task {tid} (DB-only)",
                    "status": status,
                    "failure_reason": rt.get("failure_reason"),
                    "end_ts": rt.get("end_ts"),
                    "_origin": "db_orphan_terminal",
                })
                continue
            # Non-terminal orphan — surface a placeholder so
            # `current` (in_progress lookup) can find it.
            # 2026-09-12: the previous code hardcoded
            # "Task {tid} (DB-only)" which gave operators no clue
            # which task they were looking at. ``runtime`` is
            # already loaded from PlanTaskRepository above, so
            # prefer the real ``title`` (and ``description``) when
            # available; only fall back to the placeholder when
            # the row has no title (true db_orphan with empty
            # static fields, which shouldn't happen for RP-*
            # bootstrap-injected tasks but might for legacy
            # orphans).
            _orphan_rt = runtime.get(tid) or {}
            _real_title = (
                _orphan_rt.get("title")
                or _orphan_rt.get("description")
                or ""
            ).strip()
            tasks.append({
                "id": tid,
                "title": _real_title or f"Task {tid} (DB-only)",
                "description": (
                    _orphan_rt.get("description")
                    or f"recovered from plan_tasks DB (no static fields "
                       f"on disk; status={status!r}; see plan_tasks row "
                       f"for the real task identity)"
                ),
                "status": status,
                "end_ts": rt.get("end_ts"),
                "_origin": "db_orphan",
            })
    except Exception:
        # Defensive — never let hydrate break the progress endpoint.
        pass

    # --- Counts: prefer SQLite plan_execution.task_progress ----------
    # 2026-09-10 v4 follow-up: re-sync ``total`` after the orphan
    # hydrate step so progress counts reflect disk+DB, not disk only
    # (otherwise an orphan flipping to in_progress would show
    # ``total=63, in_progress=1`` → "118% 超出" again).
    counts = {"total": len(tasks), "completed": 0, "failed": 0, "in_progress": 0, "pending": 0}

    # The SQLite ``task_progress`` column has two on-disk shapes that
    # have shown up over time:
    #
    #   * **Aggregated** (``{"total": N, "completed": N, ...}``) —
    #     legacy schema written by the very first state-machine
    #     migration; lifts counts directly.
    #   * **Per-task** (``{"tasks": {"1-1": {"status": "failed", ...}, ...}}``) —
    #     current schema written by ``task_manager`` runtime overlay
    #     (see lines around 3875 above); the authoritative per-task
    #     ``status`` has already been merged into ``tasks`` via
    #     ``PlanTaskRepository.load_all``, so we can simply count
    #     from ``tasks`` afterwards.
    #
    # The old ``not (isinstance(db_progress, dict) and db_progress)``
    # gate skipped the per-task counting whenever ``db_progress`` was
    # truthy, which meant a current-schema row left ``counts`` at its
    # initialised zeros and the Telegram progress card showed
    # ``进行中 0  待执行 0`` even while a task was actively running.
    # Use ``AGGREGATE_KEYS`` membership to decide instead: if the row
    # is the legacy aggregated shape, trust it; otherwise count from
    # ``tasks`` (which is the per-task source of truth either way).
    AGGREGATE_KEYS = ("total", "completed", "failed", "in_progress", "pending")
    # 2026-08-20 audit: the previous "trust aggregated row OR count from
    # tasks" heuristic was wrong because a stale aggregated row (e.g.
    # {"total": 50, "completed": 0} written before task #3.8) silently
    # won the gate and reported 0 completed for plans that actually had
    # 43 completed tasks in the per-task overlay. The per-task map has
    # ALREADY been merged onto ``tasks`` via PlanTaskRepository.load_all
    # above, so it is always the authoritative source.
    for t in tasks:
        st = t.get("status", "pending")
        if st in counts:
            counts[st] += 1
    # Sanity-check: log a warning if the SQLite aggregated row disagrees
    # with the per-task overlay by more than 5 tasks, so operators can
    # spot stale aggregations early.
    if isinstance(db_progress, dict):
        for key in AGGREGATE_KEYS:
            if key in db_progress and key in counts:
                diff = abs((db_progress.get(key, 0) or 0) - counts[key])
                if diff > 5:
                    print(
                        f"[execution-progress] plan_id={plan_id} "
                        f"counts.{key} drift={diff} "
                        f"(db_progress={db_progress.get(key)} vs "
                        f"per_task={counts[key]})"
                    )

    current = None
    for t in tasks:
        st = t.get("status", "pending")
        if st == "in_progress" and current is None:
            current = {"id": t.get("id"), "title": t.get("title"), "description": t.get("description")}

    # Fallback: if no in_progress task, surface first pending as "next"
    next_task = None
    if current is None:
        for t in tasks:
            if t.get("status", "pending") == "pending":
                next_task = {"id": t.get("id"), "title": t.get("title")}
                break

    s = _server._execution_state.get(plan_id, {})

    # Detect API errors in failed tasks for frontend alerting
    api_error = None
    for t in tasks:
        if t.get("status") == "failed":
            reason = t.get("failure_reason") or ""
            if reason.startswith("[API_ERROR:"):
                import re
                m = re.match(r"\[API_ERROR:([^\]]+)\]\s*(.*)", reason)
                if m:
                    api_error = {"status": m.group(1), "message": m.group(2), "task_id": t.get("id")}
                break

    # Derive execution_status from in-memory state or tasks.json (for resilience after server restart)
    # If in-memory state is missing or shows "not_started" despite having task progress, derive from tasks.json
    execution_status = s.get("status")
    has_task_progress = counts["completed"] > 0 or counts["failed"] > 0 or counts["in_progress"] > 0
    if not execution_status or (execution_status == "not_started" and has_task_progress):
        if counts["in_progress"] > 0:
            execution_status = "running"
        elif counts["pending"] > 0:
            if counts["completed"] > 0 or counts["failed"] > 0:
                execution_status = "running"
            else:
                execution_status = "not_started"
        elif counts["failed"] > 0:
            execution_status = "failed"
        elif counts["completed"] > 0 and counts["total"] > 0:
            execution_status = "completed"
        else:
            execution_status = "not_started"

    return {
        "plan_id": plan_id,
        "pid": s.get("pid"),
        "project_dir": str(project_dir),
        "execution_status": execution_status,
        "tasks": tasks,
        "current": current,
        "next": next_task,
        "counts": counts,
        "stop_reason": data.get("stop_reason") if isinstance(data, dict) else None,
        "stop_detail": data.get("stop_detail") if isinstance(data, dict) else None,
        "api_error": api_error,
        "started_at": s.get("started_at"),
        "ended_at": s.get("ended_at"),
        "execution_stop_reason": s.get("stop_reason"),
    }


@router.get("/api/execution/{plan_id}/files")
def get_execution_files(plan_id: str):
    """List files currently in the project directory (excludes vcs/build dirs).

    Useful for AI agents to understand the realized system architecture.

    The recursive scan is cached per ``project_dir`` for
    ``_FILES_CACHE_TTL_SECONDS`` (see :data:`_FILES_CACHE`) so repeated
    polls within the TTL do not re-walk the tree. The 404 path is
    checked before the cache read, so a missing project directory is
    never served from a stale entry.
    """
    project_dir = _server._get_project_dir(plan_id)
    if not project_dir or not project_dir.exists():
        raise HTTPException(404, "Project directory not found")

    cached = _server._read_files_cache(project_dir)
    if cached is not None:
        return cached

    entries = []
    for p in project_dir.rglob("*"):
        if any(part in _server._FILES_EXCLUDE_DIRS for part in p.parts):
            continue
        if p.is_file():
            try:
                size = p.stat().st_size
            except Exception:
                size = 0
            entries.append({
                "path": str(p.relative_to(project_dir)),
                "size": size,
            })
    entries.sort(key=lambda e: e["path"])
    result = {"project_dir": str(project_dir), "files": entries, "count": len(entries)}
    _server._write_files_cache(project_dir, result)
    return result


@router.get("/api/execution/{plan_id}/logs")
def get_execution_logs(
    plan_id: str,
    level: Optional[str] = None,
    task_id: Optional[str] = None,
    event: Optional[str] = None,
    limit: int = 100,
    since: Optional[str] = None,
):
    """Query structured execution logs with filters.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        task_id: Filter by task ID
        event: Filter by event type (e.g., task_failed, refine_completed)
        limit: Maximum entries to return (default 100, max 1000)
        since: ISO timestamp — return entries after this time
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    limit = min(limit, 1000)
    entries = _server.ExecutionLogger.read_logs(
        plan_id,
        plans_dir=_server.PLANS_DIR,
        level=level,
        task_id=task_id,
        event=event,
        limit=limit,
        since=since,
    )
    return {
        "plan_id": plan_id,
        "entries": entries,
        "logs": entries,
        "total": len(entries),
        "filters": {"level": level, "task_id": task_id, "event": event, "since": since},
    }


@router.get("/api/execution/{plan_id}/diagnose")
def diagnose_execution(plan_id: str):
    """AI-friendly diagnostic summary for stuck or failed executions.

    Analyzes execution logs to detect patterns: repeated failures, timeouts,
    refinement loops, stuck duration, etc. Returns actionable suggestions.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    return _server.ExecutionLogger.diagnose(plan_id, plans_dir=_server.PLANS_DIR)

