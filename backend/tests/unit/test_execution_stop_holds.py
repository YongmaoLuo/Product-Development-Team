"""2026-10-23: ``POST /api/execution/{id}/stop`` must actually HOLD.

The live plan ``20261022-932bd42d-2`` answered the operator's stop
with a clean ``200 {"status": "stopped", "paused": true,
"interrupted_task_count": 1}`` — and then re-entered verification
**0.9 seconds later**, ran four more verification rounds, manufactured
another repair batch and spawned a fresh executor
(``execution_started total_tasks=18``).

The chain, every hop of it:

    routes/execution.py::stop_execution
      -> _stop_running_execution            (kills the executor's pgid)
      -> the killed process's ``wait()`` callback returns ``rc=-15``
    verification_loop.py::_on_repair_complete(returncode)
      -> the ONLY stop gate: ``verification_cancel.is_cancelled(plan_id)``
      -> begin_verification(round_n=4)
      -> start_verification_cycle -> _run_repair_execution_async
      -> a NEW executor subprocess

The gate was RIGHT — its own comment says "if the operator stopped the
plan while this repair was running, do NOT re-enter verification". It
never fired because **nothing ever set the flag**:
``POST /api/execution/{id}/stop`` never called
``verification_cancel.request_cancel``. Only
``POST /api/verification/{id}/stop`` did.

What is pinned here:

  1. **P1** — the execution stop requests the cooperative cancel.
  2. **P1 + the gate** — a repair executor killed by the stop
     (``rc=-15``) does NOT re-enter verification and does NOT spawn a
     new executor. This is the regression that produced the live
     timeline.
  3. **P2** — a start clears the flag again. Without this the stop
     would be a one-way door: every later start of the plan would be
     born cancelled and the verification chain would never run again.
  4. **P5** — a stop that cannot park the phase says so
     (``phase_transition_error`` in the body, ERROR in the log)
     instead of silently swallowing the refusal.

The tests drive the real HTTP endpoints — the flag has to be set by
the endpoint the operator actually calls, not by a helper the test
calls directly.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import plan_state
import server
import verification_cancel
import verification_loop
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261023-stop-must-hold"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProc:
    """A live process handle — the route only ever asks it ``poll()``."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.alive = True
        self.returncode = None

    def poll(self) -> Optional[int]:
        return None if self.alive else (self.returncode or 0)

    def wait(self, timeout=None) -> int:
        # The start endpoint's watcher thread parks here; it must not
        # race the assertions of the test.
        threading.Event().wait(timeout=30)
        return 0


class FakeSpawnedProc:
    """The executor subprocess the chain spawns.

    ``wait()`` returns ``-15`` straight away: the executor was killed,
    which is exactly what the stop does to the repair executor. The
    watcher thread that waits on it therefore fires its callback
    immediately and finishes — no worker outlives the test.
    """

    def __init__(self) -> None:
        self.pid = 5150
        self.returncode = -15
        self._gate = threading.Event()
        self._gate.set()

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout=None) -> int:
        self._gate.wait(timeout=1)
        return self.returncode

    def release(self) -> None:
        self._gate.set()

    def terminate(self) -> None:
        self._gate.set()


def _worker_threads() -> List[Any]:
    """The application worker threads that are alive right now."""
    return [
        t for t in threading.enumerate()
        if getattr(t, "_target", None) is server._run_in_plan_ctx
    ]


def _wait_for_new_workers_to_settle(
    before: set, timeout: float = 15.0
) -> None:
    """Wait until the workers this test started have finished.

    The repair dispatch runs its watcher on a background thread; that
    thread is the one that would call back into the chain. The test
    must not end while it is still running — a worker that outlives
    its test resolves its runtime paths against a LATER test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [t for t in _worker_threads() if t.ident not in before]
        if not alive:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"worker threads still alive after the test: {alive!r}"
    )


@pytest.fixture(autouse=True)
def _clean_cancel_flags():
    verification_cancel._reset_for_tests()
    yield
    verification_cancel._reset_for_tests()


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


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    phase: str = "executing",
):
    """A plan with a routing row, a phase and a running execution."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "first task"}]}),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": phase,
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
    finally:
        conn.close()

    return plan_dir, project_dir


