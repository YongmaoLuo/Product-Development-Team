"""End-to-end: stop → restart the backend → resume, across the boundary.

Everything the pause/resume feature claims is only true *together*.
Each half has its own test elsewhere:

* ``integration/api/test_execution_stop_is_pause.py`` — a stop leaves
  a resume point;
* ``integration/api/test_execution_resume_start.py`` — a start honours
  one, including with an empty request body;
* ``api/test_plan_import_api.py`` — the read endpoints expose the
  pause.

None of them crosses the process boundary, and that is where this
feature actually lives. The pause is written to disk precisely because
**the process that holds the in-memory record is the process that goes
away**. A suite that stops and resumes inside one interpreter proves
the two halves agree; it cannot prove the record survives the only
event that matters.

So this file drives the whole chain in one go:

1. a plan mid-execution, two tasks finished and two in flight;
2. ``POST /stop`` → the plan is paused, not failed, not discarded;
3. **a restart** — the in-memory state is thrown away and rebuilt by
   the very recovery scan the server runs at boot, against the same
   ``plans/`` tree and the same ``state.db``;
4. ``POST /start`` with an **empty body** → the work continues from the
   pause point, in the sessions the interruption left usable, without
   the operator repeating the project directory.

What is pinned here, and why each one would be a real defect:

* **The pause survives the restart.** The recovery scan rebuilds the
  runtime state from SQLite; the pause comes from the plan directory.
  If either half were read from the wrong place, this is the test that
  would notice — and the operator would find out by clicking "继续" on
  a plan that had silently lost its interruption.
* **The finished tasks stay finished.** A restart is not a re-run.
  Their ``updated_at`` is asserted byte-identical across the whole
  chain, because that is what a re-run would move.
* **The resume point is single-shot.** The pause is retired once the
  executor is up, so a second start cannot re-consume the same
  interruption.
* **Only the interrupted tasks are continued.** ``PDT_RESUME_SESSIONS``
  carries the two in-flight sessions and nothing else; handing a
  finished task a session id would re-open a conversation whose work
  is already committed.
* **The 2026-07-16 guard is untouched.** A plan with *no* resume point
  still has to name its project directory — eleven empty commits in the
  wrong repository came from defaulting that field.

No real executor and no real model: ``_spawn_executor_subprocess`` is
replaced with a recorder and the process-group kill with a cooperative
stub, so the test runs in-process in under a second and calls nothing.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import pause_state
import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository

pytestmark = [pytest.mark.e2e]

PLAN_ID = "20261020-pause-resume-e2e"

#: The half of the plan that finished before the stop. It must not be
#: touched by anything that follows.
COMPLETED_TASKS = ("1", "2")

#: The half that was still running when the stop landed. These are the
#: only tasks a resume may continue.
INTERRUPTED_TASKS = ("3", "4")

#: ``task_id`` → the session that task was bound to when it was cut
#: short. This is what makes the interruption resumable rather than a
#: re-run, so it is the highest-value thing on disk.
TASK_SESSIONS = {"3": "sess-3", "4": "sess-4"}


class FakeProc:
    """A process handle that never ends on its own.

    ``wait()`` parks until the process is released, so the plan stays
    ``running`` for the length of the test instead of racing to
    ``completed`` before a single assertion runs. ``terminate`` /
    ``kill`` model what a signalled process does — it goes away, and
    ``poll()`` stops answering ``None``.

    The pid is deliberately out of range for every platform PDT runs
    on: nothing resolves it, so a stray ``os.getpgid`` in the kill
    helper raises ``ProcessLookupError`` instead of reaching a real
    process group.
    """

    def __init__(self, pid: int = 0x3FFFFFFF) -> None:
        self.pid = pid
        self.returncode: Optional[int] = None
        self._done = threading.Event()

    def poll(self) -> Optional[int]:
        return None if not self._done.is_set() else 0

    def wait(self, timeout: Optional[float] = None) -> int:
        self._done.wait(timeout)
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self._done.set()

    def kill(self) -> None:
        self._done.set()

    def release(self) -> None:
        self._done.set()


class SpawnRecorder:
    """Stands in for ``server._spawn_executor_subprocess``.

    Records the keyword arguments the start endpoint passed and hands
    back a fake process. ``cli.py`` is never launched and no model is
    ever called — which is also why the sessions it was handed can be
    inspected afterwards: nothing else read them.

    The environment it records is where the resume point shows up:
    ``PDT_RESUME_SESSIONS`` is the executor's only instruction about
    which conversations to re-open.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.processes: List[FakeProc] = []

    def __call__(self, **kwargs: Any):
        self.calls.append(kwargs)
        proc = FakeProc()
        self.processes.append(proc)
        return proc, Path(kwargs["plan_id"]) / "logs" / "executor.log"

    def release_all(self) -> None:
        """Let every parked watcher thread finish (teardown)."""
        for proc in self.processes:
            proc.release()

    @property
    def last(self) -> Dict[str, Any]:
        assert self.calls, "the executor was never spawned"
        return self.calls[-1]

    def sessions(self) -> Dict[str, str]:
        """The ``task_id`` → ``session_id`` mapping handed to the executor."""
        extra_env = self.last.get("extra_env") or {}
        raw = extra_env.get("PDT_RESUME_SESSIONS")
        return json.loads(raw) if raw else {}


