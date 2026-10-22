"""``stop`` works from ``verification_repairing`` — and from a RESIDUAL
``verification_rerunning`` row.

A verification round is not parked at ``verification_running`` for its
whole life. Once a round finishes, the auto-loop moves the routing row
forward to ``verification_repairing`` (repair tasks to confirm) — and a
round's ``plan_verification`` row is still ``running`` through it. The
operator's stop is exactly as legitimate there as it is at
``verification_running``.

``verification_rerunning`` was a DEAD state and left the phase
vocabulary on 2026-10-22, but a routing row written before that can
still hold the historical value. The residual case is pinned here too:
it must not crash and it must not be a stranded row.

What is pinned here is that such a stop both **lands** and is
**resumable**:

1. **The stop lands.** ``stop_verification`` CASes the routing row back
   to ``verification``. With a source set of only
   ``verification_running`` the CAS silently no-ops on the other two
   phases — the row is left where it was and the operator gets a 200
   that did nothing.
2. **The stop is recoverable.** That is the part that bit hardest: a
   row left at ``verification_repairing`` is not in ``/start``'s source
   whitelist, so the very next ``/start`` answers
   ``409 {"error": "conflict", "reason": "stage_mismatch"}`` — and so
   does every one after it. The plan is stuck permanently, recoverable
   only by hand-editing SQLite.

So the regression is the whole chain: stop → ``current_phase ==
"verification"`` → start → **200**, ``resumed`` true, same round.

The three-phase source set is not a new invention:
``force_verification_terminal`` already CASes from exactly these three,
as does the dead-end rollback in ``verification_loop.py``.

**The negative control matters as much as the main case.**
``test_start_from_running_phase_without_checkpoint_is_still_409`` pins
that ``/start``'s own whitelist was *not* relaxed to match: a plan still
at ``verification_running`` with no checkpoint is still refused. Widening
the stop's CAS must not turn a genuinely-running round into a re-enterable
one.

The auto-loop is stubbed, so no LLM call and no subprocess is made.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import verification_cancel
from pause_state import checkpoint_path
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20261021-stop-from-repair"

#: The VPs the round was working on. ``VP-003`` never reached a verdict
#: — it is the VP the stop cut short, and the one a resume still owes.
VP_IDS = ["VP-001", "VP-002", "VP-003"]

#: The verdicts the round had already reached when the operator pressed
#: stop. The shape is the executor's own (``verification_executor.py``
#: writes ``vp_id`` / ``status`` / ``reasons`` / ``evidence``).
ROUND_VERDICTS = [
    {
        "vp_id": "VP-001",
        "status": "PASSED",
        "worker_id": "executor",
        "seq": 1,
        "reasons": [],
        "evidence": {},
    },
    {
        "vp_id": "VP-002",
        "status": "FAILED",
        "worker_id": "executor",
        "seq": 2,
        "reasons": ["500"],
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
    """Replace the auto-loop with a recorder.

    The auto-loop is the round→round chain under ``auto_fix=True``. It
    is stubbed out here so no LLM call is made; the round it was asked
    to run is recorded instead.
    """
    calls = _LoopCalls()

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
    status: str = "running",
    verdicts=None,
    max_rounds: int = 3,
):
    """A plan mid-verification, parked at ``phase``, with a round's work.

    ``phase`` is the routing row's ``current_phase`` and is the whole
    point of this module: the round is in flight at
    ``verification_repairing`` (or at a residual historical value), not
    at ``verification_running``.

    ``status="running"`` is the row status of a round that is still in
    flight — the operator pressed stop while it was running.

    Returns ``(plan_dir, project_dir)``. A SECOND connection is used
    (not the fixture's) so the rows the endpoint reads back are the
    committed ones.
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
        for verdict in (verdicts if verdicts is not None else ROUND_VERDICTS):
            verif.append_verdict(PLAN_ID, dict(verdict))
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


def _read_phase(db_path: Path, plan_id: str):
    """The plan's routing ``current_phase``, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _assert_stop_then_resume(env, client, stub_auto_loop, phase: str) -> None:
    """Stop a round parked at ``phase``, then start it again — same round.

    The shared contract of the live phase: the stop lands, it leaves a
    resumable trace, and the next ``/start`` picks the same round back
    up.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase=phase, round_n=2,
        status="running", verdicts=ROUND_VERDICTS,
    )
    assert _read_phase(db_path, PLAN_ID) == phase

    # --- the stop: the operator interrupts round 2 ------------------------
    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    stop_body = stop.json()
    assert stop_body["status"] == "stopped", stop_body
    assert stop_body["paused"] is True, stop_body
    assert stop_body["round"] == 2, stop_body

    # The stop must actually MOVE the routing row. A CAS whose source
    # set omits the phase the row is parked on silently no-ops and still
    # answers 200 — so the phase is read back, not inferred from the
    # status code.
    parked = _read_phase(db_path, PLAN_ID)
    assert parked == "verification", (
        f"the stop left the routing row at {parked!r} instead of parking it "
        f"at 'verification' — it was stopped while at {phase!r}, and a stop "
        "whose CAS source set omits that phase does nothing while still "
        "answering 200"
    )

    # ...and leave a resumable trace behind — that is what makes the next
    # start a resume rather than a fresh round.
    assert checkpoint_path(plan_dir).exists(), (
        "the stop left no checkpoint behind — the next /start has nothing "
        "to resume"
    )
    checkpoint = json.loads(
        checkpoint_path(plan_dir).read_text(encoding="utf-8")
    )
    assert checkpoint["verification_round"] == 2, checkpoint

    # --- the start: the same round is picked up ---------------------------
    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, (
        "the start was refused after a legitimate stop — the plan is now "
        f"permanently unrecoverable through the API: {start.status_code} "
        f"{start.text}"
    )
    start_body = start.json()
    assert start_body["resumed"] is True, (
        "the start did not say it resumed the interrupted round — a client "
        f"cannot tell a resume from a fresh round: {start_body}"
    )
    assert start_body["round"] == 2, (
        "the start moved to another round — the interrupted round's work "
        f"would be relabelled: {start_body}"
    )

    # The background chain is entered with that same round.
    assert stub_auto_loop.reached.wait(timeout=5), "the round never ran"
    assert len(stub_auto_loop) == 1, stub_auto_loop
    assert stub_auto_loop[0]["start_round"] == 2, stub_auto_loop[0]


