"""A plan that is stopping cannot be started — and the status says so.

2026-11-21 — the verification stop became a *process* (tasks 9 and 10):
the stop closes the gate, parks the plan at the plan-level stopping
phase, and the background layer decides when the plan has really fallen
silent.  Two things follow from that, and neither of them was true
before:

1. **A start during a stop is a race.**  The stop's own worker is
   shutting the plan's work down right now, and a start would put a
   second round on a plan that is still being torn down — two rounds
   writing one plan's verdict row, and a resume point written from under
   the running one.  So the start has to refuse, and the refusal has to
   say *why*: "a stop is in progress" is a different thing to act on
   than "the phase conflicts".
2. **The status has to show the stop.**  A plan that is stopping is
   neither "running" nor "stopped": the operator polling the card needs
   to see that a stop is in progress, which scope is shutting down,
   which phase it came from and what was still running.

What is pinned here
-------------------
1. a start during a stop is refused with HTTP 409 and
   ``{"detail": {"error": "plan_user_stopping", "previous_phase": ...}}``;
2. the status of a stopping plan says ``stopping`` and names the scope,
   the phase the stop came from and the survivors;
3. a plan that is not stopping keeps the answer it always published —
   the stopping fields are **absent**, not null;
4. once the stop is over the start works again — and it resumes the
   round the stop interrupted, from the same round number.
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

PLAN_ID = "20261121-verification-status-stopping"

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

#: The published shape of the status answer.  The stopping half of the
#: answer is added at runtime and only while a stop is in progress, so
#: an idle plan's answer is exactly these keys — nothing more.
_IDLE_STATUS_KEYS = {
    "plan_id",
    "current_phase",
    "verification_status",
    "verification_round",
    "verification_max_rounds",
    "results",
    "repair_tasks",
    "execution_profile",
    "verification_points",
    "started_at",
    "updated_at",
    "stop_reason",
}

#: The keys the stopping half of the answer adds.  They are **absent**
#: on a plan that is not stopping — a reader that sees ``survivors:
#: null`` on every plan learns to ignore them.
_STOPPING_STATUS_KEYS = {
    "stopping",
    "stop_scope",
    "previous_phase",
    "survivors",
}


@pytest.fixture(autouse=True)
def _clean_process_state():
    """The stop's process-local state is global — do not leak it.

    ``stop_state`` (the stop record), ``stop_supervisor`` (the
    supervisor threads) and ``verification_cancel`` (the cancel flag)
    are process-wide, so a test that leaves one behind decides the next
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
def captured_finalize(monkeypatch):
    """The finalize callback the stop hands to the supervisor.

    The supervisor is replaced by a recorder, so the callback is driven
    by the test instead of by a background thread: the verdict is then
    something the test observes, not something it races.
    """
    captured: dict = {}

    def _fake_start(plan_id, *, finalize, **kwargs):
        captured["plan_id"] = plan_id
        captured["finalize"] = finalize
        return None

    monkeypatch.setattr(stop_supervisor, "start", _fake_start)
    return captured


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


@pytest.fixture
def init_round_calls(monkeypatch):
    """Record every round number ``init_round`` is called with."""
    calls: list[int] = []
    original = VerificationRepository.init_round

    def _spy(self, plan_id, round_n, max_rounds):
        calls.append(int(round_n))
        return original(self, plan_id, round_n=round_n, max_rounds=max_rounds)

    monkeypatch.setattr(VerificationRepository, "init_round", _spy)
    return calls