def _mark_running(plan_id: str, project_dir: Path) -> None:
    """Put the plan into the state the stop endpoint sees as in-flight."""
    server._execution_state[plan_id] = {
        "status": "running",
        "logs": deque(maxlen=100),
        "project_dir": str(project_dir),
        "process": FakeProc(),
        "started_at": "2026-10-23T04:20:00",
        "ended_at": None,
        "pid": 4242,
        "stop_reason": None,
    }


@pytest.fixture
def spawn_recorder(monkeypatch):
    """Record every executor spawn; never spawn a real one.

    ``server._spawn_executor_subprocess`` IS
    ``routes/execution.py::_spawn_executor_subprocess`` — the single
    place an executor subprocess comes into existence.
    """
    calls: List[Dict[str, Any]] = []

    def _spawn(**kwargs):
        calls.append(kwargs)
        return (
            FakeSpawnedProc(),
            Path(str(kwargs.get("log_path") or "/dev/null")),
        )

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _spawn)
    return calls


@pytest.fixture
def begin_verification_recorder(monkeypatch):
    """Record every ``PlanState.begin_verification`` call."""
    calls: List[Optional[int]] = []
    original = plan_state.PlanState.begin_verification

    def _spy(self, round_n=None):
        calls.append(round_n)
        return original(self, round_n=round_n)

    monkeypatch.setattr(plan_state.PlanState, "begin_verification", _spy)
    return calls


@pytest.fixture
def orchestrator_recorder(monkeypatch):
    """Record every verification round the chain starts.

    The chain builds a fresh ``VerificationOrchestrator`` for each
    re-entered round; that construction IS the "a new verification
    round started" event, and it is what leads to a new executor.

    The FIRST round answers "still failing" — the verdict that makes
    the chain dispatch the NEXT repair executor. Later rounds answer
    "passed", so a chain that really does re-enter terminates instead
    of looping for ever.
    """
    built: List[Any] = []

    class _FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            built.append(args)
            self.round_number = None

        def start_verification_cycle(self, **kwargs):
            self.round_number = kwargs.get("round_number")
            return {"verification_results": []}

        def check_cycle_conditions(self, report, **kwargs):
            if not built[:-1]:
                # "still failing" — the branch that dispatches the
                # NEXT repair executor. This is the chain the stop
                # has to cut.
                return {
                    "status": "failed",
                    "repair_tasks": [
                        {"id": "repair-r4-01", "title": "fix it"},
                    ],
                }
            return {"status": "passed", "repair_tasks": []}

    monkeypatch.setattr(server, "VerificationOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: None)
    return built


def _drive_repair_callback(
    plan_id: str, project_dir: Path, returncode: int
) -> None:
    """Feed ``returncode`` into the REAL post-repair callback.

    ``_make_repair_on_complete`` is the factory that builds the
    ``on_complete`` callback the repair dispatcher hands to the
    executor watcher thread — the very callback that fires with
    ``rc=-15`` when the stop kills the repair executor.
    """
    callback = verification_loop._make_repair_on_complete(
        plan_id, project_dir, tool=None,
    )
    callback(returncode)


# ---------------------------------------------------------------------------
# P1 — the execution stop requests the cooperative cancel
# ---------------------------------------------------------------------------


def test_execution_stop_requests_the_verification_cancel(env, client, monkeypatch):
    """P1: ``POST /api/execution/{id}/stop`` flips the cancel flag.

    This is the missing half of a contract that was already half
    written: the gate in ``_on_repair_complete`` asks
    ``verification_cancel.is_cancelled(plan_id)``, and only the
    *verification* stop ever set it. The execution stop killed the
    process and left the flag alone, so the gate could never fire for
    an execution stop.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    assert verification_cancel.is_cancelled(PLAN_ID) is True, (
        "the execution stop must request the cooperative cancel — the "
        "verification chain's only stop gate reads this flag, and "
        "nothing else sets it for an execution stop"
    )


def test_the_flag_is_set_before_the_executor_is_killed(env, client, monkeypatch):
    """The flag has to be in place BEFORE the kill.

    The kill is what makes the executor's ``wait()`` callback fire
    with ``rc=-15``. If the flag were set after the kill, the
    callback could win the race and re-enter verification before the
    flag exists.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)

    seen_at_kill: List[bool] = []

    def _kill(proc, grace_seconds=None):
        seen_at_kill.append(verification_cancel.is_cancelled(PLAN_ID))
        proc.alive = False
        return True

    monkeypatch.setattr(server, "_kill_process_tree", _kill)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    assert seen_at_kill == [True], (
        "the cancel flag must be set before the executor is killed; "
        f"at kill time it was {seen_at_kill!r}"
    )


