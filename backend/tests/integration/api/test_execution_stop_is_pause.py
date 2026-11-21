"""``POST /api/execution/{plan_id}/stop`` is a pause, not a discard.

The endpoint used to be a pure "stop the work" switch: it killed the
executor subprocess and flipped a flag. What it did **not** do was
leave anything behind that a later resume could read. A stopped plan
and a plan that had never run looked exactly the same on disk — no
record that task 2 was in flight, no record of the session id a
resume could re-open, no record of *which kind* of interruption
happened (a cooperative exit and a hard kill need different resume
strategies).

A stop is therefore a pause: it must leave a resumable trace.

What is pinned here:

1. **The order is the contract.** The plan is marked *before* the
   process is killed, and the checkpoint is written *after* the kill
   completed. The first half stops the executor's watcher thread and
   the heartbeat watchdog from stamping the plan ``failed`` (both act
   on ``status == "running"``); the second half makes the checkpoint
   describe the interruption as it actually ended.
2. **The checkpoint is complete.** It carries the tasks the stop cut
   short, the project directory, and — per task — the strategy a
   resume should use.
3. **A pause is not a failure.** The tasks stay ``in_progress``; the
   stop never writes ``failed`` over a task's runtime state.
4. **``?discard=true`` is the only way to throw the work away**, and
   it leaves no checkpoint behind.
5. **A repeated stop is idempotent** — one stop kills once, writes one
   checkpoint and keeps one stop record.

The stop is a **process**, not an instant
--------------------------------------------
2026-11-20: the endpoint used to do its killing on the request thread
and then answer ``{"status": "stopped"}`` — the plan was parked in the
``stopped`` phase by the very request that asked for the stop, while
the executor it had just signalled was still running.  The answer and
the plan's truth were two different things.

Now the request only closes the gate: it parks the routing row at the
plan-level ``user_stopping`` phase, writes the stop record and hands
the shutdown to a background worker.  The answer is ``stopping`` — the
plan is on its way down — and the phase is flipped by the background
layer, once the plan has really fallen silent.

So every case below reads its side effects (the kill, the checkpoint,
the row) AFTER the background worker has finished, and the phase is
polled rather than assumed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import pause_state
import server
import stop_state
import stop_supervisor
import verification_cancel
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261020-exec-stop-is-pause"

#: A second plan, used where one test needs two independent plans.
OTHER_PLAN_ID = "20261020-exec-stop-hardkill"

#: The quiet window the background layer waits for before it may park
#: the plan.  Short here on purpose: the window is a policy of the
#: deployment, not a contract of the stop, and a test that pays five
#: seconds per stop is a test nobody runs.
QUIET_SECONDS = "0.2"


class FakeProc:
    """A live process handle — the route only ever asks it ``poll()``.

    ``alive`` models what a real SIGTERM does: once the process has
    been signalled it is gone, and ``poll()`` stops answering
    ``None``. Without that, a second stop would believe the executor
    is still running and kill it again.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.alive = True

    def poll(self) -> Optional[int]:
        return None if self.alive else 0


