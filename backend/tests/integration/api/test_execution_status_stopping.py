"""A plan that is stopping cannot be started — and it says so when asked.

2026-11-20 — the execution stop became a *process* (task 11): the stop
closes the gate, shuts the work down off the request thread, and the
background layer flips the phase once the plan has really fallen
silent.  Two things follow from that, and neither of them was true
before:

1. **A start during a stop is a race.**  The stop's own worker is
   killing the plan's tree right now, and a start would put a second
   executor on a plan that is still being torn down — two runs writing
   one plan's task rows, and a resume point written from under the
   running one.  So the start has to refuse, and the refusal has to
   say *why*: "a stop is in progress" is a different thing to act on
   than "the phase conflicts".
2. **The status has to show the stop.**  A plan that is stopping is
   neither "running" nor "stopped": the operator polling the card
   needs to see that a stop is in progress, which scope is shutting
   down and what was still running.

What is pinned here
-------------------
1. a start during a stop is refused with HTTP 409 and
   ``{"detail": {"error": "plan_user_stopping"}}``;
2. the status of a stopping plan says ``stopping`` and names the
   scope and the survivors;
3. a plan that is not stopping keeps the answer it always published —
   the stopping fields are **absent**, not null;
4. once the stop is over the start works again — the refusal is the
   stop's, not the start's.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

import server
import stop_state
import stop_supervisor
import verification_cancel
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261120-exec-status-stopping"

#: The quiet window the background layer waits for before it may park
#: the plan.  Short here on purpose: the window is a policy of the
#: deployment, not a contract of this endpoint.
QUIET_SECONDS = "0.2"

#: The published shape of the status answer.  The stopping half of the
#: answer is added at runtime and only while a stop is in progress, so
#: an idle plan's answer is exactly these keys — nothing more.
_IDLE_STATUS_KEYS = {
    "plan_id",
    "status",
    "logs",
    "project_dir",
    "pid",
    "started_at",
    "ended_at",
    "stop_reason",
    "sync_targets",
    "data_source",
    "data_source_detail",
    "paused",
    "paused_at",
    "interrupted_task_count",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_process_state():
    """The stop's process-local state is global — do not leak it.

    ``stop_state`` (the stop record), ``stop_supervisor`` (the
    supervisor threads) and ``verification_cancel`` (the cancel flag)
    are process-wide, so a test that leaves one behind decides the next
    test's outcome.
    """
    stop_state._reset_for_tests()
    stop_supervisor._reset_for_tests()
    verification_cancel._reset_for_tests()
    yield
    stop_supervisor._reset_for_tests()
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test plans directory + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    monkeypatch.setenv("PDT_STOP_QUIET_SECONDS", QUIET_SECONDS)
    # The heartbeat watchdog and the auto-verification loop are not
    # what this file is about; they would only add threads that outlive
    # the assertion.
    monkeypatch.setattr(server, "_lazy_check_execution", lambda plan_id: None)
    monkeypatch.setattr(server, "_run_auto_verification_loop", lambda *a, **k: None)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


class _FakeProcess:
    """A stand-in for the executor subprocess.

    The start is not what this file is about; the executor is only
    spawned so the start can finish the way it finishes in production.
    The fake reports "already exited" — what the executor does after
    the start is not this file's business.
    """

    pid = 424242

    def __init__(self):
        self.returncode = 0

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return 0

    def kill(self):
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "user_stopping",
    live_executor: bool = False,
) -> Tuple[Path, Path, Optional[subprocess.Popen]]:
    """A plan whose execution is on its way down.

    ``phase`` is the routing row's phase — ``user_stopping`` is where a
    stop parks a plan while the stop is in progress.  ``live_executor``
    decides whether the plan's executor is still running: with it, the
    executor is a real process the stop still has to shut down; without
    it, the state says "running" while the process is already gone.

    Returns ``(plan_dir, project_dir, proc)`` — ``proc`` is ``None``
    when no real process was started.
    """
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        '{"tasks": [{"id": "1", "title": "first task"}]}', encoding="utf-8",
    )

    proc = None
    exec_pid = None
    if live_executor:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        exec_pid = proc.pid

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, phase)
        ExecutionRepository(conn).insert(
            plan_id=PLAN_ID,
            project_dir=str(project_dir),
            exec_status="running",
            exec_pid=exec_pid,
        )
    finally:
        conn.close()

    server._execution_state[PLAN_ID] = {
        "status": "running",
        "logs": [],
        "project_dir": str(project_dir),
        "process": proc,
        "started_at": "2026-11-20T12:00:00",
        "ended_at": None,
        "pid": exec_pid,
        "stop_reason": None,
    }
    return plan_dir, project_dir, proc


def _begin_stop(previous_phase: str = "executing") -> None:
    """Write the stop record the way the stop route writes it."""
    stop_state.begin(
        PLAN_ID,
        stop_state.SCOPE_EXECUTION,
        previous_phase=previous_phase,
        dispatch_baseline=0,
    )


def _read_phase(db_path: Path, plan_id: str = PLAN_ID) -> str:
    """The routing row's phase, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _wait_for_phase(
    db_path: Path, phase: str, *, plan_id: str = PLAN_ID, timeout: float = 30.0,
) -> bool:
    """Wait for the plan's phase to reach ``phase``; True when it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_phase(db_path, plan_id) == phase:
            return True
        time.sleep(0.05)
    return _read_phase(db_path, plan_id) == phase


def _survivor_pids(survivors: List[Dict[str, Any]]) -> List[int]:
    return [int(s.get("pid") or 0) for s in survivors]


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_execution_start_rejected_while_stopping(env, client):
    """A plan that is stopping is not startable — and the answer says so.

    The refusal has to name the reason.  "conflict" tells the operator
    to look at the phase; ``plan_user_stopping`` tells them a stop is
    in progress and that the start will work once the stop is over.
    The gate is also the FIRST thing the endpoint does: a start that
    swept the plan's tree on its way to the refusal would race the
    stop's own shutdown.
    """
    plans_dir, db_path = env
    _plan_dir, project_dir, _proc = _seed_plan(plans_dir, db_path)
    _begin_stop(previous_phase="executing")

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": {"error": "plan_user_stopping"}}, resp.json()

    # The refusal changed nothing: the plan is still the plan the stop
    # is shutting down, and no executor was spawned.
    assert _read_phase(db_path) == "user_stopping", (
        f"the refused start moved the plan's phase to {_read_phase(db_path)!r}"
    )


def test_execution_start_rejected_by_the_stop_record_alone(env, client):
    """The stop record alone refuses the start.

    The record is written when the stop closes its gate and cleared
    when the stop's finalize parks the plan — it is the plan's own
    statement that a stop is in progress, and it is what says *which*
    work is stopping.  This case pins that the record is enough on its
    own: the plan's phase is still the phase it had when the operator
    pressed stop, and the start is still refused.
    """
    plans_dir, db_path = env
    _plan_dir, project_dir, _proc = _seed_plan(
        plans_dir, db_path, phase="executing",
    )
    _begin_stop(previous_phase="executing")

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": {"error": "plan_user_stopping"}}, resp.json()


def test_execution_status_reports_stopping(env, client):
    """The status of a stopping plan says ``stopping``.

    Not ``running`` — the plan's work is being taken down — and not
    ``stopped`` — the stop has not finished.  The scope says which work
    is shutting down and the survivors say what was still running when
    the reader asked.
    """
    plans_dir, db_path = env
    _plan_dir, _project_dir, proc = _seed_plan(
        plans_dir, db_path, live_executor=True,
    )
    assert proc is not None
    try:
        _begin_stop(previous_phase="executing")

        resp = client.get(f"/api/execution/{PLAN_ID}/status")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "stopping", body
        assert body["stop_scope"] == "execution", body
        survivors = body["survivors"]
        assert isinstance(survivors, list), body
        assert _survivor_pids(survivors) == [proc.pid], body
        assert survivors[0]["kind"] == "execution_executor", body
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


def test_execution_status_shape_unchanged_when_idle(env, client):
    """A plan that is not stopping keeps the answer it always published.

    The stopping half of the answer is added *while a stop is in
    progress*.  An idle plan must not grow the fields: a reader that
    sees ``survivors: null`` on every plan learns to ignore them, and a
    reader that sees ``stop_scope: null`` cannot tell "this build does
    not report it" from "there is no stop".
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="stopped")

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _IDLE_STATUS_KEYS, (
        f"the idle answer changed shape: {sorted(body)!r}"
    )
    assert body["status"] == "stopped", body


def test_execution_start_works_after_the_stop_is_finalized(env, client, monkeypatch):
    """Once the stop is over, the start works again.

    The refusal is the *stop's*, not the start's: the stop's finalize
    clears the record when the plan has fallen silent, and the start
    that comes after it is an ordinary start — the plan resumes from
    the pause checkpoint the stop left behind.
    """
    plans_dir, db_path = env
    _plan_dir, project_dir, _proc = _seed_plan(
        plans_dir, db_path, phase="executing",
    )

    # A real stop: the gate closes, the plan is parked at the stopping
    # phase, and the background layer parks it at "stopped" once it is
    # quiet.
    stopped = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopping", stopped.json()
    assert _wait_for_phase(db_path, "stopped"), (
        f"the plan never reached the stopped phase — it is "
        f"{_read_phase(db_path)!r}"
    )
    assert stop_state.get(PLAN_ID) is None, (
        "the finalize did not clear the stop record — the start would "
        "stay refused forever"
    )

    log_path = plans_dir / PLAN_ID / "logs" / "executor.log"
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (_FakeProcess(), log_path),
    )

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "started", body
