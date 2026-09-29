"""TDD tests for HeartbeatMonitor verification-thread death detection (VP-011).

Validates that when a verification background thread dies (exits silently
without writing its terminal status), the HeartbeatMonitor / lazy check
detects this on the next tick and:

  - sets ``_verification_state[plan_id]['verification_status'] = 'failed'``
  - sets ``_verification_state[plan_id]['stop_reason'] = 'verification_thread_died_unexpectedly'``
"""

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import (
    HeartbeatMonitor,
    _execution_state,
    _lazy_check_verification,
    _lazy_check_execution,
    _lazy_check_sub_agents,
    _mark_verification_failed_dead,
    _verification_locks,
    _verification_state,
    app,
)
from plan_state import PlanState
from sub_agent_registry import sub_agent_registry

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Clear global verification state and redirect PLANS_DIR before each test."""
    _verification_state.clear()
    _verification_locks.clear()
    _execution_state.clear()
    # Drain sub-agent registry to a known empty state.
    for plan_id in list(sub_agent_registry._by_plan.keys()):
        sub_agent_registry.cleanup_for_plan(plan_id)
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    yield
    _verification_state.clear()
    _verification_locks.clear()
    _execution_state.clear()
    for plan_id in list(sub_agent_registry._by_plan.keys()):
        sub_agent_registry.cleanup_for_plan(plan_id)


def _setup_plan(plan_id: str, phase: str = "verification_running") -> Path:
    """Create a minimal plan directory with the given current_phase."""
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
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


def _running_verification_state(thread_obj=None) -> dict:
    """Build a typical in-memory _verification_state[plan_id] for an active run."""
    return {
        "plan_id": "test-plan",
        "verification_status": "running",
        "verification_round": 1,
        "verification_max_rounds": 3,
        "results": {"pytest_summary": "", "llm_findings": "", "performance_metrics": {}},
        "repair_tasks": [],
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "orchestrator": None,
        "stop_reason": None,
        "thread": thread_obj,
    }


class TestVerificationThreadDeath:
    """HeartbeatMonitor must mark verification as failed when the thread dies."""

    def test_dead_thread_marked_failed(self, monkeypatch):
        """A dead thread reference → verification_state goes to failed with the expected stop_reason."""
        plan_id = "vp011-dead-thread"
        plan_dir = _setup_plan(plan_id)

        # Build a thread object that is *not* alive (never started)
        dead_thread = threading.Thread(target=lambda: None)
        # Not started → not alive
        assert not dead_thread.is_alive()

        _verification_state[plan_id] = _running_verification_state(thread_obj=dead_thread)

        # Drive the check
        _lazy_check_verification(plan_id)

        assert _verification_state[plan_id]["verification_status"] == "failed"
        assert _verification_state[plan_id]["stop_reason"] == "verification_thread_died_unexpectedly"
        assert _verification_state[plan_id].get("ended_at") is not None

    def test_alive_thread_unchanged(self):
        """A live thread → no state change."""
        plan_id = "vp011-alive-thread"
        plan_dir = _setup_plan(plan_id)

        started = threading.Event()
        stop_flag = threading.Event()

        def worker():
            started.set()
            stop_flag.wait(timeout=2.0)

        live_thread = threading.Thread(target=worker, daemon=True)
        live_thread.start()
        try:
            assert started.wait(timeout=2.0)
            assert live_thread.is_alive()

            _verification_state[plan_id] = _running_verification_state(thread_obj=live_thread)

            _lazy_check_verification(plan_id)

            assert _verification_state[plan_id]["verification_status"] == "running"
            assert _verification_state[plan_id]["stop_reason"] is None
        finally:
            stop_flag.set()
            live_thread.join(timeout=2.0)

    def test_finished_thread_marked_failed(self):
        """A thread that started, ran, and exited → marked as failed by the lazy check."""
        plan_id = "vp011-finished-thread"
        plan_dir = _setup_plan(plan_id)

        finished = threading.Event()

        def quick_worker():
            finished.set()

        finished_thread = threading.Thread(target=quick_worker, daemon=True)
        finished_thread.start()
        finished_thread.join(timeout=2.0)
        assert finished.is_set()
        assert not finished_thread.is_alive()

        _verification_state[plan_id] = _running_verification_state(thread_obj=finished_thread)

        _lazy_check_verification(plan_id)

        assert _verification_state[plan_id]["verification_status"] == "failed"
        assert _verification_state[plan_id]["stop_reason"] == "verification_thread_died_unexpectedly"

    def test_idempotent_when_already_failed(self, monkeypatch):
        """Calling _mark_verification_failed_dead twice is a no-op the second time."""
        plan_id = "vp011-idempotent"
        plan_dir = _setup_plan(plan_id)

        dead_thread = threading.Thread(target=lambda: None)
        assert not dead_thread.is_alive()

        _verification_state[plan_id] = _running_verification_state(thread_obj=dead_thread)

        _lazy_check_verification(plan_id)
        first_updated_at = _verification_state[plan_id]["updated_at"]
        first_stop_reason = _verification_state[plan_id]["stop_reason"]

        # Second call should not re-mutate the state (status is no longer 'running')
        _lazy_check_verification(plan_id)
        assert _verification_state[plan_id]["verification_status"] == "failed"
        assert _verification_state[plan_id]["stop_reason"] == "verification_thread_died_unexpectedly"
        assert _verification_state[plan_id]["updated_at"] == first_updated_at

    def test_mark_function_directly(self, monkeypatch):
        """Direct call to _mark_verification_failed_dead sets the expected fields."""
        plan_id = "vp011-direct-mark"
        plan_dir = _setup_plan(plan_id)

        _verification_state[plan_id] = _running_verification_state(thread_obj=None)

        _mark_verification_failed_dead(plan_id)

        state = _verification_state[plan_id]
        assert state["verification_status"] == "failed"
        assert state["stop_reason"] == "verification_thread_died_unexpectedly"
        assert state.get("ended_at") is not None

    def test_heartbeat_check_scans_verification(self, monkeypatch):
        """HeartbeatMonitor._check_once walks _verification_state and applies the lazy check."""
        plan_id = "vp011-monitor-scan"
        plan_dir = _setup_plan(plan_id)

        dead_thread = threading.Thread(target=lambda: None)
        assert not dead_thread.is_alive()
        _verification_state[plan_id] = _running_verification_state(thread_obj=dead_thread)

        # Stub the execution-side check to avoid coupling on _execution_state
        monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)

        monitor = HeartbeatMonitor(interval=60.0)
        monitor._check_once()

        assert _verification_state[plan_id]["verification_status"] == "failed"
        assert _verification_state[plan_id]["stop_reason"] == "verification_thread_died_unexpectedly"

    def test_check_once_early_exits_when_all_state_empty(self, monkeypatch):
        """HeartbeatMonitor._check_once must skip lazy checks entirely when
        no execution / verification / sub-agents are registered.

        Without the early-exit, every 30s tick on an idle server acquires
        three registry locks and iterates empty dicts. With the early-exit,
        none of the per-tick lock acquisitions happen.
        """
        # All clean_state fixture work has left _execution_state,
        # _verification_state and sub_agent_registry empty.
        assert _execution_state == {}
        assert _verification_state == {}
        assert sub_agent_registry._by_plan == {}

        # Track calls into the three lazy-check entry points.
        exec_calls: list = []
        verif_calls: list = []
        sub_calls: list = []

        monkeypatch.setattr(
            "server._lazy_check_execution",
            lambda pid: exec_calls.append(pid),
        )
        monkeypatch.setattr(
            "server._lazy_check_verification",
            lambda pid: verif_calls.append(pid),
        )
        monkeypatch.setattr(
            "server._lazy_check_sub_agents",
            lambda: sub_calls.append(True),
        )

        monitor = HeartbeatMonitor(interval=60.0)
        monitor._check_once()

        # Early-exit contract: none of the three expensive checks fire.
        assert exec_calls == [], (
            f"_lazy_check_execution should not be called when "
            f"_execution_state is empty; got {exec_calls!r}"
        )
        assert verif_calls == [], (
            f"_lazy_check_verification should not be called when "
            f"_verification_state is empty; got {verif_calls!r}"
        )
        assert sub_calls == [], (
            f"_lazy_check_sub_agents should not be called when "
            f"sub_agent_registry is empty; got {sub_calls!r}"
        )

    def test_check_once_runs_when_any_state_nonempty(self, monkeypatch):
        """HeartbeatMonitor._check_once must run the corresponding lazy check
        whenever at least one of the three state sources is non-empty.

        Guards against an early-exit that is too aggressive — i.e. one
        that returns purely because _execution_state is empty even when
        _verification_state has work to do.
        """
        plan_id = "vp011-early-exit-nonempty"
        _setup_plan(plan_id)

        # Inject a dead verification thread — this should still be detected.
        dead_thread = threading.Thread(target=lambda: None)
        assert not dead_thread.is_alive()
        _verification_state[plan_id] = _running_verification_state(thread_obj=dead_thread)

        # Stub _lazy_check_execution so this assertion only depends on
        # verification + sub-agent paths.
        monkeypatch.setattr("server._lazy_check_execution", lambda pid: None)
        sub_calls: list = []
        monkeypatch.setattr(
            "server._lazy_check_sub_agents",
            lambda: sub_calls.append(True),
        )

        monitor = HeartbeatMonitor(interval=60.0)
        monitor._check_once()

        # Verification state was walked → thread was marked dead.
        assert _verification_state[plan_id]["verification_status"] == "failed"
        assert _verification_state[plan_id]["stop_reason"] == "verification_thread_died_unexpectedly"
        # _verification_state was non-empty, so the early-exit must NOT
        # short-circuit — _lazy_check_sub_agents still fires even though
        # the registry itself is empty.
        assert sub_calls == [True]
