"""``/start`` resumes the REPAIR half of the loop; ``/stop`` stops it.

The repair executor is a stateless scanner: it re-reads ``tasks.json``
+ ``state.db`` and runs whatever is still pending. So "continue the
repair" is "spawn it once more" — there is nothing to attach to and
nothing to resume in-process. The two ends of that fact are pinned
here.

What is pinned:

1. ``/start`` on a plan parked at ``verification_repairing`` RESUMES
   the repair: the executor is spawned exactly once, the plan moves to
   ``executing``, and the round is counted ONCE — the resume path does
   not also run the ordinary ``init_round`` of a new verification
   round.
2. ``/start`` on a plan already at ``executing`` with pending repair
   tasks is a PURE resume: the executor is spawned, the plan stays at
   ``executing``, and the round is NOT bumped a second time (the
   round was already counted when the repair was first confirmed).
3. **The negative control.** An ``executing`` plan whose executor is
   NOT the repair one takes the ORIGINAL verification branch — the
   repair resume is not unconditional, and a plan whose MAIN executor
   owns the queue must not get a second executor spawned over it.
4. ``/stop`` on a plan whose REPAIR executor is running parks the plan
   back at ``verification_repairing`` and SIGTERMs that executor
   (``plan_execution.exec_pid``). The old behaviour left the repair
   running and parked the plan at ``verification``.
5. The operator's stop reaches the auto-loop's dispatch points: with
   the cancel flag set, the loop neither confirms the repair nor
   spawns the executor.
6. A residual ``verification_rerunning`` value — the dead state that
   was removed from the vocabulary — is read back as
   ``verification_repairing`` instead of crashing the reader.

Nothing here spawns a real executor: ``_run_repair_execution_async``
is recorded, not called. The one real subprocess is the dummy the
stop has to SIGTERM (test 4) — it has to be a real process for the
signal to be observable.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import verification_cancel
import verification_loop
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20261022-repair-resume"

#: The repair task the executor still has to run. Its id is repair-
#: shaped (``RP-*``), which is what ``_get_pending_repair_tasks``
#: looks for.
REPAIR_TASK_ID = "RP-001"


@pytest.fixture(autouse=True)
def _clean_cancel_flags():
    """The cancel flag is process-global — do not leak it."""
    verification_cancel._reset_for_tests()
    yield
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


class _Dispatch(list):
    """The repair dispatches the route was asked to make."""

    def __init__(self):
        super().__init__()
        self.reached = threading.Event()


@pytest.fixture
def repair_dispatch(monkeypatch):
    """Record the repair dispatches instead of spawning an executor."""
    calls = _Dispatch()

    def _fake(plan_id, project_dir, tool=None, on_complete=None, outcome=None):
        calls.append({
            "plan_id": plan_id,
            "project_dir": project_dir,
            "tool": tool,
            "on_complete": on_complete,
        })
        calls.reached.set()
        return {"status": "started", "pid": None}

    monkeypatch.setattr(server, "_run_repair_execution_async", _fake)
    return calls


@pytest.fixture
def stub_auto_loop(monkeypatch):
    """Record the auto-loop instead of running a verification round."""
    calls = _Dispatch()

    def _fake(plan_id, plan_dir, project_dir, max_rounds=None, tool=None,
              start_round=1):
        calls.append({"start_round": start_round})
        calls.reached.set()
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake)
    return calls


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str,
    round_n: int = 2,
    max_rounds: int = 3,
    with_repair_task: bool = True,
    exec_pid=None,
):
    """A plan parked at ``phase``, with a round's bookkeeping.

    ``phase`` is the routing row's ``current_phase``. The plan's
    ``plan_verification`` row carries the round (2) and a terminal
    ``failed`` verdict — the state a plan is in after a round failed
    and produced repair tasks.

    The routing row's ``verification`` JSON carries the same round:
    that is the mirror the repair path bumps (``start_repair_execution``
    increments it), so the resume path can report the round it is
    running.

    Returns ``(plan_dir, project_dir)``.
    """
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, phase)
        ExecutionRepository(conn).insert(
            plan_id=PLAN_ID, project_dir=str(project_dir),
        )
        verif = VerificationRepository(conn)
        verif.insert(PLAN_ID, verification_status="pending",
                     max_rounds=max_rounds)
        verif.init_round(PLAN_ID, round_n=round_n, max_rounds=max_rounds)
        verif._update(PLAN_ID, verification_status="failed")
        if exec_pid is not None:
            ExecutionRepository(conn).update_fields(
                plan_id=PLAN_ID, exec_pid=exec_pid, exec_status="running",
            )
        if with_repair_task:
            repo = PlanTaskRepository(conn)
            repo.add_task(PLAN_ID, {
                "id": REPAIR_TASK_ID,
                "title": "repair the failing check",
                "description": "fix it",
                "task_group": "repair-round-1",
                "failed_vp_id": "VP-001",
                "round": 1,
            })
    finally:
        conn.close()

    # The routing mirror of the round — the repair path bumps this one.
    ps = server.PlanState(plan_dir)
    ps.set_verification_status("failed", round_n=round_n)
    return plan_dir, project_dir


def _read_phase(db_path: Path, plan_id: str = PLAN_ID):
    """The plan's routing ``current_phase``, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _read_verification_round(db_path: Path, plan_id: str = PLAN_ID):
    """The plan's ``plan_verification.round`` — the round column."""
    conn = open_db(db_path)
    try:
        row = VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()
    return int((row or {}).get("round") or 0)


