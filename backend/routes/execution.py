"""Execution lifecycle: start / status / progress / stop / artifacts.

Extracted from ``server.py`` on 2026-09-25. See ``routes/phases.py`` for the
late-binding rule that governs every module in this package.
"""

from __future__ import annotations

from utils.atomic_io import atomic_write_json
from fastapi import APIRouter

from typing import Dict, List, NamedTuple, Optional, Tuple, Any, Literal
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path
from datetime import datetime, timezone
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

# The single place that decides whether a ``project_dir`` is usable.
# Imported by value (it has no state and no late binding to preserve):
# the route below must not have a second opinion about what a working
# directory is.
from project_dir_guard import ProjectDirInvalidError, validate_project_dir

# The park's kind is decided ONCE — in the module that owns the
# decision — and written down by the writer that records the park.
# Imported by value for the same reason as the guard above: the
# disposition must not have a second opinion about what kind of park it
# is recording, and a second opinion is exactly how a hard cap ends up
# described as a window that reopens by itself.
from capacity_wait import WAIT_KIND_FIELD, quota_wait_event_data

router = APIRouter()


def _restore_execution_state_from_row(
    plan_id: str, row: Dict[str, Any]
) -> Dict[str, Any]:
    """Put the row's run back into this process's in-memory registry.

    The registry (``server._execution_state``) is one process's view of
    a run; the ``plan_execution`` row is the durable record of it.  The
    registry is empty in every process that did not start the run — a
    fresh boot whose recovery scan has not walked this plan yet, another
    process serving for it, a plan whose directory the scan never saw.

    The registry is also what the liveness rule reads: the watchdog
    (``verification_loop._lazy_check_execution``) judges the in-memory
    status and probes the in-memory pid.  So a row that says
    ``running`` while its process is gone is never probed at all when
    the registry is empty — and the read reports a run that is still
    going, forever.

    Restoring the row here hands it to the standing rule instead of
    inventing a verdict for it.  The shape is the one the boot-time
    scan restores, so a plan restored here and a plan restored at boot
    are judged by the same rule and answer identically.

    The caller decides *whether* to restore: the boot scan restores
    unconditionally (the registry is empty at boot), while a status
    read restores only a gap — this process's own view of a run it is
    executing is more detailed than the row (it holds the process
    handle and the logs), and the row's *verdict* is adopted into that
    view separately.
    """
    status, stop_reason = _execution_verdict(row)
    state = {
        "status": status,
        "started_at": row.get("started_at"),
        # A terminal verdict carries its end time, and the row's own
        # stamp is that time: the row's last write IS the verdict.  So
        # a restored plan answers with the end time the verdict was
        # written with, not with a second derivation of it.
        "ended_at": _verdict_stamp(row),
        "pid": row.get("exec_pid"),
        "stop_reason": stop_reason,
        # Runtime-only, like ``sync_targets``: the row's stamp is the
        # durable copy of it.
        "verdict_persisted_at": _verdict_stamp(row),
        "project_dir": row.get("project_dir"),
        "logs": deque(maxlen=2000),
        "process": None,
    }
    _server._execution_state[plan_id] = state
    return state


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
            # Restore in-memory state (handle None values gracefully).
            # The status and the pid are read out of the restored state,
            # so the scan judges exactly what a reader would see.
            state = _restore_execution_state_from_row(plan_id, row)
            status = state.get("status")
            pid = state.get("pid")

            # If still running, check if the process is alive
            if status == "running":
                if not pid:
                    _judge_process_dead(plan_id, row)
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    _judge_process_dead(plan_id, row)
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


# ---------------------------------------------------------------------------
# The resume point — a paused execution continued
# ---------------------------------------------------------------------------
#
# A stop is a pause: it leaves a checkpoint on disk (see ``pause_state``
# and ``_stop_running_execution``). The start is the other half — the
# one that *continues* the paused work instead of re-running it from
# scratch.
#
# Three things follow from the checkpoint being the record of an
# interruption:
#
# * **The project directory comes from the checkpoint.** The operator
#   stopped a run that was working in a particular tree; a resume that
#   ran somewhere else would continue a task in a directory it was
#   never written for. So the checkpoint wins over the request body,
#   and a start *without* a checkpoint still has to name the directory
#   (the 2026-07-16 audit guard).
# * **Only the tasks that can continue in their session do.** A task
#   the interruption invalidated (``full_rerun``) is re-run whole, and
#   handing the executor its old session id would resume a
#   conversation that can no longer be trusted.
# * **The checkpoint is retired only once the work actually started.**
#   A spawn that failed continued nothing, so the interruption is
#   still the truth about what was interrupted.

#: The variable the executor reads the session mapping under
#: (``agent.RESUME_SESSIONS_ENV``). Spelled out here so the route does
#: not have to import the executor module to name one variable.
_RESUME_SESSIONS_ENV = "PDT_RESUME_SESSIONS"

#: The two ways a checkpointed task can be continued.
_RESUME_SESSION = "session_resume"
_RESUME_FULL_RERUN = "full_rerun"


def _read_execution_checkpoint(plan_dir: Path) -> Optional[Dict[str, Any]]:
    """The plan's pause checkpoint, when it is an *execution* one.

    ``pause_state`` owns the file's shape; this reader only answers the
    question the start endpoint asks: "is there an interrupted execution
    that this start should continue?".

    A checkpoint of another kind — an interrupted *verification* round —
    is not ours. It belongs to a different resume and must survive
    untouched.

    A checkpoint with no ``kind`` at all is treated as an execution one:
    that is the kind the execution stop has always written, and a
    resume that refused to honour it would strand the very plan it was
    written for.
    """
    try:
        from pause_state import read_pause_checkpoint

        checkpoint = read_pause_checkpoint(plan_dir)
    except Exception:  # noqa: BLE001 — an unreadable checkpoint is "no checkpoint"
        return None
    if not isinstance(checkpoint, dict):
        return None
    kind = checkpoint.get("kind")
    if kind and kind != "execution":
        return None
    return checkpoint


def _resume_entry_decision(entry: Dict[str, Any]) -> Dict[str, Any]:
    """How one interrupted task continues, and why.

    A checkpoint written before the ``strategy`` field existed carries
    no strategy: a session id is then the only evidence that there is
    something to re-open, and without one the task runs again from the
    beginning. The same fallback covers the reason, so a decision is
    never reported without one.
    """
    raw_session = entry.get("session_id")
    session_id = (
        raw_session if isinstance(raw_session, str) and raw_session else None
    )

    strategy = entry.get("strategy")
    if strategy not in (_RESUME_SESSION, _RESUME_FULL_RERUN):
        strategy = _RESUME_SESSION if session_id else _RESUME_FULL_RERUN

    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "session_id_available" if session_id else "no_session_id"

    raw_task_id = entry.get("task_id")
    task_id = str(raw_task_id) if raw_task_id is not None else ""

    return {"task_id": task_id, "strategy": strategy, "reason": reason}


def _resolve_resume_plan(
    checkpoint: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]], Dict[str, str]]:
    """The strategy, the per-task plan, and the session mapping.

    Returns ``(resume_strategy, resume_plan, sessions)``:

    * ``resume_strategy`` — ``"session_resume"`` when every interrupted
      task continues in its own session, ``"full_rerun"`` when none of
      them can, ``"mixed"`` when the two are mixed;
    * ``resume_plan`` — one decision per interrupted task: the task id,
      the strategy, and the reason;
    * ``sessions`` — the task id → session id mapping for the tasks
      that continue *in* a session. A ``full_rerun`` task is
      deliberately absent: the executor re-runs it from the beginning,
      and handing it a session id would resume a conversation the
      interruption invalidated.
    """
    plan: List[Dict[str, Any]] = []
    sessions: Dict[str, str] = {}

    for entry in checkpoint.get("interrupted_tasks") or []:
        if not isinstance(entry, dict):
            continue
        decision = _resume_entry_decision(entry)
        plan.append(decision)
        if decision["strategy"] != _RESUME_SESSION:
            continue
        session_id = entry.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if decision["task_id"]:
            sessions[decision["task_id"]] = session_id

    strategies = {decision["strategy"] for decision in plan}
    if _RESUME_SESSION in strategies and _RESUME_FULL_RERUN in strategies:
        overall = "mixed"
    elif _RESUME_SESSION in strategies:
        overall = _RESUME_SESSION
    else:
        overall = _RESUME_FULL_RERUN
    return overall, plan, sessions


def _resume_env(sessions: Dict[str, str]) -> Dict[str, str]:
    """The environment the executor needs to continue in its sessions.

    ``PDT_RESUME_SESSIONS`` carries a JSON object of task id → session
    id. Only the tasks that continue *in* a session are in it — see
    :func:`_resolve_resume_plan`.
    """
    return {_RESUME_SESSIONS_ENV: json.dumps(sessions)}


def _consume_execution_checkpoint(plan_dir: Path) -> None:
    """Retire the execution checkpoint this start has honoured.

    Called only *after* the executor has been spawned: a start that
    could not spawn continued nothing, and the resume point must
    survive it — otherwise the interruption is lost and the plan can
    only be re-run from scratch.

    Only an execution checkpoint is consumed. A checkpoint of another
    kind belongs to a different resume and must not be deleted here.
    """
    try:
        from pause_state import consume_pause_checkpoint, read_pause_checkpoint

        checkpoint = read_pause_checkpoint(plan_dir)
        if not isinstance(checkpoint, dict):
            return
        kind = checkpoint.get("kind")
        if kind and kind != "execution":
            return
        consume_pause_checkpoint(plan_dir)
    except Exception:  # noqa: BLE001 — a checkpoint we cannot retire is not fatal
        _server.logger.warning(
            "execution_start: could not consume the execution checkpoint — "
            "it stays on disk and may be consumed again",
        )


#: 2026-10-24 — the marker the executor leaves in the top level of
#: ``tasks.json`` when a run parks because every provider had no
#: capacity left (written by ``agent._stop_reason_for_quota_wait``).
#:
#: A plan carrying this marker has NOT failed: its tasks are still
#: eligible for another dispatch, which is the whole point of the
#: marker.  The disposition for such a run is therefore "parked", not
#: "failed" and not "skipped".
_QUOTA_WAIT_STOP_REASON = "waiting_for_quota"

#: The verdict a process death gets.  It is written to the row, so it
#: is the reason every later reader sees — a restart included.
_CRASH_STOP_REASON = "process_died_unexpectedly"

#: The execution statuses that mean the run is over.  A run that is
#: over has an end time; a parked run (``waiting_for_quota``) and a
#: live one have not ended yet.  The same set the repository uses to
#: decide that a pid has to go back to NULL.
_TERMINAL_EXEC_STATUSES = frozenset({"completed", "failed", "stopped"})

#: The provenance of a status answer: where the answer was read from.
#: ``state_db`` means it was assembled from the durable
#: ``plan_execution`` row; ``default`` means the library really holds
#: no row for this plan and the answer is the empty default.
#:
#: The field exists because the two answers used to look alike.  A
#: reader that got ``status=not_started`` with every other field null
#: could not tell "this plan never ran" from "this process knows
#: nothing about this plan" — and the second one is what a run that
#: was dying looked like.  The answer now certifies itself.
STATUS_DATA_SOURCE_STATE_DB = "state_db"
STATUS_DATA_SOURCE_DEFAULT = "default"

#: The fields the provenance of a status read carries.
#:
#: The block answers one question — **which database answered, and which
#: row did it find** — and it answers it in facts rather than in a word:
#: the database by path (so a reader knows WHICH file) and by inode (so
#: a reader knows it is not a different file wearing the same name), the
#: moment the read opened it, the row it found (by rowid and by the
#: row's own stamp), and the verdict of the integrity check that ran
#: before the row was trusted.
#:
#: It exists because the answer's own wording cannot say it.  A reader
#: that got ``status=not_started`` with every other field null could
#: not tell "this plan never ran" from "this instance cannot see the
#: plan" — and the second one is what a dying run used to look like.
#: A round that restarts the service inside the round is exactly where
#: the two come apart: two instances can read two different snapshots of
#: the same file and answer the same question differently.
STATUS_SOURCE_FIELDS = (
    "kind",
    "state_db_path",
    "state_db_inode",
    "opened_at",
    "row_updated_at",
    "rowid",
    "integrity",
)

