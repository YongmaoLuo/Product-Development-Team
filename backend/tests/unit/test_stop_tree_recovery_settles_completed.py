"""A recovered run that finished is settled as completed, not as a crash.

Background (F4 / R5, 2026-10-27)
--------------------------------
After a restart, a run whose executor is gone is settled by
``_settle_recovered_execution`` (``backend/routes/execution.py``).
Task 10 gave that settle a *watcher* to wait on; this task gives it
the evidence it needs to tell two different endings apart:

* the executor finished its work and exited cleanly — the plan's
  ``execution.log`` carries the ``execution_completed`` marker the
  agent writes when the dispatcher has nothing left to run, and no
  task is left unfinished;
* the executor died — there is no marker, or there is work still
  owed.

Before this task both endings were ``failed``: a run that completed
7/7 tasks and was then restarted was reported to the operator as a
crash.  Washing a crash into a success is the opposite mistake, and
it is the more expensive one — a plan whose tasks are half done must
never be stamped ``completed`` and handed to the verification round.

Contract pinned here:

1. Marker + every task terminal → the run is settled as ``completed``:
   the state says ``completed``, the row says ``completed``, the usage
   report is written, and the auto-verification round is handed the
   plan.
2. No marker → ``crashed`` → ``_mark_failed_dead``.  This is the
   negative control: it is the assertion that keeps 1 from being
   "always completed".
3. Marker + unfinished tasks → ``crashed``.  The marker alone is not
   enough; the task list has the last word.
4. The settlement happens **once** — a second settle does not settle
   the run again.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from routes import execution as execution_routes  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


PLAN_ID = "plan-recovery-settled"
TASK_IDS = [f"T{i}" for i in range(1, 8)]
EXECUTOR_PID = 72395


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Wait for ``predicate`` to become true (or give up after ``timeout``).

    The usage report is written by a background thread (that is the
    framework's contract — accounting must never delay the hand-off),
    so "the file exists" is a fact that becomes true a moment after
    the settle returns, not at the same instant.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """A plan directory and a throwaway state DB, wired in as the live ones.

    ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` are the framework's own
    escape hatches (see ``config_paths.resolve_plans_dir`` /
    ``resolve_state_db_path``), so every resolver in the process agrees
    on this test's directories instead of the operator's live tree.
    """
    plans_dir = tmp_path / "plans"
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    conn = open_db(db_path)
    migrate(conn)
    try:
        yield {
            "plans_dir": plans_dir,
            "plan_dir": plans_dir / PLAN_ID,
            "project_dir": tmp_path / "project",
            "db_path": db_path,
            "conn": conn,
        }
    finally:
        conn.close()


@pytest.fixture
def recorded_verification(monkeypatch):
    """Record the auto-verification hand-off instead of running it.

    The real loop starts a whole verification round; a unit test wants
    to know that the hand-off happened, not what the round did.
    """
    calls: List[Dict[str, Any]] = []
    finished = threading.Event()

    def _fake_loop(plan_id, plan_dir, project_dir, *args, **kwargs):
        calls.append(
            {
                "plan_id": plan_id,
                "plan_dir": plan_dir,
                "project_dir": project_dir,
            }
        )
        finished.set()
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake_loop)
    return {"calls": calls, "finished": finished}


def _marker_line(completed: int = len(TASK_IDS), total: int = len(TASK_IDS)) -> str:
    """One ``execution.log`` line: the executor's own "I finished" marker."""
    return json.dumps(
        {
            "ts": "2026-10-27T00:00:00",
            "level": "INFO",
            "event": "execution_completed",
            "message": "All tasks completed",
            "data": {"total": total, "completed": completed},
        },
        ensure_ascii=False,
    )


def _seed_plan(
    env: Dict[str, Any],
    *,
    marker: bool = True,
    unfinished_task: Optional[str] = None,
    pid: int = EXECUTOR_PID,
) -> Path:
    """Write a recovered plan: its directory, its tasks, its log, its row.

    Args:
        env: the fixture's directory/DB bundle.
        marker: whether the executor's ``execution_completed`` marker is
            in the log.  ``False`` is the crash shape: the executor died
            before it could say anything.
        unfinished_task: a task id that is still ``pending`` — in
            ``tasks.json`` *and* in the runtime overlay, which is where
            the framework reads a task's real status from.
        pid: the executor's pid, as the restored row carries it.

    Returns:
        The plan directory.
    """
    plan_dir: Path = env["plan_dir"]
    plan_dir.mkdir(parents=True, exist_ok=True)
    env["project_dir"].mkdir(parents=True, exist_ok=True)

    tasks = []
    for tid in TASK_IDS:
        status = "pending" if tid == unfinished_task else "completed"
        tasks.append({"id": tid, "title": f"task {tid}", "status": status})
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8"
    )

    # The runtime overlay is the authoritative record of a task's status
    # (``_count_unfinished_tasks`` reads it, not the on-disk status).
    repo = PlanTaskRepository(env["conn"])
    for tid in TASK_IDS:
        status = "pending" if tid == unfinished_task else "completed"
        repo.update_task(PLAN_ID, tid, {"status": status})

    lines = []
    if marker:
        lines.append(_marker_line())
    (plan_dir / "execution.log").write_text(
        ("\n".join(lines) + "\n") if lines else "", encoding="utf-8"
    )

    # The restored ``plan_execution`` row: the run was ``running`` and
    # its executor is the pid the watcher was probing.
    env["conn"].execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
        " next_run_at, card_state, flags, exec_pid, exec_status, "
        " started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            PLAN_ID,
            0,
            str(env["project_dir"]),
            None,
            json.dumps({"tasks": {}}),
            None,
            "{}",
            "{}",
            pid,
            "running",
            "2026-10-27T00:00:00Z",
            "2026-10-27T00:00:00Z",
        ),
    )
    env["conn"].commit()

    # The in-memory record the boot scan restores: a run that says
    # ``running`` and holds a pid that is no longer there.
    server._execution_state[PLAN_ID] = {
        "status": "running",
        "pid": pid,
        "started_at": "2026-10-27T00:00:00Z",
        "ended_at": None,
        "stop_reason": None,
        "project_dir": str(env["project_dir"]),
        "logs": [],
        "process": None,
        "recovery_watcher": threading.current_thread(),
    }
    return plan_dir


def _row_exec_status(env: Dict[str, Any]) -> Optional[str]:
    """The ``exec_status`` the row carries right now."""
    cur = env["conn"].execute(
        "SELECT exec_status FROM plan_execution WHERE plan_id = ?", (PLAN_ID,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def _settled_events(env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The ``execution_recovery_settled`` events in the plan's log."""
    return [
        entry
        for entry in server.ExecutionLogger.read_logs(
            PLAN_ID,
            plans_dir=env["plans_dir"],
            event="execution_recovery_settled",
            limit=10,
        )
        if isinstance(entry, dict)
    ]


# ---------------------------------------------------------------------------
# 1. Marker + every task terminal → completed
# ---------------------------------------------------------------------------


def test_completed_marker_and_all_tasks_terminal_settles_completed(
    plan_env, recorded_verification
):
    """A run that finished is settled as ``completed``, not as a crash.

    The marker is the executor's own statement that it finished, and
    the task list confirms there is nothing left to do.  Together they
    are the success path: the state, the row, the usage report and the
    verification hand-off.
    """
    plan_dir = _seed_plan(plan_env, marker=True)

    execution_routes._settle_recovered_execution(PLAN_ID)

    state = server._execution_state[PLAN_ID]
    assert state.get("status") == "completed", (
        "a run that finished was not settled as completed; "
        f"status={state.get('status')!r}"
    )
    assert state.get("stop_reason") is None, (
        "a completed run kept a stop reason; "
        f"stop_reason={state.get('stop_reason')!r}"
    )
    assert _row_exec_status(plan_env) == "completed", (
        "the row does not carry the completed verdict; "
        f"exec_status={_row_exec_status(plan_env)!r}"
    )

    usage_report = plan_dir / "usage_report.json"
    assert _wait_until(lambda: usage_report.exists()), (
        "the usage report was not written for the settled run"
    )

    events = _settled_events(plan_env)
    assert len(events) == 1, (
        f"the settlement was not recorded exactly once; events={events!r}"
    )
    data = events[0].get("data") or {}
    assert data.get("outcome") == "completed", (
        f"the settlement recorded the wrong outcome; data={data!r}"
    )
    assert data.get("unfinished") == 0, (
        f"the settlement recorded unfinished work; data={data!r}"
    )
    assert data.get("pid") == EXECUTOR_PID, (
        f"the settlement did not record the executor's pid; data={data!r}"
    )


# ---------------------------------------------------------------------------
# 2. No marker → crashed (the negative control)
# ---------------------------------------------------------------------------


def test_missing_completion_marker_stays_failed(plan_env, recorded_verification):
    """No completion marker → the run is a crash, and stays one.

    This is the assertion that keeps the success path honest: without
    it, "every recovered run is completed" would satisfy the suite.
    """
    plan_dir = _seed_plan(plan_env, marker=False)

    outcome = execution_routes._recovered_execution_outcome(
        PLAN_ID, plan_dir, plan_dir / "tasks.json"
    )
    assert outcome == "crashed", (
        "a run without the completion marker was read as completed; "
        f"outcome={outcome!r}"
    )

    execution_routes._settle_recovered_execution(PLAN_ID)

    state = server._execution_state[PLAN_ID]
    assert state.get("status") == "failed", (
        "a run without the completion marker was not settled as failed; "
        f"status={state.get('status')!r}"
    )
    assert _row_exec_status(plan_env) == "failed", (
        "the row does not carry the failed verdict; "
        f"exec_status={_row_exec_status(plan_env)!r}"
    )
    assert recorded_verification["calls"] == [], (
        "a crashed run was handed to the verification round; "
        f"calls={recorded_verification['calls']!r}"
    )


# ---------------------------------------------------------------------------
# 3. Marker + unfinished tasks → crashed
# ---------------------------------------------------------------------------


def test_unfinished_tasks_block_success(plan_env, recorded_verification):
    """The marker alone does not make a run successful.

    The marker says the executor stopped; the task list says whether
    it stopped because it was done.  A plan with a task still pending
    is not a completed plan, so it stays a crash.
    """
    plan_dir = _seed_plan(plan_env, marker=True, unfinished_task=TASK_IDS[-1])

    outcome = execution_routes._recovered_execution_outcome(
        PLAN_ID, plan_dir, plan_dir / "tasks.json"
    )
    assert outcome == "crashed", (
        "a run with an unfinished task was read as completed; "
        f"outcome={outcome!r}"
    )

    execution_routes._settle_recovered_execution(PLAN_ID)

    state = server._execution_state[PLAN_ID]
    assert state.get("status") == "failed", (
        "a run with an unfinished task was not settled as failed; "
        f"status={state.get('status')!r}"
    )
    assert _row_exec_status(plan_env) == "failed", (
        "the row does not carry the failed verdict; "
        f"exec_status={_row_exec_status(plan_env)!r}"
    )

    events = _settled_events(plan_env)
    assert len(events) == 1, (
        f"the settlement was not recorded exactly once; events={events!r}"
    )
    data = events[0].get("data") or {}
    assert data.get("outcome") == "crashed", (
        f"the settlement recorded the wrong outcome; data={data!r}"
    )
    assert data.get("unfinished") == 1, (
        f"the settlement did not record the unfinished task; data={data!r}"
    )


# ---------------------------------------------------------------------------
# 4. The success path hands the plan to the verification round
# ---------------------------------------------------------------------------


def test_success_starts_auto_verification(plan_env, recorded_verification):
    """A settled-as-completed run is handed to the auto-verification loop.

    Completing the tasks is not the end of a plan: the verification
    round is what decides whether the work is actually good.  A
    recovered run that finished must enter it exactly like a run that
    finished without a restart.
    """
    plan_dir = _seed_plan(plan_env, marker=True)

    execution_routes._settle_recovered_execution(PLAN_ID)

    assert recorded_verification["finished"].wait(timeout=10), (
        "the verification round was never handed the settled plan"
    )
    calls = recorded_verification["calls"]
    assert len(calls) == 1, (
        f"the verification round was started {len(calls)} time(s): {calls!r}"
    )
    assert calls[0]["plan_id"] == PLAN_ID, (
        f"the verification round was handed the wrong plan: {calls[0]!r}"
    )
    assert calls[0]["plan_dir"] == plan_dir, (
        f"the verification round was handed the wrong directory: {calls[0]!r}"
    )


# ---------------------------------------------------------------------------
# 5. The settlement happens once
# ---------------------------------------------------------------------------


def test_settlement_runs_once(plan_env, recorded_verification):
    """A second settle does not settle the run again.

    The watcher hands the run over once.  A settle that arrives twice
    — a second watcher, a second probe, a retry — must not write a
    second verdict, start a second verification round, or re-open a
    run that is already over.
    """
    _seed_plan(plan_env, marker=True)

    execution_routes._settle_recovered_execution(PLAN_ID)
    execution_routes._settle_recovered_execution(PLAN_ID)

    assert recorded_verification["finished"].wait(timeout=10), (
        "the verification round was never handed the settled plan"
    )
    assert len(recorded_verification["calls"]) == 1, (
        "the settled run was handed to the verification round "
        f"{len(recorded_verification['calls'])} time(s): "
        f"{recorded_verification['calls']!r}"
    )

    events = _settled_events(plan_env)
    assert len(events) == 1, (
        f"the settlement was recorded more than once; events={events!r}"
    )