# ---------------------------------------------------------------------------
# P1 + the gate — the regression that produced the live timeline
# ---------------------------------------------------------------------------


def test_killed_repair_executor_does_not_re_enter_verification(
    env,
    client,
    monkeypatch,
    spawn_recorder,
    begin_verification_recorder,
    orchestrator_recorder,
):
    """The live timeline, reproduced and pinned.

    Stop -> the repair executor is killed -> its ``wait()`` callback
    fires with ``rc=-15`` -> the chain must NOT re-enter verification
    and must NOT spawn a new executor.

    Without P1 the flag is never set, the gate never fires, and the
    callback walks straight into ``begin_verification`` -> a new
    verification round -> a new repair executor. That is exactly what
    the live plan did (round 4 started 0.9s after the stop).
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    before = {t.ident for t in _worker_threads()}
    # The repair executor's watcher thread fires with rc=-15.
    _drive_repair_callback(PLAN_ID, project_dir, -15)
    _wait_for_new_workers_to_settle(before)

    assert begin_verification_recorder == [], (
        "the killed repair executor's callback re-entered verification — "
        "begin_verification was called; the stop did not hold"
    )
    assert orchestrator_recorder == [], (
        "a new verification round was started after the stop"
    )
    assert spawn_recorder == [], (
        "a new executor was spawned after the stop — the chain the "
        "operator asked to stop kept running"
    )


def test_the_stop_gate_is_not_vacuous(
    env,
    client,
    monkeypatch,
    spawn_recorder,
    begin_verification_recorder,
    orchestrator_recorder,
):
    """The control: WITHOUT the stop, the same callback DOES re-enter.

    This is what makes the assertions of the test above meaningful.
    If the chain never re-entered anyway, "the stop did not hold"
    would be indistinguishable from "the chain was dead to begin
    with".
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    # No stop: no cancel flag, so the gate lets the callback through.
    before = {t.ident for t in _worker_threads()}
    _drive_repair_callback(PLAN_ID, project_dir, 0)
    _wait_for_new_workers_to_settle(before)

    assert begin_verification_recorder, (
        "without a stop the post-repair callback MUST re-enter "
        "verification — otherwise the stop test above proves nothing"
    )
    assert orchestrator_recorder, (
        "without a stop a new verification round MUST start"
    )
    assert spawn_recorder, (
        "without a stop the chain MUST dispatch the next repair "
        "executor — the stop test above would otherwise be vacuous"
    )


# ---------------------------------------------------------------------------
# P2 — a start clears the flag again
# ---------------------------------------------------------------------------


def test_start_clears_the_verification_cancel_flag(
    env, client, monkeypatch, spawn_recorder
):
    """P2: the stop's flag must not outlive the stop.

    The flag is what P1 sets. Without clearing it on the next start,
    the flag would be permanent: every later start of this plan would
    be born cancelled and the verification chain would exit at its
    first check. P1 and P2 are two halves of one contract.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert verification_cancel.is_cancelled(PLAN_ID) is True

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )
    assert resp.status_code == 200, resp.text

    assert verification_cancel.is_cancelled(PLAN_ID) is False, (
        "the start must clear the cancel flag — otherwise every later "
        "start of this plan is born cancelled and the verification "
        "chain can never run again"
    )
    assert len(spawn_recorder) == 1, spawn_recorder


def test_stop_then_start_lets_the_verification_chain_run_again(
    env,
    client,
    monkeypatch,
    spawn_recorder,
    begin_verification_recorder,
    orchestrator_recorder,
):
    """The whole contract, end to end: stop holds, resume works.

    1. stop -> the flag is set, the killed repair executor's callback
       does NOT re-enter verification;
    2. start -> the flag is cleared and the chain is live again, so a
       later callback DOES re-enter verification.

    Without step 2 the plan would be permanently wedged: the operator
    could not resume at all. Without step 1 the operator could not
    stop at all.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    # --- 1. the stop holds -------------------------------------------
    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    before = {t.ident for t in _worker_threads()}
    _drive_repair_callback(PLAN_ID, project_dir, -15)
    _wait_for_new_workers_to_settle(before)
    assert begin_verification_recorder == [], begin_verification_recorder
    assert orchestrator_recorder == [], orchestrator_recorder
    assert spawn_recorder == [], spawn_recorder

    # --- 2. the resume works -----------------------------------------
    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )
    assert resp.status_code == 200, resp.text
    assert verification_cancel.is_cancelled(PLAN_ID) is False

    before = {t.ident for t in _worker_threads()}
    _drive_repair_callback(PLAN_ID, project_dir, 0)
    _wait_for_new_workers_to_settle(before)

    assert begin_verification_recorder, (
        "after the resume the chain must be able to re-enter "
        "verification again — the stop's cancel flag was not cleared"
    )
    assert orchestrator_recorder, (
        "after the resume a new verification round must be possible"
    )


