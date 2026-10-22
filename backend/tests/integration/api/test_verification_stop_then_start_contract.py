"""``stop`` then ``start`` resumes the **same** round — the whole chain.

The two halves of the pause contract live in two different suites:

* ``test_verification_stop_is_pause.py`` pins the **stop** — it parks
  the routing row, writes the ``pause_state.json`` checkpoint and
  answers ``{"status": "stopped", "paused": true, "round": N}``.
* ``test_verification_resume_same_round.py`` pins the **start** — but it
  hand-writes the checkpoint, so it never exercises the one path an
  operator actually takes.

Between them there is a gap: no test drives "stop a round that was
half-way through, then start it again" as one chain. The two suites
can each stay green while the chain between them is broken — e.g. if
the stop wrote a checkpoint the start cannot read, or wrote the wrong
round, the stop suite would still pass (it only looks at the response
and the file) and the start suite would still pass (it writes its own
checkpoint).

What is pinned here is the chain, end to end:

1. **The stop leaves a resumable trace** and names the round it
   interrupted.
2. **The start picks that same round up** — not round N+1. The round
   number is the contract a plan's verdicts, report and repair tasks
   are written against; moving to N+1 would relabel the interrupted
   round's work and start the budget over.
3. **The auto-loop is entered with that round** — the number the
   background chain is asked to run is the observable that ties the
   HTTP answer to the work that actually happens.
4. **The DB row still carries that round** after the start, so the
   verdicts, the report and the progress endpoint all agree on which
   round the plan is on.
5. **The start says it resumed** — ``resumed`` is the only signal a
   client has about which of the two things happened: a resume of the
   interrupted round, or a fresh next round.
6. **The interrupted round's verdicts survive** — in the DB row, in
   ``verification_report.json`` — and the checkpoint is retired, because
   a resume is single-shot.

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

PLAN_ID = "20261021-stop-then-start"

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
    phase: str = "verification_running",
    round_n: int = 2,
    status: str = "running",
    verdicts=None,
    max_rounds: int = 3,
):
    """A plan mid-verification, with a round's worth of verdicts.

    ``phase="verification_running"`` is the phase the stop's CAS
    requires: a plan whose routing row is parked anywhere else gets a
    409 instead of a pause.

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


def _read_verification_row(db_path: Path, plan_id: str) -> dict:
    """The plan's ``plan_verification`` row, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()
    return row or {}


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


def test_stop_then_start_keeps_the_same_round(env, client, stub_auto_loop):
    """Stop a half-finished round, then start it again — same round.

    The round number is the contract a plan's verdicts, report and
    repair tasks are written against. A resume that moved to round N+1
    would relabel the interrupted round's work as round N+1's and start
    the round budget over — so the round must survive the whole
    stop→start chain, in the HTTP answer, in the round the auto-loop
    is asked to run, and in the row the rest of the system reads.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_running", round_n=2,
        status="running", verdicts=ROUND_VERDICTS,
    )

    # --- the stop: the operator interrupts round 2 ------------------------
    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    stop_body = stop.json()
    assert stop_body["status"] == "stopped", stop_body
    assert stop_body["paused"] is True, stop_body
    assert stop_body["round"] == 2, stop_body

    # The stop left a resumable trace behind — that is what makes the
    # next start a resume rather than a fresh round.
    checkpoint_path = plan_dir / "pause_state.json"
    assert checkpoint_path.exists(), "the stop left no checkpoint behind"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["kind"] == "verification", checkpoint
    assert checkpoint["verification_round"] == 2, checkpoint

    # --- the start: the same round is picked up ---------------------------
    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, start.text
    start_body = start.json()
    assert start_body["status"] == "started", start_body
    assert start_body["round"] == 2, (
        "the start moved to another round — the interrupted round's work "
        f"would be relabelled: {start_body}"
    )

    # The background chain is entered with that same round.
    assert stub_auto_loop.reached.wait(timeout=5), "the round never ran"
    assert len(stub_auto_loop) == 1, stub_auto_loop
    assert stub_auto_loop[0]["start_round"] == 2, stub_auto_loop[0]

    # --- the row the rest of the system reads -----------------------------
    row = _read_verification_row(db_path, PLAN_ID)
    assert int(row.get("round") or 0) == 2, row


