"""``POST /api/execution/{plan_id}/stop`` parks the task in ITS session.

Background
----------
2026-10-26. The executor records a task's session binding twice: once
*before* the dispatch starts (the id the dispatch was asked for) and
once after it returns (the id it actually used — a provider fallback
may have switched it). The first record is the one that matters when a
stop lands while a dispatch is still blocked: without it the stop can
only answer ``no_session_id``, and the whole plan is re-run from
scratch instead of continuing in the conversation it was in.

The two ends of that record have to agree, and the two halves of the
agreement are pinned by different files:

* the writer is ``agent._log_task_session_bound`` — the one place that
  writes the shape (pinned by the writer's own test);
* the reader is ``pause_state.collect_task_sessions``, reached through
  ``routes.execution._stop_interruption`` (pinned by the reader's own
  test, and by the unit-level proof that a line the *writer* produced
  is a line the *reader* understands).

Neither of those proves the *endpoint* honours it. The unit test calls
``_stop_interruption`` directly; the endpoint test that covers the stop
(``test_execution_stop_is_pause.py``) hand-writes the log line it
reads. This file is the one that joins the two: the log line is
produced by the **product writer** into a **real** ``execution.log``,
and the answer comes back out of the **real** HTTP endpoint, as a
checkpoint on disk.

What is pinned
--------------
1. **The session survives the stop.** A cooperative stop
   (``hard_killed=False``) on a task that had a pre-dispatch binding
   records ``session_resume`` and the very session id that was bound.
2. **A pause is not a failure.** The interrupted task stays
   ``in_progress`` in ``plan_tasks``; the stop never stamps it
   ``failed``.
3. **No binding, no session.** A task that was running but had no
   binding falls back to ``full_rerun`` / ``no_session_id`` — the
   regression guard for the reader.
4. **A hard kill still refuses the session.** The tree was SIGKILLed,
   so the conversation may have been cut mid-write: the strategy stays
   ``full_rerun`` / ``sigkill_after_grace`` even though the log names a
   session. ``server._kill_process_tree`` stays the fallback it is, and
   the ``hard_killed`` verdict keeps its meaning.
5. **The stop parks the phase.** ``plan_routing.current_phase`` becomes
   ``stopped``. This is the berth the requeue facade
   (``plan_lifecycle_service``) expects to find: it moves a stopped
   plan back to ``queued`` and never decides the berth itself. The
   transition rules are NOT touched by this file — the assertion only
   reads where the plan landed.

Boundary cases
--------------
* no ``execution.log`` at all -> no binding, no exception, a full
  re-run;
* a repeated stop -> the same answer, and the checkpoint on disk is
  not rewritten.

Harness
-------
The fixtures are those of ``test_execution_stop_is_pause.py`` (a seeded
plan, a fake process handle, a kill recorder), kept separate on
purpose: the two files answer different questions, and a shared
fixture would let a change to one silently change the other.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

# ``backend/`` on ``sys.path`` so ``import agent`` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` is imported BEFORE anything that reaches into the route
# modules: the application module seeds ``sys.modules['server']`` and
# then imports the routes, so importing a route first would make it
# import a half-initialised ``server`` back.
import server  # noqa: E402
from server import app  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.execution_repository import (  # noqa: E402
    ExecutionRepository,
)
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)

#: The plan the stop runs against. The ``20261026-`` prefix is load
#: bearing: a plan directory named after a date on or before the
#: archive cutoff is archived, and an archived plan is read-only — the
#: stop would answer 410 and nothing would be pinned at all.
PLAN_ID = "20261026-stop-coop"

#: A second plan, for the case that needs two independent plans.
HARD_KILL_PLAN_ID = "20261026-stop-coop-hardkill"

#: The task the stop interrupts. Task ``1`` stays ``pending`` so the
#: checkpoint has exactly one interrupted entry.
TASK_ID = "2"

#: The session id the pre-dispatch binding names.
SESSION_ID = "sid-2"


class FakeProc:
    """A live process handle — the route only ever asks it ``poll()``.

    ``alive`` models what a real SIGTERM does: once the process has
    been signalled it is gone, and ``poll()`` stops answering ``None``.
    Without that, a second stop would believe the executor is still
    running and kill it again.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.alive = True

    def poll(self) -> Optional[int]:
        return None if self.alive else 0


