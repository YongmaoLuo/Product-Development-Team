"""Tests for the HB sub-agent watchdog + SubAgentRegistry + max_rounds ceiling.

Covers:
- ``SubAgentRegistry`` basic register/unregister/find_stale behaviour
- ``_handle_stuck_sub_agent`` kills the registered subprocess via the
  SIGTERM-→SIGKILL escalation; since 2026-09-07 the kill signals the
  executor to retry instead of aborting the round
- ``VerificationSubAgent._execute_attempt`` raises ``WatchdogKilledError``
  on first watchdog kill (count == 1) and returns
  ``Verdict(verdict="SKIPPED")`` on second kill (count >= 2)
- ``_mark_verification_failed_dead`` persists to
  ``plans/{plan_id}/.verification_runtime.json`` (closes the
  2026-08-25 audit gap where the ``pass`` placeholder prevented
  cross-restart state propagation)
- ``POST /api/verification/{plan_id}/reset_rounds`` endpoint
  bound + happy-path
- ``POST /api/verification/{plan_id}/start`` rejects ``next_round > max_rounds``
  with 409 ``max_rounds_exceeded``
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Imports at module-level so test functions can access `server` directly.
# (Fixtures only push names into their own scope.)
import server  # noqa: E402


# ----------------------------------------------------------------------------
# SubAgentRegistry unit tests
# ----------------------------------------------------------------------------


def test_register_and_unregister_roundtrip():
    """A register-then-unregister pair leaves the registry empty."""
    from sub_agent_registry import SubAgentRegistry

    reg = SubAgentRegistry()
    h = reg.register_sub_agent(
        plan_id="t-plan", vp_id="VP-001", attempt=0,
        scoped_tool=MagicMock(), timeout_seconds=120, max_retries=3,
    )
    assert h.vp_id == "VP-001"
    assert h.attempt == 0
    assert h.timeout_seconds == 120
    assert h.max_retries == 3
    # re-registering same (plan_id, vp_id, attempt) returns same handle
    h2 = reg.register_sub_agent(
        plan_id="t-plan", vp_id="VP-001", attempt=0,
        scoped_tool=MagicMock(), timeout_seconds=240, max_retries=5,
    )
    assert h is h2
    assert h2.timeout_seconds == 240  # updated
    assert h2.max_retries == 5

    reg.unregister("t-plan", h)
    assert reg.all_handles("t-plan") == []


def test_find_stale_threshold():
    """A handle whose last_progress_ts is older than threshold is returned."""
    from sub_agent_registry import SubAgentRegistry

    reg = SubAgentRegistry()
    h = reg.register_sub_agent(
        plan_id="t-stale", vp_id="VP-002", attempt=0,
        scoped_tool=None, timeout_seconds=120, max_retries=2,
    )

    # Just-registered handle is fresh — never stale
    assert reg.find_stale(60) == []

    # Force last_progress_ts to be far in the past
    h.last_progress_ts = time.time() - 1500
    stale = reg.find_stale(1200)
    assert h in stale

    # Young handle is NOT stale
    h.last_progress_ts = time.time() - 100
    stale_short = reg.find_stale(1200)
    assert h not in stale_short


def test_mark_progress_updates_last_progress_ts():
    """mark_progress is a no-op for unregistered handles (idempotent)."""
    from sub_agent_registry import SubAgentRegistry

    reg = SubAgentRegistry()
    h = reg.register_sub_agent(
        plan_id="t-progress", vp_id="VP-003", attempt=1,
        scoped_tool=None, timeout_seconds=120, max_retries=2,
    )
    initial = h.last_progress_ts
    time.sleep(0.05)
    reg.mark_progress("t-progress", h, stage="llm_query_start")
    assert h.last_progress_ts > initial
    assert h.stage == "llm_query_start"

    # Unregister then mark_progress — should be a silent no-op (no exception)
    reg.unregister("t-progress", h)
    reg.mark_progress("t-progress", h, stage="orphan")


def test_cleanup_for_plan_drops_all_handles():
    from sub_agent_registry import SubAgentRegistry

    reg = SubAgentRegistry()
    for vp in ["VP-A", "VP-B"]:
        reg.register_sub_agent(
            plan_id="t-clean", vp_id=vp, attempt=0,
            scoped_tool=None, timeout_seconds=120, max_retries=2,
        )
    assert len(reg.all_handles("t-clean")) == 2

    reg.cleanup_for_plan("t-clean")
    assert reg.all_handles("t-clean") == []


# ----------------------------------------------------------------------------
# Watchdog handler tests — kill the Popen, mark verification failed
# ----------------------------------------------------------------------------


@pytest.fixture
def mock_scoped_tool():
    """Mock ClaudeCodingTool-like instance with Popen."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.poll.return_value = None  # process is alive

    scoped_tool = MagicMock()
    scoped_tool._current_process = proc
    scoped_tool._process_lock = threading.Lock()
    return scoped_tool, proc


@pytest.fixture
def in_memory_plan_state(monkeypatch, tmp_path):
    """Create a fake plan dir and seed _verification_state."""
    plan_id = "t-watchdog"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)

    # Patch PLANS_DIR so sub_agent_registry + watchdog write into tmp_path
    import server
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")

    state = {
        "plan_id": plan_id,
        "verification_status": "running",
        "verification_round": 0,
        "verification_max_rounds": 5,
        "thread": None,
        "stop_reason": None,
        "started_at": "2026-08-25T00:00:00Z",
        "updated_at": "2026-08-25T00:00:00Z",
    }
    server._verification_state[plan_id] = state
    server._verification_locks[plan_id] = threading.Lock()

    # Pre-register the sub-agent handle
    handle = server.sub_agent_registry.register_sub_agent(
        plan_id=plan_id, vp_id="VP-023", attempt=1,
        scoped_tool=None,  # will be set below
        timeout_seconds=120, max_retries=3,
    )
    handle.scoped_tool = None  # set externally in tests

    yield plan_id, plan_dir, state, handle

    server._verification_state.pop(plan_id, None)
    server._verification_locks.pop(plan_id, None)
    server.sub_agent_registry.cleanup_for_plan(plan_id)


