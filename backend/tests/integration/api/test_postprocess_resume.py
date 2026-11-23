"""``POST /api/verification/{id}/postprocess/resume`` — the missing exit.

Background
----------
``postprocess_failed`` is a **parking** phase: the finishing stage
(收尾阶段) failed a required item, and a human decides whether to run
the stage again.  The phase graph has always allowed
``postprocess_failed → post_processing``, but nothing could take that
edge — one finishing failure and the plan was a dead end.

What is pinned here
-------------------
1. **The phase gate.** Only ``postprocess_failed`` may be resumed.
   Any other phase is a 409 that names the current phase — and nothing
   is spent, nothing is started.
2. **The happy path.** A plan parked at ``postprocess_failed`` answers
   200, spends the first unit of the budget (``retry_index == 1``),
   moves the plan to ``post_processing`` and starts the stage.
3. **The cap.** Two resumes are allowed; the third is refused with a
   409 whose answer carries ``max_retries`` — and the budget file still
   says 2, because a refused resume spends nothing.

Nothing here runs the stage: ``verification_loop.resume_postprocess`` is
stubbed, so no thread is spawned and no command is executed.  The
plan's directory and the state database are both temporary — the real
``plans/`` tree and the real ``state.db`` are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import verification_loop
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import RoutingRepository


PLAN_ID = "20261122-postprocess-resume"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test plans directory + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def resumes(monkeypatch):
    """Record the resumes instead of running the finishing stage."""
    calls = []

    def _fake(plan_id):
        calls.append(plan_id)

    monkeypatch.setattr(verification_loop, "resume_postprocess", _fake)
    return calls


def _seed_plan(plans_dir: Path, db_path: Path, *, phase: str) -> Path:
    """A plan whose routing row sits at ``phase``."""
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, phase)
    finally:
        conn.close()
    return plan_dir


def _read_phase(db_path: Path) -> str:
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(PLAN_ID)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _park_again(db_path: Path) -> None:
    """Put the plan back at ``postprocess_failed``.

    This is what the finishing stage itself does when a resumed run
    fails again (``_settle_postprocess_phase``): the plan is parked a
    second time, and a human may try again — until the budget runs out.
    """
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            ("postprocess_failed", PLAN_ID),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_plan_that_is_not_parked_cannot_be_resumed(
    env, client, resumes,
):
    """Only ``postprocess_failed`` may be resumed — anything else is 409."""
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="post_processing")

    response = client.post(f"/api/verification/{PLAN_ID}/postprocess/resume")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "postprocess_failed" in detail
    assert response.json()["current_phase"] == "post_processing"
    assert resumes == []
    # A refused resume spends nothing and starts nothing.
    assert not (plans_dir / PLAN_ID / "postprocess_retries.json").exists()


def test_a_parked_plan_is_resumed_and_spends_one_retry(
    env, client, resumes,
):
    """The happy path: 200, the plan moves, the budget is spent."""
    plans_dir, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path, phase="postprocess_failed")

    response = client.post(f"/api/verification/{PLAN_ID}/postprocess/resume")

    assert response.status_code == 200
    body = response.json()
    assert body["plan_id"] == PLAN_ID
    assert body["resumed"] is True
    assert body["retry_index"] == 1
    assert body["retries_left"] == 1
    assert body["phase"] == "post_processing"

    # The plan left the parking phase.
    assert _read_phase(db_path) == "post_processing"

    # The stage was started — through the stubbed seam.
    assert resumes == [PLAN_ID]

    # The budget says one unit was spent.
    budget = json.loads(
        (plan_dir / "postprocess_retries.json").read_text(encoding="utf-8")
    )
    assert budget["retries_used"] == 1


def test_the_third_resume_is_refused(env, client, resumes):
    """Two resumes are allowed; the third one is refused by the cap."""
    plans_dir, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path, phase="postprocess_failed")

    first = client.post(f"/api/verification/{PLAN_ID}/postprocess/resume")
    assert first.status_code == 200
    assert first.json()["retry_index"] == 1
    _park_again(db_path)

    second = client.post(f"/api/verification/{PLAN_ID}/postprocess/resume")
    assert second.status_code == 200
    assert second.json()["retry_index"] == 2
    assert second.json()["retries_left"] == 0
    _park_again(db_path)

    third = client.post(f"/api/verification/{PLAN_ID}/postprocess/resume")

    assert third.status_code == 409
    body = third.json()
    assert body["max_retries"] == 2
    assert body["retries_used"] == 2
    assert "max_retries" in body["detail"]
    assert "人工介入" in body["detail"]

    # A refused resume spends nothing and starts nothing.
    budget = json.loads(
        (plan_dir / "postprocess_retries.json").read_text(encoding="utf-8")
    )
    assert budget["retries_used"] == 2
    assert len(resumes) == 2


def test_an_unknown_plan_is_a_404(env, client, resumes):
    """No plan, no resume."""
    plans_dir, db_path = env

    response = client.post(
        f"/api/verification/{PLAN_ID}/postprocess/resume"
    )

    assert response.status_code == 404
    assert "没有这个 plan" in response.json()["detail"]
    assert resumes == []
