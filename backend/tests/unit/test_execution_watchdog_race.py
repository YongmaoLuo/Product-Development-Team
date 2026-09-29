"""TDD tests for the 2026-09-11 plan v11 watchdog race condition fix.

Background (2026-09-11):
  ``_lazy_check_execution`` (server.py:4134) is a HeartbeatMonitor
  watchdog that ticks every ``HEARTBEAT_INTERVAL`` (30s). On each tick
  it probes the executor PID with ``os.kill(pid, 0)`` and, on
  ``ProcessLookupError``, calls ``_mark_failed_dead(plan_id)`` which
  writes ``status="failed"`` + ``stop_reason="process_died_unexpectedly"``
  + ExecutionRepository.update_status("failed").

  The executor's ``_run`` 协程 (server.py:5966+) calls
  ``process.wait()`` which returns the instant the executor exits. Then
  ~30ms later, the success path (line 6163+) writes
  ``state["status"]="completed"`` and persists. If a watchdog tick
  lands inside that ~30ms window — which IS realistic since the
  watchdog ticks every 30s — the watchdog's ``_mark_failed_dead``
  races the success path and overwrites in-memory state["status"],
  leaving the state machine stuck at ``failed`` even though
  the executor returned 0 and emitted ``execution_cleanup`` cleanly.

  The v11 contract: ``_run`` 协程 sets
  ``state["executor_finished_cleanly"]=True`` as soon as
  ``process.wait()`` returns (BEFORE any state mutation). The
  watchdog checks this flag on ``ProcessLookupError``; if True, the
  success path is already in flight, so the watchdog yields and does
  NOT fire ``_mark_failed_dead``.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server import (
    _execution_state,
    _lazy_check_execution,
    _mark_failed_dead,
    app,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Clear global execution state + redirect PLANS_DIR."""
    _execution_state.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    yield
    _execution_state.clear()


def _setup_state(plan_id: str, pid: int = 99999, **state_overrides):
    """Populate _execution_state[plan_id] with a 'running' plan state.

    Returns the state dict so the test can mutate it directly.
    """
    state = {
        "status": "running",
        "pid": pid,
        "started_at": "2026-09-11T06:00:00Z",
        "ended_at": None,
        "stop_reason": None,
        "project_dir": "/tmp/fake",
        "logs": [],
        "process": None,
    }
    state.update(state_overrides)
    _execution_state[plan_id] = state
    return state


# ---------------------------------------------------------------------------
# Fix contract — the watchdog must NOT race the success path
# ---------------------------------------------------------------------------


def test_lazy_check_yields_when_executor_finished_cleanly():
    """``executor_finished_cleanly=True`` + ProcessLookupError → no-op.

    Reproduces the race: ``process.wait()`` returned,
    the success path is about to write ``state["status"]="completed"``,
    but the watchdog tick fires ``os.kill(pid, 0)`` first.

    Expected: ``_lazy_check_execution`` sees the flag and returns
    without calling ``_mark_failed_dead``. ``state["status"]`` stays
    at "running" so the success path can complete its write.
    """
    plan_id = "test-plan-watchdog-yield"
    state = _setup_state(plan_id, pid=99999, executor_finished_cleanly=True)

    # Simulate PID gone (ProcessLookupError from os.kill)
    with patch("server.os.kill", side_effect=ProcessLookupError):
        with patch("server._mark_failed_dead") as mock_mark:
            _lazy_check_execution(plan_id)

    # Critical: _mark_failed_dead must NOT be called
    mock_mark.assert_not_called()
    # State must be unchanged (success path will write it)
    assert state["status"] == "running"


def test_lazy_check_fires_when_executor_truly_dead_no_flag():
    """No ``executor_finished_cleanly`` flag + ProcessLookupError → fires.

    Negative test: the same race window but ``_run`` 协程 has NOT yet
    marked ``executor_finished_cleanly=True`` (e.g. ``process.wait()``
    hasn't returned yet — perhaps the executor was SIGKILL'd before
    it could emit ``execution_cleanup``). In this case the watchdog
    should fire ``_mark_failed_dead`` as before.
    """
    plan_id = "test-plan-watchdog-fire"
    state = _setup_state(plan_id, pid=99999)
    # executor_finished_cleanly NOT set (default = missing)

    with patch("server.os.kill", side_effect=ProcessLookupError):
        with patch("server._mark_failed_dead") as mock_mark:
            _lazy_check_execution(plan_id)

    mock_mark.assert_called_once_with(plan_id)


def test_lazy_check_ignores_permission_error():
    """``PermissionError`` from ``os.kill`` → no-op (preserved behaviour)."""
    plan_id = "test-plan-permission"
    state = _setup_state(plan_id, pid=99999)

    with patch("server.os.kill", side_effect=PermissionError):
        with patch("server._mark_failed_dead") as mock_mark:
            _lazy_check_execution(plan_id)

    mock_mark.assert_not_called()
    assert state["status"] == "running"


def test_lazy_check_no_op_when_status_not_running():
    """``status != "running"`` → no-op at entry (preserved behaviour)."""
    plan_id = "test-plan-completed"
    _setup_state(plan_id, pid=99999, status="completed",
                 executor_finished_cleanly=False)

    with patch("server.os.kill") as mock_kill:
        with patch("server._mark_failed_dead") as mock_mark:
            _lazy_check_execution(plan_id)

    mock_kill.assert_not_called()  # exited before probing PID
    mock_mark.assert_not_called()


def test_lazy_check_yields_even_after_mark_completed_by_success_path():
    """``state["status"]="completed"`` (set by success path first) → no-op.

    Race scenario: success path writes ``status="completed"`` BEFORE
    the watchdog tick. The watchdog's existing
    ``state.get("status") != "running"`` guard already handles this
    case (no-op at entry). This test pins that preserved behaviour
    so the v11 fix doesn't accidentally regress it.
    """
    plan_id = "test-plan-already-completed"
    _setup_state(plan_id, pid=99999, status="completed",
                 executor_finished_cleanly=True)

    with patch("server.os.kill") as mock_kill:
        with patch("server._mark_failed_dead") as mock_mark:
            _lazy_check_execution(plan_id)

    mock_kill.assert_not_called()
    mock_mark.assert_not_called()