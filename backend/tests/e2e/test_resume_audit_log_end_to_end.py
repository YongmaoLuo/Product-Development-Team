"""End-to-end: the stop's resume point and the resume's decision agree.

What this pins
--------------
The two halves of the resume audit trail are written by two different
endpoints, and each half has its own test elsewhere:

* ``tests/unit/test_stop_checkpoint_audit_event.py`` — a stop writes one
  ``execution_stop_checkpoint`` event;
* ``tests/unit/test_resume_decision_audit_event.py`` — a resumed start
  writes one ``execution_resume_decision`` event.

Neither of them can catch the defect this file exists for. Each one
looks at a single event in isolation, so both pass when the two events
describe *different plans* — a stop that recorded one task and a resume
that continued another. The trail only means something if the two ends
of the pause agree, and that is a property of the pair, not of either
half.

So this file drives the whole chain in one go:

1. a plan mid-execution — one task caught in flight, one still pending;
2. ``POST /stop`` → the resume point is written and recorded;
3. ``POST /start`` with an **empty body** → the resume consumes that
   point and records the decision it made about it;
4. the two events are read back out of the plan's own execution log.

What is pinned here, and why each one would be a real defect:

* **One event per side.** A reader counts stops and resumes by counting
  events. A second event for the same stop would make the count a lie.
* **The two sides name the same tasks.** The stop says what was in
  flight; the resume says what it continued. If the sets differ, the
  audit record tells two different stories about the same interruption —
  and the resume's version is the one that decided what actually ran.
* **The strategy in the log is the strategy in the response.** The
  event is a copy of the decision the response published, not a second
  derivation of it. Two answers that disagree make the record worse
  than no record at all.
* **The stop is written before the resume.** The log is append-only,
  so the line order *is* the timeline. A resume recorded before the
  stop that caused it would put the pause out of order in the one
  place that is supposed to keep the order.

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

import server
from execution_logger import ExecutionLogger
from resume_audit_log import RESUME_EVENT, STOP_EVENT
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository

pytestmark = [pytest.mark.e2e]

PLAN_ID = "20261023-resume-audit-e2e"

#: The task the stop catches mid-flight. Its id is zero-padded because
#: that is how the executor names tasks.
INTERRUPTED_TASK = "08"

#: The task that was never reached — it was still pending when the stop
#: landed, so it is *owed* work, not interrupted work.
PENDING_TASK = "09"

#: ``task_id`` → the session that task was bound to when it was cut
#: short. This is what makes the interruption resumable rather than a
#: re-run, and it is the highest-value thing on disk.
TASK_SESSIONS = {INTERRUPTED_TASK: "sess-08"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProc:
    """A process handle that never ends on its own.

    ``wait()`` parks until the process is released, so the plan stays
    ``running`` for the length of the test instead of racing to
    ``completed`` before a single assertion runs. ``terminate`` /
    ``kill`` model what a signalled process does — it goes away, and
    ``poll()`` stops answering ``None``.

    ``release()`` is what ``tests/conftest.py::clean_execution_state``
    calls at teardown: a worker that outlived its own test would
    resolve the runtime paths against a later test and write there.

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
    ever called.
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

    def sessions(self) -> Dict[str, str]:
        """The ``task_id`` → ``session_id`` mapping handed to the executor."""
        extra_env = (self.calls[-1].get("extra_env") or {}) if self.calls else {}
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
    escalation", which is what keeps the interrupted task resumable in
    its own session instead of being invalidated into a full re-run.
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
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()


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
    monkeypatch.setattr(server, "_kill_process_tree", CooperativeKill())
    monkeypatch.setattr(
        server, "_run_auto_verification_loop", lambda *a, **k: None,
    )
    monkeypatch.setattr(server, "_lazy_check_execution", lambda *a, **k: None)
    yield recorder
    recorder.release_all()


def _seed_plan(plans_dir: Path, db_path: Path):
    """A plan mid-execution: one task in flight, one still pending.

    Returns ``(plan_dir, project_dir)``. The in-memory execution state
    is installed as well, so the plan looks mid-execution to the stop
    endpoint.
    """
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    tasks = [
        {"id": INTERRUPTED_TASK, "title": "eighth task"},
        {"id": PENDING_TASK, "title": "ninth task"},
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "executing")
        ExecutionRepository(conn).insert(
            plan_id=PLAN_ID,
            project_dir=str(project_dir),
            exec_status="running",
            exec_pid=FakeProc().pid,
        )
        repo = PlanTaskRepository(conn)
        for task in tasks:
            repo.add_task(PLAN_ID, dict(task))
        repo.update_task(PLAN_ID, INTERRUPTED_TASK, {"status": "in_progress"})
        repo.update_task(PLAN_ID, PENDING_TASK, {"status": "pending"})
    finally:
        conn.close()

    # The execution log — the executor's own record of which session
    # each task ran in, and the only durable copy of the binding. It is
    # also the file the audit events are appended to, so the binding
    # lines are the oldest lines in the file by a wide margin.
    with open(plan_dir / "execution.log", "w", encoding="utf-8") as handle:
        for task_id, session_id in TASK_SESSIONS.items():
            handle.write(
                json.dumps(
                    {
                        "ts": "2026-10-23T04:20:00",
                        "level": "INFO",
                        "event": "task_session_bound",
                        "message": f"Task [{task_id}] ran in session {session_id}",
                        "task_id": task_id,
                        "data": {"session_id": session_id},
                    },
                    ensure_ascii=False,
                ) + "\n"
            )

    proc = FakeProc()
    server._execution_state[PLAN_ID] = {
        "status": "running",
        "logs": deque(maxlen=2000),
        "project_dir": str(project_dir),
        "process": proc,
        "started_at": "2026-10-23T04:20:00",
        "ended_at": None,
        "pid": proc.pid,
        "stop_reason": None,
    }
    return plan_dir, project_dir