@pytest.fixture
def stub_auto_loop(monkeypatch):
    """Replace the auto-loop with a recorder.

    The auto-loop is the round→round chain under ``auto_fix=True``.  It
    is stubbed out here so no LLM call is made; what it was asked to do
    is recorded instead.
    """
    calls: list[dict] = []
    reached = threading.Event()

    def _fake(plan_id, plan_dir, project_dir, max_rounds=None, tool=None,
              start_round=1):
        calls.append({
            "plan_id": plan_id,
            "max_rounds": max_rounds,
            "tool": tool,
            "start_round": start_round,
        })
        reached.set()
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake)
    return calls, reached


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "verification_running",
    round_n: int = 2,
    status: str | None = "running",
    verdicts=None,
    max_rounds: int = 3,
):
    """A plan mid-verification, parked at ``phase``, with a round's work."""
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
        if status:
            verif.mark_stopped(PLAN_ID, "user_stopped", status=status)
    finally:
        conn.close()

    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "verification_points": [
                    {"id": vp_id, "title": f"check {vp_id}"} for vp_id in VP_IDS
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir, project_dir


def _read_phase(db_path: Path, plan_id: str = PLAN_ID) -> str:
    """The routing row's phase, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _begin_stop(
    previous_phase: str = "verification_running", dispatch_baseline: int = 3,
) -> None:
    """Write the stop record the way the stop route writes it."""
    stop_state.begin(
        PLAN_ID,
        stop_state.SCOPE_VERIFICATION,
        previous_phase=previous_phase,
        dispatch_baseline=dispatch_baseline,
    )


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_start_rejected_409_while_stopping(env, client):
    """A plan that is stopping is not startable — and the answer says so.

    The refusal has to name the reason.  "conflict" tells the operator
    to look at the phase; ``plan_user_stopping`` tells them a stop is in
    progress and that the start will work once the stop is over.  The
    phase the stop came from is in the answer too: the operator has to
    be able to tell a stopped round from a stopped repair without
    reading the plan's row.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)
    _begin_stop(previous_phase="verification_running")

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})

    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "detail": {
            "error": "plan_user_stopping",
            "previous_phase": "verification_running",
        },
    }, resp.json()

    # The refusal changed nothing: the plan is still the plan the stop
    # is shutting down, and no round was started.
    assert _read_phase(db_path) == "verification_running", (
        f"the refused start moved the plan's phase to "
        f"{_read_phase(db_path)!r}"
    )


def test_status_reports_stopping_and_survivors(env, client, live_sub_agent):
    """The status of a stopping plan says ``stopping``.

    Not ``running`` — the plan's work is being taken down — and not
    ``stopped`` — the stop has not finished.  The scope says which work
    is shutting down, the previous phase says where it came from, and
    the survivors say what was still running when the reader asked.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)
    _begin_stop(previous_phase="verification_running")

    resp = client.get(f"/api/verification/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verification_status"] == "stopping", body
    assert body["stopping"] is True, body
    assert body["stop_scope"] == "verification", body
    assert body["previous_phase"] == "verification_running", body
    survivors = body["survivors"]
    assert isinstance(survivors, list), body
    assert any(
        entry.get("pid") == live_sub_agent.pid
        and entry.get("kind") == "registry"
        and entry.get("detail") == "VP-003"
        for entry in survivors
    ), f"the live sub-agent is not in the answer: {survivors}"


def test_status_shape_unchanged_when_not_stopping(env, client):
    """A plan that is not stopping keeps the answer it always published.

    The stopping half of the answer is added *while a stop is in
    progress*.  An idle plan must not grow the fields: a reader that
    sees ``survivors: null`` on every plan learns to ignore them, and a
    reader that sees ``stop_scope: null`` cannot tell "this build does
    not report it" from "there is no stop".
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path)

    resp = client.get(f"/api/verification/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _IDLE_STATUS_KEYS, (
        f"the idle answer changed shape: {sorted(body)!r}"
    )
    assert not (_STOPPING_STATUS_KEYS & set(body)), (
        f"the idle answer grew the stopping fields: {sorted(body)!r}"
    )


def test_start_resumes_same_round_after_finalize(
    env, client, captured_finalize, init_round_calls, stub_auto_loop,
):
    """Once the stop is over, the start works again — and it resumes.

    The refusal is the *stop's*, not the start's: the stop's finalize
    clears the record when the plan has fallen silent, and the start
    that comes after it is an ordinary start — the plan resumes the
    round the stop interrupted, from the same round number.  A resume
    that moved to the next round would relabel half-done work under a
    round that never ran.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running", round_n=2)

    # A real stop: the gate closes, the plan is parked at the stopping
    # phase, and the background layer parks it at "verification" once it
    # is quiet.
    stopped = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopping", stopped.json()
    assert _read_phase(db_path) == "user_stopping", (
        f"the stop did not park the plan at the stopping phase — it is "
        f"{_read_phase(db_path)!r}"
    )
    assert stop_state.get(PLAN_ID) is not None, "the stop left no record"

    captured_finalize["finalize"](PLAN_ID)

    assert stop_state.get(PLAN_ID) is None, (
        "the finalize did not clear the stop record — the start would "
        "stay refused forever"
    )
    assert _read_phase(db_path) == "verification", (
        f"the quiet plan was not parked at 'verification' — it is "
        f"{_read_phase(db_path)!r}"
    )

    init_round_calls.clear()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["round"] == 2, body
    assert body["resumed"] is True, body
    assert init_round_calls == [2], init_round_calls