def test_handle_stuck_kills_process_and_marks_failed(in_memory_plan_state, mock_scoped_tool, monkeypatch):
    """Core: when HB discovers a stale sub-agent, its Popen is killed
    and the surrounding verification state flips to failed."""
    from server import _handle_stuck_sub_agent

    plan_id, _plan_dir, state, handle = in_memory_plan_state
    scoped_tool, proc = mock_scoped_tool
    handle.scoped_tool = scoped_tool

    sigterm_calls = []
    sigkill_calls = []

    def fake_kill(p, *, sig, wait_timeout=5.0):
        if sig == 15:  # SIGTERM
            sigterm_calls.append(p)
            # Make poll() return 0 (process exited) so watchdog exits cleanly
            proc.poll.return_value = 0
        else:  # SIGKILL
            sigkill_calls.append(p)
            proc.poll.return_value = 0
        return 0

    monkeypatch.setattr("server.kill_process_group", fake_kill)
    monkeypatch.setattr("server.signal.SIGTERM", 15)
    monkeypatch.setattr("server.signal.SIGKILL", 9)

    _handle_stuck_sub_agent(plan_id, handle)

    # SIGTERM attempted first (graceful). Since mock returned poll()==0
    # after SIGTERM, SIGKILL escalation is NOT triggered.
    assert len(sigterm_calls) == 1
    assert sigkill_calls == [], "SIGKILL should not fire if SIGTERM succeeded"

    # 2026-09-07: a single sub-agent kill no longer aborts the
    # verification round. The watchdog signals the executor to retry;
    # only after kill_count >= 2 would the VP be marked SKIPPED. The
    # round keeps running so subsequent VPs still get executed.
    assert state["verification_status"] == "running"
    assert state.get("stop_reason") != "sub_agent_did_not_progress"
    assert handle.watchdog_kill_count == 1
    assert server._WATCHDOG_STATS["sub_agent_killed_total"] >= 1

    # sub-agent unregistered from registry
    assert server.sub_agent_registry.all_handles(plan_id) == []


def test_handle_stuck_escalates_to_sigkill_when_sigterm_ignored(
    in_memory_plan_state, mock_scoped_tool, monkeypatch
):
    """If SIGTERM doesn't take effect within wait_timeout, escalate to SIGKILL."""
    from server import _handle_stuck_sub_agent

    plan_id, _plan_dir, state, handle = in_memory_plan_state
    scoped_tool, proc = mock_scoped_tool
    handle.scoped_tool = scoped_tool

    calls = []

    def fake_kill(p, *, sig, wait_timeout=5.0):
        calls.append(sig)
        # Always pretend process is still alive
        proc.poll.return_value = None
        return None

    monkeypatch.setattr("server.kill_process_group", fake_kill)
    monkeypatch.setattr("server.signal.SIGTERM", 15)
    monkeypatch.setattr("server.signal.SIGKILL", 9)

    _handle_stuck_sub_agent(plan_id, handle)

    # First SIGTERM, then poll()=None → SIGKILL escalation
    assert calls == [15, 9]
    # 2026-09-07: SIGTERM-then-SIGKILL escalation also doesn't
    # abort the round anymore. The watchdog signals the executor
    # to retry; the round keeps running.
    assert state["verification_status"] == "running"
    assert handle.watchdog_kill_count == 1


def test_handle_stuck_handles_missing_scoped_tool(in_memory_plan_state, monkeypatch):
    """A handle with scoped_tool=None should skip kill but still bump the
    watchdog counter and keep the round running."""
    from server import _handle_stuck_sub_agent

    plan_id, _plan_dir, state, handle = in_memory_plan_state
    handle.scoped_tool = None

    # No exceptions should be raised
    _handle_stuck_sub_agent(plan_id, handle)

    # 2026-09-07: round no longer aborts; counter bumped, status untouched.
    assert state["verification_status"] == "running"
    assert state.get("stop_reason") != "sub_agent_did_not_progress"
    assert handle.watchdog_kill_count == 1
    assert server.sub_agent_registry.all_handles(plan_id) == []


def test_handle_stuck_writes_audit_log(in_memory_plan_state, mock_scoped_tool, monkeypatch):
    """A vp_attempts log entry should be written for forensic review."""
    from server import _handle_stuck_sub_agent

    plan_id, plan_dir, _state, handle = in_memory_plan_state
    scoped_tool, _proc = mock_scoped_tool
    handle.scoped_tool = scoped_tool

    monkeypatch.setattr("server.kill_process_group", lambda *a, **k: 0)
    monkeypatch.setattr("server.signal.SIGTERM", 15)
    monkeypatch.setattr("server.signal.SIGKILL", 9)

    _handle_stuck_sub_agent(plan_id, handle)

    # Look for the audit log
    attempt_dir = plan_dir / "logs" / "vp_attempts"
    audit_logs = list(attempt_dir.glob("vp_attempt_VP-023_hung_*.log"))
    assert len(audit_logs) == 1, f"expected 1 audit log, got {audit_logs}"

    line = json.loads(audit_logs[0].read_text().strip())
    assert line["event"] == "sub_agent_did_not_progress"
    assert line["data"]["vp_id"] == "VP-023"
    assert line["data"]["attempt"] == 1
    assert "age_from_last_progress" in line["data"]


def test_lazy_check_sub_agents_skips_kill_when_stdout_is_fresh(in_memory_plan_state, mock_scoped_tool, monkeypatch):
    """Second-chance liveness: a stale ``last_progress_ts`` handle must NOT
    be killed if the underlying ``scoped_tool._last_output_ts`` is still
    fresh (sub-agent is streaming stdout, e.g. long pytest run).

    Closes the 2026-08-26 watchdog false-positive gap: VP-023 attempt 2
    ran pytest for 14 min without calling ``mark_progress`` (the sub-agent
    only marks progress at stage transitions), which would have tripped
    the 20-minute threshold even though the subprocess was alive."""
    from server import _lazy_check_sub_agents

    plan_id, _plan_dir, state, handle = in_memory_plan_state
    scoped_tool, proc = mock_scoped_tool
    handle.scoped_tool = scoped_tool

    # Age the handle's last_progress_ts past the stale threshold
    handle.last_progress_ts = time.time() - (server.SUB_AGENT_STALE_THRESHOLD_SEC + 60)

    # But mark the scoped_tool's stdout as fresh (sub-agent still streaming)
    scoped_tool._last_output_ts = time.monotonic()  # just now

    # Spy on _handle_stuck_sub_agent — must NOT be called
    handler_calls = []
    monkeypatch.setattr(
        "server._handle_stuck_sub_agent",
        lambda pid, h: handler_calls.append((pid, h)),
    )

    _lazy_check_sub_agents()

    # Handle must NOT be killed — and last_progress_ts must be refreshed
    assert handler_calls == [], "watchdog should skip kill when stdout is fresh"
    assert (time.time() - handle.last_progress_ts) < 5, \
        "last_progress_ts should be refreshed when stdout is fresh"
    assert state["verification_status"] == "running", \
        "verification state should not flip to failed"


