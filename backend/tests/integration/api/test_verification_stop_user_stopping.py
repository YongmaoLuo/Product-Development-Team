"""The verification stop enters the plan-level ``user_stopping`` phase.

2026-11-20 — the stop did its killing on the request thread and then
answered ``{"status": "stopped"}``: the routing row was CASed back to
``verification`` (or back to ``verification_running`` when a sub-agent
survived the kill) and the verification row was written ``loop_stopped``
— all while the round was still burning tokens.  The answer and the
plan's truth were two different things, and the answer was the one the
operator saw.

A stop is a *process*, not an instant.  The gate has to close first,
then the work has to be shut down layer by layer, and only a plan that
has really fallen silent may be called stopped.  So the endpoint closes
the gate, hands the shutdown to the background supervisor and answers
``stopping`` — fast.

What is pinned here
-------------------
1. the answer says ``stopping`` and arrives inside two seconds;
2. the routing row is parked at ``user_stopping``;
3. the stop record carries the scope and the phase the stop came from;
4. the repair executor gets a SIGTERM — and only a SIGTERM;
5. a second stop is the same stop: idempotent, one supervisor;
6. a phase that cannot be stopped still answers 409;
7. the phase is NOT flipped to a stopped phase before the answer comes
   back — the flip belongs to the background layer;
8. the answer reports the work that was still running;
9. the cooperative stop belongs to the supervisor's thread, not to the
   request thread.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
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
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20261120-stop-user-stopping"

VP_IDS = ["VP-001", "VP-002", "VP-003"]

ROUND_VERDICTS = [
    {
        "vp_id": "VP-001",
        "status": "PASSED",
        "worker_id": "executor",
        "seq": 1,
        "reasons": [],
        "evidence": {},
    },
]

#: A pid that names no running process — the repair executor of a plan
#: whose executor is already gone.  The signal is recorded, not
#: delivered: the test watches what the stop *sent*, not what it hit.
DEAD_EXEC_PID = 424242


@pytest.fixture(autouse=True)
def _clean_process_state():
    """The stop's process-local state is global — do not leak it.

    ``verification_cancel`` (the cancel flag), ``stop_state`` (the stop
    record) and ``stop_supervisor`` (the supervisor threads) are all
    process-wide, so a test that leaves one behind decides the next
    test's outcome.
    """
    verification_cancel._reset_for_tests()
    stop_state._reset_for_tests()
    stop_supervisor._reset_for_tests()
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
def live_sub_agent():
    """A registered sub-agent whose process is still running.

    The handle carries a live process and no ``scoped_tool`` — the
    production shape of a verification point that is still working.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = server.sub_agent_registry.register_sub_agent(
        PLAN_ID, "VP-003", 1, scoped_tool=None, pid=proc.pid,
    )
    try:
        yield handle
    finally:
        server.sub_agent_registry.unregister(PLAN_ID, handle)
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "verification_running",
    round_n: int = 2,
    exec_pid: int | None = None,
    verdicts=None,
    max_rounds: int = 3,
):
    """A plan mid-verification, with a round's worth of verdicts."""
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, phase)
        execution = ExecutionRepository(conn)
        execution.insert(plan_id=PLAN_ID, project_dir=str(project_dir))
        if exec_pid is not None:
            execution.update_fields(PLAN_ID, exec_pid=exec_pid)
        verif = VerificationRepository(conn)
        verif.insert(PLAN_ID, verification_status="running",
                     max_rounds=max_rounds)
        verif.init_round(PLAN_ID, round_n=round_n, max_rounds=max_rounds)
        for verdict in (verdicts if verdicts is not None else ROUND_VERDICTS):
            verif.append_verdict(PLAN_ID, dict(verdict))
    finally:
        conn.close()

    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "verification_points": [
                    {"id": vp_id, "title": f"检查 {vp_id}"} for vp_id in VP_IDS
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir, project_dir


def _read_phase(db_path: Path, plan_id: str) -> str:
    """The routing row's phase, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _read_verification_row(db_path: Path, plan_id: str) -> dict:
    """The verification row, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()
    return row or {}


def _supervisor_threads() -> list:
    """The supervisor threads that are alive right now."""
    return [
        t for t in threading.enumerate()
        if t.name.startswith("stop-supervisor:")
    ]


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_stop_returns_stopping_within_2s(env, client):
    """The answer says ``stopping`` — and it comes back fast.

    The wait for silence is measured in seconds and belongs to the
    background layer; the request has to come back in milliseconds.  An
    endpoint that blocks on the kill is an endpoint the operator cannot
    use while the thing they are trying to stop keeps running.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    started = time.monotonic()
    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    elapsed = time.monotonic() - started

    assert stop.status_code == 200, stop.text
    body = stop.json()
    assert body["status"] == "stopping", body
    assert body["plan_id"] == PLAN_ID, body
    assert elapsed < 2.0, (
        f"the stop took {elapsed:.3f}s — the wait for silence is the "
        f"supervisor's job, not the request's"
    )


def test_phase_becomes_user_stopping(env, client):
    """The routing row parks at the plan-level ``user_stopping``.

    ``user_stopping`` is the phase that says "a stop is in progress" —
    the plan is not running a round any more, and it is not parked yet
    either.  Both of those are true at the same time now, and the phase
    is where the truth lives.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text

    assert _read_phase(db_path, PLAN_ID) == "user_stopping", (
        "the stop did not park the plan at the plan-level stopping phase"
    )


def test_stop_record_has_scope_and_previous_phase(env, client):
    """The stop record says what is being stopped and from where.

    The background layer works from this record: the scope says *which*
    work is shutting down, and ``previous_phase`` says which phase the
    plan came in from — because that is what decides where it goes
    back to when the plan really falls silent.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text

    record = stop_state.get(PLAN_ID)
    assert record is not None, "the stop left no record behind"
    assert record.scope == stop_state.SCOPE_VERIFICATION, record
    assert record.previous_phase == "verification_running", record


def test_repair_executor_receives_sigterm(env, client, monkeypatch):
    """The repair executor is signalled — with SIGTERM, and only SIGTERM.

    The repair executor is a separate subprocess, so the stop's kill of
    the verification sub-agents never reached it: the plan was reported
    stopped while the repair kept spending.  A stop that comes from the
    repair half has to signal it.

    SIGTERM only: the executor has its own handler, and a SIGKILL would
    take away the chance to shut its sub-agents down cooperatively.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path,
        phase="verification_repairing",
        exec_pid=DEAD_EXEC_PID,
    )

    signals: list = []

    def _record_kill(pid, sig):
        signals.append((int(pid), int(sig)))
        return None

    monkeypatch.setattr(os, "kill", _record_kill)

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text

    assert (DEAD_EXEC_PID, int(signal.SIGTERM)) in signals, (
        f"the repair executor was not signalled: {signals}"
    )
    assert not any(sig == int(signal.SIGKILL) for _, sig in signals), (
        f"the stop must not SIGKILL the repair executor: {signals}"
    )