class KillRecorder:
    """Stands in for ``server._kill_process_tree``.

    Records what the kill saw — the process it was handed, the grace
    window it was given, the in-memory state at that moment and the
    ``exec_status`` that was on disk — and reports whether the kill
    had to escalate to SIGKILL.
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
                "exec_status": _read_exec_status(plan_id),
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


@pytest.fixture(autouse=True)
def _clean_stop_state(monkeypatch):
    """The stop's process-local state is global — do not leak it.

    ``stop_state`` (the stop record), ``stop_supervisor`` (the
    supervisor threads) and ``verification_cancel`` (the cancel flag)
    are process-wide and indexed by plan id, and this suite reuses ONE
    plan id — so a record or a supervisor left behind by one case
    decides the next case's stop behaviour.

    The quiet window is shortened for the same reason the deployment
    may choose it: the window is a policy, not a contract.
    """
    monkeypatch.setenv("PDT_STOP_QUIET_SECONDS", QUIET_SECONDS)

    stop_state._reset_for_tests()
    stop_supervisor._reset_for_tests()
    verification_cancel._reset_for_tests()
    yield
    stop_supervisor._reset_for_tests()
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()


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


def _read_exec_status(plan_id: str) -> Optional[str]:
    """The ``exec_status`` on disk right now (separate connection)."""
    conn = open_db(server._state_db_path())
    try:
        migrate(conn)
        row = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    return (row or {}).get("exec_status")


def _a_pid_that_is_not_running() -> int:
    """A pid that is guaranteed not to name a running process.

    The stop asks the machine whether the plan's executor is still
    alive before it parks the plan — and the executor here is a fake.
    A hard-coded number could belong to an unrelated process on the
    machine, which would make the plan look busy and the stop look
    stuck.  So the pid is taken from a child this test spawns and
    reaps: it was alive a moment ago, and it is gone now.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child.wait(timeout=30)
    return child.pid


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    task_statuses: Optional[Dict[str, str]] = None,
    sessions: Optional[Dict[str, str]] = None,
):
    """A plan mid-execution, with tasks and (optionally) bound sessions.

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

    exec_pid = _a_pid_that_is_not_running()

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "executing")
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=str(project_dir),
            exec_status="running",
            exec_pid=exec_pid,
        )
        repo = PlanTaskRepository(conn)
        for task in tasks:
            repo.add_task(plan_id, dict(task))
        for task_id, status in (task_statuses or {}).items():
            repo.update_task(plan_id, task_id, {"status": status})
    finally:
        conn.close()

    # The execution log — the executor's record of which session each
    # task ran in. This is the only durable record of the binding.
    if sessions:
        with open(plan_dir / "execution.log", "w", encoding="utf-8") as handle:
            for task_id, session_id in sessions.items():
                handle.write(
                    json.dumps(
                        {
                            "ts": "2026-10-20T12:00:00",
                            "level": "INFO",
                            "event": "task_session_bound",
                            "message": f"Task [{task_id}] ran in session {session_id}",
                            "task_id": task_id,
                            "data": {"session_id": session_id},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    server._execution_state[plan_id] = {
        "status": "running",
        "logs": deque(maxlen=100),
        "project_dir": str(project_dir),
        "process": FakeProc(exec_pid),
        "started_at": "2026-10-20T11:00:00",
        "ended_at": None,
        "pid": exec_pid,
        "stop_reason": None,
    }
    return plan_dir, project_dir


def _read_checkpoint(plan_dir: Path) -> Dict[str, Any]:
    return json.loads((plan_dir / "pause_state.json").read_text(encoding="utf-8"))


def _read_phase(db_path: Path, plan_id: str = PLAN_ID) -> str:
    """The routing row's ``current_phase``, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _wait_for_stop_worker(
    plan_id: str = PLAN_ID, timeout: float = 60.0,
) -> None:
    """Wait for the background worker that shuts the plan's tree down.

    The stop answers as soon as the gate is closed; the kill and the
    checkpoint happen on the worker's own thread.  So every side effect
    of the shutdown — the kill, the checkpoint, the row — is read
    AFTER this wait, never before it.
    """
    deadline = time.monotonic() + timeout
    while server._routes_execution._stop_worker_running(plan_id):
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"the stop's background worker is still running after "
                f"{timeout:.0f}s"
            )
        time.sleep(0.05)


def _wait_for_stop_to_settle(
    db_path: Path, plan_id: str = PLAN_ID, timeout: float = 60.0,
) -> str:
    """Wait for the background layer's verdict, and read the phase back.

    The phase is flipped by the background layer, not by the request:
    the request parks the plan at the stopping phase and hands the
    shutdown over.  So the phase is polled, never assumed.

    The stop record is waited for too — it is cleared when the stop
    finalizes, and it is what says "a stop is in progress for this
    plan".
    """
    deadline = time.monotonic() + timeout
    phase = _read_phase(db_path, plan_id)
    while time.monotonic() < deadline:
        if phase != "user_stopping" and stop_state.get(plan_id) is None:
            return phase
        time.sleep(0.05)
        phase = _read_phase(db_path, plan_id)
    raise AssertionError(
        f"the stop never finalized — the plan is still parked at "
        f"{phase!r} after {timeout:.0f}s"
    )


# ---------------------------------------------------------------------------
# 1) the order: mark → kill → checkpoint
# ---------------------------------------------------------------------------