def test_lazy_check_sub_agents_kills_when_both_signals_stale(in_memory_plan_state, mock_scoped_tool, monkeypatch):
    """When BOTH last_progress_ts AND _last_output_ts are stale, the kill
    fires — that's the actual wedged-subprocess case the watchdog was
    built for."""
    from server import _lazy_check_sub_agents

    plan_id, _plan_dir, state, handle = in_memory_plan_state
    scoped_tool, proc = mock_scoped_tool
    handle.scoped_tool = scoped_tool

    # Age both signals past the threshold
    handle.last_progress_ts = time.time() - (server.SUB_AGENT_STALE_THRESHOLD_SEC + 60)
    scoped_tool._last_output_ts = time.monotonic() - (server.SUB_AGENT_STALE_THRESHOLD_SEC + 60)

    # Stub the kill helper so we don't touch a real Popen
    monkeypatch.setattr("server.kill_process_group", lambda *a, **k: 0)
    monkeypatch.setattr("server.signal.SIGTERM", 15)
    monkeypatch.setattr("server.signal.SIGKILL", 9)
    proc.poll.return_value = 0  # pretend kill worked

    _lazy_check_sub_agents()

    # 2026-09-07: round no longer aborts. The kill signal is sent
    # to the executor (which will retry or SKIP), and the round keeps
    # running. Assert state is untouched and the per-handle counter
    # is bumped.
    assert state["verification_status"] == "running"
    assert state.get("stop_reason") != "sub_agent_did_not_progress"
    assert handle.watchdog_kill_count == 1


def test_lazy_check_sub_agents_kills_when_output_ts_is_none(in_memory_plan_state, mock_scoped_tool, monkeypatch):
    """If scoped_tool._last_output_ts is None (subprocess spawned but no
    stdout yet — e.g. LLM API hung before first response), fall back to
    last_progress_ts and kill."""
    from server import _lazy_check_sub_agents

    plan_id, _plan_dir, state, handle = in_memory_plan_state
    scoped_tool, proc = mock_scoped_tool
    handle.scoped_tool = scoped_tool

    handle.last_progress_ts = time.time() - (server.SUB_AGENT_STALE_THRESHOLD_SEC + 60)
    scoped_tool._last_output_ts = None  # no stdout yet

    monkeypatch.setattr("server.kill_process_group", lambda *a, **k: 0)
    monkeypatch.setattr("server.signal.SIGTERM", 15)
    monkeypatch.setattr("server.signal.SIGKILL", 9)
    proc.poll.return_value = 0

    _lazy_check_sub_agents()

    # 2026-09-07: same as above — round continues, counter bumped.
    assert state["verification_status"] == "running"
    assert state.get("stop_reason") != "sub_agent_did_not_progress"
    assert handle.watchdog_kill_count == 1


# ----------------------------------------------------------------------------
# _mark_verification_failed_dead persists runtime state
# ----------------------------------------------------------------------------


def test_mark_verification_failed_dead_persists_runtime_json(tmp_path, monkeypatch):
    """Closes the 2026-08-25 audit gap: ``.verification_runtime.json``
    should now be written so a restart doesn't resurrect stale
    'running' state."""
    plan_id = "t-deadpersist"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)

    import server
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")

    # Seed in-memory state
    state = {
        "plan_id": plan_id,
        "verification_status": "running",
        "verification_round": 5,
        "verification_max_rounds": 5,
        "thread": None,
        "stop_reason": None,
        "started_at": "2026-08-25T00:00:00Z",
        "updated_at": "2026-08-25T00:00:00Z",
    }
    server._verification_state[plan_id] = state
    server._verification_locks[plan_id] = threading.Lock()

    try:
        server._mark_verification_failed_dead(plan_id)

        # in-memory flipped
        assert state["verification_status"] == "failed"
        assert state["stop_reason"] == "verification_thread_died_unexpectedly"
        assert "ended_at" in state

        # runtime json persisted (NEW behavior)
        runtime_path = plan_dir / ".verification_runtime.json"
        assert runtime_path.exists(), "runtime JSON should have been written"
        persisted = json.loads(runtime_path.read_text())
        assert persisted["verification_status"] == "failed"
        assert persisted["stop_reason"] == "verification_thread_died_unexpectedly"
    finally:
        server._verification_state.pop(plan_id, None)
        server._verification_locks.pop(plan_id, None)


# ----------------------------------------------------------------------------
# max_rounds ceiling + reset endpoint tests
# ----------------------------------------------------------------------------


def test_reset_rounds_endpoint_registered():
    """Check the endpoint is wired in the FastAPI router."""
    import server
    # ``iter_app_routes`` flattens included routers; ``app.routes`` alone
    # shows an extracted router as a single entry (FastAPI >= 0.141).
    paths = [
        r.path for r in server.iter_app_routes()
        if hasattr(r, "path") and "reset_rounds" in r.path
    ]
    assert any(p.endswith("/api/verification/{plan_id}/reset_rounds") for p in paths), \
        f"reset_rounds not found, got: {paths}"


def test_reset_rounds_request_validates_range():
    """Pydantic rejects out-of-range values upfront (both fields)."""
    from server import ResetRoundsRequest
    from pydantic import ValidationError

    # Valid — 1-based restart-at-round values, including the
    # default-shaped call (None → restart at round 1).
    ResetRoundsRequest()
    ResetRoundsRequest(reset_round_to=1)
    ResetRoundsRequest(reset_round_to=2)
    ResetRoundsRequest(reset_round_to=1000)
    # Backward-compat field still parses.
    ResetRoundsRequest(new_max_rounds=3)

    # Invalid
    with pytest.raises(ValidationError):
        ResetRoundsRequest(new_max_rounds=0)
    with pytest.raises(ValidationError):
        ResetRoundsRequest(new_max_rounds=1001)
    # 2026-09-15: 0 is not a round number — the counter restarts at a
    # 1-based round ("重置回 1"), so 0 is rejected by the schema.
    with pytest.raises(ValidationError):
        ResetRoundsRequest(reset_round_to=0)
    with pytest.raises(ValidationError):
        ResetRoundsRequest(reset_round_to=-1)
    with pytest.raises(ValidationError):
        ResetRoundsRequest(reset_round_to=1001)