#: The three states the library this instance reads can be in.
#:
#: * ``absent`` — there is no state database at all.  Nothing was ever
#:   recorded by this instance, so the empty answer is the honest one.
#: * ``unreadable`` — there is one and it could not be read.  This
#:   instance cannot confirm anything, so it refuses rather than
#:   answering.
#: * ``readable`` — it was read, and what it holds is what the answer
#:   is made of.
LIBRARY_ABSENT = "absent"
LIBRARY_UNREADABLE = "unreadable"
LIBRARY_READABLE = "readable"

#: The refusal's ``detail``.  It says what happened, not what the plan
#: is: the caller is being told that this instance cannot answer, not
#: that the plan never ran.
STATUS_REFUSAL_DETAIL = "the execution state of this plan could not be read"


class _ExecutionRead(NamedTuple):
    """What one read of the durable state found — and where it read it.

    ``row`` is the plan's ``plan_execution`` row (``None`` when the
    library holds none), ``source`` is the provenance block, ``library``
    is the state of the database the read tried to use, ``known`` says
    whether the library holds a row of its own for this plan, and
    ``populated`` says whether the library holds rows of its own at all.
    """

    row: Optional[Dict[str, Any]]
    source: Dict[str, Any]
    library: str
    known: bool
    populated: bool


def _utc_now_iso() -> str:
    """This instant, as an ISO-8601 stamp that carries a timezone."""
    return datetime.now(timezone.utc).isoformat()


def _quick_check(conn: sqlite3.Connection) -> Optional[str]:
    """The integrity check of the database this read opened.

    ``PRAGMA quick_check`` is the cheap half of SQLite's integrity
    check: it reads every page and verifies the b-trees against each
    other, without the index-consistency pass.  It runs BEFORE the row
    is trusted, because a database whose pages do not agree cannot be
    asked for the truth — and what this read produces is the truth
    about a run.

    ``None`` means the check could not be run at all (an old SQLite, a
    locked file).  The read still answers; the block then says the
    check did not run rather than claiming that it passed.
    """
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    except Exception:  # noqa: BLE001 — a check that cannot run is not a crash
        return None
    if not row or row[0] is None:
        return None
    return str(row[0])


def _row_identity(
    conn: sqlite3.Connection, plan_id: str
) -> Tuple[Optional[int], Optional[str]]:
    """The row's own identity: its ``rowid`` and the stamp of its last write.

    Two rows in two snapshots of the same file are told apart by these
    two facts, so they are what the answer carries: "the row you are
    hearing about is *this* row of *this* file, as of *this* write".
    """
    try:
        row = conn.execute(
            "SELECT rowid, updated_at FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001 — an unreadable row is not a crash
        return (None, None)
    if not row:
        return (None, None)
    rowid = int(row[0]) if row[0] is not None else None
    return (rowid, row[1])


def _library_holds_rows(conn: sqlite3.Connection) -> bool:
    """Does this library hold any row of its own?

    An empty library and a library that holds other plans' rows are two
    different things, and the difference decides what may be claimed:

      * **empty** — this instance has never recorded anything.  There is
        nothing here that could have described a run, so there is
        nothing here that contradicts the plan's directory;
      * **populated** — this instance records runs, and it has never
        heard of *this* plan, while the plan's directory is right
        here.  The database and the tree do not describe each other,
        and that is the shape a stale snapshot takes.

    The check asks the database itself, on the same connection the row
    was read from, so the two facts describe one snapshot.
    """
    try:
        row = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM plan_routing) "
            "OR EXISTS(SELECT 1 FROM plan_execution)"
        ).fetchone()
    except Exception:  # noqa: BLE001 — an unreadable library is not a crash
        return False
    return bool(row and row[0])