# ---------------------------------------------------------------------------
# 1) /start at ``verification_repairing`` resumes the repair
# ---------------------------------------------------------------------------


def test_start_at_repairing_phase_resumes_the_repair(
    env, client, repair_dispatch,
):
    """A plan parked at ``verification_repairing`` is RESUMED, not re-verified.

    The repair executor is a stateless scanner, so "continue the
    repair" is "spawn it once more". The start must:
      * spawn the executor exactly once,
      * route the plan into ``executing`` (where the executor runs),
      * count the round ONCE — the repair path bumps it, so the
        ordinary ``init_round`` of a new verification round must NOT
        run on top of that.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_repairing")

    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["status"] == "started", body
    assert body["mode"] == "repair_resume", body
    assert body["round"] == 3, (
        "the resume did not report the bumped round — the repair path "
        f"increments the round so the post-repair cycle starts on N+1: {body}"
    )

    assert len(repair_dispatch) == 1, repair_dispatch
    assert repair_dispatch[0]["plan_id"] == PLAN_ID, repair_dispatch[0]
    # The callback the resume dispatches with is the shared one — the
    # same implementation the auto-loop uses.
    assert callable(repair_dispatch[0]["on_complete"]), repair_dispatch[0]

    assert _read_phase(db_path) == "executing", (
        "the resume did not route the plan into 'executing' — the "
        f"executor runs there: {_read_phase(db_path)!r}"
    )

    # The round is counted ONCE. ``init_round`` writes the round to
    # ``plan_verification``; the repair path bumps the routing mirror
    # instead. If both ran, the plan's round would be counted twice.
    assert _read_verification_round(db_path) == 2, (
        "the resume started a verification round as well — the round was "
        f"counted twice: {_read_verification_round(db_path)}"
    )


# ---------------------------------------------------------------------------
# 2) /start at ``executing`` with pending repair tasks is a pure resume
# ---------------------------------------------------------------------------


def test_start_at_executing_with_pending_repair_tasks_only_spawns(
    env, client, repair_dispatch,
):
    """Already ``executing`` with repair work queued: spawn, and nothing else.

    The phase is already ``executing`` and the round was already
    counted when the repair was confirmed, so this start must NOT
    ``start_repair_execution`` again — that would count the same
    repair twice and move the plan out of the phase the executor
    already runs in.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="executing")
    # The repair executor's own marker on the shared execution record.
    server._execution_state[PLAN_ID] = {
        "status": "running",
        "_source": "repair_execution",
    }

    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["mode"] == "repair_resume", body
    assert body["round"] == 2, (
        "the pure resume counted the round a second time — the phase was "
        f"already 'executing', so the round was already counted: {body}"
    )

    assert len(repair_dispatch) == 1, repair_dispatch
    assert _read_phase(db_path) == "executing", (
        "the pure resume moved the plan out of 'executing': "
        f"{_read_phase(db_path)!r}"
    )
    assert _read_verification_round(db_path) == 2, (
        "the pure resume started a verification round as well: "
        f"{_read_verification_round(db_path)}"
    )


# ---------------------------------------------------------------------------
# 3) negative control — a NON-repair executor takes the original branch
# ---------------------------------------------------------------------------