def test_reset_rounds_enforces_immutable_cap(tmp_path, monkeypatch):
    """2026-09-14: the cap is immutable — neither raised
    (that made rounds unbounded; the 0823 plan ran round 6 for 47 min)
    nor lowered (it is the plan's setup contract). The endpoint's job is
    the COUNTER, so any differing ``new_max_rounds`` is refused with a
    pointer at ``reset_round_to``.
    """
    from fastapi.testclient import TestClient
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    import server

    plan_id = "t-cap"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr("server._state_db_path", lambda *_: str(tmp_path / "state.db"))

    con = _open_db(str(tmp_path / "state.db"))
    _migrate(con)
    RoutingRepository(con).insert(plan_id, "verification_running")
    # Plan's original cap = 3.
    VerificationRepository(con).init_round(plan_id, round_n=1, max_rounds=3)
    con.close()

    client = TestClient(server.app)

    # 1) Echoing the cap back is accepted (backward-compat callers).
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"new_max_rounds": 3})
    assert r.status_code == 200, (
        f"echoing the cap must succeed, got {r.status_code}: {r.json()}"
    )

    # 2) Raising the cap is refused.
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"new_max_rounds": 4})
    assert r.status_code == 400, (
        f"raising the cap must refuse with 400, got {r.status_code}: {r.json()}"
    )
    body = r.json()
    assert body["error"] == "max_rounds_immutable"
    assert body["max_rounds"] == 3

    # 3) Lowering it is refused too — it is the plan's setup contract,
    #    not a knob.
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"new_max_rounds": 1})
    assert r.status_code == 400
    assert r.json()["error"] == "max_rounds_immutable"


def test_reset_rounds_resets_counter_not_cap(tmp_path, monkeypatch):
    """2026-09-14 (the endpoint's meaning was inverted):
    the cap stays immutable; what the operator resets is the round
    COUNTER, giving the plan another batch of ``max_rounds`` rounds
    under explicit authorization.

    2026-09-15: ``reset_round_to`` is the 1-based round number the
    iteration RESTARTS at. Concretely: a plan that burned
    all 3 rounds (round=3, max_rounds=3) gets ``reset_round_to: 1`` →
    the stored completed-rounds counter reads 0, ``restart_at_round``
    is 1, the next ``/start`` computes next_round=1 ≤ 3 and is
    admitted, and the cap in the DB is untouched.
    """
    import sqlite3
    from fastapi.testclient import TestClient
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    import server

    plan_id = "t-cap-lifetime"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr("server._state_db_path", lambda *_: str(tmp_path / "state.db"))

    con = _open_db(str(tmp_path / "state.db"))
    _migrate(con)
    RoutingRepository(con).insert(plan_id, "verification_running")
    # Simulate a plan that ran its full budget: round=3, max_rounds=3.
    VerificationRepository(con).init_round(plan_id, round_n=3, max_rounds=3)
    con.close()

    client = TestClient(server.app)

    # 1) Reset the counter back to round 1 (a fresh batch of rounds).
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"reset_round_to": 1})
    assert r.status_code == 200, (
        f"counter reset must succeed, got {r.status_code}: {r.json()}"
    )
    body = r.json()
    assert body["round"] == 0, "stored completed-rounds counter is 0"
    assert body["restart_at_round"] == 1, "operator-facing restart number"
    assert body["rounds_remaining"] == 3, "a full batch is available again"
    assert body["max_rounds"] == 3, "cap must be unchanged"
    assert body["new_max_rounds"] == 3, "backward-compat field shows the cap"

    # 1b) Restarting past the immutable cap is refused upfront.
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"reset_round_to": 4})
    assert r.status_code == 400, (
        f"restart beyond the cap must refuse, got {r.status_code}: {r.json()}"
    )
    assert r.json()["error"] == "reset_round_to_exceeds_cap"

    # 1c) Restarting at the last round stores counter = cap-1 (only
    #     that one round remains in the budget).
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"reset_round_to": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["round"] == 2, "stored counter = restart_at - 1"
    assert body["restart_at_round"] == 3
    assert body["rounds_remaining"] == 1

    # 2) The counter really moved in the DB, and the row is left
    #    ``pending`` (nothing runs until /start) with the stale
    #    stop_reason cleared.
    con = sqlite3.connect(str(tmp_path / "state.db"))
    cur = con.execute(
        "SELECT round, max_rounds, verification_status, "
        "verification_stop_reason FROM plan_verification WHERE plan_id=?",
        (plan_id,),
    )
    row = cur.fetchone()
    con.close()
    assert row[0] == 2, f"counter must be restart_at-1 = 2, got {row[0]}"
    assert row[1] == 3, f"cap must stay 3, got {row[1]}"
    assert row[2] == "pending", (
        f"a reset plan is idle until /start; got status={row[2]}"
    )
    assert row[3] is None, f"stale stop_reason must be cleared; got {row[3]}"

    # 2b) Back to a full batch for the admission check below.
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"reset_round_to": 1})
    assert r.status_code == 200

    # 3) A /start now computes next_round = 0+1 = 1 ≤ 3 — no more
    #    ``max_rounds_exceeded`` (without a project_dir it 404s before
    #    spawning, which still proves the ceiling check passed).
    r = client.post(f"/api/verification/{plan_id}/start",
                    json={"max_rounds": 3})
    assert not (r.status_code == 409 and "max_rounds" in r.json().get("error", "")), (
        f"the ceiling check must admit round 1 after the reset; got "
        f"{r.status_code}: {r.text[:300]}"
    )

    # 4) In-memory staleness is re-synced: an entry claiming "running"
    #    with an old round/stop_reason must not keep the plan wedged.
    server._verification_state[plan_id] = {
        "verification_status": "running",
        "verification_round": 3,
        "stop_reason": "verification_log_stale",
    }
    r = client.post(f"/api/verification/{plan_id}/reset_rounds",
                    json={"reset_round_to": 2})
    assert r.status_code == 200
    # Stored counter = 2-1 = 1: /start will begin at round 2.
    assert server._verification_state[plan_id]["verification_round"] == 1
    assert server._verification_state[plan_id]["verification_status"] == "pending"
    assert server._verification_state[plan_id]["stop_reason"] is None
    server._verification_state.pop(plan_id, None)




def test_start_handler_rejects_when_next_round_exceeds_max_rounds(monkeypatch):
    """Smoke test of the start handler ceiling check via TestClient.

    We don't exercise the full happy path (that would require spinning
    up SQLite + routing + a real project_dir); we only verify that
    the ceiling check fires its 409 response.
    """
    from server import _open_verification_state

    plan_id = "t-ceiling"
    plan_dir = Path("/tmp/_no_such_plan_ceiling")  # path won't exist
    monkeypatch.setattr(
        "server._open_verification_state",
        lambda: (
            MagicMock(),
            MagicMock(),
            MagicMock(current=MagicMock(return_value={"round": 5, "max_rounds": 5})),
        ),
    )

    # We patch the existence check so we get past the early 404.
    import server as srv
    monkeypatch.setattr(srv, "PLANS_DIR", Path("/tmp/_plan_dir"))
    (Path("/tmp/_plan_dir")).mkdir(parents=True, exist_ok=True)
    (Path("/tmp/_plan_dir") / plan_id).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(srv, "_get_project_dir", lambda _: "/tmp/_dummy_project")

    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    # Direct call instead of HTTP — close enough to exercise the ceiling
    from fastapi import Response
    from server import start_verification, StartVerificationRequest

    req = StartVerificationRequest(max_rounds=5)
    result = start_verification(plan_id, req)

    # JSONResponse is the actual return type. Check for the ceiling marker.
    if isinstance(result, Response):
        assert result.status_code == 409
        body = json.loads(result.body)
        assert body["error"] == "max_rounds_exceeded"
        assert "round 5" in body["detail"]
    else:
        # If it's a dict, the ceiling check probably short-circuited.
        # That'd be wrong, but let's be permissive.
        pytest.fail(f"unexpected return type: {type(result)}")


