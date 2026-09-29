"""Tests for the 2026-09-06 verification-watchdog fix.

Background
----------
A plan in verification (round 2 of 3) hit ``stop_reason=
\"repair_execution_failed\"`` but the orchestrator thread died before
``_record_terminal(\"failed\", \"repair_execution_failed\")`` actually ran
— so ``plan_routing.stage`` was never CAS'd out of ``verification_running``
and the Feishu card was stuck on \"still verifying\" while the actual work
was long dead (12+ hours for ``2026-09-04 plan``).

The fix layers two things on top of the existing
``_lazy_check_verification``:

1. Detection ladder widened — ``thread.is_alive()`` alone is unreliable
   for threads blocked on I/O. We also check the mtime of
   ``logs/verification_*.log`` and ``verification_execution_results.json``:
   any of the three exceeding ``VERIFICATION_WATCHDOG_STALENESS_SECONDS``
   is treated as stuck.
2. CAS is now actually invoked — on detection the lazy check calls
   ``_persist_verification_terminal(plan_id, \"failed\", stop_reason)``
   which does the SQL CAS on ``plan_routing.stage`` and fires
   ``KIND_PLAN_CLOSED`` so the Feishu card updates.

This file covers the 7 acceptance scenarios from plan
``2026-09-06 plan``.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Imports & fixtures
# ---------------------------------------------------------------------------

from server import (
    HeartbeatMonitor,
    _execution_state,
    _lazy_check_verification,
    _mark_verification_failed_dead,
    _verification_locks,
    _verification_state,
    _WATCHDOG_STATS,
    _latest_mtime,
    _record_watchdog_action,
    _persist_verification_terminal,
    app,
)
from plan_state import PlanState
from sub_agent_registry import sub_agent_registry


client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state():
    """Clear global verification state + reset watchdog dedup between tests.

    The repo-wide ``isolated_plans_dir`` fixture (autouse from conftest)
    already redirects ``PLANS_DIR`` and ``_state_db_path`` to ``tmp_path``.
    We only need to clear the in-memory state dicts + the per-plan
    watchdog-action dedup so a stale dedup from a prior test doesn't
    suppress the new test's actions.
    """
    _verification_state.clear()
    _verification_locks.clear()
    _execution_state.clear()
    for plan_id in list(sub_agent_registry._by_plan.keys()):
        sub_agent_registry.cleanup_for_plan(plan_id)
    _WATCHDOG_STATS["actions"].clear()
    _WATCHDOG_STATS["per_plan_last_action_ts"].clear()
    _WATCHDOG_STATS["last_sweep_at"] = None
    _WATCHDOG_STATS["last_sweep_count"] = 0
    yield
    _verification_state.clear()
    _verification_locks.clear()
    _execution_state.clear()
    for plan_id in list(sub_agent_registry._by_plan.keys()):
        sub_agent_registry.cleanup_for_plan(plan_id)
    _WATCHDOG_STATS["actions"].clear()
    _WATCHDOG_STATS["per_plan_last_action_ts"].clear()


@pytest.fixture
def short_grace(monkeypatch):
    """Drop the 60s startup-grace guard so tests don't have to wait it out.

    The guard exists in production to avoid racing the
    ``POST /start`` round-trip; in tests we synthesize the
    ``_verification_state`` directly and want the watchdog to act
    immediately on the first ``_lazy_check_verification`` call.
    """
    monkeypatch.setattr("server._VERIFICATION_STARTUP_GRACE_SECONDS", 0.0)


@pytest.fixture
def short_staleness(monkeypatch):
    """Drop ``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` from 600 to 5.

    Production default is 15 minutes — far too long for a test.
    5s is short enough that a single ``os.utime`` backdating immediately
    crosses the threshold.
    """
    monkeypatch.setattr("server.VERIFICATION_WATCHDOG_STALENESS_SECONDS", 5.0)


# ---------------------------------------------------------------------------
# Plan + state scaffolding helpers (match test_heartbeat_monitor.py patterns)
# ---------------------------------------------------------------------------


def _setup_plan(plan_id: str, phase: str = "verification_running") -> Path:
    """Create a minimal plan directory and plan_state.json under tmp_path.

    The ``isolated_plans_dir`` autouse fixture from conftest already
    points ``server.PLANS_DIR`` at ``tmp_path / plans``.
    """
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": phase,
                "completed_phases": ["execution"],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
                "verification": {
                    "status": "running",
                    "round": 0,
                    "max_rounds": 3,
                    "stop_reason": None,
                },
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def _running_verification_state(thread_obj=None, started_at=None):
    """Build an ``_verification_state`` dict for an in-flight run."""
    iso_started = started_at or datetime.now().isoformat()
    return {
        "plan_id": "test-plan",
        "verification_status": "running",
        "verification_round": 1,
        "verification_max_rounds": 3,
        "results": {
            "pytest_summary": "",
            "llm_findings": "",
            "performance_metrics": {},
        },
        "repair_tasks": [],
        "started_at": iso_started,
        "updated_at": iso_started,
        "orchestrator": None,
        "stop_reason": None,
        "thread": thread_obj,
    }


def _seed_routing_phase(plan_id: str, phase: str, db_path: Path):
    """Seed ``plan_routing.current_phase`` and a running ``plan_verification`` row."""
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    con = _open_db(str(db_path))
    try:
        _migrate(con)
        rr = RoutingRepository(con)
        # 2026-09-17 (schema v5): the bootstrap value used to be the
        # non-phase sentinel ``"new"`` — it only ever lived in the
        # routing column, which no longer exists separately. Seeding
        # at ``interview`` keeps the same "walk legal transitions to
        # the target" shape with values that are all real phases.
        rr.insert(plan_id, "interview")
        if phase == "verification_running":
            rr.try_mark_phase(plan_id, ("interview",), "verification_running")
        elif phase == "verification_rerunning":
            rr.try_mark_phase(plan_id, ("interview",), "verification_running")
            rr.try_mark_phase(plan_id, ("verification_running",), "verification_rerunning")
        elif phase == "verification_repairing":
            rr.try_mark_phase(plan_id, ("interview",), "verification_running")
            rr.try_mark_phase(plan_id, ("verification_running",), "verification_repairing")
        elif phase == "completed":
            rr.try_mark_phase(plan_id, ("interview",), "verification_running")
            rr.try_mark_phase(
                plan_id,
                ("verification_running", "verification_rerunning", "verification_repairing"),
                "completed",
            )
        else:
            raise ValueError(f"unhandled seed phase {phase!r}")
        VerificationRepository(con).init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        con.close()


def _read_state(plan_id: str, db_path: Path):
    """Read routing phase + plan_verification row as a dict."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id=?", (plan_id,)
    )
    routing_stage = cur.fetchone()["current_phase"]
    cur = con.execute(
        "SELECT verification_status, verification_stop_reason, results "
        "FROM plan_verification WHERE plan_id=?",
        (plan_id,),
    )
    row = cur.fetchone()
    results = (
        json.loads(row["results"]) if row and row["results"] else {}
    )
    con.close()
    return routing_stage, dict(row) if row else None, results


# ===========================================================================
# (a) thread 真死 → failed, stop_reason=verification_thread_died_unexpectedly
# ===========================================================================


class TestThreadDeathForcesTerminal:
    def test_dead_thread_advances_routing_stage(
        self, tmp_path, short_grace, monkeypatch
    ):
        """Scenario (a): a dead verification thread → plan_routing.stage
        becomes failed AND plan_verification.verification_status
        becomes \"failed\" with stop_reason=verification_thread_died_unexpectedly.

        This is the regression lock for the 2026-09-06 incident — the
        previous code only flipped in-memory state and never advanced
        plan_routing.stage, so Feishu cards stayed stuck on \"still
        verifying\" while the orchestrator was actually dead.
        """
        from server import _state_db_path

        plan_id = "vp20260906-thread-died"
        db_path = tmp_path / "state.db"
        # ``isolated_plans_dir`` already pointed _state_db_path at
        # tmp_path/state.db; assert that to avoid silent test-data leakage.
        assert str(_state_db_path()) == str(db_path)

        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        # Synthesize a dead thread — never started, so ``is_alive()`` False.
        dead_thread = threading.Thread(target=lambda: None)
        assert not dead_thread.is_alive()

        # started_at well in the past so the grace-period guard doesn't fire.
        past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
        _verification_state[plan_id] = _running_verification_state(
            thread_obj=dead_thread, started_at=past_iso
        )

        _lazy_check_verification(plan_id)

        routing_stage, verif_row, results = _read_state(plan_id, db_path)
        assert routing_stage == "failed", (
            f"plan_routing.stage should be failed, got {routing_stage!r}. "
            "This is the regression: previously the stage stayed at "
            "verification_running even when _lazy_check detected thread death."
        )
        assert verif_row["verification_status"] == "failed"
        # Note: stop_reason is stored in the ``results`` JSON envelope
        # (``complete_round`` only sets verification_status + results on
        # the plan_verification row, not the verification_stop_reason
        # column — that column is touched by ``mark_stopped`` and
        # ``init_round``, not by the terminal-transition path).
        assert results.get("stop_reason") == "verification_thread_died_unexpectedly"
        assert results.get("recorded_by") == "_persist_verification_terminal"

        # In-memory state must also reflect the transition so /status is consistent.
        assert _verification_state[plan_id]["verification_status"] == "failed"

        # Watchdog stats must have an entry (observability contract).
        actions = list(_WATCHDOG_STATS["actions"])
        assert len(actions) == 1
        assert actions[0]["plan_id"] == plan_id
        assert actions[0]["stop_reason"] == "verification_thread_died_unexpectedly"

    def test_reentered_round_is_not_mistaken_for_a_dead_thread(
        self, tmp_path, monkeypatch
    ):
        """Regression (the post-repair re-entry): the post-repair re-entry
        runs ``start_verification_cycle`` on the repair-watcher thread and
        never passed through the binding in
        ``POST /api/verification/{id}/start``. ``_verification_state[plan]
        ["thread"]`` therefore kept the round-1 orchestrator handle — dead
        since round 1 ended — and this very detection ladder declared the
        plan dead seconds into the round, terminally failing it while a
        VP's pytest was still running.

        ``_mark_verification_round_running`` now rebinds the handle, so the
        same tick that would have fired must leave the round alone.
        """
        from server import _mark_verification_round_running, _state_db_path

        plan_id = "vp20260915-reentry-thread"
        db_path = tmp_path / "state.db"
        assert str(_state_db_path()) == str(db_path)

        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        # Round 1's orchestrator thread: a real thread object that has run
        # and exited. This is exactly what the re-entry used to leave behind.
        round_one_thread = threading.Thread(target=lambda: None)
        round_one_thread.start()
        round_one_thread.join()
        assert not round_one_thread.is_alive()

        past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
        _verification_state[plan_id] = _running_verification_state(
            thread_obj=round_one_thread, started_at=past_iso
        )

        # Round 2 starts — on this (the current) thread.
        _mark_verification_round_running(plan_id, 2)
        assert _verification_state[plan_id]["thread"] is threading.current_thread()

        _lazy_check_verification(plan_id)

        assert _verification_state[plan_id]["verification_status"] == "running", (
            "a freshly re-entered round must not be declared dead — the "
            "watchdog was looking at the previous round's thread handle"
        )
        routing_stage, _, results = _read_state(plan_id, db_path)
        assert routing_stage == "verification_running", (
            f"the routing stage must not be CAS'd to terminal, got "
            f"{routing_stage!r}"
        )
        assert results.get("stop_reason") != "verification_thread_died_unexpectedly"
        assert [
            a for a in _WATCHDOG_STATS["actions"]
            if a["stop_reason"] == "verification_thread_died_unexpectedly"
        ] == []


# ===========================================================================
# (b) log stale / results stale → failed with the matching stop_reason
# ===========================================================================


class TestStalenessForcesTerminal:
    def test_log_stale_advances_routing_stage(
        self, tmp_path, short_grace, short_staleness
    ):
        """Scenario (b): ``logs/verification_*.log`` mtime > staleness threshold
        → stop_reason=verification_log_stale. Thread may still be technically
        alive (blocked on subprocess/lock/network) — staleness is what catches it.

        2026-09-14 triage update: an alive thread + stale file whose age is
        UNDER ``VERIFICATION_HARD_STALE_SECONDS`` is now HELD (post-execution
        phase protection — judgment runs in-process and writes nothing to the
        plan dir). Staleness only forces terminal for an alive thread once the
        age passes the hard cap, so backdate past it here.
        """
        from server import PLANS_DIR, VERIFICATION_HARD_STALE_SECONDS

        plan_id = "vp20260906-log-stale"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        plan_dir = PLANS_DIR / plan_id
        logs_dir = plan_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / "verification_round1.log"
        log_file.write_text("early log line\n", encoding="utf-8")
        # Backdate past BOTH the 5s staleness threshold and the hard cap —
        # only then does staleness stamp terminal while the thread lives.
        old = time.time() - (VERIFICATION_HARD_STALE_SECONDS + 60)
        os.utime(log_file, (old, old))

        # A still-alive thread (sleeping, daemon=True). We want the lazy
        # check to NOT mark it dead on thread.is_alive() but to catch it
        # via the log staleness path instead.
        stop_event = threading.Event()

        def sleeping():
            stop_event.wait(timeout=30)

        live_thread = threading.Thread(target=sleeping, daemon=True)
        live_thread.start()
        try:
            past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
            _verification_state[plan_id] = _running_verification_state(
                thread_obj=live_thread, started_at=past_iso
            )

            _lazy_check_verification(plan_id)

            routing_stage, verif_row, results = _read_state(plan_id, db_path)
            assert routing_stage == "failed"
            assert verif_row["verification_status"] == "failed"
            assert results.get("stop_reason") == "verification_log_stale"
        finally:
            stop_event.set()
            live_thread.join(timeout=2.0)

    def test_results_stale_advances_routing_stage(
        self, tmp_path, short_grace, short_staleness
    ):
        """Scenario (b extension): ``verification_execution_results.json`` mtime
        > staleness threshold → stop_reason=verification_results_stale.

        Same 2026-09-14 triage contract as the log-stale sibling: an alive
        thread is held until the hard cap, so backdate past it.
        """
        from server import PLANS_DIR, VERIFICATION_HARD_STALE_SECONDS

        plan_id = "vp20260906-results-stale"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        plan_dir = PLANS_DIR / plan_id
        results_file = plan_dir / "verification_execution_results.json"
        results_file.write_text(json.dumps({"results": []}), encoding="utf-8")
        old = time.time() - (VERIFICATION_HARD_STALE_SECONDS + 60)
        os.utime(results_file, (old, old))

        stop_event = threading.Event()

        def sleeping():
            stop_event.wait(timeout=30)

        live_thread = threading.Thread(target=sleeping, daemon=True)
        live_thread.start()
        try:
            past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
            _verification_state[plan_id] = _running_verification_state(
                thread_obj=live_thread, started_at=past_iso
            )

            _lazy_check_verification(plan_id)

            routing_stage, verif_row, results = _read_state(plan_id, db_path)
            assert routing_stage == "failed"
            assert results.get("stop_reason") == "verification_results_stale"
        finally:
            stop_event.set()
            live_thread.join(timeout=2.0)

    def test_no_log_or_results_file_does_not_force_terminal(
        self, tmp_path, short_grace, short_staleness
    ):
        """If neither logs nor results exist yet, the watchdog must NOT
        force-terminal — the plan is fresh and just hasn't produced any
        output yet (legitimate state for a brand-new verification)."""
        plan_id = "vp20260906-no-files"
        _setup_plan(plan_id)

        stop_event = threading.Event()

        def sleeping():
            stop_event.wait(timeout=30)

        live_thread = threading.Thread(target=sleeping, daemon=True)
        live_thread.start()
        try:
            past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
            _verification_state[plan_id] = _running_verification_state(
                thread_obj=live_thread, started_at=past_iso
            )

            _lazy_check_verification(plan_id)

            # No transition; in-memory state still "running".
            assert _verification_state[plan_id]["verification_status"] == "running"
            assert len(_WATCHDOG_STATS["actions"]) == 0
        finally:
            stop_event.set()
            live_thread.join(timeout=2.0)


# ===========================================================================
# (c) force_terminal API — synchronous CAS + immediate summary visibility
# ===========================================================================


class TestForceTerminalAPI:
    def test_force_terminal_success(self, tmp_path):
        """Scenario (c): POST /api/verification/{id}/force_terminal on a
        plan in verification_running returns 200 and immediately advances
        plan_routing.stage to failed. The next /summary read
        reflects the change without waiting for a heartbeat tick.
        """
        plan_id = "vp20260906-force-success"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        # Also seed an in-memory state so the endpoint can update it.
        past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
        _verification_state[plan_id] = _running_verification_state(started_at=past_iso)

        resp = client.post(f"/api/verification/{plan_id}/force_terminal")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["plan_id"] == plan_id
        assert body["current_phase"] == "failed"
        assert body["stop_reason"] == "user_force_terminal"
        assert "forced_at" in body

        routing_stage, verif_row, results = _read_state(plan_id, db_path)
        assert routing_stage == "failed"
        assert verif_row["verification_status"] == "failed"
        assert results.get("stop_reason") == "user_force_terminal"

        # In-memory state also reflects the new status.
        assert _verification_state[plan_id]["verification_status"] == "failed"
        assert _verification_state[plan_id]["stop_reason"] == "user_force_terminal"

        # Watchdog stats recorded the user-driven action.
        actions = list(_WATCHDOG_STATS["actions"])
        assert any(
            a["plan_id"] == plan_id and a["stop_reason"] == "user_force_terminal"
            for a in actions
        )

    def test_force_terminal_from_repairing_succeeds(self, tmp_path):
        """``force_terminal`` must work from any of the three verification_*
        source-stages. ``verification_repairing`` is the state the
        actual stuck plan was in (round 2 hit repair_execution_failed
        but the orchestrator thread died before transitioning).
        """
        plan_id = "vp20260906-force-repairing"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_repairing", db_path)

        resp = client.post(f"/api/verification/{plan_id}/force_terminal")
        assert resp.status_code == 200, resp.text
        assert resp.json()["current_phase"] == "failed"
        routing_stage, _, _ = _read_state(plan_id, db_path)
        assert routing_stage == "failed"

    def test_force_terminal_409_on_completed(self, tmp_path):
        """If the plan is already in a terminal stage, the endpoint must
        409 (no transition) — don't accidentally overwrite completed
        with failed just because the user clicked force.
        """
        plan_id = "vp20260906-force-409-terminal-done"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "completed", db_path)

        resp = client.post(f"/api/verification/{plan_id}/force_terminal")
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["error"] == "stage_mismatch"
        # 2026-09-17 (schema v5): the payload key follows the column
        # (``current_phase``); ``stage_mismatch`` stays as the reason
        # code because it is a pinned public error contract.
        assert body["current_phase"] == "completed"

        # State must be unchanged.
        routing_stage, _, _ = _read_state(plan_id, db_path)
        assert routing_stage == "completed"

    def test_force_terminal_404_on_missing_plan(self):
        """A plan_id that was never inserted must return 404, not 409."""
        resp = client.post("/api/verification/does-not-exist-plan/force_terminal")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "plan_not_found"


# ===========================================================================
# (d) Concurrent CAS — only one wins, the other gets ConflictError
# ===========================================================================


class TestConcurrentCasSafety:
    def test_concurrent_cas_only_one_wins(self, tmp_path):
        """Two threads call ``RoutingRepository.try_mark_phase`` with the
        same source-stages simultaneously. Exactly one must succeed
        (return True); the other must raise ``ConflictError``.

        Why we test the repository directly instead of the HTTP endpoint:
        ``fastapi.testclient.TestClient`` runs requests through an
        anyio portal that effectively serializes them — so the
        concurrent threads cannot actually race at the SQL layer when
        going through HTTP. Calling ``try_mark_phase`` directly is a
        much stronger test of the SQL CAS itself, which is what we
        actually care about (the HTTP endpoint is a thin wrapper).
        """
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.routing_repository import (
            ConflictError,
            RoutingRepository,
        )

        plan_id = "vp20260906-concurrent-cas"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        results: list = []
        barrier = threading.Barrier(2, timeout=5.0)

        def worker():
            barrier.wait()
            con = _open_db(str(db_path))
            try:
                _migrate(con)
                rr = RoutingRepository(con)
                try:
                    rr.try_mark_phase(
                        plan_id,
                        (
                            "verification_running",
                            "verification_rerunning",
                            "verification_repairing",
                        ),
                        "failed",
                    )
                    results.append("won")
                except ConflictError:
                    results.append("conflict")
            finally:
                con.close()

        t1 = threading.Thread(target=worker, daemon=True)
        t2 = threading.Thread(target=worker, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        assert not t1.is_alive() and not t2.is_alive(), "workers timed out"

        outcomes = sorted(results)
        assert outcomes == ["conflict", "won"], (
            f"Expected exactly one winner and one conflict, got {outcomes}. "
            "If both won, the CAS is racy and we may produce duplicate "
            "KIND_PLAN_CLOSED events; if both conflicted, the test harness "
            "failed to seed the row."
        )

        # Database must show the row transitioned exactly once.
        routing_stage, _, _ = _read_state(plan_id, db_path)
        assert routing_stage == "failed"

    def test_concurrent_force_terminal_via_http_only_one_200(self, tmp_path):
        """Smoke check at the HTTP layer — the TestClient may serialize
        requests, but at least the endpoint must accept both calls
        without raising. We don't assert [200, 409] here because the
        serialization order is implementation-defined; the SQL CAS is
        tested separately in the previous test."""
        plan_id = "vp20260906-concurrent-http-smoke"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        r1 = client.post(f"/api/verification/{plan_id}/force_terminal")
        r2 = client.post(f"/api/verification/{plan_id}/force_terminal")
        # First call wins with 200, second sees failed and 409s.
        statuses = sorted([r1.status_code, r2.status_code])
        assert statuses == [200, 409], (
            f"Expected one 200 and one 409, got {statuses}. "
            "Either both serialized 200s (would indicate the second call "
            "didn't see the first's CAS — a regression) or both 409ed."
        )


# ===========================================================================
# (f) plan_closed event fires when force_terminal runs
# ===========================================================================


class TestEventFiring:
    def test_force_terminal_publishes_plan_closed_event(self, tmp_path, monkeypatch):
        """Scenario (f): the Feishu notifier subscribes to
        ``KIND_PLAN_CLOSED`` on the state bus. After force_terminal
        succeeds, the bus must publish that event so the card updates.

        We monkeypatch ``publish_safe`` (the publish_safe wrapper in
        ``notifications.state_events``) to capture events without
        actually delivering them to subscribers.
        """
        from notifications import state_events
        captured: list = []
        original_publish_safe = state_events.publish_safe

        def capturing_publish_safe(kind, plan_id_arg, **payload):
            captured.append({"kind": kind, "plan_id": plan_id_arg, "payload": payload})
            return original_publish_safe(kind, plan_id_arg, **payload)

        monkeypatch.setattr(state_events, "publish_safe", capturing_publish_safe)

        plan_id = "vp20260906-plan-closed-event"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        resp = client.post(f"/api/verification/{plan_id}/force_terminal")
        assert resp.status_code == 200, resp.text

        # Must have observed at least one KIND_PLAN_CLOSED for this plan.
        plan_closed_events = [
            e for e in captured
            if e["kind"] == state_events.KIND_PLAN_CLOSED
            and e["plan_id"] == plan_id
        ]
        assert plan_closed_events, (
            f"Expected at least one KIND_PLAN_CLOSED event for {plan_id}, "
            f"got kinds: {[e['kind'] for e in captured]}"
        )

    def test_watchdog_path_publishes_plan_closed_event(self, tmp_path, monkeypatch):
        """Same expectation for the watchdog (lazy check) path —
        ``_persist_verification_terminal`` is what both paths share, so
        the event-firing contract must hold there too."""
        from notifications import state_events
        captured: list = []
        original_publish_safe = state_events.publish_safe

        def capturing_publish_safe(kind, plan_id_arg, **payload):
            captured.append({"kind": kind, "plan_id": plan_id_arg, "payload": payload})
            return original_publish_safe(kind, plan_id_arg, **payload)

        monkeypatch.setattr(state_events, "publish_safe", capturing_publish_safe)

        plan_id = "vp20260906-watchdog-event"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        # Synthesize a dead thread.
        dead_thread = threading.Thread(target=lambda: None)
        past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
        _verification_state[plan_id] = _running_verification_state(
            thread_obj=dead_thread, started_at=past_iso
        )

        _lazy_check_verification(plan_id)

        plan_closed_events = [
            e for e in captured
            if e["kind"] == state_events.KIND_PLAN_CLOSED
            and e["plan_id"] == plan_id
        ]
        assert plan_closed_events, (
            "Watchdog path must also publish KIND_PLAN_CLOSED — that's "
            "what triggers the Feishu card update."
        )


# ===========================================================================
# (g) /api/debug/verification_watchdog — observability contract
# ===========================================================================


class TestWatchdogObservability:
    def test_endpoint_returns_recent_actions_after_force_terminal(self, tmp_path):
        """After a force_terminal, the endpoint must surface the action
        in ``recent_terminal_actions`` so operators can see what happened.
        """
        plan_id = "vp20260906-debug-endpoint"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        resp = client.post(f"/api/verification/{plan_id}/force_terminal")
        assert resp.status_code == 200, resp.text

        debug = client.get("/api/debug/verification_watchdog")
        assert debug.status_code == 200, debug.text
        body = debug.json()

        # Required keys per the endpoint contract.
        for key in (
            "interval_seconds",
            "staleness_threshold_seconds",
            "last_sweep_at",
            "last_sweep_count",
            "recent_terminal_actions",
            "per_plan_last_action_ts",
        ):
            assert key in body, f"missing key {key!r} in response: {body}"

        # The action we just performed must be in the recent list.
        matching = [
            a for a in body["recent_terminal_actions"]
            if a["plan_id"] == plan_id
            and a["stop_reason"] == "user_force_terminal"
        ]
        assert matching, (
            f"Expected recent_terminal_actions to contain user_force_terminal "
            f"for {plan_id}, got: {body['recent_terminal_actions']}"
        )

    def test_endpoint_records_last_sweep_after_heartbeat_tick(self):
        """Triggering a single ``HeartbeatMonitor._check_once`` invocation
        (even on an empty server) must stamp ``last_sweep_at``."""
        monitor = HeartbeatMonitor(interval=999.0)  # huge interval — no auto-tick
        before_ts = _WATCHDOG_STATS["last_sweep_at"]
        # No plans in flight — the early-exit path runs but should still
        # stamp the sweep timestamp (we moved the stats update ABOVE the
        # early-exit in ``_check_once``).
        monitor._check_once()
        after_ts = _WATCHDOG_STATS["last_sweep_at"]
        assert after_ts is not None
        assert after_ts >= (before_ts or 0.0)


# ===========================================================================
# Dedup: a stuck plan should only fire _persist_verification_terminal ONCE
# per episode — otherwise we get duplicate KIND_PLAN_CLOSED events → duplicate
# Feishu card pushes.
# ===========================================================================


class TestWatchdogDedup:
    def test_repeated_lazy_check_does_not_republish_plan_closed(
        self, tmp_path, monkeypatch
    ):
        """Calling ``_lazy_check_verification`` twice on the same stuck
        plan within the dedup window must only fire one
        ``KIND_PLAN_CLOSED``. We measure this by counting calls to
        ``_persist_verification_terminal``.
        """
        from notifications import state_events

        persist_calls: list = []
        original_persist = _persist_verification_terminal

        def counting_persist(plan_id_arg, status, stop_reason, **kwargs):
            persist_calls.append((plan_id_arg, status, stop_reason))
            return original_persist(
                plan_id_arg, status, stop_reason, **kwargs
            )

        monkeypatch.setattr("server._persist_verification_terminal", counting_persist)

        plan_id = "vp20260906-dedup"
        db_path = tmp_path / "state.db"
        _setup_plan(plan_id)
        _seed_routing_phase(plan_id, "verification_running", db_path)

        dead_thread = threading.Thread(target=lambda: None)
        past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
        _verification_state[plan_id] = _running_verification_state(
            thread_obj=dead_thread, started_at=past_iso
        )

        # First tick — should detect + persist.
        _lazy_check_verification(plan_id)
        # Second tick — must be a no-op (within 60s dedup window).
        _lazy_check_verification(plan_id)
        # Third tick — also no-op.
        _lazy_check_verification(plan_id)

        assert len(persist_calls) == 1, (
            f"Expected exactly 1 _persist_verification_terminal call (dedup), "
            f"got {len(persist_calls)}: {persist_calls}"
        )


# ===========================================================================
# Helpers used by multiple tests
# ===========================================================================


def test_latest_mtime_helper():
    """``_latest_mtime`` returns None for an empty glob, max(st_mtime) otherwise."""
    from server import PLANS_DIR

    # Empty dir: no matching files.
    plan_dir = PLANS_DIR / "fresh-plan-no-files"
    plan_dir.mkdir(parents=True, exist_ok=True)
    assert _latest_mtime(plan_dir, "*.log") is None

    # Two files — return the larger st_mtime.
    f1 = plan_dir / "a.log"
    f2 = plan_dir / "b.log"
    f1.write_text("a", encoding="utf-8")
    f2.write_text("b", encoding="utf-8")
    os.utime(f1, (time.time() - 100, time.time() - 100))
    os.utime(f2, (time.time() - 50, time.time() - 50))
    mtime = _latest_mtime(plan_dir, "*.log")
    assert mtime is not None
    assert abs(mtime - (time.time() - 50)) < 1.0


def test_record_watchdog_action_writes_dedup_key():
    """``_record_watchdog_action`` must stamp per-plan dedup so the lazy
    check knows it already acted on this plan."""
    plan_id = "test-dedup-stamp"
    assert _WATCHDOG_STATS["per_plan_last_action_ts"].get(plan_id) is None
    before = time.time()
    _record_watchdog_action(plan_id, "verification_thread_died_unexpectedly")
    after = time.time()

    ts = _WATCHDOG_STATS["per_plan_last_action_ts"][plan_id]
    assert before <= ts <= after

    actions = list(_WATCHDOG_STATS["actions"])
    assert any(
        a["plan_id"] == plan_id
        and a["stop_reason"] == "verification_thread_died_unexpectedly"
        for a in actions
    )


# ===========================================================================
# Plan-state mirror: the helper must update SQLite ``plan_routing.verification``
# without clobbering ``stage`` (which the caller already CAS'd to
# ``failed``).
# ===========================================================================


class TestPlanStateMirror:
    """``_update_plan_state_to_terminal`` keeps the ``plan_routing.verification``
    JSON column in sync with the terminal transition that the SQL CAS on
    ``stage`` just won, without touching ``stage`` itself.

    Why this exists: ``/api/plan/{id}/summary`` reads
    ``state.verification.status`` from that JSON column (via
    ``PlanState._sqlite_row_to_state``); without this update, the Feishu
    notifier would keep rendering "still verifying" even though
    ``plan_routing.current_phase`` is already ``failed``.

    Regression target: the earlier version of this helper called
    ``PlanState._save_state``, which re-derived the routing value from
    ``current_phase`` through a projection map with no entry for
    ``"failed"`` — so it clobbered the SQL-CAS'd terminal. The fix is a
    raw SQL UPDATE on the ``verification`` column only. Since v5 the
    projection map is gone, but the helper still must not re-enter
    ``_save_state``: that would rewrite the phase from a stale
    ``PlanState`` snapshot.
    """

    def _seed_plan_routing_row(
        self,
        plan_id: str,
        phase: str,
        verification_json: str,
    ) -> None:
        """Insert a ``plan_routing`` row directly so we control every column.

        Goes through ``RoutingRepository`` so the ``_migrate`` schema is
        applied (otherwise raw ``INSERT INTO plan_routing`` fails with
        "no such table" on a fresh tmp_path/state.db).
        """
        from server import _state_db_path
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )
        conn = _open_db(str(_state_db_path()))
        try:
            _migrate(conn)
            repo = RoutingRepository(conn)
            # ``insert`` lands the plan at the phase so the routing
            # row exists; we then overwrite phase / verification to
            # the exact values the test wants (bypassing legal
            # transitions on purpose — this helper models the
            # post-CAS end state).
            repo.insert(plan_id, phase)
            conn.execute(
                """
                UPDATE plan_routing SET
                    current_phase = ?,
                    verification = ?,
                    last_updated = ?,
                    version = 1
                WHERE plan_id = ?
                """,
                (phase, verification_json,
                 datetime.now().isoformat(), plan_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _read_plan_routing_row(self, plan_id: str) -> dict:
        from server import _state_db_path
        conn = sqlite3.connect(_state_db_path())
        try:
            cur = conn.execute(
                "SELECT plan_id, current_phase, verification FROM plan_routing WHERE plan_id = ?",
                (plan_id,),
            )
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else {}
        finally:
            conn.close()

    def test_helper_updates_verification_column_only(
        self, monkeypatch, tmp_path,
    ):
        """The helper must update ``verification`` JSON, leaving
        ``current_phase`` exactly as the caller left them."""
        from server import _update_plan_state_to_terminal
        monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")

        plan_id = "mirror-test-001"
        existing_verif = (
            '{"status": "running", "round": 2, "max_rounds": 3, '
            '"stop_reason": "repair_execution_failed"}'
        )
        self._seed_plan_routing_row(
            plan_id=plan_id,
            phase="failed",
            verification_json=existing_verif,
        )

        _update_plan_state_to_terminal(plan_id, "user_force_terminal")

        row = self._read_plan_routing_row(plan_id)
        assert row["current_phase"] == "failed", (
            f"helper MUST NOT touch current_phase; got {row['current_phase']!r}"
        )
        verif = json.loads(row["verification"])
        assert verif["status"] == "failed"
        assert verif["round"] == 2  # preserved from existing
        assert verif["max_rounds"] == 3  # preserved from existing
        assert verif["stop_reason"] == "user_force_terminal"

    def test_helper_preserves_round_when_existing_verification_is_null(
        self, monkeypatch, tmp_path,
    ):
        """Defensive: if ``verification`` was NULL/missing, defaults
        to round=0 / the default round budget rather than crashing."""
        from server import (
            _update_plan_state_to_terminal,
            DEFAULT_MAX_VERIFICATION_ROUNDS,
        )
        monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")

        plan_id = "mirror-test-null"
        self._seed_plan_routing_row(
            plan_id=plan_id,
            phase="failed",
            verification_json=json.dumps({}),  # empty dict → defaults
        )

        _update_plan_state_to_terminal(plan_id, "verification_thread_died_unexpectedly")

        row = self._read_plan_routing_row(plan_id)
        verif = json.loads(row["verification"])
        assert verif["round"] == 0
        assert verif["max_rounds"] == DEFAULT_MAX_VERIFICATION_ROUNDS
        assert verif["stop_reason"] == "verification_thread_died_unexpectedly"

    def test_helper_skips_when_no_plan_routing_row(
        self, monkeypatch, tmp_path, caplog,
    ):
        """Helper must NOT crash when the row doesn't exist (e.g. the
        plan was force_terminal'd before its row was seeded)."""
        from server import _update_plan_state_to_terminal
        monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")

        # No _seed_plan_routing_row call → row does not exist.
        with caplog.at_level("WARNING"):
            _update_plan_state_to_terminal("ghost-plan", "user_force_terminal")
        assert any(
            "no plan_routing row" in rec.message
            for rec in caplog.records
        ), f"expected a warning; got {[r.message for r in caplog.records]}"