class CooperativeKill:
    """Stands in for ``server._kill_process_tree``.

    The real helper signals a real process *group* with ``os.killpg``.
    There is no real group here — :class:`SpawnRecorder` never launched
    ``cli.py`` — and signalling a pid that happens to belong to some
    unrelated process on the developer's machine is not a thing a test
    may do.

    What the resume logic actually branches on is the *outcome*, so
    that is what this models: the executor was asked to stop and
    stopped inside the grace window. ``False`` means "no SIGKILL
    escalation", which is what keeps the interrupted tasks resumable in
    their own sessions instead of being invalidated into full re-runs.
    """

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def __call__(self, proc: Any, grace_seconds: Any = None) -> bool:
        self.calls.append(proc)
        terminate = getattr(proc, "terminate", None)
        if callable(terminate):
            terminate()
        return False


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test ``PLANS_DIR`` + ``state.db`` isolation.

    Same two paths the autouse ``isolated_plans_dir`` fixture points at,
    so the routes, ``plan_state`` and ``config_paths`` all resolve one
    database rather than three.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def spawn(monkeypatch):
    """No real executor: a recorder stands in for the spawn.

    The verification hand-off and the heartbeat probe are stubbed too —
    both act on a *live* executor process, and there is none. Leaving
    them real would have them poll a fake pid and conclude, wrongly,
    that the executor died.
    """
    recorder = SpawnRecorder()
    monkeypatch.setattr(server, "_spawn_executor_subprocess", recorder)
    monkeypatch.setattr(
        server, "_kill_process_tree", CooperativeKill(),
    )
    monkeypatch.setattr(
        server, "_run_auto_verification_loop", lambda *a, **k: None,
    )
    monkeypatch.setattr(server, "_lazy_check_execution", lambda *a, **k: None)
    yield recorder
    recorder.release_all()


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    task_statuses: Optional[Dict[str, str]] = None,
    sessions: Optional[Dict[str, str]] = None,
    running: bool = False,
):
    """A plan on disk: tasks, runtime state, sessions, and a run record.

    Returns ``(plan_dir, project_dir)``. With ``running=True`` the
    in-memory execution state is installed as well, so the plan looks
    mid-execution to both the stop endpoint and the status endpoint.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    task_ids = list(COMPLETED_TASKS) + list(INTERRUPTED_TASKS)
    tasks = [{"id": task_id, "title": f"task {task_id}"} for task_id in task_ids]
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
            exec_status="running" if running else "not_started",
            exec_pid=FakeProc().pid if running else None,
        )
        repo = PlanTaskRepository(conn)
        for task in tasks:
            repo.add_task(plan_id, dict(task))
        for task_id, status in (task_statuses or {}).items():
            repo.update_task(plan_id, task_id, {"status": status})
    finally:
        conn.close()

    # The execution log — the executor's own record of which session
    # each task ran in, and the only durable copy of the binding.
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
                    ) + "\n"
                )

    if running:
        proc = FakeProc()
        server._execution_state[plan_id] = {
            "status": "running",
            "logs": deque(maxlen=2000),
            "project_dir": str(project_dir),
            "process": proc,
            "started_at": "2026-10-20T11:00:00",
            "ended_at": None,
            "pid": proc.pid,
            "stop_reason": None,
        }
    return plan_dir, project_dir


def _task_updated_at(db_path: Path, plan_id: str, task_id: str) -> str:
    """A finished task's last write time, read on its own connection."""
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT updated_at FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no row for task {task_id}"
    return row[0]


def _restart(plans_dir: Path) -> None:
    """Rebuild the in-memory state the way a boot does.

    Everything that lives only in RAM is dropped, and then the server's
    own recovery scan runs against the *same* ``plans/`` tree and the
    *same* ``state.db`` — the two things a restart does not take with
    it. Nothing here touches disk: a restart is a loss of memory, not
    of work.
    """
    server._execution_state.clear()
    server._verification_state.clear()
    server._recover_execution_states(plans_dir)


# ---------------------------------------------------------------------------
# 1) stop → restart → resume, end to end
# ---------------------------------------------------------------------------


