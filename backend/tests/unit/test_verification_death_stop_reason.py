"""2026-11-02: the watchdog must record the reason it ACTUALLY fired for.

``_mark_verification_failed_dead`` hard-wired ``stop_reason`` to
``verification_thread_died_unexpectedly``.  A round that was declared
dead because its log went silent — or because its results file went
stale — was therefore written down as a *thread* death.  The record
lied: the thread was still sitting there, and every post-mortem that
trusted the record went looking for the wrong failure.

The DB half of the record was already honest (``_persist_verification_terminal``
was handed the real reason), so the plan's routing row said
``verification_log_stale`` while the in-memory state and the
side-channel ``.verification_runtime.json`` said
``verification_thread_died_unexpectedly``.  Two records of one event,
and they disagreed.

What this file pins:

  * the reason the watchdog DETECTED is the reason the state RECORDS;
  * the state and the side-channel ``.verification_runtime.json`` carry
    the same value;
  * a caller that passes no reason keeps the old fallback
    (``verification_thread_died_unexpectedly``) — the thread-death path
    is unchanged;
  * a state that has already failed is not overwritten.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import server as server_mod


PLAN = "plan-death-stop-reason"

#: The reason each watchdog trigger must be recorded with.  The
#: assertions below read the expected value out of here and name the
#: trigger they exercise, so an assertion always states the reason of
#: the TRIGGER — never a literal that could belong to a different one.
THREAD_DEATH_REASON = "verification_thread_died_unexpectedly"
LOG_STALE_REASON = "verification_log_stale"
RESULTS_STALE_REASON = "verification_results_stale"


def _running_state(thread_alive: bool = True) -> dict:
    """A running verification whose thread is alive unless told otherwise."""
    thread = Mock()
    thread.is_alive.return_value = thread_alive
    return {
        "plan_id": PLAN,
        "verification_status": "running",
        "verification_round": 2,
        "thread": thread,
        "started_at": "2026-09-14T17:00:00",
        "started_at_ts": time.time() - 3600.0,  # well past the startup grace
        "stop_reason": None,
    }


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """A plan directory with the watchdog's knobs pinned to test values."""
    plan_dir = tmp_path / "plans" / PLAN
    plan_dir.mkdir(parents=True)
    monkeypatch.setattr(server_mod, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(
        server_mod, "VERIFICATION_WATCHDOG_STALENESS_SECONDS", 900.0,
    )
    monkeypatch.setattr(server_mod, "VERIFICATION_HARD_STALE_SECONDS", 2700.0)
    # No live process references the VP artifacts: the liveness probe
    # must not find a signal that is not there.
    monkeypatch.setattr(server_mod, "_vp_artifact_processes", lambda vp_id: [])
    # Reset the per-plan dedup clock so the lazy check always acts.
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN, None)
    server_mod._verification_state[PLAN] = _running_state()
    yield plan_dir
    server_mod._verification_state.pop(PLAN, None)
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN, None)


def _write_stale_round_log(plan_dir: Path, age: float) -> Path:
    """A round log that stopped being written ``age`` seconds ago.

    It carries no in-flight VP (no ``vp_start`` without a
    ``vp_complete``), so the triage branch sees no work in flight and
    lets the staleness verdict stand.
    """
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / "verification_2_20260914_172324.log"
    p.write_text("{}\n", encoding="utf-8")
    old = time.time() - age
    os.utime(p, (old, old))
    return p


def _write_stale_results(plan_dir: Path, age: float) -> Path:
    """A results file that stopped being written ``age`` seconds ago."""
    p = plan_dir / "verification_execution_results.json"
    p.write_text(json.dumps({"results": []}), encoding="utf-8")
    old = time.time() - age
    os.utime(p, (old, old))
    return p


def _run_lazy_check() -> Mock:
    """Run one watchdog tick with the terminal persist observable.

    ``_mark_verification_failed_dead`` is NOT stubbed: the in-memory
    state and the side-channel file it writes are the subject here.
    """
    with patch.object(server_mod, "_persist_verification_terminal") as persist, \
         patch.object(server_mod, "_update_plan_state_to_terminal"):
        server_mod._lazy_check_verification(PLAN)
    return persist


