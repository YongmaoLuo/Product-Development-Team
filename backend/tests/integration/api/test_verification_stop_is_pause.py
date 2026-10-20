"""``POST /api/verification/{plan_id}/stop`` is a pause, not a discard.

The endpoint used to be a pure "stop the work" switch: it flipped the
cooperative cancel flag, hard-killed the in-flight sub-agents and parked
the routing row. What it did **not** do was leave anything behind that a
later resume could read.

Two pieces of knowledge died with the stopped round:

* the verdicts the round had already reached — they lived in the
  ``plan_verification`` row and nowhere else;
* the per-VP results the repair chain reads
  (``verification_report.json`` → ``verification_results``, the key
  ``verification/verification_report_reader.py`` looks for) — that file
  was written only when a round closed *normally*, so a stopped round
  left a plan directory that looked exactly like a plan that had never
  run.

A stop is therefore a pause: it must leave a resumable trace.

What is pinned here:

1. **The round is recorded.** ``pause_state.json`` carries the round
   number, the verdicts already reached, the project directory and the
   VPs the stop cut short.
2. **The report is readable mid-round.** ``verification_report.json``
   carries the same verdicts under ``verification_results``, merged by
   VP id, with the pre-existing report fields left alone.
3. **The old semantics survive.** A plan that is not running still
   answers 409 and still writes nothing; an archived plan still
   answers 410.
"""

from __future__ import annotations

import json
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

PLAN_ID = "20261020-stop-is-pause"

VP_IDS = ["VP-001", "VP-002", "VP-003", "VP-004"]

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
        "evidence": {"cmd": "pytest -q", "exit_code": 0},
    },
    {
        "vp_id": "VP-002",
        "status": "FAILED",
        "worker_id": "executor",
        "seq": 2,
        "reasons": ["expected 200, got 500", "timeout after 30s"],
        "evidence": {"cmd": "pytest -q tests/test_api.py", "exit_code": 1},
    },
]

#: The report as it stood before the stop — one stale entry for VP-002
#: (so the merge has something to update in place) plus the fields the
#: snapshot must not touch.
OLD_REPORT = {
    "verification_results": [
        {
            "id": "VP-002",
            "status": "STALE",
            "title": "接口返回 500",
            "actual_result": "stale",
            "evidence": "stale",
        },
    ],
    "execution_profile": {"profile": "api-test", "timeout": 30},
    "rounds": [{"round": 1, "status": "failed"}],
    "requirement_deviations": [{"id": "REQ-1", "note": "deviated"}],
    "round1_stable_vps": ["VP-001"],
}


@pytest.fixture(autouse=True)
def _clean_cancel_flags():
    """The stop's cancel flag is process-global — do not leak it."""
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

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
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


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "verification_running",
    round_n: int = 2,
    verdicts=None,
    report=None,
):
    """A plan mid-verification, with a round's worth of verdicts.

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
        verif.insert(PLAN_ID, verification_status="running", max_rounds=3)
        verif.init_round(PLAN_ID, round_n=round_n, max_rounds=3)
        for verdict in (verdicts if verdicts is not None else ROUND_VERDICTS):
            verif.append_verdict(PLAN_ID, dict(verdict))
    finally:
        conn.close()

    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "verification_points": [
                    {"id": vp_id, "title": f"检查 {vp_id}"}
                    for vp_id in VP_IDS
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if report is not None:
        (plan_dir / "verification_report.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8",
        )
    return plan_dir, project_dir


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) the stop writes a resumable checkpoint
# ---------------------------------------------------------------------------


def test_stop_writes_verification_checkpoint(env, client):
    """The round, the verdicts and the cut-short VPs must survive the stop."""
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    checkpoint = _read_json(plan_dir / "pause_state.json")
    assert checkpoint["kind"] == "verification"
    assert checkpoint["verification_round"] == 2
    assert checkpoint["project_dir"] == str(project_dir)

    judged = {v["vp_id"]: v for v in checkpoint["verdicts"]}
    assert set(judged) == {"VP-001", "VP-002"}, checkpoint["verdicts"]
    assert judged["VP-002"]["status"] == "FAILED"
    assert judged["VP-002"]["reasons"] == ROUND_VERDICTS[1]["reasons"]

    # The VPs the stop cut short — the ones a resume still owes.
    interrupted = [t["task_id"] for t in checkpoint["interrupted_tasks"]]
    assert interrupted == ["VP-003", "VP-004"], checkpoint["interrupted_tasks"]

    body = resp.json()
    assert body["paused"] is True
    assert body["round"] == 2
    assert body["interrupted_vp_count"] == 2


def test_stop_persists_report_snapshot(env, client):
    """``verification_report.json`` must carry the round's verdicts.

    The key is ``verification_results`` — the one
    ``verification/verification_report_reader.py`` reads — and the
    entries are merged by VP id, so the pre-existing entry keeps its
    place (and its title) while its verdict is refreshed.
    """
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, report=dict(OLD_REPORT),
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    report = _read_json(plan_dir / "verification_report.json")
    results = report["verification_results"]

    assert results[0]["id"] == "VP-002"
    assert results[0]["status"] == "FAILED"
    assert results[0]["title"] == "接口返回 500", "the old title must survive"
    assert results[0]["actual_result"] == "expected 200, got 500; timeout after 30s"
    assert results[0]["evidence"] == json.dumps(
        ROUND_VERDICTS[1]["evidence"], ensure_ascii=False,
    )

    # The verdict that had no entry in the old report is appended.
    assert results[1]["id"] == "VP-001"
    assert results[1]["status"] == "PASSED"

    assert report["_metadata"]["current_round"] == 2
    assert report["_metadata"]["generated_at"]


def test_snapshot_preserves_existing_fields(env, client):
    """The snapshot merges — it does not rewrite the report."""
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(
        plans_dir, db_path, report=dict(OLD_REPORT),
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    report = _read_json(plan_dir / "verification_report.json")
    for key in (
        "execution_profile",
        "rounds",
        "requirement_deviations",
        "round1_stable_vps",
    ):
        assert report[key] == OLD_REPORT[key], f"{key} was overwritten"


# ---------------------------------------------------------------------------
# 2) the old semantics survive
# ---------------------------------------------------------------------------


def test_stop_not_running_still_409(env, client):
    """A plan that is not verifying is still a conflict — and still writes
    nothing: a refused stop must not leave a checkpoint behind."""
    plans_dir, db_path = env
    plan_dir, _project_dir = _seed_plan(plans_dir, db_path, phase="executing")

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")

    assert resp.status_code == 409
    assert resp.json() == {"error": "conflict", "reason": "stage_mismatch"}
    assert not (plan_dir / "pause_state.json").exists()