def test_pause_restart_resume_e2e(env, client, spawn):
    """The whole chain, across a restart, from an empty request body.

    The operator presses "停止", the backend goes away and comes back,
    and then presses "继续" without retyping anything. What they get
    back is the two tasks that were in flight — continued in the
    sessions they were in — and not a second run of the two that had
    already finished.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path,
        task_statuses={
            "1": "completed", "2": "completed",
            "3": "in_progress", "4": "in_progress",
        },
        sessions=TASK_SESSIONS,
        running=True,
    )

    # --- stop: a pause, not a failure and not a discard ---------------
    stopped = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["paused"] is True, stopped.text
    assert stopped.json()["interrupted_task_count"] == len(INTERRUPTED_TASKS)

    paused = client.get(f"/api/execution/{PLAN_ID}/status").json()
    assert paused["status"] == "stopped", paused
    assert paused["paused"] is True, paused
    assert pause_state.read_pause_checkpoint(plan_dir) is not None

    # The two finished tasks were not counted as owed work, and the two
    # interrupted ones were not pushed into a terminal state.
    owed = {
        entry["task_id"]
        for entry in pause_state.read_pause_checkpoint(plan_dir)["unfinished_tasks"]
    }
    assert owed == set(INTERRUPTED_TASKS), owed

    # --- restart: only memory is lost ---------------------------------
    finished_before = {
        task_id: _task_updated_at(db_path, PLAN_ID, task_id)
        for task_id in COMPLETED_TASKS
    }
    _restart(plans_dir)

    # The runtime state really was rebuilt rather than left over: what
    # the boot scan can reconstruct carries no process handle.
    recovered = server._execution_state[PLAN_ID]
    assert recovered["status"] == "stopped", recovered
    assert recovered["process"] is None, recovered
    assert recovered["project_dir"] == str(project_dir), recovered

    # And the pause is still there — it lives in the plan directory, so
    # it is exactly what a restart cannot take away.
    after_restart = client.get(f"/api/execution/{PLAN_ID}/status").json()
    assert after_restart["paused"] is True, after_restart
    assert after_restart["interrupted_task_count"] == len(INTERRUPTED_TASKS)
    assert (
        client.get("/api/system/active").json()["paused_executions"]
        == [{
            "plan_id": PLAN_ID,
            "kind": "execution",
            "paused_at": after_restart["paused_at"],
            "interrupted_task_count": len(INTERRUPTED_TASKS),
        }]
    ), "a paused plan must stay visible after the restart"

    # Nothing re-ran across the restart: a restart is not a re-run.
    assert {
        task_id: _task_updated_at(db_path, PLAN_ID, task_id)
        for task_id in COMPLETED_TASKS
    } == finished_before

    # --- resume: an empty body is enough ------------------------------
    # ``project_dir`` is deliberately absent — this is the decision the
    # whole feature rests on: the checkpoint already knows where the
    # work was happening.
    resumed = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resumed.status_code == 200, resumed.text

    body = resumed.json()
    assert body["resumed"] is True, body
    assert body["project_dir"] == str(project_dir.resolve()), body
    assert body["resume_strategy"] == "session_resume", body
    assert {
        entry["task_id"]: entry["strategy"] for entry in body["resume_plan"]
    } == {task_id: "session_resume" for task_id in INTERRUPTED_TASKS}, body

    # The executor continues exactly the sessions the interruption left
    # usable. A finished task has nothing to continue, and handing it a
    # session id would re-open a conversation whose work is committed.
    assert len(spawn.calls) == 1, spawn.calls
    assert spawn.sessions() == TASK_SESSIONS, spawn.calls

    # The resume point is single-shot: retired once the executor is up,
    # so a restart loop cannot re-consume the same interruption.
    assert pause_state.read_pause_checkpoint(plan_dir) is None
    assert not (plan_dir / "pause_state.json").exists()

    resumed_status = client.get(f"/api/execution/{PLAN_ID}/status").json()
    assert resumed_status["status"] == "running", resumed_status
    assert resumed_status["paused"] is False, resumed_status
    assert resumed_status["interrupted_task_count"] == 0

    # And still: the finished tasks are untouched by the resume too.
    assert {
        task_id: _task_updated_at(db_path, PLAN_ID, task_id)
        for task_id in COMPLETED_TASKS
    } == finished_before

    print(json.dumps({
        "paused": True,
        "resumed": True,
        "completed_tasks_preserved": True,
    }))


# ---------------------------------------------------------------------------
# 2) the 2026-07-16 guard still refuses a start with nothing to go on
# ---------------------------------------------------------------------------


def test_fresh_plan_without_project_dir_rejected(env, client, spawn):
    """No resume point → the operator still has to say where to work.

    A resume point is the one case where ``project_dir`` may be left
    out, because the checkpoint carries it. Without one there is
    nothing to carry it, and the default this guard replaced produced
    eleven empty commits in the wrong git repository (audit
    2026-07-16). Emptying the body here must therefore still fail —
    and must fail *before* anything is spawned.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path,
        task_statuses={"1": "completed", "2": "completed"},
    )

    refused = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert refused.status_code == 400, refused.text
    assert "project_dir" in refused.json()["detail"], refused.text
    assert spawn.calls == [], "the executor was spawned with no directory"
    assert pause_state.read_pause_checkpoint(plans_dir / PLAN_ID) is None