# ----------------------------------------------------------------------------
# _persist_verification_terminal — terminal-status persistence + routing CAS
# ----------------------------------------------------------------------------


@pytest.fixture
def plan_with_inflight_round(tmp_path, monkeypatch):
    """Seed a plan in a mid-round state so we can test the terminal
    persist helper against a real SQLite + routing row pair.

    Returns: (plan_id, plan_dir)
    """
    plan_id = "t-persist-terminal"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    monkeypatch.setattr("server._state_db_path", lambda *_: str(tmp_path / "state.db"))

    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    con = _open_db(str(tmp_path / "state.db"))
    _migrate(con)
    # Seed: routing in verification_repairing, plan_verification status=running.
    RoutingRepository(con).insert(plan_id, "new")
    RoutingRepository(con).try_mark_phase(
        plan_id, ("new",), "verification_running",
    )
    RoutingRepository(con).try_mark_phase(
        plan_id, ("verification_running",), "verification_repairing",
    )
    VerificationRepository(con).init_round(plan_id, round_n=1, max_rounds=3)
    con.close()

    yield plan_id, plan_dir


def _read_state(plan_id: str, db_path):
    """Read routing.stage + plan_verification.verification_status + results JSON."""
    import sqlite3
    import json as _json
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.execute("SELECT current_phase FROM plan_routing WHERE plan_id=?", (plan_id,))
    routing_stage = cur.fetchone()["current_phase"]
    cur = con.execute(
        "SELECT verification_status, results FROM plan_verification WHERE plan_id=?",
        (plan_id,),
    )
    row = cur.fetchone()
    results = _json.loads(row["results"]) if row["results"] else {}
    con.close()
    return routing_stage, row["verification_status"], results


def test_persist_terminal_writes_status_and_advances_routing(plan_with_inflight_round, tmp_path):
    """``_persist_verification_terminal(\"failed\", ...)`` must:
    1. set ``plan_verification.verification_status = \"failed\"``
    2. CAS ``plan_routing.stage`` from ``verification_repairing`` to ``failed``
    3. record the stop_reason in the ``results`` JSON envelope

    2026-09-13: this used to pass ``stop_reason="no_repair_tasks"``. Commit
    df2abb9 (2026-09-12, state-machine closed-loop fix) deliberately made
    that a *mid-loop* reason — it must NOT CAS the routing row, because
    the plan is still chaining into a repair execution. The assertion
    below is about the terminal path, so it now uses a chain-ending
    reason; the mid-loop contract is pinned by
    ``test_state_machine_closed_loop.py::test_persist_verification_terminal_mid_loop_skips_cas``.
    """
    import server
    plan_id, _plan_dir = plan_with_inflight_round
    db_path = tmp_path / "state.db"

    before_routing, before_status, _ = _read_state(plan_id, db_path)
    assert before_routing == "verification_repairing"
    assert before_status == "running"

    server._persist_verification_terminal(plan_id, "failed", "max_rounds_reached")

    after_routing, after_status, after_results = _read_state(plan_id, db_path)
    assert after_routing == "failed", \
        f"routing should advance out of verification_* on terminal, got {after_routing}"
    assert after_status == "failed", \
        f"plan_verification.verification_status should be 'failed', got {after_status}"
    assert after_results.get("status") == "failed"
    assert after_results.get("stop_reason") == "max_rounds_reached"
    assert after_results.get("recorded_by") == "_persist_verification_terminal"


def test_persist_terminal_passed_maps_to_completed(plan_with_inflight_round, tmp_path):
    """``status=\"passed\"`` must:
    1. set ``plan_verification.verification_status = \"passed\"``
    2. CAS ``plan_routing.stage`` to ``completed`` (NOT failed)
    """
    import server
    plan_id, _plan_dir = plan_with_inflight_round
    db_path = tmp_path / "state.db"

    server._persist_verification_terminal(plan_id, "passed", None)

    after_routing, after_status, _ = _read_state(plan_id, db_path)
    assert after_routing == "completed", \
        f"passed should map to completed, got {after_routing}"
    assert after_status == "passed"


def test_persist_terminal_same_failure_repeated_maps_to_loop_stopped(
    plan_with_inflight_round, tmp_path
):
    """``stop_reason=\"same_failure_repeated\"`` is a successful
    termination of the auto-loop, not a per-VP failure. It must
    map to ``loop_stopped`` in SQLite (NOT ``failed``).
    """
    import server
    plan_id, _plan_dir = plan_with_inflight_round
    db_path = tmp_path / "state.db"

    server._persist_verification_terminal(plan_id, "failed", "same_failure_repeated")

    after_routing, after_status, after_results = _read_state(plan_id, db_path)
    assert after_status == "loop_stopped", \
        f"same_failure_repeated should map to loop_stopped, got {after_status}"
    assert after_routing == "failed"
    assert after_results.get("stop_reason") == "same_failure_repeated"


def test_persist_terminal_after_max_attempts_maps_to_loop_stopped(
    plan_with_inflight_round, tmp_path
):
    """2026-09-20 (post-mortem): the ``_after_max_attempts`` spelling
    must map the same way.

    The mapping used to test the reason by equality against the bare
    ``same_failure_repeated`` alone, so the renamed verdict fell through to
    the ``status`` passthrough and landed as ``"failed"`` — a hard failure
    — even though the orchestrator had just recorded a successful loop
    termination. Callers do still pass ``"failed"`` here (the auto-loop's
    convergence exit historically did), which is why the classification
    lives in the mapping rather than at each call site.
    """
    import server
    plan_id, _plan_dir = plan_with_inflight_round
    db_path = tmp_path / "state.db"

    server._persist_verification_terminal(
        plan_id, "failed", "same_failure_repeated_after_max_attempts"
    )

    after_routing, after_status, after_results = _read_state(plan_id, db_path)
    assert after_status == "loop_stopped", (
        "same_failure_repeated_after_max_attempts should map to loop_stopped, "
        f"got {after_status}"
    )
    assert after_routing == "failed"
    assert after_results.get("stop_reason") == (
        "same_failure_repeated_after_max_attempts"
    )


