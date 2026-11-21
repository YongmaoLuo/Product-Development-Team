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
  3. ``POST /stop`` — sets the flag, and hands the hard kill to the
     background layer.

(2026-11-20: layer 3 changed shape.  The stop is a process now, not an
instant: the request closes the gate — the cooperative cancel flag — and
answers in milliseconds, and the background supervisor does the killing.
The entry point the background layer calls is
``coding_tool.stop_live_dispatches_for_plan``, reached through the
supervisor's escalated sweep (``stop_supervisor._default_sweep`` →
``_cooperative_stop``).  ``server._cleanup_dead_verification_processes``
is **not** on the stop path: its only production call site is the
watchdog, which is a different job.)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
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
    """The request flips the flag; the background layer does the killing.

    **The contract changed on 2026-11-20.**  The stop is a *process*:
    the request closes the gate — the cooperative cancel flag — and
    answers in milliseconds, while the work that is still running is shut
    down by the background supervisor.  The entry point the background
    layer calls is ``coding_tool.stop_live_dispatches_for_plan`` (the
    per-plan cooperative stop, reached through the supervisor's escalated
    sweep), so that is what this test stubs.

    The guarantee is pinned from both sides:

    * the request sets the cancel flag — the gate is closed the moment
      the operator asks;
    * the request does **not** run the cooperative stop itself (the
      background layer owns that job), and the background layer does run
      it — within a bounded wait, on its own thread.
    """
    from fastapi.testclient import TestClient
    import coding_tool
    import server
    import stop_state
    import stop_supervisor

    plan_id = "plan-stop-endpoint"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    db = tmp_path / "state.db"
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr("server._state_db_path", lambda *_: str(db))
    _seed_running_plan(db, plan_id)

    # A registered sub-agent whose process is still running.  Without it
    # the plan is quiet, the quiet verdict comes first and the escalated
    # sweep — the pass that stops the work — never runs.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = server.sub_agent_registry.register_sub_agent(
        plan_id, "VP-023", 1, scoped_tool=None, pid=proc.pid,
    )

    # The supervisor's own knobs are a deployment's business, not a
    # test's: the escalated sweep runs when the quiet attempt runs out,
    # and that attempt is two minutes long by default.  The route asks
    # the supervisor to start, so the test shortens the attempt through
    # the same entry point the route uses.
    real_start = stop_supervisor.start

    def _fast_start(started_plan_id, *, finalize, scan=None, **kwargs):
        kwargs.setdefault("max_seconds", 0.2)
        kwargs.setdefault("hard_max_seconds", 30.0)
        return real_start(
            started_plan_id, finalize=finalize, scan=scan, **kwargs
        )

    monkeypatch.setattr(stop_supervisor, "start", _fast_start)

    cooperative: list = []

    def _record_cooperative(stopped_plan_id, *args, **kwargs):
        cooperative.append(
            (stopped_plan_id, threading.current_thread().name)
        )
        return []

    monkeypatch.setattr(
        coding_tool, "stop_live_dispatches_for_plan", _record_cooperative,
    )

    try:
        resp = TestClient(server.app).post(f"/api/verification/{plan_id}/stop")

        assert resp.status_code == 200, resp.text
        assert verification_cancel.is_cancelled(plan_id), (
            "the stop must flip the cooperative cancel flag"
        )
        assert not cooperative, (
            "the request thread ran the cooperative stop itself — the "
            "work must be shut down by the background layer, not by the "
            "request that has to answer in milliseconds"
        )

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not cooperative:
            time.sleep(0.05)

        assert cooperative, (
            "the background layer never ran the cooperative stop — the "
            "in-flight work is still running behind the operator's back"
        )
        assert cooperative[0][0] == plan_id, (
            f"the cooperative stop named another plan: {cooperative}"
        )
    finally:
        stop_supervisor._reset_for_tests()
        stop_state._reset_for_tests()
        try:
            server.sub_agent_registry.unregister(plan_id, handle)
        except Exception:  # noqa: BLE001 — the registry is not the test's job
            pass
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    # A later /start clears the flag so the fresh round is not born
    # cancelled.
    verification_cancel.clear(plan_id)
    assert verification_cancel.is_cancelled(plan_id) is False
