"""2026-09-15: ``/stop`` must actually stop the work.

Before this fix the endpoint only CAS'd the routing row to
``verification`` and touched the in-memory status. The verification
thread kept going: the in-flight VP sub-agent stayed alive (tokens + CPU)
and the thread carried on into the judgment / repair / next-round chain,
re-stamping the stage the operator had just parked — which then blocked
the next ``/start`` with ``409 stage_mismatch``.

Three layers are pinned here:

  1. ``verification_cancel`` — the cooperative flag itself;
  2. ``VerificationExecutor._run_single_vp`` — VPs that have not started
     are SKIPPED without invoking the runner, and an attempt interrupted
     by the stop's hard kill is reported SKIPPED (not FAILED: a stopped
     round must not manufacture failures for the operator to triage);
  3. ``POST /stop`` — sets the flag AND hard-kills the in-flight
     sub-agents via ``_cleanup_dead_verification_processes``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock

import pytest

import verification_cancel
from verification_executor import VerificationExecutor


@pytest.fixture(autouse=True)
def _clean_cancel_flags():
    verification_cancel._reset_for_tests()
    yield
    verification_cancel._reset_for_tests()


# ---------------------------------------------------------------------------
# 1) the flag
# ---------------------------------------------------------------------------


def test_cancel_flag_round_trip():
    assert verification_cancel.is_cancelled("p1") is False

    verification_cancel.request_cancel("p1")
    assert verification_cancel.is_cancelled("p1") is True
    assert verification_cancel.cancelled_plans() == ["p1"]

    verification_cancel.clear("p1")
    assert verification_cancel.is_cancelled("p1") is False
    assert verification_cancel.cancelled_plans() == []


def test_cancel_flag_is_per_plan_and_tolerates_empty_ids():
    verification_cancel.request_cancel("p1")

    assert verification_cancel.is_cancelled("p2") is False, (
        "cancelling one plan must not cancel another"
    )
    verification_cancel.request_cancel("")
    verification_cancel.clear("")
    assert verification_cancel.is_cancelled("") is False


# ---------------------------------------------------------------------------
# 2) the executor honours it
# ---------------------------------------------------------------------------


def _executor(plan: Dict[str, Any], plan_dir: Path, runner=None):
    return VerificationExecutor(
        verification_plan=plan,
        plan_id="plan-stop",
        plan_dir=plan_dir,
        sub_agent_runner=runner or Mock(return_value={"status": "PASSED"}),
    )


_VP = {
    "id": "VP-023",
    "title": "Nightly CI 全过",
    "verification_method": "automated_test",
    "test_command": "pytest tests/ -v",
    "expected_result": "exit 0",
    "priority": "high",
}


@pytest.mark.asyncio
async def test_not_started_vp_is_skipped_without_running_the_runner(tmp_path):
    runner = Mock(return_value={"status": "PASSED"})
    ex = _executor({"verification_points": [dict(_VP)]}, tmp_path, runner)
    verification_cancel.request_cancel("plan-stop")

    verdict = await ex._run_single_vp(dict(_VP))

    assert verdict["status"] == "SKIPPED", verdict
    assert "cancelled" in verdict["reasons"][0]
    runner.assert_not_called(), "a stopped round must not start new VP work"


@pytest.mark.asyncio
async def test_interrupted_attempt_reports_skipped_not_failed(tmp_path):
    """The stop's hard kill makes the runner raise; that must surface as
    SKIPPED — otherwise the operator's own stop pollutes the round with a
    fabricated FAILED verdict."""
    def _killed(_vp):
        verification_cancel.request_cancel("plan-stop")
        raise RuntimeError("sub-agent killed by stop")

    ex = _executor({"verification_points": [dict(_VP)]}, tmp_path, _killed)

    verdict = await ex._run_single_vp(dict(_VP))

    assert verdict["status"] == "SKIPPED", verdict
    assert "cancelled" in verdict["reasons"][0]


@pytest.mark.asyncio
async def test_normal_failure_is_still_failed_when_not_cancelled(tmp_path):
    """Regression guard: without a stop, a raising runner stays FAILED."""
    def _boom(_vp):
        raise RuntimeError("real verification crash")

    ex = _executor({"verification_points": [dict(_VP)]}, tmp_path, _boom)

    verdict = await ex._run_single_vp(dict(_VP))

    assert verdict["status"] == "FAILED", verdict


# ---------------------------------------------------------------------------
# 3) the endpoint wires flag + hard kill
# ---------------------------------------------------------------------------


def _seed_running_plan(db_path: Path, plan_id: str) -> None:
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "verification_running")
        VerificationRepository(conn).init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        conn.close()


def test_stop_sets_the_cancel_flag_and_kills_inflight_work(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import server

    plan_id = "plan-stop-endpoint"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    db = tmp_path / "state.db"
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr("server._state_db_path", lambda *_: str(db))
    _seed_running_plan(db, plan_id)

    killed: list = []
    monkeypatch.setattr(
        server, "_cleanup_dead_verification_processes",
        lambda pid, vps: killed.append((pid, sorted(vps))),
    )
    # One registered sub-agent so the kill list is non-empty.
    handle = Mock()
    handle.vp_id = "VP-023"
    monkeypatch.setattr(
        server.sub_agent_registry, "all_handles", lambda pid: [handle],
    )

    resp = TestClient(server.app).post(f"/api/verification/{plan_id}/stop")

    assert resp.status_code == 200, resp.text
    assert verification_cancel.is_cancelled(plan_id), (
        "the stop must flip the cooperative cancel flag"
    )
    assert killed == [(plan_id, ["VP-023"])], (
        f"the in-flight sub-agent must be hard-killed; got {killed}"
    )

    # A later /start clears the flag so the fresh round is not born
    # cancelled.
    verification_cancel.clear(plan_id)
    assert verification_cancel.is_cancelled(plan_id) is False
