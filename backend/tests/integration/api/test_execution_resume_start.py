"""``POST /api/execution/{plan_id}/start`` continues a paused plan.

A stop is a pause (see ``test_execution_stop_is_pause.py``): it leaves a
resume point on disk. This file pins the other half — the start that
*uses* that resume point.

What is pinned here:

1. **A paused plan does not have to repeat its project directory.**
   The checkpoint carries the directory the work was happening in, so
   the operator can continue a plan with an empty body. The checkpoint
   also *wins* over the request body: a resume that ran somewhere else
   would continue a task in a directory it was never written for.
2. **A plan with no resume point still has to name its directory.**
   The 2026-07-16 audit guard (eleven empty commits replayed onto the
   wrong git repo) is untouched by the resume path.
3. **Only the tasks that can continue in their session do.** A task the
   interruption invalidated (``full_rerun``) is not handed to the
   executor as a session to re-open — it runs again from the beginning.
4. **The start says what it did.** ``resumed`` / ``resume_strategy`` /
   ``resume_plan`` tell the operator which tasks continued where, and
   why.
5. **The resume point is single-shot, but only once the work started.**
   A successful start retires the checkpoint; a start that failed to
   spawn leaves it alone, because nothing was continued.
"""

from __future__ import annotations

import json
import threading
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
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261020-exec-resume-start"


class FakeProc:
    """An executor process handle that parks the watcher thread.

    ``wait()`` blocks until the process is released, so the plan stays
    ``running`` for the duration of the test. ``release`` is what
    ``tests/conftest.py::clean_execution_state`` calls at teardown —
    a worker that outlived its test would resolve the runtime paths
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

    Records the keyword arguments the route passed — that is where the
    resume point shows up: the project directory the work continues in,
    and the ``PDT_RESUME_SESSIONS`` mapping the executor reads.
    """

    def __init__(self, error: Optional[OSError] = None) -> None:
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        log_path = Path(kwargs["plan_id"]) / "logs" / "executor.log"
        return FakeProc(), log_path

    @property
    def last(self) -> Dict[str, Any]:
        assert self.calls, "the executor was never spawned"
        return self.calls[-1]

    def sessions(self) -> Dict[str, str]:
        """The task id → session id mapping handed to the executor."""
        extra_env = self.last.get("extra_env") or {}
        raw = extra_env.get("PDT_RESUME_SESSIONS")
        if not raw:
            return {}
        return json.loads(raw)


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


@pytest.fixture
def failing_spawn(monkeypatch):
    """An executor spawn that fails the way a missing binary does."""
    recorder = SpawnRecorder(error=OSError("cannot spawn executor"))
    monkeypatch.setattr(server, "_spawn_executor_subprocess", recorder)
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
    interrupted: Optional[List[Dict[str, Any]]] = None,
    unfinished: Optional[List[Dict[str, Any]]] = None,
    raw_checkpoint: Optional[Dict[str, Any]] = None,
):
    """A paused plan: tasks, a routing row, and a resume point.

    Returns ``(plan_dir, project_dir)``. ``raw_checkpoint`` is written
    verbatim — that is how the "old format" (a checkpoint without a
    ``strategy`` field) is expressed.
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
        RoutingRepository(conn).insert(plan_id, phase)
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=str(project_dir),
            exec_status="stopped",
            exec_pid=None,
        )
    finally:
        conn.close()

    if raw_checkpoint is not None:
        (plan_dir / "pause_state.json").write_text(
            json.dumps(raw_checkpoint, ensure_ascii=False), encoding="utf-8",
        )
    elif interrupted is not None or unfinished is not None:
        pause_state.write_pause_checkpoint(
            plan_dir,
            kind="execution",
            project_dir=str(project_dir),
            reason="user_requested",
            interrupted_tasks=interrupted or [],
            unfinished_tasks=unfinished or [],
        )
    return plan_dir, project_dir


# ---------------------------------------------------------------------------
# 1) a paused plan continues without repeating its project directory
# ---------------------------------------------------------------------------


def test_resume_without_project_dir_succeeds(env, client, spawn):
    """An empty body is enough when the plan carries a resume point.

    The checkpoint knows where the work was happening; the operator
    does not have to say it again — and the executor is told to
    continue there.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path,
        interrupted=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "session_resume", "reason": "session_id_available",
        }],
        unfinished=[{"task_id": "4", "status": "pending"}],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["status"] == "started", body
    assert body["resumed"] is True, body
    assert body["project_dir"] == str(project_dir.resolve()), body

    # The spawn was told where the work continues.
    assert len(spawn.calls) == 1, spawn.calls
    assert spawn.last["project_dir"] == project_dir.resolve()


def test_fresh_start_without_project_dir_still_400(env, client, spawn):
    """No resume point → the 2026-07-16 guard still refuses the start.

    The guard exists because a defaulted project directory produced
    empty commits in the wrong git repository. A resume point does not
    weaken it: without one, the operator still has to say where the
    work happens.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="ready")

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 400, resp.text
    assert "project_dir" in resp.json()["detail"]
    assert spawn.calls == [], "the executor was spawned without a directory"


def test_fresh_start_keeps_the_published_response_shape(env, client, spawn):
    """A start with no resume point answers exactly as it always did.

    The resume fields describe a resume; a fresh start has none, so
    the response keeps the four-key contract the endpoint publishes
    (see ``tests/api/test_start_endpoint.py::test_response_field_contract``).
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, phase="ready")

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert set(body.keys()) == {"plan_id", "status", "pid", "project_dir"}, body
    assert spawn.calls, "the executor was not spawned"