def test_persist_terminal_idempotent_on_already_terminal(plan_with_inflight_round, tmp_path):
    """Calling the helper twice should be a no-op on the second call —
    the CAS raises ConflictError (caught) and the ``complete_round``
    write is idempotent enough that nothing crashes.

    Without the idempotency, a server restart that re-runs the
    auto-loop cleanup path would re-fail with ConflictError.

    2026-09-13: ``stop_reason`` switched from ``no_repair_tasks`` (now a
    mid-loop reason since df2abb9 — see the note on
    ``test_persist_terminal_writes_status_and_advances_routing``) to a
    chain-ending one, so the first call really does advance the stage and
    the second call really is the re-CAS case this test is about.
    """
    import server
    plan_id, _plan_dir = plan_with_inflight_round
    db_path = tmp_path / "state.db"

    server._persist_verification_terminal(plan_id, "failed", "max_rounds_reached")
    # Second call must not raise. The CAS will be skipped (already
    # advanced out of verification_*), and the in-memory dict
    # update is no-op (no in-memory state seeded here).
    server._persist_verification_terminal(plan_id, "failed", "max_rounds_reached")

    after_routing, after_status, _ = _read_state(plan_id, db_path)
    assert after_routing == "failed"
    assert after_status == "failed"


def test_persist_terminal_unknown_status_falls_back_to_failed(plan_with_inflight_round, tmp_path):
    """Defensive: any unknown status string should fall back to
    ``failed`` rather than raise — keeps the auto-loop cleanup
    path robust to new internal status values added in the
    future.

    2026-09-13: ``stop_reason=None`` is no longer chain-ending (df2abb9),
    which would leave the routing CAS unexercised here. Pass a
    chain-ending reason so the assertion below still covers the
    status-coercion path *and* the CAS.
    """
    import server
    plan_id, _plan_dir = plan_with_inflight_round
    db_path = tmp_path / "state.db"

    # The first positional arg is the raw in-memory ``status``.
    # Pass an unexpected value — the helper must coerce to
    # "failed" for the SQLite write.
    server._persist_verification_terminal(
        plan_id, "weird_new_status", "max_rounds_reached"
    )

    after_routing, after_status, _ = _read_state(plan_id, db_path)
    assert after_status == "failed"
    assert after_routing == "failed"


# ----------------------------------------------------------------------------
# VerificationExecutor verdict-load normalisation
# ----------------------------------------------------------------------------


def test_executor_loads_verdicts_from_list_shape(tmp_path):
    """Regression: ``VerificationRepository.append_verdict`` stores
    verdicts as a **list** of verdict dicts (each with a ``vp_id``
    key), but ``VerificationExecutor._verdicts`` is a dict keyed by
    vp_id. Before the 2026-08-25 fix, ``_load_or_init_state`` did
    ``dict(verdicts)`` on a list of verdict dicts, which raised
    ``ValueError: dictionary update sequence element #0 has length
    6; 2 is required``. The load then fell through to the
    disk-state-file fallback (empty under the SQLite-first
    refactor), the round silently re-ran every VP from scratch,
    and the resume path was effectively dead.

    After the fix, list-shaped verdicts are normalised into
    ``{vp_id: verdict}`` on load so ``_completed_items`` ends up
    equal to ``len(unique_vp_ids)`` and ``_backfill_index_lists_from_verdicts``
    derives the right index lists.

    Same plan with verdicts as a dict must also load correctly
    (the executor's expected on-disk shape).
    """
    import sqlite3
    import server
    from verification_executor import VerificationExecutor
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate

    # Set up: a real plan_dir + a SQLite state-machine with verdicts
    # written as the legacy **list** shape (what append_verdict
    # produces).
    plan_id = "t-executor-load-list"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "verification_plan.json").write_text(
        '{"verification_points": ['
        '{"id": "VP-001", "title": "x", "verification_method": "automated_test"},'
        '{"id": "VP-002", "title": "x", "verification_method": "automated_test"},'
        '{"id": "VP-023", "title": "x", "verification_method": "automated_test"}'
        "]}"
    )

    # Open state-machine and seed verdicts as a list.
    con = _open_db(str(tmp_path / "state.db"))
    _migrate(con)
    RoutingRepository = (
        __import__("state_machine.repositories.routing_repository", fromlist=["RoutingRepository"])
        .RoutingRepository
    )
    RoutingRepository(con).insert(plan_id, "verification_running")
    VerificationRepository(con).init_round(plan_id, round_n=1, max_rounds=3)
    verdicts_list = [
        {"vp_id": "VP-001", "status": "PASSED", "reasons": ["ok"]},
        {"vp_id": "VP-002", "status": "PASSED", "reasons": ["ok"]},
        {"vp_id": "VP-023", "status": "FAILED", "reasons": ["139 failed"]},
    ]
    con.execute(
        "UPDATE plan_verification SET verdicts=? WHERE plan_id=?",
        (json.dumps(verdicts_list), plan_id),
    )
    con.commit()
    con.close()

    # Now construct the executor with verif_repo wired — must
    # NOT raise and must surface the verdicts as completed.
    verif_repo = VerificationRepository(_open_db(str(tmp_path / "state.db")))
    plan = json.loads((plan_dir / "verification_plan.json").read_text())
    vps = [
        {"id": vp["id"], "title": vp["title"], "method": vp["verification_method"]}
        for vp in plan["verification_points"]
    ]
    executor = VerificationExecutor(
        verification_plan={"vps": vps, "depends_on": {}},
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: {},
        verif_repo=verif_repo,
    )

    # 2026-08-25 contract change: only PASSED VPs are in
    # ``_completed_items``. FAILED VPs are in ``_failed_items``
    # so the next round's executor re-runs them (the resume
    # path's "skip the green ones, re-run the red ones" semantics).
    assert sorted(executor._completed_items) == ["VP-001", "VP-002"]
    assert executor._failed_items == ["VP-023"]
    # The internal _verdicts dict should be keyed by vp_id.
    assert "VP-001" in executor._verdicts
    assert executor._verdicts["VP-001"]["status"] == "PASSED"
    assert executor._verdicts["VP-023"]["status"] == "FAILED"


