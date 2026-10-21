"""``POST /api/verification/{plan_id}/start`` resumes the interrupted round.

The endpoint used to always answer "the next round"::

    next_round = current.round + 1

which is right for a plan whose last round really did finish, and wrong
for a plan whose round was *interrupted*. A round that was stopped by
the operator, or killed along with the backend, left its work behind —
the verdicts in the ``plan_verification`` row, the report the repair
chain reads, the VPs that never got a verdict — and the next
``/start`` threw all of it away by moving to round N+1.

An interruption leaves two traces, and this suite pins both:

* a ``kind="verification"`` pause checkpoint (the operator pressed
  stop — see ``test_verification_stop_is_pause.py``), and
* a ``plan_verification`` row whose status is still ``running`` /
  ``loop_stopped`` with a round > 0 (the process was killed before it
  could write anything).

Either one means "resume this round", not "start the next one".

What is pinned here:

1. **The round does not move.** A resumed start runs the interrupted
   round — ``init_round`` receives the round that was interrupted.
2. **The verdict cache is inherited.** The resumed cycle asks the
   orchestrator for ``resume=True``, so the VPs that already reached a
   verdict are not run again.
3. **A finished plan still advances.** No checkpoint and a terminal
   row → the next round, exactly as before.
4. **The checkpoint is single-shot.** The start that honoured it
   retires it.
5. **A hard kill is recoverable.** The routing row parked at
   ``verification_running`` plus a recovered in-memory state must not
   answer 409 forever.
6. **The report is rebuilt.** A killed round never wrote one, so the
   start rebuilds it from the verdicts the DB kept.
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

PLAN_ID = "20261020-resume-same-round"

VP_IDS = ["VP-001", "VP-002", "VP-003"]

#: The verdicts the interrupted round had already reached. The shape is
#: the executor's own (``verification_executor.py`` writes ``vp_id`` /
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
def init_round_calls(monkeypatch):
    """Record every round number ``init_round`` is called with."""
    calls: list[int] = []
    original = VerificationRepository.init_round

    def _spy(self, plan_id, round_n, max_rounds):
        calls.append(int(round_n))
        return original(self, plan_id, round_n=round_n, max_rounds=max_rounds)

    monkeypatch.setattr(VerificationRepository, "init_round", _spy)
    return calls


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
    is stubbed out here so no LLM call is made; what it was asked to do
    is recorded instead.

    The status the plan carried *at the moment the loop was entered* is
    recorded too: the loop refuses to start a round that looks like it
    is already running, so that field is the difference between a
    resume that runs and a resume that silently does nothing.
    """
    calls = _LoopCalls()

    def _fake(plan_id, plan_dir, project_dir, max_rounds=None, tool=None,
              start_round=1):
        state = server._verification_state.get(plan_id) or {}
        calls.append({
            "plan_id": plan_id,
            "max_rounds": max_rounds,
            "tool": tool,
            "start_round": start_round,
            "status_at_call": state.get("verification_status"),
        })
        calls.reached.set()
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake)
    return calls