def test_stop_signal_order(env, client, kill_recorder, monkeypatch):
    """The mark precedes the kill; the checkpoint follows it.

    Both halves matter. The mark first: the executor's watcher thread
    and the heartbeat watchdog both act on ``status == "running"``, so
    a kill that lands before the mark lets the executor wake up, see
    a process that is gone and stamp the plan ``failed`` — the exact
    outcome a pause must never produce. The checkpoint last: it has
    to describe the interruption as it actually ended.

    The order is made on the background worker now, so the case waits
    for that worker before it reads the order back.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})

    order: List[str] = []
    real_write = pause_state.write_pause_checkpoint

    def _spy_write(*args, **kwargs):
        order.append("write_checkpoint")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(pause_state, "write_pause_checkpoint", _spy_write)

    real_kill = kill_recorder.__call__

    def _spy_kill(proc, grace_seconds=None):
        order.append("kill")
        return real_kill(proc, grace_seconds=grace_seconds)

    monkeypatch.setattr(server, "_kill_process_tree", _spy_kill)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stopping", resp.json()

    _wait_for_stop_worker(PLAN_ID)

    assert order == ["kill", "write_checkpoint"], order

    # At the moment of the kill the plan was already marked — in
    # memory and on disk.
    at_kill = kill_recorder.calls[0]
    assert at_kill["state"]["status"] == "stopped"
    assert at_kill["state"]["stop_reason"] == "pausing"
    assert at_kill["exec_status"] == "stopped"


def test_stop_kills_only_its_own_plan(env, client, kill_recorder):
    """A stop must not touch another plan's executor."""
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    _seed_plan(plans_dir, db_path, plan_id=OTHER_PLAN_ID)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stopping", resp.json()

    _wait_for_stop_worker(PLAN_ID)

    assert len(kill_recorder.calls) == 1, kill_recorder.calls
    assert kill_recorder.calls[0]["plan_id"] == PLAN_ID
    other_proc = server._execution_state[OTHER_PLAN_ID]["process"]
    assert kill_recorder.calls[0]["proc"] is not other_proc
    assert other_proc.alive is True


# ---------------------------------------------------------------------------
# 2) the checkpoint says what was interrupted
# ---------------------------------------------------------------------------


def test_stop_writes_checkpoint(env, client, kill_recorder):
    """The resume point carries the interrupted task and the project dir."""
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path, task_statuses={"2": "in_progress"},
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stopping", body
    assert body["paused"] is True, body

    # The resume point is written by the background worker, so it is
    # read after that worker is done.
    _wait_for_stop_worker(PLAN_ID)

    checkpoint = _read_checkpoint(plan_dir)
    assert checkpoint["kind"] == "execution"
    assert checkpoint["project_dir"] == str(project_dir)
    assert checkpoint["paused_at"]

    interrupted = {t["task_id"]: t for t in checkpoint["interrupted_tasks"]}
    assert "2" in interrupted, checkpoint["interrupted_tasks"]
    assert interrupted["2"]["status"] == "in_progress"
    assert interrupted["2"]["phase"] == "execution"
    assert interrupted["2"]["paused_at"] == checkpoint["paused_at"]

    # The answer is written before the checkpoint exists, so the
    # checkpoint's own numbers are the ones that are asserted here.
    assert len(checkpoint["interrupted_tasks"]) == 1, checkpoint


def test_stop_marks_tasks_not_failed(env, client, kill_recorder):
    """A pause is not a failure: the task stays ``in_progress``."""
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stopping", resp.json()

    _wait_for_stop_worker(PLAN_ID)

    conn = open_db(db_path)
    try:
        row = PlanTaskRepository(conn).get_task(PLAN_ID, "2")
    finally:
        conn.close()
    assert row is not None, "the task row disappeared"
    assert row["status"] == "in_progress", row


def test_stop_records_per_task_strategy(env, client, kill_recorder):
    """Each interrupted task records how a resume should treat it.

    A task that had a session bound can be resumed *in* that session
    — unless the tree had to be hard-killed, in which case the
    conversation may have been cut mid-write and the task has to run
    again from scratch.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        task_statuses={"2": "in_progress"},
        sessions={"2": "sess-2"},
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stopping", resp.json()

    _wait_for_stop_worker(PLAN_ID)

    entry = {t["task_id"]: t for t in _read_checkpoint(plan_dir)["interrupted_tasks"]}["2"]
    assert entry["session_id"] == "sess-2"
    assert entry["strategy"] == "session_resume"
    assert entry["reason"] == "session_id_available"

    # Same plan shape, but the tree had to be hard-killed.
    hard_dir, _ = _seed_plan(
        plans_dir, db_path,
        plan_id=OTHER_PLAN_ID,
        task_statuses={"2": "in_progress"},
        sessions={"2": "sess-2"},
    )
    kill_recorder.hard_killed = True

    resp = client.post(f"/api/execution/{OTHER_PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stopping", resp.json()

    _wait_for_stop_worker(OTHER_PLAN_ID)

    hard_entry = {
        t["task_id"]: t for t in _read_checkpoint(hard_dir)["interrupted_tasks"]
    }["2"]
    assert hard_entry["strategy"] == "full_rerun"
    assert hard_entry["reason"] == "sigkill_after_grace"


# ---------------------------------------------------------------------------
# 3) discard, and idempotence
# ---------------------------------------------------------------------------


def test_stop_discard_skips_checkpoint(env, client, kill_recorder):
    """``?discard=true`` throws the work away instead of pausing it."""
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, task_statuses={"2": "in_progress"},
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop?discard=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stopping", body
    # The answer describes the request that was made: a discard is not
    # a pause, so the answer says the work was not paused.
    assert body["paused"] is False, body

    _wait_for_stop_worker(PLAN_ID)

    assert not (plan_dir / "pause_state.json").exists()


def test_stop_discard_clears_an_existing_checkpoint(env, client, kill_recorder):
    """A discard after a pause removes the resume point.

    The discard has to reach the **idle** path to clear the
    checkpoint, and the idle path is only reached once the plan has
    been parked: while the stop is still in progress the plan sits at
    the stopping phase, and a discard issued there is answered by the
    stopping path — which does not clear anything.

    So the case waits for the background layer's verdict first.  That
    is the whole point: the two requests are two different stops, and
    the second one is only allowed to throw the work away once the
    first one is really over.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, task_statuses={"2": "in_progress"},
    )

    first = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "stopping", first.json()

    _wait_for_stop_worker(PLAN_ID)
    assert (plan_dir / "pause_state.json").exists()

    # The plan is still at the stopping phase until the background
    # layer's quiet window ends.
    assert _read_phase(db_path, PLAN_ID) == "user_stopping"
    assert _wait_for_stop_to_settle(db_path, PLAN_ID) == "stopped"

    second = client.post(f"/api/execution/{PLAN_ID}/stop?discard=true")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "stopped", body
    assert body["paused"] is False, body

    assert not (plan_dir / "pause_state.json").exists()