# ---------------------------------------------------------------------------
# 1) the stop lands, from both repair-side phases
# ---------------------------------------------------------------------------


def test_stop_from_repairing_phase_then_start_resumes(
    env, client, stub_auto_loop,
):
    """A round parked at ``verification_repairing`` can be stopped and resumed.

    This is the exact shape of the live defect: the round finished, the
    auto-loop moved the row to ``verification_repairing`` to ask the
    operator about repair tasks, and the operator pressed stop instead.
    Before the fix the stop's CAS did not fire, the row stayed at
    ``verification_repairing``, and every ``/start`` from then on
    answered 409 ``stage_mismatch`` — the plan could only be rescued by
    editing SQLite by hand.
    """
    _assert_stop_then_resume(
        env, client, stub_auto_loop, "verification_repairing",
    )


def test_stop_from_residual_rerunning_phase_is_mapped_and_lands(
    env, client, stub_auto_loop,
):
    """A RESIDUAL ``verification_rerunning`` row is a historical value.

    2026-10-22: that phase left the vocabulary — it was a dead state
    (its only writer had no callers), so nothing enters it any more. A
    routing row written before that still holds the value, and two things
    must hold for it:

    1. **It does not crash.** Reading the plan's phase maps the
       historical value onto the phase that replaced it
       (``verification_repairing``) instead of raising or reporting a
       phase no code knows any more.
    2. **It is not stranded.** The operator's stop still lands on such a
       row — the historical value is accepted by the stop's CAS source
       set — and the plan stays resumable through ``/start``.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_rerunning", round_n=2,
        status="running", verdicts=ROUND_VERDICTS,
    )

    # The raw row still holds the historical value ...
    assert _read_phase(db_path, PLAN_ID) == "verification_rerunning"

    # ... and reading it back maps it, without raising.
    ps = server.PlanState(plan_dir)
    ps.reload()
    assert ps.get_current_phase() == "verification_repairing", (
        "the residual 'verification_rerunning' value was not mapped onto "
        f"the phase that replaced it: {ps.get_current_phase()!r}"
    )

    # --- the stop: the residual row is not a stranded row ------------------
    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    stop_body = stop.json()
    assert stop_body["status"] == "stopped", stop_body
    assert stop_body["paused"] is True, stop_body
    assert stop_body["round"] == 2, stop_body

    parked = _read_phase(db_path, PLAN_ID)
    assert parked == "verification", (
        f"the stop left the routing row at {parked!r} instead of parking it "
        "at 'verification' — a row that still holds the historical "
        "'verification_rerunning' value must be stoppable, or the plan is "
        "stuck at a phase no code knows any more"
    )

    # --- the start: the plan is still resumable ----------------------------
    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, (
        "the start was refused after a legitimate stop — the plan is now "
        f"permanently unrecoverable through the API: {start.status_code} "
        f"{start.text}"
    )
    start_body = start.json()
    assert start_body["resumed"] is True, start_body
    assert start_body["round"] == 2, start_body

    assert stub_auto_loop.reached.wait(timeout=5), "the round never ran"
    assert len(stub_auto_loop) == 1, stub_auto_loop
    assert stub_auto_loop[0]["start_round"] == 2, stub_auto_loop


# ---------------------------------------------------------------------------
# 2) negative control — /start's whitelist was NOT relaxed to match
# ---------------------------------------------------------------------------


def test_start_from_running_phase_without_checkpoint_is_still_409(
    env, client, stub_auto_loop,
):
    """A plan still at ``verification_running`` with no checkpoint is refused.

    The fix widens the STOP's source set. It must not widen ``/start``'s:
    a round that is genuinely in flight must not become re-enterable, or
    two rounds would write the same ``plan_verification`` row at once.

    The ``verification_running`` entry ``/start`` does accept is gated
    on ``_recovered`` — an in-memory marker set only by
    ``_recover_verification_states`` when the owning process died. There
    is no such marker here and no checkpoint, so the start must be
    refused exactly as before.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_running", round_n=2,
        status="running", verdicts=ROUND_VERDICTS,
    )

    # No checkpoint on disk — this is a round in flight, not an
    # interrupted one.
    assert not checkpoint_path(plan_dir).exists()

    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 409, (
        "a genuinely-running round became re-enterable — two rounds would "
        f"now write the same plan_verification row: {start.status_code} "
        f"{start.text}"
    )
    body = start.json()
    assert body["error"] == "conflict", body
    assert body["reason"] in ("stage_mismatch", "version_mismatch"), body

    # And it must not have started anything.
    assert stub_auto_loop.reached.wait(timeout=0.5) is False, (
        "the refused start still entered the auto-loop"
    )