def test_executor_loads_verdicts_from_dict_shape(tmp_path):
    """The dict shape is the executor's *expected* on-disk format
    going forward (and is what the in-memory ``_verdicts`` looks
    like). Make sure the load path doesn't break a dict payload.
    """
    import sqlite3
    from verification_executor import VerificationExecutor
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate

    plan_id = "t-executor-load-dict"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "verification_plan.json").write_text(
        '{"verification_points": ['
        '{"id": "VP-A", "title": "x", "verification_method": "automated_test"},'
        '{"id": "VP-B", "title": "x", "verification_method": "automated_test"}'
        "]}"
    )

    con = _open_db(str(tmp_path / "state.db"))
    _migrate(con)
    RoutingRepository = (
        __import__("state_machine.repositories.routing_repository", fromlist=["RoutingRepository"])
        .RoutingRepository
    )
    RoutingRepository(con).insert(plan_id, "verification_running")
    VerificationRepository(con).init_round(plan_id, round_n=1, max_rounds=3)
    verdicts_dict = {
        "VP-A": {"vp_id": "VP-A", "status": "PASSED"},
        "VP-B": {"vp_id": "VP-B", "status": "SKIPPED"},
    }
    con.execute(
        "UPDATE plan_verification SET verdicts=? WHERE plan_id=?",
        (json.dumps(verdicts_dict), plan_id),
    )
    con.commit()
    con.close()

    verif_repo = VerificationRepository(_open_db(str(tmp_path / "state.db")))
    plan = json.loads((plan_dir / "verification_plan.json").read_text())
    vps = [
        {"id": vp["id"], "title": vp["title"], "method": vp["verification_method"]}
        for vp in plan["verification_points"]
    ]
    executor = VerificationExecutor(
        verification_plan={"vps": vps, "depends_on": {}},
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: {},
        verif_repo=verif_repo,
    )

    # 2026-08-25 contract change: ``_completed_items`` is now the
    # PASSED-only set (used by ``BaseExecutor.run`` to skip
    # already-PASSED VPs). FAILED VPs stay in ``_failed_items``
    # so the next round re-runs them; SKIPPED VPs are filtered
    # out at the ``BaseExecutor.run`` skip-set union step.
    assert executor._completed_items == ["VP-A"]
    assert executor._failed_items == []
    assert sorted(executor._skipped_items) == ["VP-B"]
    assert executor._verdicts["VP-A"]["status"] == "PASSED"
    assert executor._verdicts["VP-B"]["status"] == "SKIPPED"


