"""A stop must not park a plan whose round is still running.

What is pinned here
-------------------
``POST /api/verification/{id}/stop`` parks the routing row at
``verification`` — the phase that means "a verification round was
interrupted, the plan is waiting".  That is only true when the work
actually stopped.

The stop's hard kill is best-effort.  A sub-agent whose process the
kill could not reach keeps running, and its verdict lands minutes
later; the round keeps its lease and its slot the whole time.  When
that happened in production, the routing row said ``verification``
while fifteen VPs were still working, the round's lease heartbeat
stayed fresh for half an hour, and the plan dropped out of
``/api/system/active`` — capacity saw a free slot and the scheduler
was free to hand it to somebody else.

So the phase has to be true:

1. **a stop whose work survived does not park the plan** — the phase
   stays ``verification_running``, because the round is still running;
2. **a stop whose work really stopped does park the plan** — the phase
   is ``verification``, which is what makes the next ``/start`` a
   resume;
3. **stop then start ends at ``verification_running``** — the chain the
   operator actually drives.

The chain is driven through the HTTP handlers, not through an internal
helper: the phase is the routing row's, and the only thing that decides
it is the endpoint.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import verification_cancel
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20261021-stop-phase-truthfulness"

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


@pytest.fixture(autouse=True)
def _clean_cancel_flags():
    """The stop's cancel flag is process-global — do not leak it."""
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


class _LoopCalls(list):
    """The calls the auto-loop was asked to make.

    A list (so it reads like the record it is) plus the event that says
    the loop has been entered — the loop runs on the verification
    thread, not on the request thread, so an assertion about it has to
    be race-free.
    """

    def __init__(self):
        super().__init__()
        self.reached = threading.Event()


@pytest.fixture
def stub_auto_loop(monkeypatch):
    """Replace the auto-loop with a recorder — no LLM call, no subprocess."""
    calls = _LoopCalls()

    def _fake(plan_id, plan_dir, project_dir, max_rounds=None, tool=None,
              start_round=1):
        calls.append({"start_round": start_round})
        calls.reached.set()
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake)
    return calls


@pytest.fixture
def live_sub_agent():
    """A registered sub-agent whose process the stop's kill cannot reach.

    The handle carries a live process and **no** ``scoped_tool``: the
    kill reaches a sub-agent through the tool's own process handle
    (``_kill_sub_agent_process`` returns immediately when
    ``scoped_tool is None``), so a handle without one is a sub-agent the
    kill never touches — the production shape, where the round kept
    running after a stop that answered 200.
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


def _seed_plan(plans_dir: Path, db_path: Path, *,
               phase: str = "verification_running",
               round_n: int = 2, status: str = "running",
               verdicts=None, max_rounds: int = 3):
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
        ExecutionRepository(conn).insert(
            plan_id=PLAN_ID, project_dir=str(project_dir),
        )
        verif = VerificationRepository(conn)
        verif.insert(PLAN_ID, verification_status="pending",
                     max_rounds=max_rounds)
        verif.init_round(PLAN_ID, round_n=round_n, max_rounds=max_rounds)
        for verdict in (verdicts if verdicts is not None else ROUND_VERDICTS):
            verif.append_verdict(PLAN_ID, dict(verdict))
        verif.mark_stopped(PLAN_ID, "user_stopped", status=status)
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


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_stop_leaves_a_running_round_on_its_running_phase(
    env, client, live_sub_agent,
):
    """A stop whose work survived must not park the plan.

    The stop answers 200 and the operator believes the round is over.
    If the row is parked at ``verification`` anyway, the plan leaves
    ``/api/system/active``, capacity sees a free slot, and the round
    keeps spending — the split the phase is supposed to prevent.

    So the phase has to keep saying that a round is running until the
    work really stops.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    body = stop.json()
    assert body["status"] == "stopped", body

    # The work is still alive (the fixture's process never exits on its
    # own), so the phase must not claim the plan is parked.
    assert _read_phase(db_path, PLAN_ID) == "verification_running", (
        "the plan was parked at 'verification' while its round was "
        "still running — it drops out of /api/system/active and the "
        "scheduler thinks the slot is free"
    )
    assert body["still_running"] is True, body


def test_stop_parks_the_plan_when_the_round_really_stopped(env, client):
    """The ordinary stop still parks the plan — the resume depends on it.

    The parked phase (``verification``) is what the next ``/start``
    reads as a resumable source, so a stop that refuses to park a plan
    whose work is gone would break the pause contract.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    assert _read_phase(db_path, PLAN_ID) == "verification"


def test_stop_then_start_ends_on_the_running_phase(
    env, client, stub_auto_loop,
):
    """Stop, then start: the chain ends at ``verification_running``.

    The invariant the operator's chain rests on — the start's CAS moves
    the row off the parked phase and onto ``verification_running``, and
    nothing writes it back.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)
    calls = stub_auto_loop

    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text

    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "started", start.json()
    assert calls.reached.wait(timeout=5), "the round never ran"

    assert _read_phase(db_path, PLAN_ID) == "verification_running"
