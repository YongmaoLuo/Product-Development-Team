"""A hard kill leaves no checkpoint — the row's own status is the trace.

A hard kill (SIGKILL, an OOM, a machine that lost power) gives the
process no chance to write anything on its way out: there is no
``pause_state.json``, no ``verification_report.json``, no "the operator
stopped me" bookkeeping. The only thing left behind is the
``plan_verification`` row, whose ``verification_status`` still says
``running`` (or ``loop_stopped``) and whose ``round`` still names the
round that was in flight.

That row is a trace of an interruption, and it has to be enough on its
own. The next ``POST /api/verification/{id}/start`` must run *that* round
again, not the round after it: the round number is the contract the
plan's verdicts, report and repair tasks are written against, so moving
to round N+1 relabels the work the killed round had already done and
starts the budget over on work that was already half done.

What is pinned here, with the checkpoint deliberately absent:

* a row parked at ``running`` (the process died mid-round) resumes its
  own round;
* a row parked at ``loop_stopped`` resumes its own round;
* a row whose round really finished still advances to the next one.

The in-memory state must carry ``recovered=True`` and no thread: that
is the proof that the process which owned the round is gone. Without it
the "already running" guard answers 409 and the plan is stuck for good.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20261021-hard-kill-resume"

VP_IDS = ["VP-001", "VP-002", "VP-003"]

#: The verdicts the killed round had already reached. The shape is the
#: executor's own (``verification_executor.py`` writes ``vp_id`` /
#: ``status`` / ``reasons`` / ``evidence``).
ROUND_VERDICTS = [
    {
        "vp_id": "VP-001",
        "status": "PASSED",
        "worker_id": "executor",
        "seq": 1,
        "reasons": [],
        "evidence": {"cmd": "pytest -q", "exit_code": 0},
    },
    {
        "vp_id": "VP-002",
        "status": "FAILED",
        "worker_id": "executor",
        "seq": 2,
        "reasons": ["expected 200, got 500"],
        "evidence": {"cmd": "pytest -q tests/test_api.py", "exit_code": 1},
    },
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test plans directory + state.db isolation.

    The plans directory and the state database are both per-test, and
    the in-memory verification state is cleared on the way in and on the
    way out — a leftover entry would make the "already running" guard
    answer 409 for a plan that is not running at all.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
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


class _LoopRecorder(list):
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
    is stubbed out here so no LLM call is made; what it was asked to do
    — in particular which round it was handed — is recorded instead.
    """
    recorder = _LoopRecorder()

    def _fake(plan_id, plan_dir, project_dir, max_rounds=None, tool=None,
              start_round=1):
        recorder.append({"start_round": start_round})
        recorder.reached.set()
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake)
    return recorder


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str,
    round_n: int,
    status: str,
    verdicts=None,
    max_rounds: int = 3,
    reason: str = "process_killed",
):
    """A plan whose verification round was cut short, with no checkpoint.

    The row is left in the state an interrupted round leaves it in:
    ``verification_status`` is ``status`` (``running`` for a process that
    died mid-round, ``loop_stopped`` for one that got that far) and
    ``round`` is the round that was in flight. The verdicts it had
    already reached are still in the row.

    **No ``pause_state.json`` is written** — that is the whole point of
    this suite. The row is the only trace of the interruption.

    A SECOND connection is used (not the fixture's) so the rows the
    endpoint reads back are the committed ones.

    Returns ``(plan_dir, project_dir)``.
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
        if round_n:
            verif.init_round(PLAN_ID, round_n=round_n, max_rounds=max_rounds)
        for verdict in (verdicts if verdicts is not None else ROUND_VERDICTS):
            verif.append_verdict(PLAN_ID, dict(verdict))
        if status:
            verif.mark_stopped(PLAN_ID, reason, status=status)
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


def _recovered_state() -> dict:
    """The in-memory state a hard kill leaves behind.

    ``verification_status`` still says ``running`` — that is what was
    persisted when the backend died. What tells the start endpoint that
    nothing is running any more is the pair ``recovered=True`` and no
    thread: the process that owned the round is gone.
    """
    return {
        "verification_status": "running",
        "recovered": True,
        "thread": None,
    }


# ---------------------------------------------------------------------------
# 1) the row alone resumes the interrupted round
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase,status",
    [("verification_running", "running"), ("verification", "loop_stopped")],
)
def test_hard_kill_row_resumes_same_round(env, client, stub_auto_loop,
                                          phase, status):
    """A killed round is resumed from the row alone — no checkpoint.

    The round does not move: the start runs the round the killed process
    was running, not the round after it. ``resumed`` says so, so a
    client that polls the status can tell a resume from a fresh round.

    Both shapes of the trace are pinned — a row still saying ``running``
    (the process died mid-round) and one saying ``loop_stopped`` (it got
    as far as writing a stop) — because they are the two statuses a
    killed process can leave behind.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase=phase, round_n=2, status=status,
        verdicts=ROUND_VERDICTS,
    )
    # The whole point: there is no checkpoint to lean on.
    assert not (plan_dir / "pause_state.json").exists()

    server._verification_state[PLAN_ID] = _recovered_state()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["round"] == 2, body
    assert body["resumed"] is True, body

    # The round that actually ran is the interrupted one.
    assert stub_auto_loop.reached.wait(timeout=10), "the round never ran"
    assert stub_auto_loop[0]["start_round"] == 2, stub_auto_loop[0]

    # A hard kill leaves no checkpoint, and a resume must not invent one.
    assert not (plan_dir / "pause_state.json").exists()


# ---------------------------------------------------------------------------
# 2) a finished round still advances
# ---------------------------------------------------------------------------


def test_finished_round_still_advances(env, client, stub_auto_loop):
    """A terminal row with no checkpoint → the next round, not a resume.

    The resume rule is narrow on purpose: it fires when the row says
    the round was interrupted (``running`` / ``loop_stopped``). A row
    whose round really finished says something else, and then the start
    must do what it always did — run the next round, and say it is not a
    resume. Widening the rule would re-run finished rounds forever.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_passed", round_n=2,
        status="passed", verdicts=ROUND_VERDICTS, reason="round_completed",
    )
    assert not (plan_dir / "pause_state.json").exists()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["round"] == 3, body
    assert body["resumed"] is False, body

    assert stub_auto_loop.reached.wait(timeout=10), "the round never ran"
    assert stub_auto_loop[0]["start_round"] == 3, stub_auto_loop[0]

    assert not (plan_dir / "pause_state.json").exists()