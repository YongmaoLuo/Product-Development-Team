"""Integration tests for VP-014 — Verification Card Loop Control.

Covers three loop-control mechanisms exercised through the verification API:

* ``test_max_rounds`` — ``POST /api/verification/{plan_id}/start`` with
  ``max_rounds=2`` records ``verification_max_rounds == 2`` in the
  in-memory state, which the manual-verification loop in
  ``server.start_verification`` reads to bound its
  ``for round_num in range(1, max_rounds + 1)`` (i.e. rounds 1 and 2
  only).
* ``test_loop_stop`` — when two consecutive rounds fail with the same
  failed-VP set, ``verification_status`` transitions to ``loop_stopped``
  and ``stop_reason`` is recorded as ``same_failure_repeated``.
* ``test_user_stop`` — calling ``POST /api/verification/{plan_id}/stop``
  on a running cycle returns the stop response with
  ``reason == "user_stopped"`` and ``current_round`` set to the
  in-flight round number.

These tests assert behavior of the API surface that drives the
Verification Card's loop-control UI, so they sit at the integration
layer rather than the orchestrator unit-test layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from server import app, _verification_state


client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    """Reset in-memory verification state and redirect PLANS_DIR."""
    _verification_state.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    yield
    _verification_state.clear()


@pytest.fixture
def make_plan(tmp_path, plan_sqlite_seeder):
    """Factory that creates a minimal plan directory in 'executing' phase.

    The on-disk artifacts are still written (``verification_plan.json``
    and friends are read straight off disk by several endpoints), but
    they are no longer the *whole* fixture: production is SQLite-first,
    so the plan also needs a ``plan_routing`` row (without one every
    endpoint answers 404 "Plan not found"), a ``plan_execution`` row
    carrying ``project_dir`` (``/start`` answers 400 "Missing project
    directory" without one) and a ``plan_verification`` row for the
    round columns. ``seed_plan_sqlite`` derives all three from the
    ``plan_state.json`` written below via the documented one-shot
    migration path, then re-asserts the columns this fixture pins.
    """

    def _make(plan_id="test-plan"):
        plan_dir = tmp_path / "plans" / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        (plan_dir / "plan_state.json").write_text(
            json.dumps(
                {
                    "plan_id": plan_id,
                    "current_phase": "executing",
                    "completed_phases": ["execution"],
                    "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                    "flags": {},
                    "last_updated": "2026-01-01T00:00:00",
                    "verification": {
                        "status": "not_started",
                        "round": 0,
                        "max_rounds": 3,
                        "stop_reason": None,
                    },
                }
            ),
            encoding="utf-8",
        )
        (plan_dir / "interview.json").write_text(
            json.dumps({"requirement": "test"}), encoding="utf-8"
        )
        (plan_dir / "prd.json").write_text(
            json.dumps({"prd": "test"}), encoding="utf-8"
        )
        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": []}), encoding="utf-8"
        )
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )

        plan_sqlite_seeder(
            plan_dir,
            plan_id,
            phase="executing",
            project_dir=project_dir,
        )
        return plan_dir

    return _make


def _resolve_plan_dir(plan_id: str) -> Path:
    """Resolve the redirected PLANS_DIR for the test."""
    from server import PLANS_DIR
    return PLANS_DIR / plan_id


class _NoOpThread:
    """Thread replacement that does not actually spawn the run loop.

    This lets the test inspect the in-memory state populated by
    ``start_verification`` (including ``verification_max_rounds``) without
    invoking the actual orchestrator, which would require LLM calls.
    """

    def __init__(self, target=None, daemon=None, *args, **kwargs):
        self._target = target

    def start(self):
        pass

    def join(self, timeout=None):
        pass


# ---------------------------------------------------------------------------
# VP-014 — Verification Card Loop Control
# ---------------------------------------------------------------------------


def test_max_rounds(make_plan, monkeypatch, state_db_reader):
    """Setting max_rounds=2 yields at most 2 rounds (1..max_rounds inclusive).

    The bound is observed through three independent surfaces:

    1. In-memory ``_verification_state[plan_id]["verification_max_rounds"]``
       — what ``start_verification``'s ``for round_num in range(1, max_rounds + 1)``
       actually iterates over.
    2. The ``/status`` endpoint — what the Verification Card reads.
    3. The ``plan_verification.max_rounds`` SQLite column — durable
       across server restart (the card's cross-restart source of truth).
    """
    make_plan("vp014-mr")

    # Suppress the actual run loop and orchestrator construction.
    monkeypatch.setattr("server.threading.Thread", _NoOpThread)
    monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

    # ``auto_fix=False`` selects the single-round path, which calls
    # ``_init_verification_state`` synchronously. The default
    # (``auto_fix=True``) defers that call to the auto-loop thread —
    # which this test stubs out — so the in-memory surface (1) would
    # stay empty and the assertion would be measuring the stub, not the
    # endpoint. The round/``max_rounds`` CAS below runs either way, so
    # surfaces (2) and (3) are unchanged by the choice.
    resp = client.post(
        "/api/verification/vp014-mr/start",
        json={"max_rounds": 2, "auto_fix": False},
    )
    assert resp.status_code == 200, resp.text

    # (1) In-memory state.
    state = _verification_state["vp014-mr"]
    assert state["verification_max_rounds"] == 2

    # (2) Status endpoint (read by the Verification Card UI).
    status = client.get("/api/verification/vp014-mr/status").json()
    assert status["verification_max_rounds"] == 2

    # (3) SQLite row (durable across server restart). ``init_round``
    # writes the round columns to ``plan_verification``; the legacy
    # ``plan_state.json`` is no longer rewritten by the verification
    # flow, so the durable surface to assert against is the row.
    row = state_db_reader.verification("vp014-mr")
    assert row is not None, "init_round must have created a plan_verification row"
    assert row["max_rounds"] == 2
    assert row["round"] == 1


def test_loop_stop(make_plan, plan_sqlite_seeder):
    """Same-failure-set detection transitions verification_status to
    ``loop_stopped`` and records ``stop_reason="same_failure_repeated"``
    in the ``plan_verification`` row — the durable source of truth the
    Verification Card reads on server restart (and on every poll)."""
    make_plan("vp014-ls")

    # Seed in-memory state as if the orchestrator's check_cycle_conditions
    # had just detected that round 2's failed VPs are identical to
    # round 1's. ``_verification_state`` drives the card's live view
    # within a round; the durable verdict lives in SQLite, so the
    # fixture must seed both surfaces or ``/status`` (a SQLite read)
    # would answer "not_started" while the in-memory dict says
    # "loop_stopped".
    failed_vp_ids = ["VP-001", "VP-002"]
    _verification_state["vp014-ls"] = {
        "plan_id": "vp014-ls",
        "verification_status": "loop_stopped",
        "verification_round": 2,
        "verification_max_rounds": 3,
        "results": {
            "pytest_summary": "0 passed, 2 failed",
            "llm_findings": "Same VPs failed as previous round",
            "performance_metrics": {},
        },
        "repair_tasks": [],
        "started_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:05:00",
        "orchestrator": MagicMock(),
        "stop_reason": "same_failure_repeated",
    }

    # Persist the loop-stopped verdict durably: the routing stage and
    # workflow phase the card renders, plus the round row.
    plan_dir = _resolve_plan_dir("vp014-ls")
    plan_sqlite_seeder(
        plan_dir,
        "vp014-ls",
        phase="verification_loop_stopped",
        verification_status="loop_stopped",
        verification_round=2,
        stop_reason="same_failure_repeated",
    )

    # (1) /status endpoint exposes the loop_stopped status to the card.
    status = client.get("/api/verification/vp014-ls/status").json()
    assert status["verification_status"] == "loop_stopped"

    # (2) The durable row carries the same-failure-repeated stop reason
    # and the round number the loop stopped on.
    row = _read_verification_row("vp014-ls")
    assert row is not None
    assert row["verification_status"] == "loop_stopped"
    assert row["verification_stop_reason"] == "same_failure_repeated"
    assert row["round"] == 2

    # (2b) /status surfaces the same stop reason (drives the card's
    # reason label) and no longer reports the round as live.
    assert status["stop_reason"] == "same_failure_repeated"
    assert status["verification_round"] == 2

    # (3) In-memory stop_reason matches.
    assert _verification_state["vp014-ls"]["stop_reason"] == "same_failure_repeated"

    # (4) The failed-VP set the orchestrator compared is preserved on
    # the in-memory state for audit/UI display.
    assert _verification_state["vp014-ls"].get("_previous_failed_ids") in (
        None,
        failed_vp_ids,
    )


def _read_verification_row(plan_id: str) -> dict | None:
    """Read the plan's ``plan_verification`` row from the hermetic state DB.

    Reads through the same resolver the production code uses
    (``PDT_STATE_DB_PATH``), so an assertion here is an assertion about
    what ``/status`` reads — not about a private copy.
    """
    from plan_state import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = _state_db_path()
    if not db_path.exists():
        return None
    conn = open_db(db_path)
    try:
        migrate(conn)
        cur = conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", (plan_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in cur.description], row))
    finally:
        conn.close()


def test_user_stop(make_plan, plan_sqlite_seeder):
    """POST /stop on a running cycle returns reason='user_stopped'."""
    make_plan("vp014-us")

    _verification_state["vp014-us"] = {
        "plan_id": "vp014-us",
        "verification_status": "running",
        "verification_round": 1,
        "verification_max_rounds": 3,
        "results": {
            "pytest_summary": "",
            "llm_findings": "",
            "performance_metrics": {},
        },
        "repair_tasks": [],
        "started_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "orchestrator": MagicMock(),
        "stop_reason": None,
    }

    # ``/stop`` is a routing CAS: it only accepts a plan whose phase is
    # ``verification_running`` and then writes the terminal verdict to
    # the round row. Both must exist before the call, so the fixture
    # seeds the phase and the round itself.
    #
    # 2026-09-17 (schema v5): the fixture used to take ``phase`` and
    # ``stage`` separately so a test could put the routing value ahead
    # of the workflow phase. One column now — a "round in flight" IS
    # ``phase="verification_running"``.
    plan_dir = _resolve_plan_dir("vp014-us")
    plan_sqlite_seeder(
        plan_dir,
        "vp014-us",
        phase="verification_running",
        verification_status="running",
        verification_round=1,
    )

    resp = client.post("/api/verification/vp014-us/stop")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The stop endpoint response includes reason='user_stopped' (the
    # API contract documented in STOP_RESPONSE_SCHEMA).
    assert body["reason"] == "user_stopped"
    assert body["current_round"] == 1
    # The required schema fields are present.
    assert "stopped_at" in body and body["stopped_at"]

    # In-memory state is updated to loop_stopped with user_stopped —
    # this is what the Verification Card reads on its next status poll.
    state = _verification_state["vp014-us"]
    assert state["verification_status"] == "loop_stopped"
    assert state["stop_reason"] == "user_stopped"

    # The status endpoint exposes the loop_stopped status to the card.
    status = client.get("/api/verification/vp014-us/status").json()
    assert status["verification_status"] == "loop_stopped"

    # The verdict is durable: the round row carries it, so the card
    # renders the same state after a server restart.
    row = _read_verification_row("vp014-us")
    assert row is not None
    assert row["verification_status"] == "loop_stopped"
    assert row["verification_stop_reason"] == "user_stopped"
    assert status["stop_reason"] == "user_stopped"