def test_stop_then_start_reports_resumed(env, client, stub_auto_loop):
    """A start that picks an interrupted round up says so — ``resumed``.

    The HTTP answer is the only signal a client has about *which* of the
    two things just happened: a resume of the round the operator
    interrupted, or the start of a fresh next round. The two look
    identical on the wire unless one of them says "resumed", and a
    client that cannot tell them apart has to guess — and the guess
    ("this is a new round, the old work is gone") is the one that loses
    the operator's half-finished round.

    So the flag is pinned here: stop a round that was in flight, start
    it again, and the answer must say the round was **resumed**, not
    replaced.
    """
    plans_dir, db_path = env
    _plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_running", round_n=2,
        status="running", verdicts=ROUND_VERDICTS,
    )

    # --- the stop: the operator interrupts round 2 ------------------------
    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    assert stop.json()["paused"] is True, stop.json()

    # --- the start: it must name the resume -------------------------------
    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["resumed"] is True, (
        "the start did not say it resumed the interrupted round — a client "
        f"cannot tell a resume from a fresh round: {body}"
    )

    # The background chain runs the round that was resumed.
    assert stub_auto_loop.reached.wait(timeout=5), "the round never ran"


def test_stop_then_start_preserves_verdicts(env, client, stub_auto_loop):
    """The interrupted round's verdicts survive the stop→start chain.

    The work a round had already done is the most expensive thing in the
    plan: each verdict is a VP that was actually judged. A resume that
    threw them away would re-run VPs the operator already paid for, and
    — worse — a resume that threw away a ``FAILED`` verdict would report
    the round as clean.

    So the verdicts must be readable in all three places the rest of the
    system looks for them:

    * the ``plan_verification`` row (``verdicts``) — the executor's own
      record, and the authority;
    * ``verification_report.json`` (``verification_results``) — what the
      repair chain reads;
    * and the checkpoint must be **gone** afterwards: the resume is
      single-shot, so a second start cannot consume the same
      interruption twice.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_running", round_n=2,
        status="running", verdicts=ROUND_VERDICTS,
    )

    # --- the stop ---------------------------------------------------------
    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text

    # --- the start --------------------------------------------------------
    start = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": True},
    )
    assert start.status_code == 200, start.text
    assert start.json()["resumed"] is True, start.json()

    # The assertions read the DB row, so they wait until the round's
    # chain has actually been entered — the row is written by the
    # request path, but a background thread may still be touching it.
    assert stub_auto_loop.reached.wait(timeout=5), "the round never ran"

    # --- the row: the verdicts the executor had already reached ----------
    row = _read_verification_row(db_path, PLAN_ID)
    db_vp_ids = {
        verdict.get("vp_id")
        for verdict in (row.get("verdicts") or [])
        if isinstance(verdict, dict)
    }
    assert db_vp_ids == {"VP-001", "VP-002"}, (
        "the resume lost the interrupted round's verdicts — the VPs that "
        f"were already judged would be re-run: {db_vp_ids}"
    )

    # --- the report: what the repair chain reads --------------------------
    report_path = plan_dir / "verification_report.json"
    assert report_path.exists(), (
        "the resume left no report behind — the repair chain finds no "
        "failed VP to repair"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = report.get("verification_results") or []
    report_vp_ids = {
        entry.get("id") for entry in results if isinstance(entry, dict)
    }
    assert {"VP-001", "VP-002"} <= report_vp_ids, (
        "the report does not carry the interrupted round's verdicts: "
        f"{report_vp_ids}"
    )

    # --- the checkpoint: the resume is single-shot -------------------------
    assert not checkpoint_path(plan_dir).exists(), (
        "the checkpoint is still on disk — a second start would consume "
        "the same interruption again"
    )