@pytest.fixture
def stub_orchestrator(monkeypatch):
    """Replace the orchestrator with a recorder.

    Returns ``(calls, reached)`` — the cycle the endpoint asked for, and
    an event that says the cycle has been asked for. The event is what
    makes the assertion race-free: the cycle runs on the verification
    thread, not on the request thread.
    """
    calls: list[dict] = []
    reached = threading.Event()

    class _FakeOrchestrator:
        def __init__(self, plan_dir, project_dir, **kwargs):
            self.plan_dir = plan_dir
            self.project_dir = project_dir

        def start_verification_cycle(self, round_number=1, force=False,
                                     resume=False):
            calls.append({
                "round_number": round_number,
                "force": force,
                "resume": resume,
            })
            reached.set()
            return {"overall_status": "PASSED", "verification_results": []}

        def check_cycle_conditions(self, report, round_number=1):
            return {"status": "passed", "repair_tasks": [], "stop_reason": None}

    monkeypatch.setattr(server, "VerificationOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: None)
    return calls, reached


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "verification",
    round_n: int = 2,
    status: str | None = "loop_stopped",
    verdicts=None,
    max_rounds: int = 3,
):
    """A plan mid-verification, with a round's worth of verdicts.

    ``status=None`` leaves the row in the state a hard kill leaves it
    in: ``running``, with the round and the verdicts it had reached.

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
        if round_n:
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
                    {"id": vp_id, "title": f"检查 {vp_id}"} for vp_id in VP_IDS
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir, project_dir


def _write_checkpoint(plan_dir: Path, project_dir: Path, round_n: int):
    """Write the checkpoint a stop leaves behind."""
    from pause_state import write_pause_checkpoint

    return write_pause_checkpoint(
        plan_dir,
        kind="verification",
        project_dir=str(project_dir),
        reason="user_stopped",
        interrupted_tasks=[{"task_id": "VP-003", "status": "running"}],
        unfinished_tasks=[],
        verification_round=round_n,
    )


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) an interrupted round is resumed, not replaced
# ---------------------------------------------------------------------------


def test_resume_does_not_increment_round(env, client, init_round_calls,
                                         stub_auto_loop):
    """A stop wrote a checkpoint for round 2 → the start runs round 2.

    The round number is the contract the plan's verdicts, report and
    repair tasks are written against. Moving to round 3 would relabel
    the interrupted round's work as round 3's and start the budget over.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, round_n=2)
    _write_checkpoint(plan_dir, project_dir, 2)
    init_round_calls.clear()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    assert init_round_calls == [2], init_round_calls
    body = resp.json()
    assert body["round"] == 2
    assert body["resumed"] is True


def test_resume_passes_resume_true(env, client, stub_orchestrator):
    """A resumed round asks the orchestrator for the verdict cache.

    Round 1 is the interesting case: the old rule (``resume`` only for
    round > 1) would hand a resumed round 1 a clean slate and re-run
    every VP the interrupted round had already judged.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, round_n=1)
    _write_checkpoint(plan_dir, project_dir, 1)

    calls, reached = stub_orchestrator
    resp = client.post(
        f"/api/verification/{PLAN_ID}/start", json={"auto_fix": False},
    )
    assert resp.status_code == 200, resp.text
    assert reached.wait(timeout=10), "the verification cycle never ran"

    assert len(calls) == 1, calls
    assert calls[0]["round_number"] == 1
    assert calls[0]["resume"] is True, calls[0]


def test_fresh_start_increments_round(env, client, init_round_calls,
                                      stub_auto_loop):
    """A plan whose last round really finished still advances.

    No checkpoint, and the row's status is terminal — there is nothing
    to resume, so the next round is the next one.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path, phase="verification_failed", round_n=1,
        status="failed",
    )
    init_round_calls.clear()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    assert init_round_calls == [2], init_round_calls
    body = resp.json()
    assert body["round"] == 2
    assert body["resumed"] is False


def test_resume_consumes_checkpoint(env, client, stub_auto_loop):
    """The checkpoint is honoured once and then retired.

    A resume must be single-shot: a restart loop that re-consumed the
    same interruption would never move past it.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, round_n=2)
    _write_checkpoint(plan_dir, project_dir, 2)
    assert (plan_dir / "pause_state.json").exists()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    assert not (plan_dir / "pause_state.json").exists()


# ---------------------------------------------------------------------------
# 2) a hard kill is recoverable
# ---------------------------------------------------------------------------


def test_start_after_hard_kill_recovers_stage(env, client, init_round_calls,
                                              stub_auto_loop):
    """A plan stranded at ``verification_running`` can be started again.

    The backend was killed mid-round: the routing row is still parked at
    ``verification_running``, the row still says ``running``, and the
    process that owned the round is gone. The recovered in-memory state
    (``recovered=True``, no thread) is the evidence that nothing is
    running — without it the "already running" guard answered 409
    forever and the plan was stuck for good.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path, phase="verification_running", round_n=2,
        status=None,
    )
    server._verification_state[PLAN_ID] = {
        "plan_id": PLAN_ID,
        "verification_status": "running",
        "verification_round": 2,
        "verification_max_rounds": 3,
        "results": {"pytest_summary": "", "llm_findings": "",
                    "performance_metrics": {}},
        "repair_tasks": [],
        "orchestrator": None,
        "thread": None,
        "recovered": True,
    }
    init_round_calls.clear()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    assert init_round_calls == [2], init_round_calls
    body = resp.json()
    assert body["round"] == 2
    assert body["resumed"] is True


