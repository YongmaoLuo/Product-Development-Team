"""The execution stop enters the plan-level ``user_stopping`` phase.

2026-11-20 — the execution stop did its killing on the request thread
and then answered ``{"status": "stopped"}``: the plan was parked in the
``stopped`` phase by the very request that asked for the stop, and the
executor it had just signalled was still running.  The answer and the
plan's truth were two different things — the same disease the
verification stop had (task 9), and the same fix applies: **one
plan-level stopping phase, shared by execution and verification**.

A stop is a *process*, not an instant, and the process has an order:

1. **close the gate** — the routing row is CASed to the plan-level
   ``user_stopping`` phase and the stop record is written.  From here
   the plan says "a stop is in progress";
2. **shut the work down** — off the request thread.  The kill takes
   seconds (a SIGTERM grace, then an escalation), and the answer has to
   come back in milliseconds;
3. **answer ``stopping``** — with the scope, the phase the stop came
   from, and the work that was still running;
4. **flip the phase** — only once the plan has really fallen silent.
   That decision belongs to the background layer, not to the request:
   a phase that claims the work stopped while it is still running is
   the bug this endpoint used to have.

What is pinned here
-------------------
1. the answer says ``stopping`` — and says which scope and which phase
   the stop came from;
2. the plan is parked at ``user_stopping``, not at ``stopped``;
3. the phase reaches ``stopped`` once the plan is quiet;
4. the finalize waits for the executor to actually exit;
5. a second stop is the same stop: idempotent, one record;
6. a plan that is not in an execution phase keeps the old behaviour.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

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

PLAN_ID = "20261120-exec-stop-user-stopping"

#: The quiet window the background layer waits for before it may park
#: the plan.  Short here on purpose: the window is a policy of the
#: deployment, not a contract of this endpoint, and a test that waits
#: five seconds for a phase flip is a test nobody runs.
QUIET_SECONDS = "0.2"


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
    # The quiet window is the deployment's to choose; the tests choose a
    # short one so they do not have to wait for it.
    monkeypatch.setenv("PDT_STOP_QUIET_SECONDS", QUIET_SECONDS)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "executing",
    live_executor: bool = False,
):
    """A plan in the middle of its execution.

    ``live_executor`` decides whether the plan's executor is still
    running: with it, the executor is a real process that the stop has
    to shut down; without it, the executor is a handle whose process is
    already gone (the state still says "running" — the watchdog's
    business, not the stop's).

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
    else:
        # A handle whose process is gone: the plan still believes it is
        # running, and the stop still has to answer for it.
        exec_pid = None

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


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_execution_stop_returns_stopping(env, client):
    """The answer says ``stopping`` — and it says what it is stopping.

    ``stop_scope`` says *which* work is shutting down and
    ``previous_phase`` says *where* the plan was when the operator
    pressed stop.  The answer must not claim a terminal state the plan
    has not reached: the executor is still running, so "stopped" would
    be a lie the operator would act on.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    started = time.monotonic()
    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    elapsed = time.monotonic() - started

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stopping", body
    assert body["plan_id"] == PLAN_ID, body
    assert body["stop_scope"] == "execution", body
    assert body["previous_phase"] == "executing", body
    assert body["survivors"] == [], body
    assert elapsed < 2.0, (
        f"the stop took {elapsed:.3f}s — the wait for silence is the "
        f"background layer's job, not the request's"
    )


def test_phase_user_stopping_then_stopped(env, client):
    """The plan parks at ``user_stopping`` — and only later at ``stopped``.

    ``user_stopping`` says "a stop is in progress": the plan is not
    running its execution any more, and it is not parked yet either.
    Both of those are true at the same time now, and the phase is where
    the truth lives.  The flip to ``stopped`` is the background layer's
    verdict, and it happens once the plan is quiet.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    assert _read_phase(db_path) == "user_stopping", (
        "the stop did not park the plan at the plan-level stopping phase"
    )
    assert _wait_for_phase(db_path, "stopped"), (
        f"the plan never reached the stopped phase — it is "
        f"{_read_phase(db_path)!r}"
    )


def test_finalize_waits_for_executor_exit(env, client, monkeypatch):
    """The phase is not flipped while the executor is still running.

    This is the whole point of the change: the old endpoint parked the
    plan at ``stopped`` while the executor it had signalled was still
    alive.  Here the kill is a no-op — the executor outlives the stop —
    so the plan must stay at ``user_stopping`` for as long as the
    executor lives, and only become ``stopped`` after it is gone.
    """
    plans_dir, db_path = env
    _plan_dir, _project_dir, proc = _seed_plan(
        plans_dir, db_path, live_executor=True,
    )
    assert proc is not None
    try:
        # The stop asks; the executor does not go.
        monkeypatch.setattr(
            server, "_kill_process_tree",
            lambda proc, grace_seconds=None: False,
        )

        resp = client.post(f"/api/execution/{PLAN_ID}/stop")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "stopping", resp.json()

        # The executor is alive → the plan is not a stopped plan.
        time.sleep(2.0)
        assert _read_phase(db_path) == "user_stopping", (
            f"the phase was flipped while the executor was still running: "
            f"{_read_phase(db_path)!r}"
        )

        # The executor exits → the background layer may park the plan.
        proc.kill()
        proc.wait(timeout=5)
        assert _wait_for_phase(db_path, "stopped"), (
            f"the plan never reached the stopped phase after the executor "
            f"exited — it is {_read_phase(db_path)!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


def test_second_stop_while_stopping_is_the_same_stop(env, client):
    """A stop that is asked for twice is one stop.

    The second request must not start a second shutdown and must not
    restart the stop record: the record is the evidence the stop is
    judged by, and a baseline re-taken later would erase it.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    first = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "stopping", first.json()
    record = stop_state.get(PLAN_ID)
    assert record is not None, "the stop left no record behind"
    assert record.scope == stop_state.SCOPE_EXECUTION, record
    assert record.previous_phase == "executing", record

    second = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "stopping", second.json()

    assert stop_state.get(PLAN_ID) == record, (
        "the second stop replaced the stop record — the baseline of the "
        "first stop is the evidence the quiet window is judged by"
    )


def test_a_phase_that_is_not_an_execution_phase_keeps_the_old_behaviour(
    env, client,
):
    """A stop outside the execution phase is not this stop's business.

    The shared stopping phase belongs to the execution chain; a plan
    that is somewhere else keeps the behaviour it always had, so the
    change does not reach sideways into another kind of work.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_repairing")

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stopped", body
    assert _read_phase(db_path) == "stopped", (
        f"the phase is {_read_phase(db_path)!r}"
    )