def _log_entries(plan_dir: Path) -> List[Dict[str, Any]]:
    """Every JSON line of the plan's execution log, in write order."""
    log_path = plan_dir / "execution.log"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


def _event_positions(plan_dir: Path, event: str) -> List[int]:
    """The line numbers (0-based) the event appears at, in write order."""
    return [
        index
        for index, entry in enumerate(_log_entries(plan_dir))
        if entry.get("event") == event
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stop_then_start_records_matching_audit_trail(env, client, spawn):
    """Stop → start leaves one stop event and one resume event, and they
    describe the same interruption.

    The two ends of the pause have to agree: the stop says what was in
    flight, the resume says what it continued. A reader who reads both
    out of the log has to find one story, not two.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(plans_dir, db_path)

    # --- stop: the resume point is left behind, and recorded -----------
    stopped = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["paused"] is True, stopped.text

    stop_events = ExecutionLogger.read_logs(
        PLAN_ID, plans_dir=plans_dir, event=STOP_EVENT,
    )
    assert len(stop_events) == 1, (
        "a stop must leave exactly one audit event, so a reader can count "
        f"stops without de-duplicating; got {len(stop_events)}"
    )

    # --- start: the resume point is honoured, and recorded --------------
    # The body is deliberately empty: the checkpoint already knows where
    # the work was happening.
    started = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["resumed"] is True, body

    resume_events = ExecutionLogger.read_logs(
        PLAN_ID, plans_dir=plans_dir, event=RESUME_EVENT,
    )
    assert len(resume_events) == 1, (
        "a resumed start must leave exactly one audit event, so a reader "
        f"can count resumes without de-duplicating; got {len(resume_events)}"
    )

    # The two sides name the same tasks. This is the property neither
    # side's own test can see: each of them looks at one event, and both
    # pass when the two events describe different interruptions.
    interrupted = {
        entry["task_id"] for entry in stop_events[0]["data"]["interrupted"]
    }
    continued = {
        entry["task_id"] for entry in resume_events[0]["data"]["resume_plan"]
    }
    assert interrupted == {INTERRUPTED_TASK}, interrupted
    assert continued == interrupted, (
        "the stop and the resume must describe the same interruption: the "
        f"stop said {sorted(interrupted)}, the resume continued "
        f"{sorted(continued)}"
    )

    # The event is a copy of the decision the response published — not a
    # second derivation of it.
    assert resume_events[0]["data"]["resume_strategy"] == body["resume_strategy"], (
        "the event must carry the decision the response published, "
        "verbatim -- not a re-derivation of it"
    )
    assert body["resume_strategy"] == "session_resume", body
    assert resume_events[0]["data"]["sessions"] == TASK_SESSIONS, (
        "the interrupted task continued in the session the stop left "
        "usable, and the event must say so"
    )

    # The stop's own view of the interruption is the checkpoint's view:
    # one task was caught in flight, and the task that was never reached
    # is still owed work.
    assert stop_events[0]["data"]["task_count"] == 1, stop_events[0]["data"]
    assert {
        entry["task_id"] for entry in stop_events[0]["data"]["unfinished"]
    } == {INTERRUPTED_TASK, PENDING_TASK}, stop_events[0]["data"]

    # And the executor really was handed that session to continue in.
    assert spawn.sessions() == TASK_SESSIONS, spawn.calls


def test_audit_events_are_ordered_stop_before_resume(env, client, spawn):
    """The stop is written before the resume, in the log's own line order.

    The execution log is append-only, so the order of the lines *is*
    the order of the events. A resume recorded before the stop that
    caused it would put the pause out of order in the one place that
    is supposed to keep the order — and a reader reconstructing the
    story from the log would read the answer before the question.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(plans_dir, db_path)

    stopped = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert stopped.status_code == 200, stopped.text

    started = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert started.status_code == 200, started.text

    stop_positions = _event_positions(plan_dir, STOP_EVENT)
    resume_positions = _event_positions(plan_dir, RESUME_EVENT)
    assert len(stop_positions) == 1, stop_positions
    assert len(resume_positions) == 1, resume_positions
    assert stop_positions[0] < resume_positions[0], (
        "the stop must be written before the resume it caused: stop at "
        f"line {stop_positions[0]}, resume at line {resume_positions[0]}"
    )