def test_resume_checkpoint_project_dir_wins(env, client, spawn):
    """The checkpoint's directory wins over the request body.

    A resume that ran somewhere else would continue a task in a
    directory it was never written for — the same class of mistake the
    2026-07-16 audit was about.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path,
        interrupted=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "session_resume", "reason": "session_id_available",
        }],
    )
    other_dir = plan_dir / "somewhere-else"
    other_dir.mkdir(exist_ok=True)

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(other_dir)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_dir"] == str(project_dir.resolve())
    assert spawn.last["project_dir"] == project_dir.resolve()


# ---------------------------------------------------------------------------
# 2) only the tasks that can continue in their session do
# ---------------------------------------------------------------------------


def test_resume_passes_session_env(env, client, spawn):
    """``PDT_RESUME_SESSIONS`` carries the session-bound tasks only."""
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path,
        interrupted=[
            {
                "task_id": "2", "status": "in_progress", "session_id": "s-2",
                "strategy": "session_resume", "reason": "session_id_available",
            },
            {
                "task_id": "3", "status": "in_progress", "session_id": None,
                "strategy": "full_rerun", "reason": "no_session_id",
            },
        ],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    assert spawn.sessions() == {"2": "s-2"}, spawn.calls


def test_resume_records_full_rerun(env, client, spawn):
    """A hard-killed task is re-run whole — and says so.

    The tree had to be SIGKILLed, so the conversation may have been cut
    mid-write: the task cannot be trusted to continue in that session.
    It is not handed to the executor as a session to re-open, and the
    reason the stop recorded is reported back.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path,
        interrupted=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "full_rerun", "reason": "sigkill_after_grace",
        }],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["resume_strategy"] == "full_rerun", body
    plan = {entry["task_id"]: entry for entry in body["resume_plan"]}
    assert plan["3"]["strategy"] == "full_rerun", body
    assert plan["3"]["reason"] == "sigkill_after_grace", body

    # The invalidated session is not handed to the executor.
    assert "3" not in spawn.sessions(), spawn.calls


def test_resume_old_checkpoint_without_strategy(env, client, spawn):
    """A checkpoint written before the strategy field still resumes.

    The old format has no strategy: a session id is the only evidence
    that there is something to re-open, and without one the task runs
    again from the beginning.
    """
    plans_dir, db_path = env
    project_dir = plans_dir / PLAN_ID / "project"
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        raw_checkpoint={
            "version": 1,
            "kind": "execution",
            "project_dir": str(project_dir),
            "paused_at": "2026-10-20T12:00:00+00:00",
            "reason": "user_requested",
            "interrupted_tasks": [
                {"task_id": "2", "status": "in_progress", "session_id": "s-2"},
                {"task_id": "3", "status": "in_progress"},
            ],
            "unfinished_tasks": [],
        },
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["resumed"] is True, body
    assert body["resume_strategy"] == "mixed", body
    plan = {entry["task_id"]: entry for entry in body["resume_plan"]}
    assert plan["2"]["strategy"] == "session_resume", body
    assert plan["3"]["strategy"] == "full_rerun", body
    assert spawn.sessions() == {"2": "s-2"}, spawn.calls


# ---------------------------------------------------------------------------
# 3) the resume point is single-shot — but only once the work started
# ---------------------------------------------------------------------------


def test_resume_still_refuses_the_backend_dir(env, client, spawn):
    """A resume point cannot smuggle the executor into ``backend/``.

    The guard exists because the executor's git operations would commit
    into the running service's own tree. A checkpoint that points there
    is refused exactly like a request body that does.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        interrupted=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "session_resume", "reason": "session_id_available",
        }],
    )
    pause_state.write_pause_checkpoint(
        plan_dir,
        kind="execution",
        project_dir=str(server._BACKEND_DIR),
        reason="user_requested",
        interrupted_tasks=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "session_resume", "reason": "session_id_available",
        }],
        unfinished_tasks=[],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 400, resp.text
    assert spawn.calls == [], "the executor was spawned inside backend/"


def test_resume_start_while_running_is_409_and_keeps_checkpoint(
    env, client, spawn
):
    """A start against a running plan is still refused — and the resume
    point survives the refusal.

    The CAS is the start-execution mutex: a second start on a plan that
    is already executing is a conflict, not a resume. Nothing was
    continued, so the interruption is still the truth — the rejected
    start must not retire it.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        phase="executing",
        interrupted=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "session_resume", "reason": "session_id_available",
        }],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 409, resp.text
    assert pause_state.read_pause_checkpoint(plan_dir) is not None
    assert spawn.calls == []


def test_resume_consumes_checkpoint(env, client, spawn):
    """A successful start retires the resume point.

    Without the delete, a restart loop would re-consume the same
    interruption on every attempt and the plan would never move past
    it.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        interrupted=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "session_resume", "reason": "session_id_available",
        }],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    assert pause_state.read_pause_checkpoint(plan_dir) is None
    assert not (plan_dir / "pause_state.json").exists()


def test_spawn_failure_keeps_checkpoint(env, client, failing_spawn):
    """A start that could not spawn leaves the resume point alone.

    Nothing was continued, so the interruption is still the truth about
    what was interrupted — throwing it away would cost the plan the
    only record of the session it could have continued in.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path,
        interrupted=[{
            "task_id": "3", "status": "in_progress", "session_id": "s-3",
            "strategy": "session_resume", "reason": "session_id_available",
        }],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed", resp.json()

    checkpoint = pause_state.read_pause_checkpoint(plan_dir)
    assert checkpoint is not None, "the resume point was consumed by a failed start"
    assert checkpoint["interrupted_tasks"][0]["task_id"] == "3"