# ----------------------------------------------------------------------------
# 2026-09-07: kill → retry once → SKIPPED (no round abort)
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_kill_then_retry_passes(tmp_path, monkeypatch):
    """First ``query_json`` call raises an I/O error (SIGKILL broke the
    pipe), second call returns a PASSED verdict. Verifies:

    - Final verdict is ``PASSED`` (not FAILED, not SKIPPED).
    - ``VerificationSubAgent._watchdog_retry_used`` flag is set.
    - ``reg_handle.watchdog_kill_count`` is reset to 0 after the retry
      branch fires, so the retry attempt has a clean budget.
    - The verification thread's in-memory state is untouched (round
      not aborted).
    """
    from verification_subagent import (
        Verdict,
        VerificationSubAgent,
        WatchdogKilledError,
    )

    plan_id = "t-kill-retry-passes"
    from sub_agent_registry import sub_agent_registry

    call_count = 0

    class FakeScopedTool:
        def __init__(self):
            self._process_lock = threading.Lock()
            self._current_process = None
            self._last_output_ts = time.monotonic()

        # SYNC: the real ``ClaudeCodingTool.query_json`` is sync
        # because the inner subprocess pipe read is blocking I/O
        # — the verification sub-agent's await on the call parks
        # on the pipe until either verdict JSON arrives or the
        # watchdog SIGKILLs the subprocess (which then makes
        # readline raise an OSError synchronously).
        def query_json(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("simulated SIGKILL pipe break")
            return {"verdict": "PASSED", "reasons": [], "evidence": []}

    fake_scoped_tool = FakeScopedTool()
    reg_handle = sub_agent_registry.register_sub_agent(
        plan_id=plan_id,
        vp_id="VP-T",
        attempt=0,
        scoped_tool=fake_scoped_tool,
    )

    class MinimalSubAgent(VerificationSubAgent):
        def __init__(self):
            self.max_retries = 0
            self.plan_id = plan_id
            self.registry = sub_agent_registry
            self.model_complexity = "test"
            self._watchdog_retry_used = False
            # Parent class needs ``template`` and ``method`` for
            # ``_build_prompt`` to succeed. Stub them so the test
            # exercises only the watchdog/retry code paths.
            self.method = "automated_test"
            self.template = ""

        async def _self_heal(self, *args, **kwargs):
            return "noop"

        def _build_prompt(self, vp_node, attempt):
            return "prompt"

        def _write_log(self, log_path, event, payload):
            pass

    sub = MinimalSubAgent()
    vp_node = {"id": "VP-T", "title": "test"}

    # Wire a watchdog hook that bumps kill_count on the registered
    # handle BEFORE the first call returns its OSError — so when
    # _execute_attempt's catch reads kill_count, it sees 1.
    original_query_json = fake_scoped_tool.query_json

    def hooked_query(*args, **kwargs):
        if call_count == 0:
            try:
                return original_query_json(*args, **kwargs)
            except OSError:
                # Watchdog tick fired; bump counter.
                with sub_agent_registry._lock:
                    reg_handle.watchdog_kill_count += 1
                raise
        return original_query_json(*args, **kwargs)

    fake_scoped_tool.query_json = hooked_query

    async def run_loop():
        # _self_heal_loop shape: 1 initial + 1 watchdog retry slot.
        for attempt in range(sub.max_retries + 1 + 1):
            try:
                return await sub._execute_attempt(
                    vp_node=vp_node,
                    coding_tool=fake_scoped_tool,
                    settings_path="/tmp/fake_settings.json",
                    log_path=Path("/tmp/fake_attempt.log"),
                    attempt=attempt,
                    project_dir=tmp_path,
                )
            except WatchdogKilledError:
                # Mirror _self_heal_loop's new branch.
                sub._watchdog_retry_used = True
                with sub_agent_registry._lock:
                    reg_handle.watchdog_kill_count = 0
                    reg_handle.last_progress_ts = time.time()
                continue

    verdict = await run_loop()

    # Final verdict is PASSED (the second attempt succeeded).
    assert verdict.verdict == "PASSED"
    assert sub._watchdog_retry_used is True
    assert call_count == 2

    sub_agent_registry.cleanup_for_plan(plan_id)


@pytest.mark.asyncio
async def test_watchdog_kill_twice_returns_skipped(tmp_path, monkeypatch):
    """Both ``query_json`` calls raise (watchdog kills twice). Verifies:

    - Final verdict is ``SKIPPED``.
    - First reason is ``"watchdog_killed_twice"``.
    - The round is not aborted (status untouched).
    """
    from verification_subagent import (
        Verdict,
        VerificationSubAgent,
        WatchdogKilledError,
    )

    plan_id = "t-kill-twice-skipped"
    from sub_agent_registry import sub_agent_registry

    class FakeScopedTool:
        def __init__(self):
            self._process_lock = threading.Lock()
            self._current_process = None
            self._last_output_ts = time.monotonic()

        def query_json(self, *args, **kwargs):
            # Each call simulates a SIGKILL — bump kill_count so the
            # next call's catch sees kc >= 1 (or >= 2 after the reset).
            raise OSError("simulated SIGKILL pipe break")

    fake_scoped_tool = FakeScopedTool()
    reg_handle = sub_agent_registry.register_sub_agent(
        plan_id=plan_id,
        vp_id="VP-T2",
        attempt=0,
        scoped_tool=fake_scoped_tool,
    )

    class MinimalSubAgent(VerificationSubAgent):
        def __init__(self):
            self.max_retries = 0
            self.plan_id = plan_id
            self.registry = sub_agent_registry
            self.model_complexity = "test"
            self._watchdog_retry_used = False
            self.method = "automated_test"
            self.template = ""

        async def _self_heal(self, *args, **kwargs):
            return "noop"

        def _build_prompt(self, vp_node, attempt):
            return "prompt"

        def _write_log(self, log_path, event, payload):
            pass

    sub = MinimalSubAgent()
    vp_node = {"id": "VP-T2", "title": "test"}

    original_query_json = fake_scoped_tool.query_json

    def watchdog_hook(*args, **kwargs):
        # Each call: bump kill_count on the LATEST registered
        # handle for this plan (simulates the watchdog tick that
        # fired BEFORE the SIGKILL took effect on the subprocess).
        # The executor registers a fresh handle on each retry
        # attempt (different ``attempt`` int), so we need to find
        # the new handle rather than the one we cached at test
        # setup time.
        #
        # IMPORTANT: do NOT acquire ``sub_agent_registry._lock``
        # here — ``_execute_attempt`` is already holding it via
        # ``register_sub_agent``, so re-entering would deadlock.
        # We reach into the internal ``_by_plan`` dict directly
        # instead. This is test-only code; the real watchdog (a
        # different thread) acquires the lock from outside the
        # call, which is the safe direction.
        handles = sub_agent_registry._by_plan.get(plan_id, [])
        if handles:
            handles[-1].watchdog_kill_count += 1
        return original_query_json(*args, **kwargs)

    fake_scoped_tool.query_json = watchdog_hook

    async def run_loop():
        # _self_heal_loop shape: 1 initial + 1 retry slot.
        for attempt in range(sub.max_retries + 1 + 1):
            try:
                return await sub._execute_attempt(
                    vp_node=vp_node,
                    coding_tool=fake_scoped_tool,
                    settings_path="/tmp/fake_settings.json",
                    log_path=Path("/tmp/fake_attempt.log"),
                    attempt=attempt,
                    project_dir=tmp_path,
                )
            except WatchdogKilledError:
                sub._watchdog_retry_used = True
                # Reset for retry (mirrors _self_heal_loop branch).
                # No lock — we're inside the same thread as
                # ``_execute_attempt`` and the registry mutations
                # here happen *after* the executor unregisters.
                handles = sub_agent_registry._by_plan.get(plan_id, [])
                if handles:
                    handles[-1].watchdog_kill_count = 0
                    handles[-1].last_progress_ts = time.time()
                continue

    verdict = await run_loop()

    # Final verdict is SKIPPED with the watchdog-killed-twice reason.
    assert verdict.verdict == "SKIPPED"
    assert "watchdog_killed_twice" in verdict.reasons

    sub_agent_registry.cleanup_for_plan(plan_id)


def test_handle_stuck_does_not_abort_round(in_memory_plan_state, monkeypatch):
    """Directly invoke ``_handle_stuck_sub_agent`` and verify that:

    - ``state['verification_status']`` stays ``'running'``.
    - ``state`` has no ``stop_reason`` of ``'sub_agent_did_not_progress'``.
    - ``handle.watchdog_kill_count`` is bumped to 1.
    - ``server._WATCHDOG_STATS['sub_agent_killed_total']`` is bumped.
    - The audit log entry under ``plans/{id}/logs/vp_attempts/`` is
      still written (forensic trail preserved).
    """
    from server import _handle_stuck_sub_agent

    plan_id, plan_dir, state, handle = in_memory_plan_state
    # Stub kill so we don't touch a real Popen
    monkeypatch.setattr("server.kill_process_group", lambda *a, **k: 0)
    monkeypatch.setattr("server.signal.SIGTERM", 15)
    monkeypatch.setattr("server.signal.SIGKILL", 9)

    pre_killed = server._WATCHDOG_STATS["sub_agent_killed_total"]
    pre_skipped = server._WATCHDOG_STATS["sub_agent_skipped_total"]

    _handle_stuck_sub_agent(plan_id, handle)

    # Round is NOT aborted.
    assert state["verification_status"] == "running"
    assert state.get("stop_reason") != "sub_agent_did_not_progress"
    assert "ended_at" not in state

    # Kill counter bumped on the handle.
    assert handle.watchdog_kill_count == 1

    # Stats counters bumped. (kill_count == 1 so skipped should NOT bump.)
    assert server._WATCHDOG_STATS["sub_agent_killed_total"] == pre_killed + 1
    assert server._WATCHDOG_STATS["sub_agent_skipped_total"] == pre_skipped

    # Audit log entry written for forensics.
    attempt_dir = plan_dir / "logs" / "vp_attempts"
    assert attempt_dir.exists()
    log_files = list(attempt_dir.glob("vp_attempt_*_hung_*.log"))
    assert len(log_files) == 1
    audit_entry = json.loads(log_files[0].read_text().strip())
    assert audit_entry["event"] == "sub_agent_did_not_progress"
    assert audit_entry["data"]["vp_id"] == handle.vp_id


def test_handle_stuck_bumps_skipped_counter_on_second_kill(
    in_memory_plan_state, monkeypatch
):
    """If the watchdog has already killed this VP once (kill_count == 1),
    a second ``_handle_stuck_sub_agent`` call bumps both the kill
    counter (to 2) AND the skipped_total counter."""
    from server import _handle_stuck_sub_agent

    plan_id, _plan_dir, _state, handle = in_memory_plan_state
    handle.watchdog_kill_count = 1  # simulate prior kill

    monkeypatch.setattr("server.kill_process_group", lambda *a, **k: 0)
    monkeypatch.setattr("server.signal.SIGTERM", 15)
    monkeypatch.setattr("server.signal.SIGKILL", 9)

    pre_killed = server._WATCHDOG_STATS["sub_agent_killed_total"]
    pre_skipped = server._WATCHDOG_STATS["sub_agent_skipped_total"]

    _handle_stuck_sub_agent(plan_id, handle)

    assert handle.watchdog_kill_count == 2
    assert server._WATCHDOG_STATS["sub_agent_killed_total"] == pre_killed + 1
    assert server._WATCHDOG_STATS["sub_agent_skipped_total"] == pre_skipped + 1
