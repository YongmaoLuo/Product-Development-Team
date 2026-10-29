"""A recovered executor gets a poll-watcher instead of a verdict by default.

Background (F4, 2026-10-27)
---------------------------
After a restart, ``_restore_execution_state_from_row``
(``backend/routes/execution.py``) reads the ``plan_execution`` row back
into memory.  The restored record carries ``"process": None`` — this
process did not spawn that executor, so it holds no handle to it — and
no watcher thread either.  The only thing that ever sets
``executor_finished_cleanly`` is the watcher that waits on the
executor, so a restored run has no way of saying "it finished
cleanly".

What happens then: ``_lazy_check_execution``
(``backend/verification_loop.py``) probes the pid; the pid is gone (the
executor already exited), so the watchdog calls ``_mark_failed_dead``
and a run that completed 7/7 tasks is recorded as a crash.

This task pins the groundwork for the fix: a recovered run whose pid
is still alive gets a **poll-watcher** — a thread that re-probes the
pid with ``os.kill(pid, 0)`` and settles the run once the pid
disappears.  The success/crash split (task 11) reads the completion
marker from that watcher; here the settle keeps today's behaviour
(``_mark_failed_dead``), so nothing else changes.

Contract pinned here:

1. The watcher polls with ``os.kill(pid, 0)`` and settles the run when
   the pid disappears.
2. ``PermissionError`` means "alive, but not ours to signal" — the
   watcher keeps waiting.
3. One watcher per plan: a second call returns the very same thread.
4. No ``Popen.wait`` — the recovered executor is not this process's
   child, so a handle to wait on does not exist.
5. ``pid <= 1`` (and ``None``) is not an executor: no watcher at all.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from routes import execution as execution_routes  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoWaitHandle:
    """A ``Popen``-shaped stand-in with **no** ``wait`` and no ``poll``.

    The recovered executor is not this process's child, so there is no
    handle to wait on: ``Popen.wait`` on a non-child blocks forever (or
    raises), which is why the watcher probes the pid instead.  This
    stand-in has neither method on purpose — if the watcher reached for
    either one, the settle in the test below would never happen.
    """

    pid = 4545


class _PidWithoutWait(int):
    """An int-shaped pid stand-in that has no ``wait`` method.

    Passed as the pid argument so the test also covers "the caller
    hands over something that is not a handle" — the watcher must
    neither call ``.wait()`` on it nor blow up on it.
    """


def _running_state(plan_id: str, pid: Optional[int]) -> Dict[str, Any]:
    """Seed the in-memory record a restored ``running`` row looks like."""
    state: Dict[str, Any] = {
        "status": "running",
        "pid": pid,
        "started_at": "2026-10-27T00:00:00Z",
        "ended_at": None,
        "stop_reason": None,
        "project_dir": "/tmp/fake",
        "logs": [],
        "process": None,
    }
    server._execution_state[plan_id] = state
    return state


def _fake_kill(
    target_pid: int, seen: List[int], outcome: str
):
    """An ``os.kill`` stand-in that only speaks for ``target_pid``.

    Every other pid is handed to the real ``os.kill``, so a watcher
    that belongs to another test keeps working while this one is
    patched in.

    Args:
        target_pid: the pid this stand-in answers for.
        seen: collects every call made for ``target_pid``.
        outcome: ``"alive"`` (never dies), ``"dead"`` (dies on the
            third probe) or ``"permission"`` (always raises
            ``PermissionError``).
    """
    real_kill = os.kill

    def _kill(pid, sig):
        if pid != target_pid:
            return real_kill(pid, sig)
        seen.append(pid)
        if outcome == "dead" and len(seen) >= 3:
            raise ProcessLookupError()
        if outcome == "permission":
            raise PermissionError()
        return None

    return _kill


def _settle_spy(monkeypatch) -> List[str]:
    """Record the settles the watcher performs instead of judging a run."""
    settled: List[str] = []
    monkeypatch.setattr(
        execution_routes,
        "_settle_recovered_execution",
        lambda plan_id: settled.append(plan_id),
    )
    return settled


def _stop_watcher(state: Dict[str, Any], watcher: threading.Thread) -> None:
    """Stop a watcher that is still polling and wait for it to end.

    A watcher only watches a *running* execution, so the run is taken
    out of ``running`` and the watcher ends by itself.  Every test that
    leaves a watcher alive has to hand it back: the autouse
    ``clean_execution_state`` fixture fails a test whose application
    worker outlives it.
    """
    state["status"] = "completed"
    watcher.join(timeout=5)
    assert not watcher.is_alive(), "the recovery watcher outlived its test"


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """Shorten the poll interval so the tests do not sleep for real.

    ``raising=False`` keeps the fixture working on a tree where the
    constant does not exist yet — the failure then lands in the test
    body, where it says what is missing.
    """
    monkeypatch.setattr(
        execution_routes, "_RECOVERY_POLL_INTERVAL_SECONDS", 0.01, raising=False
    )


# ---------------------------------------------------------------------------
# 1. The watcher polls the pid and settles when it disappears
# ---------------------------------------------------------------------------


def test_watcher_settles_when_pid_disappears(monkeypatch):
    """The pid vanishes on the third probe → the run is settled once.

    The watcher is the only thing that can say "the executor I did not
    spawn is gone"; without it the watchdog's probe is the only voice,
    and it cannot tell a crash from a clean exit.
    """
    plan_id = "plan-settle"
    _running_state(plan_id, pid=4242)
    settled = _settle_spy(monkeypatch)

    seen: List[int] = []
    monkeypatch.setattr(os, "kill", _fake_kill(4242, seen, "dead"))

    watcher = execution_routes._start_recovery_watcher(plan_id, 4242)

    assert watcher is not None, "no watcher was started for a live pid"
    watcher.join(timeout=5)

    assert not watcher.is_alive(), "the watcher kept polling after the pid vanished"
    assert settled == [plan_id], (
        "the vanished pid did not settle the run; "
        f"settled={settled!r}"
    )
    assert len(seen) == 3, (
        f"the watcher probed the pid {len(seen)} time(s); the third probe is "
        "the one that found it gone"
    )


def test_settle_recovered_execution_marks_the_run_failed(monkeypatch):
    """The settle keeps today's behaviour: ``_mark_failed_dead``.

    Task 10 only lays the groundwork — the success/crash split lands in
    task 11.  Until then the settle judges the run exactly like the
    watchdog does.
    """
    judged: List[str] = []
    monkeypatch.setattr(server, "_mark_failed_dead", lambda plan_id: judged.append(plan_id))

    execution_routes._settle_recovered_execution("plan-settle-contract")

    assert judged == ["plan-settle-contract"], (
        "the settle did not judge the run as dead; "
        f"judged={judged!r}"
    )


# ---------------------------------------------------------------------------
# 2. PermissionError means "alive", not "gone"
# ---------------------------------------------------------------------------


def test_permission_error_keeps_waiting(monkeypatch):
    """``PermissionError`` → the process is alive, keep polling.

    The pid belongs to another user: signalling it is not allowed, but
    it exists.  Reading that as "the process is gone" would mark a
    running executor dead.
    """
    plan_id = "plan-permission"
    state = _running_state(plan_id, pid=4343)
    settled = _settle_spy(monkeypatch)

    seen: List[int] = []
    monkeypatch.setattr(os, "kill", _fake_kill(4343, seen, "permission"))

    watcher = execution_routes._start_recovery_watcher(plan_id, 4343)

    assert watcher is not None
    time.sleep(0.2)

    assert watcher.is_alive(), "the watcher gave up on a PermissionError"
    assert settled == [], (
        "a PermissionError was read as 'the process is gone'; "
        f"settled={settled!r}"
    )

    _stop_watcher(state, watcher)


# ---------------------------------------------------------------------------
# 3. One watcher per plan
# ---------------------------------------------------------------------------


def test_watcher_is_not_started_twice(monkeypatch):
    """A second call returns the very same thread.

    Two watchers would settle the same run twice, and the second settle
    would judge a run that is already over.
    """
    plan_id = "plan-once"
    state = _running_state(plan_id, pid=4444)
    settled = _settle_spy(monkeypatch)

    seen: List[int] = []
    monkeypatch.setattr(os, "kill", _fake_kill(4444, seen, "alive"))

    first = execution_routes._start_recovery_watcher(plan_id, 4444)
    second = execution_routes._start_recovery_watcher(plan_id, 4444)

    assert first is not None
    assert second is first, "a second watcher was started for the same plan"
    assert state.get("recovery_watcher") is first, (
        "the watcher is not hung on state['recovery_watcher']"
    )
    assert first.daemon is True, (
        "the watcher is not a daemon thread — it would hold the process open"
    )

    _stop_watcher(state, first)


# ---------------------------------------------------------------------------
# 4. No Popen.wait — the recovered executor is not this process's child
# ---------------------------------------------------------------------------


def test_uses_no_popen_wait(monkeypatch):
    """The watcher probes the pid; it never waits on a process handle.

    ``state["process"]`` is ``None`` for a restored run, and a handle
    to a foreign process cannot be waited on anyway.  The stand-ins
    below have no ``wait`` and no ``poll``: if the watcher reached for
    either, the settle would never happen.
    """
    plan_id = "plan-nowait"
    state = _running_state(plan_id, pid=4545)
    state["process"] = _NoWaitHandle()
    settled = _settle_spy(monkeypatch)

    seen: List[int] = []
    monkeypatch.setattr(os, "kill", _fake_kill(4545, seen, "dead"))

    watcher = execution_routes._start_recovery_watcher(
        plan_id, _PidWithoutWait(4545)
    )

    assert watcher is not None, "a pid stand-in without a wait method was refused"
    watcher.join(timeout=5)

    assert not watcher.is_alive()
    assert settled == [plan_id], (
        "the watcher did not settle through os.kill alone — it reached for "
        "the process handle instead"
    )


# ---------------------------------------------------------------------------
# 5. A pid of 1 or less is not an executor
# ---------------------------------------------------------------------------


def test_pid_one_is_ignored():
    """pid 1 (init), pid 0 and ``None`` are not executors to watch."""
    plan_id = "plan-pid-one"
    state = _running_state(plan_id, pid=1)

    assert execution_routes._start_recovery_watcher(plan_id, 1) is None
    assert execution_routes._start_recovery_watcher(plan_id, 0) is None
    assert execution_routes._start_recovery_watcher(plan_id, None) is None
    assert "recovery_watcher" not in state, (
        "a watcher was attached to a plan whose pid is not an executor"
    )


# ---------------------------------------------------------------------------
# 6. The boot scan hands a live executor to a watcher
# ---------------------------------------------------------------------------


def _seed_running_execution(db_path: Path, plan_id: str, pid: int) -> None:
    """Write a ``plan_execution`` row that says ``running`` with a live pid."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
            " next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                0,
                "/tmp/fake",
                None,
                json.dumps({"tasks": {}}),
                None,
                "{}",
                "{}",
                pid,
                "running",
                "2026-10-27T00:00:00Z",
                "2026-10-27T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_recovery_scan_watches_a_live_pid(tmp_path, monkeypatch):
    """The boot scan attaches a watcher to a restored run whose pid lives.

    Without the watcher the restored run has no watcher, no process
    handle and no ``executor_finished_cleanly`` — the watchdog's probe
    is then the only voice, and it cannot tell a crash from a clean
    exit.
    """
    plan_id = "plan-recovery-scan"
    plans_dir = tmp_path / "plans"
    (plans_dir / plan_id).mkdir(parents=True)

    db_path = tmp_path / "state.db"
    # This process is certainly alive, so the scan sees a live executor.
    _seed_running_execution(db_path, plan_id, os.getpid())
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)

    execution_routes._recover_execution_states(plans_dir)

    state = server._execution_state.get(plan_id)
    assert state is not None, "the running row was not restored"
    assert state.get("status") == "running"

    watcher = state.get("recovery_watcher")
    assert isinstance(watcher, threading.Thread), (
        "the boot scan left the restored run without a watcher"
    )
    assert watcher.is_alive(), "the watcher ended before the executor did"

    _stop_watcher(state, watcher)