# ---------------------------------------------------------------------------
# P5 — the stop reports a phase it could not park
# ---------------------------------------------------------------------------


def test_stop_reports_a_refused_phase_transition(
    env, client, monkeypatch, caplog
):
    """P5: a stop that cannot park the phase must say so.

    ``transition_to("stopped")`` raises ``ValueError`` from a phase
    that has no edge to ``stopped`` (``completed`` is one). The old
    code swallowed it, so the endpoint answered a clean 200 for a
    stop that did not hold: the work was killed, but the plan still
    sat in the phase it had before.
    """
    plans_dir, db_path = env
    # ``completed`` has no edge to ``stopped`` — by design.
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, phase="completed")
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    with caplog.at_level("ERROR"):
        resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "phase_transition_error" in body, (
        f"the stop could not park the plan in 'stopped' and said "
        f"nothing about it; body={body!r}"
    )
    assert "completed" in body["phase_transition_error"], body
    errors = [
        r for r in caplog.records
        if r.levelname == "ERROR" and PLAN_ID in r.getMessage()
    ]
    assert errors, (
        "the refusal must be logged at ERROR level with the plan id; "
        f"records={[r.getMessage() for r in caplog.records]!r}"
    )


def test_a_clean_stop_does_not_report_a_phase_transition_error(
    env, client, monkeypatch
):
    """The key is only there when the stop did not hold."""
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    assert "phase_transition_error" not in resp.json(), resp.json()
    assert set(resp.json()) == {
        "status", "paused", "paused_at", "interrupted_task_count",
    }, resp.json()


# ---------------------------------------------------------------------------
# The idle path — a stop of a plan that is not running parks the phase too
# ---------------------------------------------------------------------------


def test_idle_stop_parks_a_non_terminal_phase(env, client):
    """A plan that is not running is still parked in ``stopped``.

    The idle path used to leave the phase completely alone, so a plan
    sitting in ``verification_repairing`` kept a phase that says
    "live" after the operator stopped it.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_repairing",
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    assert "phase_transition_error" not in resp.json(), resp.json()
    ps = plan_state.PlanState(plan_dir)
    ps.reload()
    assert ps.get_current_phase() == "stopped", (
        f"the idle stop must park the plan; phase is "
        f"{ps.get_current_phase()!r}"
    )


def test_idle_stop_reports_a_phase_it_cannot_park(env, client, caplog):
    """A phase with no edge to ``stopped`` is REPORTED, not swallowed.

    The idle path now touches the phase, so a plan parked in a phase
    the table does not connect to ``stopped`` (the early workflow
    phases) cannot be parked. The refusal is reported the same way
    the running path reports it — in the body and in the log — so
    "the stop did not hold" is never a silent 200.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="interview",
    )

    with caplog.at_level("ERROR"):
        resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "phase_transition_error" in body, body
    assert "interview" in body["phase_transition_error"], body
    assert any(
        r.levelname == "ERROR" and PLAN_ID in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_idle_stop_with_a_checkpoint_parks_the_phase_too(env, client):
    """The checkpoint branch parks the phase as well.

    The checkpoint branch is the one a repeated stop takes; it must
    not be the branch where the phase is left alone.
    """
    import pause_state

    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_repairing",
    )
    pause_state.write_pause_checkpoint(
        plan_dir,
        kind="execution",
        project_dir=str(project_dir),
        reason="user_requested",
        interrupted_tasks=[],
        unfinished_tasks=[],
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    assert resp.json()["paused"] is True, resp.json()
    ps = plan_state.PlanState(plan_dir)
    ps.reload()
    assert ps.get_current_phase() == "stopped", (
        f"the idle stop must park the plan even with a checkpoint; "
        f"phase is {ps.get_current_phase()!r}"
    )