# ---------------------------------------------------------------------------
# the reason the state records
# ---------------------------------------------------------------------------

def test_log_stale_records_verification_log_stale(plan_env):
    """A round whose log went silent is recorded as a LOG death.

    Before 2026-11-02 this wrote ``verification_thread_died_unexpectedly``
    — the thread was alive; it was the log that was silent.
    """
    plan_dir = plan_env
    # Backdated past the hard cap: an alive thread is held until then.
    _write_stale_round_log(plan_dir, age=3000.0)

    persist = _run_lazy_check()

    state = server_mod._verification_state[PLAN]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == LOG_STALE_REASON, (
        "the recorded reason must be the reason the watchdog detected — "
        "a log-stale round is not a thread death"
    )
    # The plan-level record must agree with the in-memory one.
    assert persist.call_args[0][2] == LOG_STALE_REASON


def test_results_stale_records_verification_results_stale(plan_env):
    """A round whose results file went stale is recorded as a RESULTS death.

    No round log exists here, so the log globs match nothing and the
    results file is what the ladder finds.
    """
    plan_dir = plan_env
    _write_stale_results(plan_dir, age=3000.0)

    persist = _run_lazy_check()

    state = server_mod._verification_state[PLAN]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == RESULTS_STALE_REASON, (
        "the recorded reason must be the reason the watchdog detected — "
        "a results-stale round is not a thread death"
    )
    assert persist.call_args[0][2] == RESULTS_STALE_REASON


def test_thread_death_still_records_thread_reason(plan_env):
    """The thread-death path is unchanged: it still records the thread reason."""
    plan_dir = plan_env
    server_mod._verification_state[PLAN]["thread"] = (
        _running_state(thread_alive=False)["thread"]
    )

    persist = _run_lazy_check()

    state = server_mod._verification_state[PLAN]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == THREAD_DEATH_REASON
    assert persist.call_args[0][2] == THREAD_DEATH_REASON


# ---------------------------------------------------------------------------
# the side-channel snapshot
# ---------------------------------------------------------------------------

def test_runtime_json_carries_actual_reason(plan_env):
    """``.verification_runtime.json`` carries the SAME reason as the state.

    The file is what a restarted server reads back, so a snapshot that
    said "thread died" for a log-stale round would resurrect the lie
    after a restart.
    """
    plan_dir = plan_env
    _write_stale_round_log(plan_dir, age=3000.0)

    _run_lazy_check()

    runtime_path = plan_dir / ".verification_runtime.json"
    assert runtime_path.exists(), "the side-channel snapshot must be written"
    persisted = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert persisted["verification_status"] == "failed"
    assert persisted["stop_reason"] == LOG_STALE_REASON, (
        "the side-channel snapshot must carry the reason the watchdog "
        "detected, not a hard-wired one"
    )


# ---------------------------------------------------------------------------
# the boundary conditions
# ---------------------------------------------------------------------------

def test_no_reason_falls_back_to_thread_death_reason(plan_env):
    """A caller that passes no reason keeps the old fallback."""
    server_mod._verification_state[PLAN] = _running_state(thread_alive=False)

    server_mod._mark_verification_failed_dead(PLAN)

    state = server_mod._verification_state[PLAN]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == THREAD_DEATH_REASON


def test_already_failed_state_is_not_overwritten(plan_env):
    """A state that has already failed keeps the reason it was given."""
    server_mod._verification_state[PLAN] = _running_state(thread_alive=False)
    server_mod._mark_verification_failed_dead(PLAN, LOG_STALE_REASON)
    first_updated_at = server_mod._verification_state[PLAN]["updated_at"]
    assert first_updated_at is not None

    server_mod._mark_verification_failed_dead(PLAN, RESULTS_STALE_REASON)

    state = server_mod._verification_state[PLAN]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == LOG_STALE_REASON, (
        "an already-failed state must not be overwritten by a later reason"
    )
    assert state["updated_at"] == first_updated_at
