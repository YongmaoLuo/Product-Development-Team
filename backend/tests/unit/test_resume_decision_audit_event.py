"""The resume decision is recorded in the plan's own execution log.

What this pins
--------------
``start`` used to publish its resume decision in exactly one place: the
HTTP response. The response is gone when the request ends, and the
checkpoint it was made from is *consumed* on the next resume — so after a
resume there was nothing left anywhere that said "this plan was
interrupted here, and this is what was in flight when it was". The
stop side already had its answer (see
``test_stop_checkpoint_audit_event.py``); this file pins the other half.

``backend/resume_audit_log.py`` writes one ``execution_resume_decision``
event per resumed start. This file pins the *start path* that produces
it, not the writer itself (the writer has its own tests in
``test_resume_audit_log_resume_event.py``).

Three properties, one per boundary:

* **a resumed start records the decision it actually published** --
  exactly one event, and the strategy / plan / sessions in it are the
  ones the response carries, verbatim;
* **a fresh start records nothing** -- there was no resume decision to
  make, so an event would be a record of something that did not happen;
* **a failing audit writer does not fail the start** -- the log is
  evidence, not control flow: the start still answers 200 and the
  executor is still spawned.

The event is written *before* the executor is spawned (architecture
decision point 3), so a spawn that fails still leaves the decision on
record, and every failure around it is isolated (architecture decision
point 4).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import pause_state
import resume_audit_log
import server
from resume_audit_log import RESUME_EVENT
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261023-resume-decision-audit-event"

#: The task the stop caught mid-flight. Its id is zero-padded because
#: that is how the executor names tasks, and the checkpoint echoes it
#: back verbatim.
TASK_ID = "08"

#: The session the interrupted task was running in.
SESSION_ID = "2db84cba"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProc:
    """An executor process handle that parks the watcher thread.

    ``wait()`` blocks until the process is released, so the plan stays
    ``running`` for the duration of the test. ``release()`` is what
    ``tests/conftest.py::clean_execution_state`` calls at teardown -- a
    worker that outlived its test would resolve the runtime paths
    against a later test and write there.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = 0
        self._block = threading.Event()

    def poll(self) -> Optional[int]:
        return 0

    def wait(self, timeout: Optional[int] = None) -> int:
        self._block.wait()
        return 0

    def terminate(self) -> None:
        self._block.set()

    def release(self) -> None:
        self._block.set()


class SpawnRecorder:
    """Stands in for ``server._spawn_executor_subprocess``.

    Records the keyword arguments the route passed, so a test can see
    that the executor really was spawned (or not).
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any):
        self.calls.append(kwargs)
        log_path = Path(kwargs["plan_id"]) / "logs" / "executor.log"
        return FakeProc(), log_path


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    server._execution_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def spawn(monkeypatch):
    """Replace the executor spawn with a recorder."""
    recorder = SpawnRecorder()
    monkeypatch.setattr(server, "_spawn_executor_subprocess", recorder)
    # The watcher thread that waits on the (never-ending) fake process
    # must not reach the verification hand-off.
    monkeypatch.setattr(
        server, "_run_auto_verification_loop", lambda *a, **k: None,
    )
    monkeypatch.setattr(server, "_lazy_check_execution", lambda *a, **k: None)
    return recorder


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    phase: str = "stopped",
    paused: bool = True,
):
    """A plan with tasks, a routing row, and (optionally) a resume point.

    Returns ``(plan_dir, project_dir)``.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    tasks = [
        {"id": TASK_ID, "title": "eighth task"},
        {"id": "09", "title": "ninth task"},
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=str(project_dir),
            exec_status="stopped",
            exec_pid=None,
        )
    finally:
        conn.close()

    if paused:
        pause_state.write_pause_checkpoint(
            plan_dir,
            kind="execution",
            project_dir=str(project_dir),
            reason="user_requested",
            interrupted_tasks=[{
                "task_id": TASK_ID,
                "status": "in_progress",
                "session_id": SESSION_ID,
                "strategy": "session_resume",
                "reason": "session_id_available",
            }],
            unfinished_tasks=[{"task_id": "09", "status": "pending"}],
        )
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


def _resume_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """The resume-decision events in the plan's execution log."""
    return [
        entry for entry in _log_entries(plan_dir)
        if entry.get("event") == RESUME_EVENT
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_start_records_resume_decision_once(env, client, spawn):
    """A resumed start writes one event, carrying the decision it published.

    The event is a *copy* of the decision, not a second derivation of it:
    the strategy and the per-task plan in the event are the ones the
    response carries, verbatim. Two answers that disagree make the audit
    record worse than none.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "started", body
    assert body["resumed"] is True, body
    assert body["resume_strategy"] == "session_resume", body

    events = _resume_events(plan_dir)
    assert len(events) == 1, (
        "a resumed start must leave exactly one audit event, so a reader "
        f"can count resumes without de-duplicating; got {len(events)}"
    )

    data = events[0]["data"]
    assert data["resume_strategy"] == body["resume_strategy"], (
        "the event must carry the decision the response published, "
        "verbatim -- not a re-derivation of it"
    )
    assert data["resume_plan"] == body["resume_plan"], (
        "the per-task plan in the event must be the one the response "
        "published, verbatim"
    )
    assert data["sessions"] == {TASK_ID: SESSION_ID}, data
    assert data["resume_plan"] == [{
        "task_id": TASK_ID,
        "strategy": "session_resume",
        "reason": "session_id_available",
    }], data


def test_fresh_start_records_no_resume_decision(env, client, spawn):
    """A start with no resume point records nothing.

    There was no resume decision to make -- no checkpoint, no sessions,
    nothing to audit. An event here would be a record of something that
    did not happen.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, phase="ready",
                                       paused=False)

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "started", body
    assert "resumed" not in body, body

    assert _resume_events(plan_dir) == [], (
        "a fresh start has no resume decision, so it must not record one"
    )


def test_start_survives_audit_log_failure(env, client, spawn, monkeypatch):
    """The audit record is evidence, not control flow.

    The writer is allowed to fail -- a full disk, a rotated log, a
    missing plan directory. None of that may turn a start that worked
    into a start that failed: the executor is still spawned and the
    operator still gets a 200.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("the audit writer is having a bad day")

    monkeypatch.setattr(resume_audit_log, "log_resume_decision", _boom)

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "started", body
    assert spawn.calls, "the executor was not spawned"
    assert _resume_events(plan_dir) == []