class KillRecorder:
    """Stands in for ``server._kill_process_tree``.

    Records what the kill saw and reports whether the kill had to
    escalate to SIGKILL — the ``hard_killed`` verdict the stop turns
    into a resume strategy.
    """

    def __init__(self) -> None:
        self.hard_killed = False
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, proc: Any, grace_seconds: Any = None) -> bool:
        plan_id = self._plan_of(proc)
        self.calls.append(
            {
                "plan_id": plan_id,
                "proc": proc,
                "grace_seconds": grace_seconds,
                "state": dict(server._execution_state.get(plan_id) or {}),
            }
        )
        # A real kill ends the process.
        if hasattr(proc, "alive"):
            proc.alive = False
        return self.hard_killed

    @staticmethod
    def _plan_of(proc: Any) -> Optional[str]:
        for plan_id, state in server._execution_state.items():
            if (state or {}).get("process") is proc:
                return plan_id
        return None


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    # The executor's logger and the stop path's reader resolve their
    # directories through the env knobs, not through ``server.PLANS_DIR``
    # — so the knobs have to point at the same tree, or the binding
    # would be written into one place and read from another.
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    server._execution_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def kill_recorder(monkeypatch):
    """Replace the process-group kill with a recorder."""
    recorder = KillRecorder()
    monkeypatch.setattr(server, "_kill_process_tree", recorder)
    return recorder


def _bind_session(plan_id: str, task_id: str, session_id: str) -> Path:
    """Write a pre-dispatch binding with the PRODUCT writer.

    ``agent._log_task_session_bound`` is the one place that writes the
    ``task_session_bound`` shape, and a real ``ExecutionLogger`` puts
    it into a real ``execution.log`` — the same file the stop path
    reads.  Writing it here by hand would pin an imitation of the
    contract instead of the contract.

    Returns the path of the log the binding was written to.
    """
    import agent as agent_module
    from coding_tool import JsonSession
    from config_paths import resolve_plans_dir
    from execution_logger import ExecutionLogger

    logger = ExecutionLogger(plan_id)
    agent_module._log_task_session_bound(
        logger,
        task_id,
        JsonSession(pinned_session_id=session_id, stage="pinned"),
        1,
    )
    return resolve_plans_dir() / plan_id / "execution.log"


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    task_statuses: Optional[Dict[str, str]] = None,
    bind_session: Optional[str] = None,
    bind_task_id: str = TASK_ID,
):
    """A plan mid-execution, with tasks and (optionally) a bound session.

    Returns ``(plan_dir, project_dir)``.  A SECOND connection is used
    (not the fixture's) so the rows the endpoint reads back are the
    committed ones.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    tasks = [
        {"id": "1", "title": "first task"},
        {"id": "2", "title": "second task"},
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "executing")
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=str(project_dir),
            exec_status="running",
            exec_pid=4242,
        )
        repo = PlanTaskRepository(conn)
        for task in tasks:
            repo.add_task(plan_id, dict(task))
        for task_id, status in (task_statuses or {}).items():
            repo.update_task(plan_id, task_id, {"status": status})
    finally:
        conn.close()

    # The binding is written AFTER the task is marked ``in_progress`` —
    # that is the order the executor uses (status, then bind, then
    # dispatch), and it is the order a stop landing mid-dispatch sees.
    if bind_session is not None:
        _bind_session(plan_id, bind_task_id, bind_session)

    server._execution_state[plan_id] = {
        "status": "running",
        "logs": deque(maxlen=100),
        "project_dir": str(project_dir),
        "process": FakeProc(),
        "started_at": "2026-10-26T11:00:00",
        "ended_at": None,
        "pid": 4242,
        "stop_reason": None,
    }
    return plan_dir, project_dir


def _read_checkpoint(plan_dir: Path) -> Dict[str, Any]:
    return json.loads((plan_dir / "pause_state.json").read_text(encoding="utf-8"))


def _interrupted_entry(plan_dir: Path) -> Dict[str, Any]:
    """The one interrupted task the checkpoint recorded."""
    entries = _read_checkpoint(plan_dir)["interrupted_tasks"]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["task_id"] == TASK_ID, entry
    return entry


# ---------------------------------------------------------------------------
# 1) the session the task was bound to survives the stop
# ---------------------------------------------------------------------------


def test_stop_records_session_resume_from_predispatch_binding(
    env, client, kill_recorder
):
    """A cooperative stop keeps the task in the session it was bound to.

    The binding was written by the product writer into the real
    ``execution.log`` before the dispatch started.  The stop reads that
    log, finds the binding, and records ``session_resume`` with the very
    id that was bound — so the next start re-opens the conversation
    instead of re-running the task from scratch.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        task_statuses={TASK_ID: "in_progress"},
        bind_session=SESSION_ID,
    )

    # The binding really is on disk, in the file the stop will read.
    log_path = plan_dir / "execution.log"
    assert log_path.exists(), (
        "the product writer must have put the binding on the log; the "
        f"stop reads {log_path}"
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    entry = _interrupted_entry(plan_dir)
    assert entry["strategy"] == "session_resume", entry
    assert entry["session_id"] == SESSION_ID, entry
    assert entry["reason"] == "session_id_available", entry
    assert entry["status"] == "in_progress", entry


def test_stop_does_not_mark_task_failed(env, client, kill_recorder):
    """A pause is not a failure: the task stays ``in_progress``.

    The stop kills the executor, but the task it was running is not
    over — it is paused.  Stamping it ``failed`` would make the next
    start treat the work as attempted-and-broken instead of as work
    still owed.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path,
        task_statuses={TASK_ID: "in_progress"},
        bind_session=SESSION_ID,
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    conn = open_db(db_path)
    try:
        row = PlanTaskRepository(conn).get_task(PLAN_ID, TASK_ID)
    finally:
        conn.close()
    assert row is not None, "the task row disappeared"
    assert row["status"] == "in_progress", row
    assert row["status"] != "failed", row


def test_no_binding_still_full_rerun(env, client, kill_recorder):
    """A running task with no binding falls back to a full re-run.

    This is the regression guard for the reader: without a binding
    there is no session to re-open, so the stop must say so plainly
    (``no_session_id``) rather than invent one.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        task_statuses={TASK_ID: "in_progress"},
    )

    # No binding was written, so there is no log at all.
    assert not (plan_dir / "execution.log").exists()

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    entry = _interrupted_entry(plan_dir)
    assert entry["strategy"] == "full_rerun", entry
    assert entry["reason"] == "no_session_id", entry
    assert entry["session_id"] is None, entry