def test_second_stop_is_idempotent(env, client):
    """A stop that is asked for twice is one stop.

    The second request must not start a second supervisor and must not
    restart the stop record: the record is the evidence the stop is
    judged by, and a baseline re-taken later would erase it.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    first = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "stopping", first.json()
    record = stop_state.get(PLAN_ID)
    assert record is not None

    second = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "stopping", second.json()

    assert stop_state.get(PLAN_ID) == record, (
        "the second stop replaced the stop record — the baseline of the "
        "first stop is the evidence the quiet window is judged by"
    )
    assert len(_supervisor_threads()) == 1, (
        f"a second supervisor was started: "
        f"{[t.name for t in _supervisor_threads()]}"
    )


def test_stop_on_a_phase_that_cannot_be_stopped_is_still_409(env, client):
    """A phase this endpoint does not own still answers 409.

    The stop owns the verification chain.  A plan in the ``executing``
    phase is the main executor's business, and refusing it is what
    keeps the stop from reaching sideways into another kind of work.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="executing")

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 409, stop.text
    assert stop.json() == {"error": "conflict", "reason": "stage_mismatch"}


def test_phase_is_not_flipped_to_a_stopped_phase_before_the_answer(env, client):
    """Nothing is called stopped before the answer comes back.

    The phase the operator sees must not claim a final state the plan
    has not reached yet: the round is still winding down, and the
    verification row must not say ``loop_stopped`` while its
    sub-agents are still running.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text

    phase = _read_phase(db_path, PLAN_ID)
    assert phase == "user_stopping", phase
    assert phase not in ("stopped", "verification", "verification_repairing"), (
        f"the phase was flipped to {phase!r} before the answer came back"
    )

    row = _read_verification_row(db_path, PLAN_ID)
    assert row.get("verification_status") != "loop_stopped", (
        f"the verification row was written terminal before the answer "
        f"came back: {row}"
    )


def test_answer_reports_the_work_that_was_still_running(env, client, live_sub_agent):
    """The answer names the work that was still running.

    "Stopping" is not "stopped": the operator has to be able to see
    what the stop is still fighting, and which verification point it
    belongs to.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    body = stop.json()

    survivors = body["survivors"]
    assert isinstance(survivors, list), body
    assert any(
        entry.get("pid") == live_sub_agent.pid
        and entry.get("kind") == "registry"
        and entry.get("detail") == "VP-003"
        for entry in survivors
    ), f"the live sub-agent is not in the answer: {survivors}"


def test_the_cooperative_stop_belongs_to_the_supervisor_thread(
    env, client, monkeypatch,
):
    """The request thread does not stop the work — the supervisor does.

    The answer has to come back in milliseconds, so the work that is
    still running cannot be shut down on the request thread: that is
    the background layer's job — the cooperative dispatch stop first,
    signals only for whatever ignores it.  A route that ran the
    cooperative stop itself would still answer fast, but it would be
    doing the background layer's work on the request thread, and it
    would only ever stop the work it could enumerate.

    So the split is pinned from both sides: the request thread does
    NOT run the cooperative stop, and a supervisor IS running when the
    answer comes back — somebody owns the rest of the stop.
    """
    import coding_tool

    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    calls: list = []

    def _record(plan_id, *args, **kwargs):
        calls.append((plan_id, threading.current_thread().name))

    monkeypatch.setattr(
        coding_tool, "stop_live_dispatches_for_plan", _record,
    )

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text

    assert not calls, (
        f"the request thread ran the cooperative stop itself: {calls}"
    )
    assert _supervisor_threads(), (
        "the rest of the stop was handed to nobody — no supervisor is "
        "running"
    )