def test_start_at_executing_with_non_repair_execution_starts_a_round(
    env, client, repair_dispatch, stub_auto_loop,
):
    """``executing`` + a MAIN executor: the original verification branch.

    The main executor owns the plan's task queue — the pending repair
    tasks are ITS work, not a repair round's. Spawning a second
    executor over the same plan would run the same tasks twice, so
    this start must take the ordinary verification branch instead.

    This is the control for the branch above: without it, "the phase
    is ``executing``" would be enough to spawn a repair executor, and
    every plan with a running executor would get a second one.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="executing")
    server._execution_state[PLAN_ID] = {
        "status": "running",
        "_source": "main_execution",
    }

    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, start.text
    body = start.json()
    assert body.get("mode") != "repair_resume", (
        f"a MAIN executor was mistaken for the repair executor: {body}"
    )

    assert repair_dispatch.reached.wait(timeout=0.5) is False, (
        "a repair executor was spawned over a plan whose MAIN executor "
        "owns the queue"
    )
    assert stub_auto_loop.reached.wait(timeout=5), (
        "the start did not enter the verification loop — the original "
        "branch was not taken"
    )
    assert len(stub_auto_loop) == 1, stub_auto_loop


# ---------------------------------------------------------------------------
# 4) /stop during a repair execution parks the plan and kills the executor
# ---------------------------------------------------------------------------


def test_stop_at_repair_execution_parks_the_plan_and_sigterms_the_executor(
    env, client,
):
    """``/stop`` during a repair execution: park, then kill the executor.

    The repair executor is a separate subprocess and the stop used to
    leave it running: the hard kill only reached the VERIFICATION
    sub-agents. So the operator pressed stop, the plan was parked at
    ``verification``, and the repair kept changing code.

    The stop must park the plan at ``verification_repairing`` (the
    repair half of the loop — the repair tasks are still waiting) and
    SIGTERM the executor recorded in ``plan_execution.exec_pid``.
    """
    plans_dir, db_path = env
    dummy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        _seed_plan(plans_dir, db_path, phase="executing", exec_pid=dummy.pid)
        server._execution_state[PLAN_ID] = {
            "status": "running",
            "pid": dummy.pid,
            "_source": "repair_execution",
        }

        stop = client.post(f"/api/verification/{PLAN_ID}/stop")
        assert stop.status_code == 200, stop.text
        body = stop.json()
        assert body["reason"] == "user_stopped_at_repair_execution", body
        assert body["status"] == "stopped", body

        assert _read_phase(db_path) == "verification_repairing", (
            "the stop did not park the plan in the repair half of the "
            f"loop: {_read_phase(db_path)!r}"
        )

        # The executor really got the signal: the dummy process is
        # gone, killed by SIGTERM (not by an ordinary exit).
        try:
            dummy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            dummy.kill()
            pytest.fail("the repair executor was not stopped by the stop")
        assert dummy.returncode == -15, (
            "the repair executor did not die of SIGTERM: "
            f"returncode={dummy.returncode}"
        )
    finally:
        if dummy.poll() is None:
            dummy.kill()
            dummy.wait(timeout=5)


# ---------------------------------------------------------------------------
# 5) the operator's stop reaches the auto-loop's dispatch points
# ---------------------------------------------------------------------------


def test_auto_loop_does_not_dispatch_a_repair_after_the_stop(
    env, monkeypatch,
):
    """Cancel set mid-round: no confirm, no spawn.

    The stop can land WHILE the round is judging — after the repair
    tasks were written, before the executor is spawned. There was no
    cancel check in that window, so the loop confirmed the repair and
    started an executor the operator had already stopped.

    The cancel flag is set by the fake orchestrator's
    ``check_cycle_conditions`` — i.e. exactly in that window.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_repairing",
    )

    confirmed = []
    dispatched = []

    class _FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            self.plan_state = None

        def start_verification_cycle(self, round_number=1, force=False,
                                     resume=False):
            return {"verification_results": []}

        def check_cycle_conditions(self, report, round_number=1):
            # The operator presses stop HERE: the round has judged,
            # the repair tasks exist, and the executor has not been
            # spawned yet.
            verification_cancel.request_cancel(PLAN_ID)
            return {
                "status": "failed",
                "stop_reason": None,
                "repair_tasks": [{
                    "id": REPAIR_TASK_ID,
                    "title": "repair the failing check",
                }],
            }

        def confirm_repair_and_rerun(self):
            confirmed.append(True)
            return None

    monkeypatch.setattr(server, "VerificationOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(
        server, "_run_repair_execution_async",
        lambda *args, **kwargs: dispatched.append(True),
    )

    verification_loop._run_auto_verification_loop_inner(
        PLAN_ID, plan_dir, project_dir, max_rounds=3, start_round=1,
    )

    assert confirmed == [], (
        "the auto-loop confirmed the repair after the operator stopped "
        "the plan — the repair tasks were routed to 'executing' anyway"
    )
    assert dispatched == [], (
        "the auto-loop spawned the repair executor after the operator "
        "stopped the plan"
    )


# ---------------------------------------------------------------------------
# 6) the residual ``verification_rerunning`` value is mapped, not fatal
# ---------------------------------------------------------------------------


def test_residual_rerunning_phase_is_read_back_as_repairing(env):
    """A row that still holds ``verification_rerunning`` reads as repairing.

    ``verification_rerunning`` was a dead state — nothing wrote it any
    more — and it was removed from the phase vocabulary. A row written
    before that still holds the historical value, so the read path maps
    it onto the phase that replaced it instead of raising or reporting
    a phase that no longer exists.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_repairing",
    )

    # Write the historical value straight into the routing row.
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            ("verification_rerunning", PLAN_ID),
        )
        conn.commit()
    finally:
        conn.close()

    assert _read_phase(db_path) == "verification_rerunning", (
        "the residual value was not written — the rest of this test would "
        "prove nothing"
    )

    ps = server.PlanState(plan_dir)
    ps.reload()
    assert ps.get_current_phase() == "verification_repairing", (
        "the residual 'verification_rerunning' value was not mapped onto "
        f"the phase that replaced it: {ps.get_current_phase()!r}"
    )