def test_resume_after_hard_kill_actually_runs_the_round(env, client,
                                                        stub_auto_loop):
    """A recovered start must reach the round, not be skipped as a duplicate.

    The auto-loop refuses to begin a round that looks like it is already
    running, and the recovered entry does look like that: it says
    ``running`` because that is what was persisted when the backend was
    killed. Left alone, the loop would skip, the start would answer 200,
    and the plan would stay exactly where the crash left it — a resume
    that resumes nothing.
    """
    plans_dir, db_path = env
    _seed_plan(
        plans_dir, db_path, phase="verification_running", round_n=2,
        status=None,
    )
    server._verification_state[PLAN_ID] = {
        "plan_id": PLAN_ID,
        "verification_status": "running",
        "verification_round": 2,
        "verification_max_rounds": 3,
        "results": {"pytest_summary": "", "llm_findings": "",
                    "performance_metrics": {}},
        "repair_tasks": [],
        "orchestrator": None,
        "thread": None,
        "recovered": True,
    }

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text
    assert stub_auto_loop.reached.wait(timeout=10), "the round never ran"

    assert len(stub_auto_loop) == 1, stub_auto_loop
    call = stub_auto_loop[0]
    assert call["start_round"] == 2, call
    assert call["status_at_call"] != "running", (
        "the plan still looked like a running round when the loop was "
        "entered — the loop would have skipped it"
    )


def test_recovery_marks_recovered(env):
    """A state rebuilt at startup is marked as recovered.

    The persisted state says "running" — but the process that was
    running it is gone. The marker is what lets the start endpoint tell
    the two apart, and it is in-memory only: it is never persisted.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, phase="verification_running", round_n=2,
        status=None,
    )

    # The runtime state is what the recovery reads back; the executor
    # is what writes it, so it is seeded here directly.
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE plan_verification SET runtime_state = ? "
            "WHERE plan_id = ?",
            (json.dumps({"pending_vps": ["VP-003"]}), PLAN_ID),
        )
        conn.commit()
    finally:
        conn.close()

    server._recover_verification_states(plans_dir)

    recovered = server._verification_state.get(PLAN_ID)
    assert recovered is not None, "the running plan must be recovered"
    assert recovered.get("recovered") is True
    assert recovered.get("thread") is None


# ---------------------------------------------------------------------------
# 3) the report a killed round never wrote
# ---------------------------------------------------------------------------


def test_resume_rebuilds_report(env, client, stub_auto_loop):
    """A round that was killed leaves no report — the start rebuilds it.

    The verdicts live in the ``plan_verification`` row (the executor
    appends them one VP at a time); the report is written only when a
    round closes normally. Without the rebuild, a plan whose backend
    was killed looks exactly like a plan that had never been verified,
    and the repair chain finds no failures to repair.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, round_n=2)
    _write_checkpoint(plan_dir, project_dir, 2)
    assert not (plan_dir / "verification_report.json").exists()

    resp = client.post(f"/api/verification/{PLAN_ID}/start", json={})
    assert resp.status_code == 200, resp.text

    report = _read_json(plan_dir / "verification_report.json")
    results = report["verification_results"]
    judged = {entry["id"]: entry["status"] for entry in results}
    assert judged == {"VP-001": "PASSED", "VP-002": "FAILED"}, results
    assert report["_metadata"]["current_round"] == 2