def _execution_verdict(row: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """The ``(status, stop_reason)`` a persisted execution row carries.

    One rule, every read path.  The row is the durable record of a run,
    so the row is what a reader must answer from — and two readers must
    not disagree about the same row, or the answer starts to depend on
    which process served the request (the bug this rule closes).

    The row's own ``stop_reason`` wins whenever it carries one.  A row
    that says ``waiting_for_quota`` but carries no reason of its own
    still says *why* it stopped: a quota wait has exactly one reason,
    the capacity wall.  That covers the rows written before the reason
    was persisted alongside the status.
    """
    status = row.get("exec_status") or "not_started"
    stop_reason = row.get("stop_reason")
    if not stop_reason and status == _QUOTA_WAIT_STOP_REASON:
        stop_reason = _QUOTA_WAIT_STOP_REASON
    return status, stop_reason


def _row_has_verdict(row: Optional[Dict[str, Any]]) -> bool:
    """Does this row carry a verdict of its own?

    A verdict is a status that is not "the run is going": a terminal
    one (``completed`` / ``failed`` / ``stopped``) and the parked one
    (``waiting_for_quota``).  A row that says ``running`` — or a row
    that says nothing at all — carries no verdict, and the process
    that owns the run is still the only one who knows what is
    happening to it.
    """
    if not row:
        return False
    status = str(row.get("exec_status") or "").strip()
    if not status or status == "not_started":
        return False
    return status != "running"


def _verdict_stamp(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """When a persisted verdict was written — the row's own stamp.

    ``plan_execution`` has no end-time column of its own.  What it has
    is ``updated_at``: the stamp of the row's last write.  For a
    terminal verdict that write *is* the verdict — the status, the
    reason and the cleared pid all land in it — so the row's stamp
    answers "when did this run end" and "when was this verdict
    persisted" with one answer, and it answers it identically before
    and after a restart.  That is what makes a verdict replayable
    instead of re-derived: a reader that replays the row hands back
    the same end time the writer recorded, rather than a second guess
    at when the run stopped.

    A parked run has not ended: ``waiting_for_quota`` is a pause, and
    reporting the row's stamp as its end time would tell the operator
    a paused run is over.  So only a terminal verdict gets a stamp.
    """
    if not row:
        return None
    if str(row.get("exec_status") or "") not in _TERMINAL_EXEC_STATUSES:
        return None
    return row.get("updated_at")


def _row_pid(row: Dict[str, Any], status: str) -> Optional[int]:
    """The pid the row carries — unless the verdict is over.

    A pid left on a terminal verdict belongs to a process that has
    already exited, and every "is this execution alive?" reader
    believes a pid means alive.  So a row that says the run is over
    reports no pid; the process handle it used to carry is a ghost.

    A run that is still going (``running``) or paused
    (``waiting_for_quota``) keeps its pid: for the parked run the pid
    is the one the executor will be resumed under, and for the live run
    it is the process the operator can look at.
    """
    if status in _TERMINAL_EXEC_STATUSES:
        return None
    return row.get("exec_pid")


def _memory_verdict(plan_id: str) -> Tuple[Optional[str], Optional[str]]:
    """The ``(status, stop_reason)`` this process's memory holds."""
    state = _server._execution_state.get(plan_id)
    if not state:
        return (None, None)
    return (state.get("status"), state.get("stop_reason"))


def _adopt_row_verdict(plan_id: str, row: Optional[Dict[str, Any]]) -> None:
    """Make this process's memory agree with the row's verdict.

    The row is the truth about a run; the in-memory map is one
    process's view of it.  The two can disagree — another process
    parked the plan on capacity, another process judged a crash — and
    the disagreement is exactly what made the answer depend on which
    process served the request (the bug this rule closes).

    The adoption is not cosmetic.  The watchdog judges liveness from
    the in-memory status, and it must not judge a row that is not
    ``running``: a plan parked on capacity keeps a pid whose process
    is long gone, and judging that pid is how a quota pause gets read
    as a crash.
    """
    if not _row_has_verdict(row):
        return
    state = _server._execution_state.get(plan_id)
    if state is None:
        return
    status, stop_reason = _execution_verdict(row)
    state["status"] = status
    if stop_reason:
        # A row that carries no reason of its own — the executor
        # watcher writes the status alone — must not wipe the reason
        # this process knows.
        state["stop_reason"] = stop_reason
    if status in _TERMINAL_EXEC_STATUSES:
        state["ended_at"] = _verdict_stamp(row)
        # A **crash** is the one terminal verdict that says the process
        # is gone: the pid belongs to a process that no longer exists,
        # and every "is this execution alive?" reader believes a pid
        # means alive.  So the crash takes the pid with it.
        #
        # The other terminal verdicts are the executor's OWN verdict —
        # the run finished, failed or was stopped and the process said
        # so before it exited.  The pid is then a fact about the run
        # ("this plan was executed by that process"), and the run's
        # reader is entitled to it: erasing it made a settled
        # execution answer "no pid", which reads as "no executor ever
        # ran" — a claim about the process boundary that is false.
        # Liveness is answered by ``execution_in_flight`` and by the
        # row's own verdict, not by the presence of a pid.
        if stop_reason == _CRASH_STOP_REASON:
            state["pid"] = None
    # Runtime-only, like ``sync_targets``: the row's stamp is the
    # durable copy of it.
    state["verdict_persisted_at"] = _verdict_stamp(row)


def _write_crash_verdict(plan_id: str) -> Optional[Dict[str, Any]]:
    """Write a process death to the row — the whole verdict, at once.

    The verdict goes to the row through ``ExecutionRepository``: the
    status, the reason and the (now meaningless) pid, in one write.  A
    row that carries only the status comes back after a restart as a
    failure with no reason; a row that carries nothing comes back as
    ``running`` — a crash that reads as something else entirely.

    **Only a row that says ``running`` may be judged.**  A row that
    already carries a verdict is the run's end — terminal, or parked
    on capacity — and a verdict is never re-judged.  That is what
    keeps a quota pause from being read as a crash.

    Returns the row as it stands after the write, or ``None`` when
    the row was not judged (no row at all, or a row that carries a
    verdict already).
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
        PlanNotFoundError as ExecPlanNotFoundError,
    )

    row = _persisted_execution_row(plan_id)
    if row is None:
        # No row: there is no run to judge, and a reader must not
        # invent a failure for a plan that never ran.
        return None
    if str(row.get("exec_status") or "") != "running":
        return None
    try:
        persist_conn = open_db(_server._state_db_path())
        try:
            migrate(persist_conn)
            ExecutionRepository(persist_conn).update_fields(
                plan_id,
                exec_status="failed",
                stop_reason=_CRASH_STOP_REASON,
                # The executor is gone; a pid left on a row that says
                # ``failed`` reads as a live execution to every reader.
                exec_pid=None,
            )
        finally:
            persist_conn.close()
    except ExecPlanNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — a verdict write must not 500 a poll
        _server.logger.exception(
            "[execution_status] could not persist the crash verdict plan=%s",
            plan_id,
        )
        return None
    return _persisted_execution_row(plan_id)


def _judge_process_dead(
    plan_id: str, row: Optional[Dict[str, Any]]
) -> bool:
    """Judge a dead process — once, and only on a row that says ``running``.

    The decision to re-judge a run is made on the **row**, not on this
    process's memory: the row is what outlives the process, and a row
    that already carries a verdict — the run is over, or it is parked
    on capacity — is never judged again.  That is what keeps a quota
    pause from being read as a crash, and a crash from being washed
    into a quota pause.

    The verdict is written to the row in full, at the moment it is
    reached: the status, the reason, and the (now meaningless) pid.
    The row's own stamp then becomes the end time, so every later
    reader — the next boot, another process, a poller that never saw
    the run — replays the same verdict instead of re-deriving it.

    Returns whether the run was judged.
    """
    if not row or str(row.get("exec_status") or "") != "running":
        # No row: there is no run to judge.  A row that already carries
        # a verdict: it is the run's end, and a verdict is never
        # re-judged.
        return False
    state = _server._execution_state.get(plan_id)
    if not state or state.get("status") != "running":
        return False
    # The shared helper flips this process's memory, writes the row and
    # syncs the plan state.
    _server._mark_failed_dead(plan_id)
    # The row now carries the verdict; its stamp is the end time, and
    # this process's memory adopts it so the two never disagree.
    _adopt_row_verdict(plan_id, _persisted_execution_row(plan_id))
    return True


def _persisted_execution_row(plan_id: str) -> Optional[Dict[str, Any]]:
    """Read the durable ``plan_execution`` row for ``plan_id``.

    The in-memory map is not the whole truth: it lives in the process
    that started the run, so it is empty in every process that did not
    — a fresh boot whose recovery scan has not run yet, or a second
    process answering for a plan the first one is running.  The row is
    what outlives the process, so the row is what a reader falls back
    to.

    Returns ``None`` when there is no row (the plan has never been
    configured for execution) and when the row cannot be read (the state
    machine is not on disk yet, or the read failed).  A failed read is
    answered as "no row" rather than as a 500: a status poll must not
    take the interface down.
    """
    sm = _server._open_state_machine()
    if sm is None:
        # No state.db yet — nothing was ever recorded for this plan.
        return None
    _conn, _routing, execution_repo, _verification, _artifact = sm
    try:
        return execution_repo.summary(plan_id)
    except Exception:  # noqa: BLE001 — a status poll must not 500
        _server.logger.exception(
            "[execution_status] could not read the execution row plan=%s",
            plan_id,
        )
        return None
    finally:
        _server._close_state_machine(sm)


def _read_execution_state(plan_id: str) -> _ExecutionRead:
    """The durable row for ``plan_id`` — and the provenance of that read.

    One read, one answer, and the facts about the read itself.  The row
    is what the answer is made of (the in-memory registry is one
    process's view of a run and goes stale the moment another process
    touches the plan), and the block says WHICH database produced it.

    The three outcomes are told apart, because they are three different
    answers:

      * **a row** — the run is there, and it is what the answer is made
        of;
      * **no row, but the library was read** — the library holds no run
        for this plan, and the block says which library that was;
      * **the library could not be read** — nothing can be confirmed,
        and the caller refuses rather than answering a default.

    The database is opened, checked and read ONCE here: the integrity
    check and the row read share the connection, so they describe one
    snapshot rather than two reads that might straddle a write.  That
    matters inside a round that restarts the service — two reads of
    the same file can land on two different snapshots.
    """
    db_path: Optional[Path] = None
    try:
        db_path = _server._state_db_path()
    except Exception:  # noqa: BLE001 — an unresolvable path is not a crash
        db_path = None
    inode: Optional[int] = None
    if db_path is not None:
        try:
            inode = int(db_path.stat().st_ino)
        except OSError:
            inode = None

    source: Dict[str, Any] = {field: None for field in STATUS_SOURCE_FIELDS}
    source["kind"] = STATUS_DATA_SOURCE_DEFAULT
    source["state_db_path"] = str(db_path) if db_path is not None else None
    source["state_db_inode"] = inode
    source["opened_at"] = _utc_now_iso()

    if db_path is None or not db_path.exists():
        # No database at all: this instance has never recorded a run.
        # The library is empty rather than broken, so the empty answer
        # is the honest one.
        return _ExecutionRead(None, source, LIBRARY_ABSENT, False, False)

    sm = _server._open_state_machine()
    if sm is None:
        # There IS a database and it could not be opened: this instance
        # cannot confirm anything about the plan, and "not started"
        # would be a claim it is not entitled to make.
        source["kind"] = STATUS_DATA_SOURCE_STATE_DB
        return _ExecutionRead(None, source, LIBRARY_UNREADABLE, False, False)

    conn, routing, execution_repo, _verification, _artifact = sm
    try:
        source["integrity"] = _quick_check(conn)
        row = execution_repo.summary(plan_id)
        rowid, updated_at = _row_identity(conn, plan_id)
        source["rowid"] = rowid
        source["row_updated_at"] = updated_at
        if row is not None:
            source["kind"] = STATUS_DATA_SOURCE_STATE_DB
            if source["row_updated_at"] is None:
                source["row_updated_at"] = row.get("updated_at")
        # Does the library hold a row of its own for this plan?  A
        # routing row means the library knows the plan — and a plan the
        # library knows but has no run for genuinely never ran.
        known = bool(routing.current(plan_id))
        # And does the library hold rows of its own at all?  An empty
        # library and a library that has never heard of this plan are
        # two different situations, and only the second one is a
        # contradiction.
        populated = _library_holds_rows(conn)
        return _ExecutionRead(row, source, LIBRARY_READABLE, known, populated)
    except Exception:  # noqa: BLE001 — a status poll must not 500
        _server.logger.exception(
            "[execution_status] could not read the execution state plan=%s",
            plan_id,
        )
        source["kind"] = STATUS_DATA_SOURCE_STATE_DB
        return _ExecutionRead(None, source, LIBRARY_UNREADABLE, False, False)
    finally:
        _server._close_state_machine(sm)


def _status_refusal_response(
    plan_id: str,
    read: _ExecutionRead,
    reason: str,
) -> JSONResponse:
    """The refusal: ``503``, with the store that could not answer.

    The body carries the same facts a successful answer carries — the
    database by path and by inode, the moment it was opened, the
    integrity check that ran against it — so a reader can see WHICH
    database was asked and go and look at it.
    """
    return JSONResponse(
        status_code=503,
        content={
            "detail": STATUS_REFUSAL_DETAIL,
            "reason": reason,
            "plan_id": plan_id,
            "data_source": STATUS_DATA_SOURCE_STATE_DB,
            "data_source_detail": read.source,
            "plans_dir": str(_server.PLANS_DIR),
            "plan_dir": str(_server._plan_dir(plan_id)),
        },
    )


def _refusal_when_the_run_cannot_be_confirmed(
    plan_id: str,
    read: _ExecutionRead,
) -> Optional[JSONResponse]:
    """The refusal: this instance cannot confirm that the plan never ran.

    "Not started" is a claim about a plan's history, and it is an
    honest claim only when this instance can back it up: the library
    was read, and it holds no run for this plan.

    Four situations, and only the last one is a refusal:

      * **the library holds the plan** — a routing row of its own and
        no execution row: the plan is known to this instance and it has
        never run, so "not started" is true and it is answered;
      * **the plan is not in this instance's tree** — there is no
        directory for it here, so there is nothing here to describe;
      * **the library is empty** — this instance has never recorded
        anything at all, so there is nothing here that could have
        described a run and nothing that contradicts the plan's
        directory.  The empty answer is the honest one;
      * **the library cannot see the plan** — it was read, it is
        intact, it holds rows of its own, and it holds no row for this
        plan at all, while the plan's directory is right there.  The
        database this instance reads does not describe the tree it
        serves: the shape a snapshot takes inside a round that restarts
        the service.  Answering "not started" here would be a claim
        about a run this instance cannot see — and a claim is worse
        than a refusal, because the caller believes it.

    A database that could not be read at all refuses in every case:
    nothing was confirmed, so nothing may be claimed.
    """
    if read.library == LIBRARY_ABSENT:
        # Nothing was ever recorded by this instance: the library is
        # empty, not foreign, and the empty answer is the honest one.
        return None
    if read.library == LIBRARY_UNREADABLE:
        return _status_refusal_response(
            plan_id,
            read,
            "the state database could not be read",
        )
    if read.known:
        # The library holds a row of its own for this plan and no
        # execution row: the plan is known here and it never ran.
        return None
    if not _server._plan_dir(plan_id).exists():
        # The plan is not in this instance's tree: there is nothing
        # here to describe.
        return None
    if not read.populated:
        # The library holds no row of its own: this instance has never
        # recorded a run, so it cannot contradict the plan's directory
        # either.  There is nothing to refuse about.
        return None
    return _status_refusal_response(
        plan_id,
        read,
        "the state database holds no row for a plan in this instance's "
        "tree",
    )


def _quota_wait_detail(tasks_file: Path) -> Optional[Dict[str, Any]]:
    """Read the ``waiting_for_quota`` marker out of ``tasks.json``.

    ``None`` means "this plan is not parked for quota" — no file, no
    parseable JSON, no ``stop_reason``, or a different ``stop_reason``.
    A marker whose ``reason_detail`` is missing / not JSON / not an
    object yields an empty dict: the marker is still a marker, there is
    just no detail to report.

    A readable detail is normalised to the four fields the marker
    carries (``providers`` / ``needs_manual_topup`` / ``waited_sec`` /
    ``at``) so a consumer never has to guess at a missing key.

    A marker that spells its kind is handed back as it was written:
    the read side does not get a second opinion about it.  A marker
    that does not spell one is left without one — the kind is decided
    when the park is recorded, and it is decided there from the
    numbers this function returns.
    """
    try:
        raw = Path(tasks_file).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — an unreadable plan is not a marker
        return None
    if not isinstance(data, dict):
        return None
    if data.get("stop_reason") != _QUOTA_WAIT_STOP_REASON:
        return None

    raw_detail = data.get("reason_detail")
    detail: Dict[str, Any] = {}
    if isinstance(raw_detail, str) and raw_detail.strip():
        try:
            parsed = json.loads(raw_detail)
        except Exception:  # noqa: BLE001 — a broken detail is no reason to lose the marker
            parsed = None
        if isinstance(parsed, dict):
            detail = parsed
    elif isinstance(raw_detail, dict):
        detail = raw_detail
    if not detail:
        return {}

    providers = detail.get("providers")
    if isinstance(providers, str):
        providers = [providers]
    elif not isinstance(providers, (list, tuple)):
        providers = []
    try:
        waited = float(detail.get("waited_sec") or 0.0)
    except (TypeError, ValueError):
        waited = 0.0

    normalised: Dict[str, Any] = {
        "providers": list(providers),
        "needs_manual_topup": bool(detail.get("needs_manual_topup", False)),
        "waited_sec": waited,
        "at": str(detail.get("at") or ""),
    }
    kind = detail.get(WAIT_KIND_FIELD)
    if kind:
        # Verbatim: the marker named the kind, so the kind is the
        # marker's to say.  Nothing here re-derives it.
        normalised[WAIT_KIND_FIELD] = str(kind)
    return normalised


def _quota_wait_log(
    plan_id: str,
    event: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one ``waiting_for_quota`` event in the plan's own log.

    The plan's ``execution.log`` is the same file the executor wrote
    ``task_quota_wait_exhausted`` into, so the whole "why did this run
    stop" story ends up in one place the operator (and the watchdog)
    already reads.  There is deliberately no second sink to reconcile.

    Logging must never break the disposition it is describing, so every
    failure here degrades to "nothing was written" — the plan stays
    parked either way.
    """
    payload = dict(data or {})
    try:
        from execution_logger import get_logger

        plan_logger = get_logger(plan_id=plan_id)
        if plan_logger is not None:
            plan_logger.warning(event, message, data=payload)
    except Exception:  # noqa: BLE001 — logging must never break a disposition
        pass


def _enter_quota_wait(
    plan_id: str,
    plan_dir: Path,
    state: Dict[str, Any],
    tasks_file: Path,
) -> bool:
    """Park the plan in ``waiting_for_quota`` — the quota disposition.

    Returns ``True`` when the plan carries the marker, and the caller
    MUST return immediately: no task is failed, no task is skipped, and
    the auto-verification loop must not run.  ``False`` means "no
    marker" and the caller keeps its normal disposition.
    """
    detail = _quota_wait_detail(tasks_file)
    if detail is None:
        return False

    # 1. The in-memory state first: it is what the status endpoint
    #    reads, and it must be right even if every write below fails.
    #    ``ended_at`` stays ``None`` — the run is parked, not over.
    state["status"] = _QUOTA_WAIT_STOP_REASON
    state["stop_reason"] = _QUOTA_WAIT_STOP_REASON
    state["ended_at"] = None

    # 2. Persist the execution status.  Best-effort: the plan is
    #    already parked in memory, and a failed write must NOT turn it
    #    into a failure.
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
            PlanNotFoundError as ExecPlanNotFoundError,
        )
    except Exception as exc:  # noqa: BLE001 — no state-machine, no write
        _quota_wait_log(
            plan_id,
            "execution_quota_wait_persist_failed",
            "Could not persist the waiting_for_quota status — the plan "
            "stays parked in memory.",
            {"error": str(exc)[:300]},
        )
    else:
        try:
            persist_conn = open_db(_server._state_db_path())
            try:
                migrate(persist_conn)
                ExecutionRepository(persist_conn).update_status(
                    plan_id, _QUOTA_WAIT_STOP_REASON,
                )
                # The reason belongs on the row too.  The row is what
                # outlives this process, and a reader that finds it
                # after a restart must be able to say *why* the run
                # stopped — not only that it is parked.  Without it the
                # durable record carries the verdict and drops the
                # reason, and every reader has to re-derive it.
                ExecutionRepository(persist_conn).update_fields(
                    plan_id, stop_reason=_QUOTA_WAIT_STOP_REASON,
                )
            finally:
                persist_conn.close()
        except ExecPlanNotFoundError as exc:
            # No ``plan_execution`` row for this plan: there is nothing
            # to write, which is not a failure of the plan itself.
            _quota_wait_log(
                plan_id,
                "execution_quota_wait_persist_failed",
                "Could not persist the waiting_for_quota status — the plan "
                "stays parked in memory.",
                {"error": str(exc)[:300]},
            )
        except Exception as exc:  # noqa: BLE001 — same contract as above
            _quota_wait_log(
                plan_id,
                "execution_quota_wait_persist_failed",
                "Could not persist the waiting_for_quota status — the plan "
                "stays parked in memory.",
                {"error": str(exc)[:300]},
            )

    # 3. Move the workflow phase.  ``executing → waiting_for_quota`` is
    #    a legal edge; anything else is forced, and the force is said
    #    out loud because it bypasses the transition table.
    #
    #    ``_server.PlanState`` on purpose — the late-bound name is what
    #    the tests patch, and ``from plan_state import PlanState``
    #    would bind the class at import time and defeat them.
    try:
        _server.PlanState(plan_dir).transition_to(_QUOTA_WAIT_STOP_REASON)
    except ValueError:
        try:
            _server.PlanState(plan_dir).force_set_phase(_QUOTA_WAIT_STOP_REASON)
        except Exception as exc:  # noqa: BLE001 — the status row is the truth
            _quota_wait_log(
                plan_id,
                "execution_quota_wait_persist_failed",
                "Could not move the plan into waiting_for_quota — the plan "
                "stays parked in memory.",
                {"error": str(exc)[:300]},
            )
        else:
            _quota_wait_log(
                plan_id,
                "execution_quota_wait_phase_forced",
                "The plan had no legal edge into waiting_for_quota — the "
                "phase was forced.",
                {"phase": _QUOTA_WAIT_STOP_REASON},
            )
    except Exception as exc:  # noqa: BLE001 — the status row is the truth
        _quota_wait_log(
            plan_id,
            "execution_quota_wait_persist_failed",
            "Could not move the plan into waiting_for_quota — the plan "
            "stays parked in memory.",
            {"error": str(exc)[:300]},
        )

    # 4. The event that says why the run stopped — with the kind of the
    #    park written down.
    #
    #    The payload comes from the module that owns the decision, so
    #    the kind and the numbers come from ONE place.  A reader is
    #    never left combining them by hand, and a hard cap can never be
    #    described as a window that reopens by itself.
    _quota_wait_log(
        plan_id,
        "execution_waiting_for_quota",
        "The run parked because every provider had no capacity — no task "
        "was failed and no task was skipped.",
        quota_wait_event_data(
            providers=detail.get("providers"),
            kind=detail.get(WAIT_KIND_FIELD),
            waited_sec=detail.get("waited_sec", 0.0),
            at=detail.get("at"),
            needs_manual_topup=bool(detail.get("needs_manual_topup", False)),
        ),
    )
    return True


def _record_project_dir_rejection(
    plan_id: str, plan_dir: Path, exc: ProjectDirInvalidError
) -> None:
    """Leave a diagnosable trace of a refused ``project_dir``.

    A refusal that says nothing is indistinguishable from a start that
    never happened: the operator sees a plan that is not running and no
    reason why. Two traces are written, and **both are best-effort** —
    a refusal must never turn into a 500 because the log directory or
    the database happened to be unreachable.

    1. ``<plan_dir>/logs/execution_start.log`` — one appended line
       naming the offending path verbatim, so the reason can be read
       off the plan's own directory without the server's stdout.
    2. The ``plan_execution`` row — ``exec_status='failed'``,
       ``exec_pid=NULL``, ``stop_reason='project_dir_invalid'`` — so
       every consumer that reads the row sees a failed run instead of a
       plan that silently never started. ``create_if_missing=True``
       because a plan whose row was never bootstrapped has no other
       home for this state.

    Args:
        plan_id: The plan whose start was refused.
        plan_dir: ``plans/<id>/`` — where the log line goes.
        exc: The refusal, carrying ``project_dir`` and ``reason``.
    """
    try:
        log_dir = plan_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "execution_start.log").open("a", encoding="utf-8") as handle:
            handle.write(
                f"{datetime.now().isoformat()} "
                f"project_dir_invalid plan={plan_id} "
                f"project_dir={exc.project_dir} reason={exc.reason}\n"
            )
    except Exception:  # noqa: BLE001 — a refusal must not become a 500
        _server.logger.exception(
            "execution_start: could not write the project_dir rejection log "
            "plan=%s project_dir=%s",
            plan_id,
            exc.project_dir,
        )

    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )

        conn = open_db(_server._state_db_path())
        try:
            migrate(conn)
            ExecutionRepository(conn).update_fields(
                plan_id,
                create_if_missing=True,
                project_dir=exc.project_dir,
                exec_status="failed",
                stop_reason="project_dir_invalid",
                exec_pid=None,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a refusal must not become a 500
        _server.logger.exception(
            "execution_start: could not persist the project_dir rejection "
            "plan=%s project_dir=%s",
            plan_id,
            exc.project_dir,
        )


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
      3. Persist ``project_dir`` and ``exec_pid`` via
         :meth:`ExecutionRepository.update_fields`.  The workflow
         phase is *not* written here — step 2's routing CAS owns it
         (schema v6: one phase column, one owner).  The transition is
         wrapped in the repository's own
         ``BEGIN IMMEDIATE`` transaction so partial writes are
         impossible.

    Resume contract (architecture decision point 4):

      4. A plan that was *stopped* has a pause checkpoint on disk. The
         start continues it: the checkpoint carries the project
         directory the work was happening in (so the operator does not
         have to repeat it, and the request body cannot redirect it),
         and it says per task whether that task continues in its own
         session or is re-run whole. Only the tasks that continue in a
         session are handed to the executor, through
         ``PDT_RESUME_SESSIONS``. A resumed start answers with
         ``resumed`` / ``resume_strategy`` / ``resume_plan`` on top of
         the four fields every start answers with; a fresh start keeps
         the response it has always published. The checkpoint is
         retired only once the executor has been spawned.
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

    # --- The resume point ---------------------------------------------
    # 2026-10-20: a plan that was stopped has a checkpoint on disk. It
    # is read before anything is written, because it decides two things
    # this start has to get right: where the work continues (the
    # checkpoint's project directory) and which tasks continue *in*
    # their own session (the ones the interruption left usable).
    checkpoint = _read_execution_checkpoint(plan_dir)
    resumed = checkpoint is not None
    resume_strategy = _RESUME_FULL_RERUN
    resume_plan: List[Dict[str, Any]] = []
    resume_sessions: Dict[str, str] = {}
    if resumed:
        (
            resume_strategy,
            resume_plan,
            resume_sessions,
        ) = _resolve_resume_plan(checkpoint)

    # The three resume fields are part of the answer *when there was a
    # resume point to answer about*. A fresh start keeps the response
    # it has always published — the four-key contract the endpoint's
    # own field-contract test pins — and a resumed start adds what it
    # continued and why.
    resume_fields: Dict[str, Any] = (
        {
            "resumed": True,
            "resume_strategy": resume_strategy,
            "resume_plan": resume_plan,
        }
        if resumed
        else {}
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
    #
    # 2026-10-20: a *paused* plan is the one case where the operator
    # does not have to say it again. The resume point carries the
    # directory the work was happening in, and it wins over the request
    # body — a resume that ran somewhere else would continue a task in
    # a directory it was never written for. Without a resume point the
    # guard below is unchanged.
    _requested_project_dir = (req.project_dir or "").strip()
    _checkpoint_project_dir = str(
        (checkpoint or {}).get("project_dir") or ""
    ).strip()
    _raw_project_dir = _checkpoint_project_dir or _requested_project_dir
    if not _raw_project_dir:
        raise HTTPException(
            400,
            "project_dir is required. Provide the absolute path to "
            "the project directory where the executor should "
            "checkout / commit. Defaulting to a relative path "
            "silently produces empty commits in the wrong git "
            "repo (audit 2026-07-16)."
        )
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

    # The directory the executor works in must already exist, and must
    # really be a directory. This used to be
    # ``mkdir(parents=True, exist_ok=True)``, which turned a typo — or a
    # worktree that had been removed — into a silently created empty
    # directory, and the real error only surfaced much later as a
    # confusing "no such file" from inside the subprocess.
    #
    # The check runs BEFORE the routing CAS, so a refused start leaves
    # the plan in the phase it was in (never ``executing``) and never
    # reaches the spawn. ``validate_project_dir`` is the single
    # definition of "usable working directory" (see
    # ``backend/project_dir_guard.py``) — it never creates anything.
    try:
        project_dir = validate_project_dir(project_dir)
    except ProjectDirInvalidError as exc:
        _record_project_dir_rejection(plan_id, plan_dir, exc)
        raise HTTPException(400, str(exc))

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
                # 2026-10-25 (R10): the phase a re-execution lands in
                # must be one the start whitelist accepts — ``queued``.
                # It used to be ``ready``, which the write path no
                # longer produces: the plan was parked in a phase that
                # could neither be started nor be picked up.
                ps.force_set_phase("queued")
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
                # 2026-10-25 (R10): same contract as the fallback
                # above — the phase must be ``queued``, the only
                # waiting phase the start whitelist accepts.
                ps.force_set_phase("queued")
                ps.transition_to("executing")
            else:
                raise

    # --- Step 2b: clear the cooperative cancel flag --------------------
    # 2026-10-23: a stop flips ``verification_cancel`` for this plan
    # (see ``_stop_running_execution``) so that the verification chain
    # does not re-enter behind the operator's back. A start is the
    # operator saying "go again" — so the flag has to go with it.
    #
    # Without this, P1 would be a one-way door: every later start of
    # this plan would be born cancelled, and the verification chain
    # would exit at its first check (verification_loop.py) and never
    # run a round again. The two halves are one contract: the stop
    # sets the flag, the start clears it.
    #
    # It is cleared AFTER the routing CAS (so a rejected start — 409 —
    # does not silently un-cancel a plan that was never restarted) and
    # BEFORE the spawn (so the executor that is about to run is not
    # born cancelled). Mirrors ``routes/verification.py``'s
    # repair-resume path.
    try:
        import verification_cancel
        verification_cancel.clear(plan_id)
    except Exception:  # noqa: BLE001 — a stale flag must not abort the start
        _server.logger.exception(
            "execution_start: could not clear the verification cancel "
            "flag plan=%s", plan_id,
        )

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
            # Only a resumed plan carries sessions: a fresh start has
            # nothing to continue, and an empty mapping would say so
            # just as well — but passing nothing keeps the fresh
            # executor's environment exactly as it was.
            extra_env=_resume_env(resume_sessions) if resumed else None,
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
                ExecutionRepository(persist_conn).update_fields(
                    plan_id,
                    create_if_missing=True,
                    exec_status="failed",
                    stop_reason="process_died_unexpectedly",
                    # The executor never came up, so there is no pid to
                    # keep.  A stale pid left on a row that says
                    # ``failed`` reads as a live execution to every
                    # "is this execution alive?" reader — the same ghost
                    # the terminal-status rule forbids (accident B,
                    # 2026-10-23).
                    exec_pid=None,
                )
            finally:
                persist_conn.close()
        except ExecPlanNotFoundError:
            pass
        try:
            _server.PlanState(plan_dir).transition_to("failed")
        except Exception:
            pass
        # The resume point is deliberately NOT consumed here: nothing was
        # continued, so the interruption is still the truth about what
        # was interrupted.
        return {
            "status": state["status"],
            "pid": None,
            **resume_fields,
        }
    state["process"] = process
    state["pid"] = process.pid

    # 2026-10-20: the executor is up — the resume point has been
    # honoured, so it is retired. A resume is single-shot: without the
    # delete, a restart loop would re-consume the same interruption on
    # every attempt and the plan would never move past it.
    if resumed:
        _consume_execution_checkpoint(plan_dir)

    # NOTE: the immediate "running" persist that used to live here is
    # redundant with the ``update_fields(exec_status="running", exec_pid=...)``
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
        ExecutionRepository(persist_conn).update_fields(
            plan_id,
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
                        # 2026-10-24: the executor parked the plan
                        # because every provider had no capacity left.
                        # The tasks are NOT failed — they are still
                        # eligible for another dispatch — so the
                        # disposition is "waiting_for_quota", not
                        # "failed" and not "blocked → skipped".
                        #
                        # This check comes FIRST, before the
                        # blocked-only branch below: that branch would
                        # skip the downstream tasks and auto-verify the
                        # incomplete subset, and the failed branch
                        # after it would strand every task that is only
                        # waiting for capacity to come back.
                        if _enter_quota_wait(
                            plan_id, plan_dir, state, tasks_file,
                        ):
                            return
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
        # 2026-10-20: what this start did with the resume point. Only a
        # resumed start has a resume point to report; a fresh start
        # keeps the response it has always published.
        **resume_fields,
    }


def _pause_fields(plan_id: str) -> Dict[str, Any]:
    """The pause half of a status answer, read from the checkpoint.

    The in-memory state cannot carry this: ``stop`` writes the
    checkpoint to the plan directory precisely because the process that
    held the in-memory record is the process that goes away. A plan
    paused and then left alone across a restart has no entry in
    ``_execution_state`` at all, so this read is the only way the
    endpoint can still say "paused, and this is when".

    A checkpoint that cannot be read — torn by a crash, written by a
    newer PDT — degrades to "not paused" rather than failing the
    status poll: ``checkpoint_summary`` already answers ``None`` for
    every unusable file, and an operator watching a plan is better
    served by a missing pause than by a 500 on a read-only endpoint.
    """
    from pause_state import checkpoint_summary

    try:
        summary = checkpoint_summary(_server._plan_dir(plan_id))
    except Exception:  # noqa: BLE001 — observability must not 500 a poll
        _server.logger.exception(
            "[execution_status] could not read the pause checkpoint plan=%s",
            plan_id,
        )
        summary = None

    if not summary:
        return {"paused": False, "paused_at": None, "interrupted_task_count": 0}
    return {
        "paused": True,
        "paused_at": summary.get("paused_at"),
        "interrupted_task_count": int(summary.get("interrupted_task_count") or 0),
    }


class _ExecutionStatusResponse(_server.ExecutionStatusResponse):
    """The status answer, plus the provenance of that answer.

    ``data_source`` says where the answer came from: ``state_db`` when
    it was assembled from the durable ``plan_execution`` row, and
    ``default`` when the library really holds no row for this plan and
    the answer is the empty default.

    The field exists because the two answers used to look alike.  A
    reader that got ``status=not_started`` with every other field null
    could not tell "this plan never ran" from "this process knows
    nothing about this plan" — and the second one is what a run that
    was dying looked like.

    ``data_source_detail`` is the same provenance in facts rather than
    in a word: the database by path and by inode, the moment the read
    opened it, the row it found (by rowid and by the row's own stamp)
    and the verdict of the integrity check that ran before the row was
    trusted.  The kind alone is not enough inside a round that restarts
    the service: two instances can read two different snapshots of the
    same file, and a reader that cannot tell the snapshots apart
    cannot tell which run it is being told about.

    Declared as a subclass of the server's own model rather than as an
    edit to it: the server module owns the shared response models, and
    a route-local subclass inherits every field it already had — the
    status vocabulary, the pause fields — without a second place to
    keep them in sync.
    """

    data_source: Literal["state_db", "default"] = STATUS_DATA_SOURCE_DEFAULT
    data_source_detail: Optional[Dict[str, Any]] = None


@router.get("/api/execution/{plan_id}/status", response_model=_ExecutionStatusResponse)
def get_execution_status(plan_id: str):
    """Poll execution status and logs.

    Reads flow through the in-memory ``_execution_state`` dict (which
    is restored on startup by :func:`_recover_execution_states` from
    the ExecutionRepository SQLite row).  The legacy direct read of
    ``execution.json`` was removed because writes already flow through
    the repository layer — there is no canonical on-disk JSON to
    read from at this point.

    **The row is the source of the answer; the in-memory map is one
    view of it.**  The map lives in the process that started the run, so
    it is empty in every process that did not: a fresh boot whose
    recovery scan has not run yet, a second process answering for a plan
    the first one is running, a plan whose directory the boot-time scan
    never walked.  Answering "not started" from an empty map turned a
    run that was parked on capacity — or one that was dying — into a
    plan that never ran, and the operator was told the run never
    happened.  So the row is read first and the answer is made of it:
    its ``exec_status``, its ``stop_reason``, its start time, its end
    time and its pid.  Only a plan that has no execution row at all is
    "not started".

    **A row with nothing in the map is restored into it.**  The map is
    also what the liveness rule reads — the watchdog judges the
    in-memory status and probes the in-memory pid — so a row that says
    ``running`` while its process is gone would never be probed at all
    when the map is empty, and the answer would report a run that is
    still going, forever.  Restoring the row (in the very shape the
    boot-time scan restores it) hands it to the standing rule instead
    of inventing a verdict for it: a process that died is ``failed`` /
    ``process_died_unexpectedly``, and never a quota wait.

    **The row is also the truth when the map holds something.**  The
    map is one process's view of a run, and it goes stale the moment
    another process touches the same plan — another process parks the
    plan on capacity, another process judges a crash.  So the row's
    verdict is read first and adopted into the map, and the answer is
    made of the row.  That is also what keeps the watchdog honest: it
    judges liveness from the in-memory status, and it must not judge a
    row that is not ``running``.

    **The answer says where it came from.**  ``data_source`` is
    ``state_db`` when the answer was assembled from the row and
    ``default`` when the library really holds no row and the answer is
    the empty default.  Without it, "this plan never ran" and "this
    process knows nothing about this plan" are the same answer — and
    the second one is what a dying run used to look like.

    **The answer says WHICH database produced it.**  ``data_source``
    names the *kind*; ``data_source_detail`` names the *store*: the
    database by path and by inode, the moment the read opened it, the
    row it found (by rowid and by the row's own stamp) and the verdict
    of the integrity check that ran before the row was trusted.  A
    round that restarts the service inside the round is exactly where
    the kind is not enough: two instances can read two different
    snapshots of the same file, and a reader that cannot tell them
    apart cannot tell which run it is being told about.

    **"Not started" is a claim, and a claim needs a witness.**  The
    empty answer is only made when this instance can back it up: the
    library was read and it holds no run for this plan.  When the
    library could not be read, or when it was read, it holds rows of
    its own, and it holds no row at all for a plan whose directory is
    right here in this instance's tree, the answer is a refusal (503)
    that names the database that could not answer — never a default
    nobody recorded.  See
    :func:`_refusal_when_the_run_cannot_be_confirmed`.

    The read itself never probes a PID on its own: a parked run keeps a
    PID whose process is gone, and judging liveness belongs to
    ``_recover_execution_states`` and ``_lazy_check_execution`` — the
    two places allowed to ask it.

    The pause fields come from the plan directory rather than from
    that state, and are filled in on **every** branch: after a
    restart, the branch a paused plan lands in is ``not_started``,
    which is exactly where the checkpoint has to still be reported.
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function

    # The row is the truth about the run; this process's memory is one
    # view of it and can be stale.  Read the row first and adopt its
    # verdict, so that anything acting on the in-memory status — the
    # watchdog judges liveness from it — acts on the row's truth.
    #
    # The read brings the provenance of the read with it, so the answer
    # can name the store it was made of.
    read = _read_execution_state(plan_id)
    row = read.row

    # A row with nothing in this process's registry: the registry is
    # empty after a restart, in another process, and for a plan the
    # boot-time scan never walked (it walks the plans *tree*, so a plan
    # without a directory is invisible to it).
    #
    # The registry is what the liveness rule reads — the watchdog
    # judges the in-memory status and probes the in-memory pid — so a
    # row that says ``running`` while its process is gone would never
    # be probed at all, and the answer would report a run that is still
    # going, forever.  Restoring the row hands it to the standing rule
    # instead of inventing a verdict for it.
    if row is not None and plan_id not in _server._execution_state:
        _restore_execution_state_from_row(plan_id, row)

    _adopt_row_verdict(plan_id, row)

    before = _memory_verdict(plan_id)
    _server._lazy_check_execution(plan_id)
    if _memory_verdict(plan_id) != before:
        # The watchdog reached a verdict.  The row is where that
        # verdict lives, so the row is re-read: from here on the answer
        # is made of the row, not of a second derivation of it.
        row = _persisted_execution_row(plan_id) or row
        _adopt_row_verdict(plan_id, row)
        # The verdict changed the row, so the provenance is read again:
        # it has to describe the row the answer is made of.
        read = _read_execution_state(plan_id)
        row = read.row or row

    if (
        _memory_verdict(plan_id)[1] == _CRASH_STOP_REASON
        and str((row or {}).get("exec_status") or "") == "running"
    ):
        # The crash was reached, but the row does not carry it: the
        # write was lost.  A ``running`` row is the only row that may
        # still be judged, so this is the one moment the verdict may be
        # written — and it is written whole.
        row = _write_crash_verdict(plan_id) or row
        _adopt_row_verdict(plan_id, row)
        # Same here: the row changed, so the provenance is read again.
        read = _read_execution_state(plan_id)
        row = read.row or row

    source = read.source
    s = _server._execution_state.get(plan_id)
    if row is not None:
        # The row survived the process that wrote it, so the row is
        # what the answer is made of.  A parked run has not ended and
        # carries no end time; a terminal run's end time is the row's
        # own stamp of the verdict, so the replay hands back the same
        # end time the writer recorded.
        #
        # The logs and the sync targets are the runtime half of the
        # answer: they live in this process only, and they are read
        # from the registry when it holds one.
        status, stop_reason = _execution_verdict(row)
        return {
            "plan_id": plan_id,
            "status": status,
            "logs": list(s.get("logs", [])) if s else [],
            "project_dir": row.get("project_dir") or "",
            "pid": _row_pid(row, status),
            "started_at": row.get("started_at"),
            "ended_at": _verdict_stamp(row),
            "stop_reason": stop_reason,
            "sync_targets": s.get("sync_targets") if s else None,
            "data_source": STATUS_DATA_SOURCE_STATE_DB,
            "data_source_detail": source,
            **_pause_fields(plan_id),
        }
    if s:
        # No row, but this process holds a view of the run: the answer
        # is that view, and it says so — it was not read from the row.
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
            "data_source": STATUS_DATA_SOURCE_DEFAULT,
            "data_source_detail": source,
            **_pause_fields(plan_id),
        }
    # No execution row at all — and nothing this process holds either.
    # The empty "not started" answer is a CLAIM about the plan's
    # history, so it may only be made when this instance can back it
    # up.  When it cannot, the answer is a refusal that names the
    # database that could not answer — a claim the caller would believe
    # is worse than a refusal it can act on.
    refusal = _refusal_when_the_run_cannot_be_confirmed(plan_id, read)
    if refusal is not None:
        return refusal
    # The library was read and it holds no run for this plan: the plan
    # has never run, and a reader must never be handed a fabricated
    # failure instead.
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
        "data_source": STATUS_DATA_SOURCE_DEFAULT,
        "data_source_detail": source,
        **_pause_fields(plan_id),
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


# ---------------------------------------------------------------------------
# stop = pause
# ---------------------------------------------------------------------------
#
# A stop used to be a pure "stop the work" switch: it killed the
# executor subprocess and flipped a flag. Nothing was written down, so
# a stopped plan and a plan that had never run looked exactly the same
# on disk — no record that task 2 was in flight, no record of the
# session id a resume could re-open, and no record of *which kind* of
# interruption happened (a cooperative exit and a hard kill need
# different resume strategies).
#
# A stop is therefore a pause: it leaves a resumable trace behind. The
# four steps below are ordered, and the order is the contract.

#: Task statuses that mean "this task will not run again".
_EXECUTION_TERMINAL_TASK_STATUSES = frozenset({
    "completed", "failed", "skipped", "superseded", "voided", "obsolete",
})

#: Task statuses that mean "this task is running right now".
_EXECUTION_IN_PROGRESS_TASK_STATUSES = frozenset({
    "in_progress", "breakdown_in_progress",
})


def _resolve_stop_grace() -> Optional[float]:
    """The SIGTERM grace window this stop gets.

    Read through ``server`` so the deployment's
    ``PDT_STOP_GRACE_SECONDS`` applies — the window is configurable
    (task 3), and a stop that hard-coded it would ignore the
    operator's setting. ``None`` means "let the killer decide", which
    is the same default the killer would have picked.
    """
    resolver = getattr(_server, "_resolve_stop_grace_seconds", None)
    if resolver is None:
        return None
    try:
        return float(resolver(None))
    except Exception:  # noqa: BLE001 — a bad grace must not stop the stop
        return None


def _execution_log_paths(plan_id: str, plan_dir: Path) -> List[Path]:
    """Where the executor's log may live.

    The executor writes it under the *resolved* plans directory
    (``PDT_PLANS_DIR`` wins), while this route resolves the plan
    directory through ``server.PLANS_DIR``. The two are the same in a
    normal deployment, but a deployment that exports only one of them
    would otherwise lose the session bindings — and with them the
    ability to resume a task in its own session.
    """
    candidates = [plan_dir / "execution.log"]
    try:
        from config_paths import resolve_plans_dir

        resolved = resolve_plans_dir() / plan_id / "execution.log"
    except Exception:  # noqa: BLE001 — the primary candidate still works
        resolved = None
    if resolved is not None and resolved not in candidates:
        candidates.append(resolved)
    return candidates


def _execution_task_statuses(plan_id: str, plan_dir: Path) -> Dict[str, str]:
    """Every task of a plan, with the status it last reached.

    Two sources, merged: the static task list on disk (``tasks.json``)
    and the runtime overlay in SQLite (``plan_tasks``). The overlay
    wins — it is what the executor writes to, and what a resume will
    read back.

    A plan whose state store cannot be opened still gets its on-disk
    list: a stop that recorded nothing because the database was busy
    would be a stop that lost the interruption.
    """
    statuses: Dict[str, str] = {}
    order: List[str] = []

    def _remember(task_id: str, status: str) -> None:
        if task_id not in statuses:
            order.append(task_id)
        statuses[task_id] = status

    try:
        data = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        data = None
    if data is not None:
        tasks = data if isinstance(data, list) else (data.get("tasks") or [])
        for task in tasks:
            if not isinstance(task, dict) or task.get("id") is None:
                continue
            _remember(str(task["id"]), str(task.get("status") or "").lower())

    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        conn = open_db(_server._state_db_path())
        try:
            migrate(conn)
            runtime = PlanTaskRepository(conn).load_all(plan_id) or {}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the on-disk list is the fallback
        runtime = {}

    for task_id, row in runtime.items():
        key = str(task_id)
        status = str((row or {}).get("status") or "").lower()
        if key in statuses:
            if status:
                statuses[key] = status
        else:
            _remember(key, status)

    return {task_id: statuses[task_id] for task_id in order}


def _stop_interruption(
    plan_id: str,
    plan_dir: Path,
    *,
    hard_killed: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The tasks a stop interrupted, and the tasks a resume still owes.

    A task was *interrupted* when it was running when the stop landed
    (``in_progress``), or when it had a session bound in the execution
    log and had not finished — a task that ran in a session and did
    not reach a terminal state is a task with work to resume, even if
    the status it last wrote was not ``in_progress``.

    The strategy says how a resume should treat each one:

    * ``full_rerun`` / ``sigkill_after_grace`` — the tree had to be
      hard-killed, so the conversation may have been cut mid-write
      and the task cannot be trusted to continue in it;
    * ``session_resume`` / ``session_id_available`` — the task ran in
      a session that is still there, so the resume re-opens it;
    * ``full_rerun`` / ``no_session_id`` — nothing to re-open, so the
      task runs again from the beginning.
    """
    from pause_state import collect_task_sessions

    sessions: Dict[str, str] = {}
    for log_path in _execution_log_paths(plan_id, plan_dir):
        sessions.update(collect_task_sessions(log_path))

    statuses = _execution_task_statuses(plan_id, plan_dir)

    interrupted: List[Dict[str, Any]] = []
    unfinished: List[Dict[str, Any]] = []
    for task_id, status in statuses.items():
        finished = status in _EXECUTION_TERMINAL_TASK_STATUSES
        if not finished:
            unfinished.append({"task_id": task_id, "status": status})

        session_id = sessions.get(task_id)
        was_running = status in _EXECUTION_IN_PROGRESS_TASK_STATUSES
        if not (was_running or (session_id and not finished)):
            continue

        if hard_killed:
            strategy, reason = "full_rerun", "sigkill_after_grace"
        elif session_id:
            strategy, reason = "session_resume", "session_id_available"
        else:
            strategy, reason = "full_rerun", "no_session_id"

        interrupted.append({
            "task_id": task_id,
            "status": status,
            "phase": "execution",
            "session_id": session_id,
            # Filled in by ``write_pause_checkpoint`` with the
            # checkpoint's own stamp, so every entry carries the same
            # "when was this caught" answer.
            "paused_at": None,
            "strategy": strategy,
            "reason": reason,
        })

    return interrupted, unfinished


def _stop_response(
    *,
    paused: bool,
    paused_at: Optional[str],
    interrupted: int,
    phase_transition_error: Optional[str] = None,
) -> Dict[str, Any]:
    """The answer ``POST /stop`` gives, whichever path it took.

    ``phase_transition_error`` is present ONLY when the plan could not
    be parked in the ``stopped`` phase (2026-10-23). Its presence is
    the signal that the stop did not fully hold: the work was killed,
    but the plan is still sitting in the phase it had before — so the
    chain that reads the phase may still consider it live. The key is
    absent on a clean stop, so the normal four-key contract is
    unchanged.
    """
    body: Dict[str, Any] = {
        "status": "stopped",
        "paused": paused,
        "paused_at": paused_at,
        "interrupted_task_count": interrupted,
    }
    if phase_transition_error is not None:
        body["phase_transition_error"] = phase_transition_error
    return body


def _park_plan_in_stopped_phase(
    plan_id: str, plan_dir: Path, *, skip_terminal: bool = True
) -> Optional[str]:
    """Move the plan into the ``stopped`` phase; return the failure text.

    Returns ``None`` when the plan is parked (or already was), and the
    ``ValueError`` text when it could not be — the caller puts that
    text in the response body under ``phase_transition_error`` so the
    operator can see that the stop did not hold, instead of the stop
    silently pretending it did.

    ``skip_terminal`` is what the idle path uses: a plan that is
    already in a terminal phase has nothing to park, and forcing
    ``completed → stopped`` would raise a ``ValueError`` that says
    nothing about the stop. The running path does NOT skip — there the
    transition is the stop's own contract, and a refusal is exactly
    the "the stop did not hold" case the response has to report.
    """
    from plan_state import TERMINAL_PHASES

    try:
        ps = _server.PlanState(plan_dir)
        ps.reload()
        if skip_terminal and ps.get_current_phase() in TERMINAL_PHASES:
            return None
        ps.transition_to("stopped")
        return None
    except ValueError as exc:
        _server.logger.error(
            "stop_execution: could not park the plan in the 'stopped' "
            "phase plan=%s: %s", plan_id, exc,
        )
        return str(exc)


def _stop_running_execution(
    plan_id: str,
    plan_dir: Path,
    state: Dict[str, Any],
    proc: Any,
    *,
    discard: bool,
) -> Dict[str, Any]:
    """Mark, terminate, checkpoint, transition — in that order."""
    from datetime import datetime
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
        PlanNotFoundError as ExecPlanNotFoundError,
    )
    from pause_state import clear_pause_checkpoint, write_pause_checkpoint

    # --- 1. mark ------------------------------------------------------
    # The plan is marked BEFORE anything is killed. The executor's
    # watcher thread and the heartbeat watchdog both act on
    # ``status == "running"``: if the kill landed first, the executor
    # could wake up, find a process that is gone and stamp the plan
    # ``failed`` — the one outcome a pause must never produce.
    #
    # ``stop_reason`` says ``pausing`` while the pause is in progress:
    # the process is still alive at this point, and the plan is not
    # merely stopped, it is being paused. It becomes
    # ``user_requested`` once the pause has completed.
    state["status"] = "stopped"
    state["stop_reason"] = "pausing"
    state["ended_at"] = datetime.now().isoformat()

    project_dir = state.get("project_dir")
    if not project_dir:
        try:
            resolved = _server._get_project_dir(plan_id)
        except Exception:  # noqa: BLE001 — a lookup failure is not a stop
            resolved = None
        project_dir = str(resolved) if resolved else None

    try:
        persist_conn = open_db(_server._state_db_path())
        try:
            migrate(persist_conn)
            fields: Dict[str, Any] = {
                "exec_status": "stopped",
                # The executor is about to be killed — the pid belongs
                # to a process that will not exist any more.  Leaving
                # it behind makes the row read as a live execution
                # (accident B, 2026-10-23).
                "exec_pid": None,
            }
            if project_dir:
                fields["project_dir"] = str(project_dir)
            ExecutionRepository(persist_conn).update_fields(
                plan_id, create_if_missing=True, **fields
            )
        finally:
            persist_conn.close()
    except ExecPlanNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — the checkpoint matters more
        _server.logger.exception(
            "stop_execution: could not persist the stopped state "
            "plan=%s", plan_id,
        )

    # --- 1b. cancel the verification chain ----------------------------
    # 2026-10-23: killing the executor is NOT enough to stop a plan.
    #
    # The repair executor is killed by step 2 below, and its
    # ``process.wait()`` callback comes back with ``rc=-15`` — which is
    # indistinguishable from a repair run that finished normally.
    # ``_on_repair_complete`` (verification_loop.py) reads that return
    # code as "the repair is done" and re-enters verification:
    # ``begin_verification`` → ``start_verification_cycle`` →
    # ``_run_repair_execution_async`` → a NEW executor subprocess.
    #
    # So without this flag the operator's stop is a no-op for the
    # chain: stop → kill the repair executor → the callback re-enters
    # verification → a new repair round → a new executor. The gate in
    # ``_on_repair_complete`` that was written for exactly this case
    # ("if the operator stopped the plan while this repair was running,
    # do NOT re-enter verification") consults
    # ``verification_cancel.is_cancelled(plan_id)`` — and this endpoint
    # never set it. The gate was right; nothing ever flipped the flag.
    #
    # The flag is set BEFORE the kill, so it is already in place when
    # the killed process's callback fires.
    try:
        import verification_cancel
        verification_cancel.request_cancel(plan_id)
    except Exception:  # noqa: BLE001 — a missing flag must not abort the stop
        _server.logger.exception(
            "stop_execution: could not request the verification cancel "
            "plan=%s", plan_id,
        )

    # --- 2. terminate -------------------------------------------------
    # Only THIS plan's process group. Another plan's executor is none
    # of this stop's business.
    hard_killed = False
    if proc is not None and proc.poll() is None:
        try:
            hard_killed = bool(
                _server._kill_process_tree(
                    proc, grace_seconds=_resolve_stop_grace()
                )
            )
        except Exception:  # noqa: BLE001 — a failed kill still pauses
            _server.logger.exception(
                "stop_execution: could not terminate the executor "
                "plan=%s", plan_id,
            )

    # --- 3. checkpoint ------------------------------------------------
    # Written AFTER the termination completed, so it describes the
    # interruption as it actually ended: a hard kill is a different
    # kind of interruption than a cooperative exit, and the resume
    # strategy follows from it.
    interrupted, unfinished = _stop_interruption(
        plan_id, plan_dir, hard_killed=hard_killed
    )
    if discard:
        # "Throw it away" — the resume point goes with the work.
        clear_pause_checkpoint(plan_dir)
        checkpoint = None
    else:
        checkpoint = write_pause_checkpoint(
            plan_dir,
            kind="execution",
            project_dir=project_dir,
            reason="user_requested",
            interrupted_tasks=interrupted,
            unfinished_tasks=unfinished,
        )

    # --- 4. transition ------------------------------------------------
    # A refusal here is reported, not swallowed (2026-10-23). The plan
    # is killed but not parked, so the phase still says whatever it
    # said before — and a phase that says "verification_*" is read by
    # the chain as "still live". Swallowing the ``ValueError`` made the
    # endpoint answer a clean 200 for a stop that did not hold.
    phase_transition_error = _park_plan_in_stopped_phase(
        plan_id, plan_dir, skip_terminal=False,
    )

    # The pause is over: the reason for it is no longer "a pause is in
    # progress" but the stop that was asked for.
    state["stop_reason"] = "user_requested"

    if checkpoint is None:
        return _stop_response(
            paused=False, paused_at=None, interrupted=0,
            phase_transition_error=phase_transition_error,
        )
    return _stop_response(
        paused=True,
        paused_at=checkpoint.get("paused_at"),
        interrupted=len(interrupted),
        phase_transition_error=phase_transition_error,
    )


def _stop_idle_execution(
    plan_id: str, plan_dir: Path, *, discard: bool
) -> Dict[str, Any]:
    """A stop against a plan that is not running.

    Two cases, one answer shape:

    * the plan already has a checkpoint — a previous stop paused it
      and this is a repeat of that stop. The checkpoint on disk IS the
      answer, so it is returned unchanged. Rewriting it would move
      ``paused_at`` and lose the record of when the interruption
      actually happened.
    * there is no checkpoint — nothing was interrupted, so there is
      nothing to pause and nothing to record.

    2026-10-23: the idle path used to leave the phase completely
    alone. A plan sitting in ``verification_repairing`` (or ``ready``,
    or ``queued``) that the operator stopped therefore kept a phase
    that says "live" — the stop did not hold for anything that reads
    the phase. The plan is now parked in ``stopped`` whenever it is
    not already in a terminal phase, on BOTH branches (the one with a
    checkpoint and the one without).
    """
    from pause_state import clear_pause_checkpoint, read_pause_checkpoint

    # Park the phase first: a refusal is reported in the body, and it
    # must not stop the checkpoint bookkeeping from happening.
    phase_transition_error = _park_plan_in_stopped_phase(
        plan_id, plan_dir, skip_terminal=True,
    )

    existing = read_pause_checkpoint(plan_dir)
    if discard:
        clear_pause_checkpoint(plan_dir)
        return _stop_response(
            paused=False, paused_at=None, interrupted=0,
            phase_transition_error=phase_transition_error,
        )
    if existing is not None:
        return _stop_response(
            paused=True,
            paused_at=existing.get("paused_at"),
            interrupted=len(existing.get("interrupted_tasks") or []),
            phase_transition_error=phase_transition_error,
        )
    return _stop_response(
        paused=False, paused_at=None, interrupted=0,
        phase_transition_error=phase_transition_error,
    )


@router.post("/api/execution/{plan_id}/stop")
def stop_execution(plan_id: str, discard: bool = False):
    """Stop a running execution — and leave a resume point behind.

    A stop is a pause (core contract 1 / 2 / 6): the plan keeps
    whatever it had achieved, and the next start picks it up instead
    of re-running the whole round.

    The order of the four steps is the contract, not an implementation
    detail:

      1. **Mark** — the in-memory state says ``stopped`` /
         ``pausing`` and ``exec_status`` is persisted, *before*
         anything is killed. The executor's watcher thread and the
         heartbeat watchdog both act on ``status == "running"``, so a
         kill that landed first could let the executor stamp the
         plan ``failed`` — the one outcome a pause must never
         produce.
      2. **Terminate** — only this plan's own process group is
         signalled; another plan's executor is never touched.
      3. **Checkpoint** — the resume point is written *after* the
         termination completed, so it describes the interruption as
         it actually ended.
      4. **Transition** — the plan state moves to ``stopped``.

    ``?discard=true`` is the escape hatch: the work is thrown away
    instead of paused, the checkpoint is removed, and the answer says
    ``paused: false``.
    """
    # VP-020: an archived plan is read-only — refuse before touching
    # ``_execution_state`` or any SQLite row. This endpoint had no
    # archived guard at all, so a stop against a pre-2026-08-05 plan
    # answered 200 and mutated the row.
    plan_dir = _server._plan_dir(plan_id)
    archived = _server._archived_plan_response(plan_dir)
    if archived is not None:
        return archived

    state = _server._execution_state.get(plan_id)
    proc = state.get("process") if state else None
    proc_alive = bool(proc is not None and proc.poll() is None)
    in_flight = bool(state) and (state.get("status") == "running" or proc_alive)

    if in_flight:
        response = _stop_running_execution(
            plan_id, plan_dir, state, proc, discard=discard,
        )
    else:
        response = _stop_idle_execution(plan_id, plan_dir, discard=discard)

    # 2026-09-18 C3: stopping the execution phase is also an exit from
    # the workflow. The execution phase does not itself start declared
    # services (only verification's preflight does), but a plan that
    # was re-executed after a verification cycle can still have a
    # ledger from that cycle, and this is the last exit point that
    # would otherwise leave it behind.
    _server._reap_managed_services(plan_id, plan_dir, "execution_stopped")

    return response


def _serving_instance(db_path: Optional[Path]) -> Dict[str, Any]:
    """The identity of the process that is answering, and of its database.

    A reader that asks a port a question gets an answer, and the answer
    does not say WHO gave it.  That was enough until an instance died
    and another one took its port: the round could not say whether the
    200 it received came from the instance it had configured or from a
    stranger, and it could not say which ``state.db`` the counts came
    from either.  Two answers about "the same" plan then looked alike
    while being about different databases.

    So the answer names itself: the pid, the boot id (minted once per
    process, never changed), the start time, and the database this
    process read — by path AND by inode, because a different file at
    the same path is a different data plane.

    The database named here is the very file the counts were read from,
    so a reader can check the two against each other instead of
    trusting the label.
    """
    try:
        identity = _server._instance_identity(db_path)
    except Exception:  # noqa: BLE001 - an identity block must never 500
        identity = {}
    return {
        "pid": identity.get("pid"),
        "boot_id": identity.get("boot_id"),
        "state_db_path": identity.get("state_db_path"),
        "state_db_inode": identity.get("state_db_inode"),
        "started_at": identity.get("started_at"),
    }


def _execution_in_flight(
    exec_row: Optional[Dict[str, Any]],
    state: Optional[Dict[str, Any]],
) -> bool:
    """Is this plan's execution still running right now?

    Two facts, in this order:

    * **The row says whether the run is supposed to be going.**  A
      parked plan (``waiting_for_quota``) keeps the pid the executor
      will be resumed under, and that pid is gone for as long as the
      park lasts — so a pid on the row is NOT evidence of a live run,
      and a parked plan is not in flight.
    * **The process handle says whether it actually is.**  Only the
      process that started the run holds one; everywhere else the
      row's own word is the last word.

    The pid is deliberately NOT probed here.  Liveness is the
    watchdog's call — ``_recover_execution_states`` at boot and
    ``_lazy_check_execution`` at runtime are the two places allowed to
    ask it — and a third judge would answer the same run differently
    from the first, which is the exact shape of bug this endpoint is
    being fixed for.
    """
    state = state or {}
    row_status = str((exec_row or {}).get("exec_status") or "")
    status = row_status or str(state.get("status") or "")
    if status != "running":
        return False
    proc = state.get("process")
    if proc is not None:
        try:
            return proc.poll() is None
        except Exception:  # noqa: BLE001 - a dead handle is not a live run
            return False
    return state.get("ended_at") is None


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

    2026-10-24 — the 404 gate is "there is no execution record", not
    "there is no project dir".  The gate used to read
    ``_get_project_dir()`` and answer 404 when it came back empty,
    which reported a quota-parked plan as a plan that was never
    started: the parked row exists, but nothing forces that row to
    carry a project directory, so the operator's question ("what is
    still left to do?") was answered with "call /start first" — on a
    plan that was already running and waiting for capacity.

    The ``plan_execution`` row IS the evidence that the plan was
    configured for execution.  Only its ABSENCE may produce the 404,
    and the original wording stays for that case: an operator who
    reads "not configured" can act on it.

    2026-10-26 — **the row is the truth and the registry is a cache.**
    The row (``plan_execution``) is the durable record of the run and
    the per-task rows (``plan_tasks``) are the durable record of the
    work; the in-memory registry (``_execution_state``) is one
    process's view of the row, and it goes stale the moment another
    process touches the plan.  A plan parked on capacity by the process
    that owns the run leaves every OTHER process holding a registry
    that still says "running" — with a pid that has since exited.

    The watchdog (``_lazy_check_execution``) judges the registry's
    status and probes the registry's pid, so a stale "running" made it
    judge a parked plan's dead pid: the pause was rewritten as a crash,
    the row was overwritten with ``failed``, and the interrupted task's
    ``in_progress`` row was reaped back to ``pending``.  The plan then
    reported "nothing is running and two tasks are still pending" for
    work that was merely waiting for capacity.

    So the row's verdict is adopted into the registry BEFORE the
    watchdog runs, the counts are read from the state database this
    process is bound to, and the answer names the instance that
    produced it (``serving_instance``) and whether the run is still in
    flight (``execution_in_flight``).  A reader can then tell this
    answer from another instance's answer about the same plan, and can
    tell a paused run from a dead one.
    """
    project_dir = _server._get_project_dir(plan_id)

    # --- Single state-machine connection ----------------------------------
    # ``execution_repository.summary`` (the row itself),
    # ``plan_task_repository.load_all`` (per-task runtime overlay) and
    # ``execution_repository.progress`` (aggregated ``task_progress``
    # column used as a sanity-check hint) all read from the same
    # SQLite file. We open the connection ONCE and pass the handle
    # to every repository so:
    #
    #   * the WAL / busy_timeout / synchronous PRAGMAs are applied
    #     once per request instead of twice,
    #   * the read-modify-write view is consistent — a writer that
    #     commits between two opens cannot show the overlay and
    #     the aggregated column out-of-order,
    #   * the second ``migrate()`` call (a no-op once the schema is
    #     current but still a DDL round-trip) is gone.
    #
    # The row is read FIRST because the 404 gate below needs it: the
    # gate is about the existence of the record, not about the
    # project dir it happens to carry.
    #
    # The database path is resolved ONCE and used twice: for the read
    # and for the identity block the answer carries.  The two cannot
    # disagree that way — the file named in the answer is the file the
    # counts came from.
    state_db_path: Optional[Path] = None

    runtime: dict = {}
    db_progress: Optional[dict] = None
    exec_row: Optional[dict] = None
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )

        state_db_path = Path(_server._state_db_path())
        sm_conn = open_db(state_db_path)
        try:
            migrate(sm_conn)
            exec_repo = ExecutionRepository(sm_conn)
            # The ``plan_execution`` row — the record that says this
            # plan was configured for execution at all.
            exec_row = exec_repo.summary(plan_id)
            # Per-task runtime overlay (current schema; the fields it
            # carries are listed at ``_RUNTIME_OVERLAY_FIELDS`` below).
            runtime = PlanTaskRepository(sm_conn).load_all(plan_id)
            # Aggregated ``task_progress`` column — the sanity-check
            # hint consumed by the drift log below.
            db_progress = exec_repo.progress(plan_id)

            # The row's verdict is adopted into the registry BEFORE the
            # watchdog runs.  The watchdog judges the registry's status
            # and probes the registry's pid; a registry that still says
            # "running" for a plan that has since parked would make it
            # judge the parked plan's dead pid and read a pause as a
            # crash.  The registry is a cache of the row, so the row
            # overwrites it.
            _adopt_row_verdict(plan_id, exec_row)

            # The watchdog may reach a verdict of its own (a row that
            # says "running" whose process is gone).  It writes that
            # verdict to the row and reaps the orphaned in_progress
            # rows, so the row and the task rows are read again on the
            # SAME connection: the answer must describe the state as
            # it is after the verdict, not as it was before it.
            try:
                before = _memory_verdict(plan_id)
                _server._lazy_check_execution(plan_id)
                judged = _memory_verdict(plan_id) != before
            except Exception:  # noqa: BLE001 - a watchdog failure must not 500
                _server.logger.exception(
                    "[execution_progress] watchdog failed plan=%s", plan_id,
                )
                judged = False
            if judged:
                exec_row = exec_repo.summary(plan_id) or exec_row
                runtime = PlanTaskRepository(sm_conn).load_all(plan_id)
                _adopt_row_verdict(plan_id, exec_row)
        finally:
            sm_conn.close()
    except (sqlite3.OperationalError, OSError):
        # State-machine not on disk yet — tasks.json carries the
        # runtime fields directly (legacy / pre-migration plan).
        runtime = {}
        db_progress = None
        exec_row = None

    if not project_dir and exec_row is None:
        raise HTTPException(404, "Execution not configured for this plan — call /api/execution/{plan_id}/start first")

    plan_dir = _server._plan_dir(plan_id)
    plan_tasks_file = plan_dir / "tasks.json"
    # The legacy fallback lives INSIDE the project dir, so it exists
    # only when there is one.  A plan whose row carries no project dir
    # still has its plan-dir task list and its ``plan_tasks`` rows —
    # that is where the counts come from.
    legacy_tasks_file = (project_dir / "tasks.json") if project_dir else None

    # Hot-path optimization B-05: short-circuit candidate files with a
    # single tuple of Path.exists() probes. The previous code did two
    # sequential .exists() calls even on the common single-source
    # layout (only plan_tasks_file present). Single tuple evaluation
    # avoids re-walking the path resolution for the legacy fallback.
    candidates = tuple(
        c for c in (plan_tasks_file, legacy_tasks_file) if c is not None
    )
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
    # The runtime overlay was read on the single connection opened
    # above (the row, the overlay and the aggregated hint all share it).
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
        _TERMINAL = {"completed", "failed", "skipped", "superseded", "voided"}
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

    # Derive execution_status from the state-machine row, the in-memory
    # state, or tasks.json (for resilience after server restart).
    #
    # The row is the FIRST source because it is the only one that can
    # say ``waiting_for_quota``: the counts cannot tell a parked plan
    # from a plan that never started — both look like "pending tasks,
    # nothing running" — and the difference is the whole point of the
    # quota wait.  The row is also the only source that survives a
    # restart, which is exactly when the in-memory state is empty.
    #
    # And it stays the first source when the registry disagrees: the
    # registry is one process's cache of the row, and the row is
    # written by whoever owns the run.  A registry that still says
    # "running" for a plan that has since parked would otherwise make
    # the answer depend on WHICH process served the request — the
    # answer would be "running" on the process that owns the run and
    # "parked" on every other one.
    #
    row_status = (exec_row or {}).get("exec_status") or None
    memory_status = s.get("status") or None
    if row_status and row_status != "not_started":
        execution_status = row_status
    else:
        execution_status = memory_status or row_status
    has_task_progress = counts["completed"] > 0 or counts["failed"] > 0 or counts["in_progress"] > 0
    # If in-memory state is missing or shows "not_started" despite having task progress, derive from tasks.json
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

    # The plan's outstanding human decisions.  The same block the plan
    # summary carries, so a consumer that polls the progress endpoints
    # reads the same shape it reads from the summary.  Imported here
    # rather than at module scope: ``routes.plans`` reaches back into
    # this module, and a module-level import between the two would be a
    # cycle.
    from routes.plans import _pending_decisions_block

    return {
        "plan_id": plan_id,
        "pid": s.get("pid"),
        # A plan whose row carries no project dir reports an empty
        # string, not "None" — the field is a path, and an empty one
        # reads as "there is none" the same way the rest of the API
        # reports a missing project dir.
        "project_dir": str(project_dir) if project_dir else "",
        "execution_status": execution_status,
        "tasks": tasks,
        "current": current,
        "next": next_task,
        "counts": counts,
        # Always present — an empty block means "nothing is
        # outstanding", which is an answer, not a missing key.
        "pending_decisions": _pending_decisions_block(plan_id),
        # What the executor is doing when no task row says. ``current``
        # is derived from ``plan_tasks`` and is None across every window
        # the executor spends on work that is not a task — the refiner
        # rewriting the task list, a layer boundary, the tail after the
        # last task. The card rendered "正在跑 tasks" and then named
        # nothing in those windows; this field is what it names instead.
        # Never raises — a plan with no readable log reports
        # ``{"kind": "unknown"}`` and the card falls back to the bare
        # phase label rather than inventing an activity.
        "current_activity": _current_activity(plan_id, plan_dir),
        "stop_reason": data.get("stop_reason") if isinstance(data, dict) else None,
        "stop_detail": data.get("stop_detail") if isinstance(data, dict) else None,
        "api_error": api_error,
        "started_at": s.get("started_at"),
        "ended_at": s.get("ended_at"),
        "execution_stop_reason": s.get("stop_reason"),
        # Is the run still going?  A parked plan (``waiting_for_quota``)
        # keeps the pid the executor will be resumed under, and that pid
        # is gone for as long as the park lasts — so the answer is
        # "no", and it says so instead of leaving the reader to read a
        # pid as "alive".
        "execution_in_flight": _execution_in_flight(exec_row, s),
        # Who answered.  A reader that cannot say WHICH instance
        # produced this answer cannot compare it with another
        # instance's answer about the same plan — and the database the
        # counts came from is named here, by path AND by inode, so the
        # two can be checked against each other.
        "serving_instance": _serving_instance(state_db_path),
    }


def _current_activity(plan_id: str, plan_dir: Path) -> Dict[str, Any]:
    """``ExecutionLogger.current_activity`` scoped to this plan's dir.

    ``plan_dir`` is already resolved by the caller, so it is passed as
    ``plans_dir/plan_id`` rather than letting the logger re-derive it —
    the endpoint resolves the plan directory through the server's
    configured root, which is the same value the logger would compute
    but without a second lookup that could disagree under a test
    redirect.

    A failure here degrades to ``unknown`` rather than 500-ing the whole
    progress payload: this field is a presentation nicety, and an
    operator asking "how is this plan doing" must still get an answer
    when the log is unreadable.
    """
    try:
        from execution_logger import ExecutionLogger

        return ExecutionLogger.current_activity(plan_id, plan_dir.parent)
    except Exception:
        _server.logger.exception(
            "[execution_progress] current_activity failed plan=%s", plan_id,
        )
        return {"kind": "unknown", "started_at": None, "task_id": None,
                "event": None, "detail": None}


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


def _plan_dir_resolves_inside(plans_dir: Path, plan_dir: Path) -> bool:
    """True when ``plan_dir`` resolves to a path inside ``plans_dir``.

    Both sides are resolved before they are compared, and the
    *resolution* is what is compared: a plans root that is itself a
    symlink resolves to the same real directory on both sides, and a
    plan directory that is a symlink out of the tree resolves to where
    it really points — which is the directory that would actually be
    read.

    The check is not about the NAME the caller sent (the plan id is
    validated before this) but about the PLACE the name leads to.  A
    plan id is a leaf under the configured root; a leaf that points out
    of the tree belongs to somebody else's tree, and reading it would
    answer about a plans root this process was never asked about.

    A path that cannot even be resolved (a symlink loop, a permission
    error) is not a path this process may read, so it is refused too.
    """
    try:
        root = plans_dir.resolve()
        candidate = plan_dir.resolve()
    except (OSError, RuntimeError):
        return False
    return candidate.is_relative_to(root)


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

    The answer says WHICH tree it read.  A 404 that names only "Plan
    not found" is the same answer for a plan this process does not
    have and for a plan that is in another instance's plans root, and
    the two must not look alike: the first is a fact about the plan,
    the second is a fact about the probe.  So the 404 carries the root
    it searched and the directory it resolved, and the 200 carries the
    file it actually read — which is what lets a reader confirm after
    a round that the evidence came from that round's instance.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        task_id: Filter by task ID
        event: Filter by event type (e.g., task_failed, refine_completed)
        limit: Maximum entries to return (default 100, max 1000)
        since: ISO timestamp — return entries after this time
    """
    plans_dir = Path(_server.PLANS_DIR)
    plan_dir = _server._plan_dir(plan_id)
    log_path = plan_dir / "execution.log"

    # The plan id is a leaf under the configured root.  A leaf that
    # resolves OUTSIDE that root is another tree's plan, and this
    # process must not answer for it.  Refusing it loudly is the point:
    # a silent read would be evidence about the wrong instance.
    if not _plan_dir_resolves_inside(plans_dir, plan_dir):
        _server.logger.warning(
            "execution logs: refusing a plan directory that resolves "
            "outside the configured plans root — plan=%s plan_dir=%s "
            "plans_dir=%s",
            plan_id,
            plan_dir,
            plans_dir,
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": "plan_path_outside_plans_root",
                "detail": (
                    "Plan directory resolves outside the configured plans "
                    "root; it belongs to another tree."
                ),
                "plans_dir": str(plans_dir),
                "plan_dir": str(plan_dir),
            },
        )

    if not plan_dir.exists():
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Plan not found",
                "plans_dir": str(plans_dir),
                "plan_dir": str(plan_dir),
            },
        )

    limit = min(limit, 1000)
    entries = _server.ExecutionLogger.read_logs(
        plan_id,
        plans_dir=plans_dir,
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
        # Which tree answered, and which file in it was read.  Without
        # these, a reader cannot tell this instance's answer from
        # another instance's — the confusion this route exists to end.
        "plans_dir": str(plans_dir),
        "plan_dir": str(plan_dir),
        "log_path": str(log_path),
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