def test_hard_killed_still_forces_full_rerun(env, client, kill_recorder):
    """A hard kill refuses the session, whatever the log says.

    A tree that had to be SIGKILLed may have been cut mid-write, so
    its session cannot be trusted to continue.  The binding is still
    read and still reported — but the strategy stays ``full_rerun``,
    and the reason says why.

    This also pins that the kill is still ``server._kill_process_tree``
    doing the escalating: the recorder stands in for it and reports
    the escalation, so the endpoint's ``hard_killed`` verdict is what
    decides the strategy.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        plan_id=HARD_KILL_PLAN_ID,
        task_statuses={TASK_ID: "in_progress"},
        bind_session=SESSION_ID,
    )

    kill_recorder.hard_killed = True

    resp = client.post(f"/api/execution/{HARD_KILL_PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    assert len(kill_recorder.calls) == 1, kill_recorder.calls
    entry = _interrupted_entry(plan_dir)
    assert entry["strategy"] == "full_rerun", entry
    assert entry["reason"] == "sigkill_after_grace", entry
    # The binding is still reported — only the strategy changed.
    assert entry["session_id"] == SESSION_ID, entry


def test_stop_parks_plan_in_stopped_phase(env, client, kill_recorder):
    """The stop parks the plan in ``stopped``.

    This is the berth the requeue facade expects: it moves a stopped
    plan back to ``queued`` and never decides the berth itself.  The
    assertion only READS where the plan landed — the transition rules
    belong to ``plan_lifecycle_service`` and are not touched here.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path,
        task_statuses={TASK_ID: "in_progress"},
        bind_session=SESSION_ID,
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    conn = open_db(db_path)
    try:
        routing_row = RoutingRepository(conn).find(PLAN_ID)
    finally:
        conn.close()

    assert routing_row is not None, "the plan_routing row disappeared"
    assert routing_row["current_phase"] == "stopped", routing_row
    # A refusal to park is reported in the body — there must not be one.
    assert "phase_transition_error" not in resp.json(), resp.json()


# ---------------------------------------------------------------------------
# 2) the boundary cases
# ---------------------------------------------------------------------------


def test_missing_execution_log_is_not_an_error(env, client, kill_recorder):
    """A plan with no log at all -> no binding, and no exception.

    A plan that never ran has no ``execution.log``.  The stop has to
    treat that as "nothing to resume" rather than as a failure of the
    stop itself.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        task_statuses={TASK_ID: "in_progress"},
    )
    assert not (plan_dir / "execution.log").exists()

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    entry = _interrupted_entry(plan_dir)
    assert entry["strategy"] == "full_rerun", entry
    assert entry["reason"] == "no_session_id", entry


def test_repeated_stop_returns_the_same_checkpoint(env, client, kill_recorder):
    """Stopping twice answers the same thing and keeps one checkpoint.

    The checkpoint on disk is the record of *when* the interruption
    happened; a second stop must not move it, or the resume point
    would claim the interruption happened later than it did.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        task_statuses={TASK_ID: "in_progress"},
        bind_session=SESSION_ID,
    )

    first = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert first.status_code == 200, first.text
    checkpoint_after_first = _read_checkpoint(plan_dir)

    second = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert second.status_code == 200, second.text

    assert first.json() == second.json(), (first.json(), second.json())
    assert _read_checkpoint(plan_dir) == checkpoint_after_first
    # And the executor was not killed twice.
    assert len(kill_recorder.calls) == 1, kill_recorder.calls