def test_stop_discard_during_the_stop_answers_not_paused(
    env, client, kill_recorder, monkeypatch,
):
    """A discard asked for DURING the stop says the work was thrown away.

    The repeated stop is answered by the stopping path, and that path
    has to answer the request it was actually given: ``?discard=true``
    means "do not keep the work", so the answer says ``paused: false``
    — the same answer the first stop gives for a discard.

    What the request does NOT do is clear the checkpoint: the stop is
    still in progress, and the checkpoint is cleared by the idle path
    once the plan has been parked.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, task_statuses={"2": "in_progress"},
    )

    # The supervisor is not started, so the plan stays at the stopping
    # phase — which is the state a repeated stop arrives in.
    monkeypatch.setattr(stop_supervisor, "start", lambda *a, **k: None)

    first = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "stopping", first.json()

    _wait_for_stop_worker(PLAN_ID)
    assert _read_phase(db_path, PLAN_ID) == "user_stopping"

    second = client.post(f"/api/execution/{PLAN_ID}/stop?discard=true")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "stopping", body
    assert body["paused"] is False, (
        "the repeated stop answered 'paused: true' for a discard — the "
        "answer has to describe the request that was made"
    )

    # And the repeated stop did not start a second shutdown.
    assert len(kill_recorder.calls) == 1, kill_recorder.calls
    # The checkpoint is still there: a stop that is still in progress
    # has not thrown anything away yet.
    assert (plan_dir / "pause_state.json").exists()


def test_stop_is_idempotent(env, client, kill_recorder, monkeypatch):
    """One stop kills once, writes one checkpoint, keeps one record.

    A second stop issued while the first one is still in progress is
    the SAME stop: it must not start a second shutdown, it must not
    rewrite the resume point, and it must not replace the stop record
    — the record carries the dispatch baseline of the stop that really
    began, and a second record would lose it.

    The answers are compared on their contract fields only:
    ``stopped_at`` is a fresh timestamp on every answer, and it is not
    part of the contract.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, task_statuses={"2": "in_progress"},
    )

    # The supervisor is not started, so the plan stays at the stopping
    # phase — which is the state a repeated stop arrives in.
    monkeypatch.setattr(stop_supervisor, "start", lambda *a, **k: None)

    first = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "stopping", first_body

    _wait_for_stop_worker(PLAN_ID)
    checkpoint_after_first = _read_checkpoint(plan_dir)
    record_after_first = stop_state.get(PLAN_ID)
    assert record_after_first is not None, "the stop wrote no record"

    second = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert second.status_code == 200, second.text
    second_body = second.json()

    # The two answers agree on the contract...
    for field in (
        "status", "plan_id", "stop_scope", "paused", "previous_phase",
        "reason",
    ):
        assert second_body[field] == first_body[field], (
            f"the repeated stop answered {field}={second_body[field]!r} "
            f"after a first answer of {first_body[field]!r}"
        )

    # ... the checkpoint was not rewritten — ``paused_at`` still says
    # when the interruption actually happened.
    assert _read_checkpoint(plan_dir) == checkpoint_after_first
    # And the executor was not killed twice.
    assert len(kill_recorder.calls) == 1, kill_recorder.calls
    # And the stop record is still the one the first stop wrote.
    assert stop_state.get(PLAN_ID) is record_after_first
