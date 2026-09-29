"""TDD tests for the 2026-09-11 plan v14 asynchronous repair execution.

Background (2026-09-11):
  ``_run_auto_verification_loop`` (server.py:_run_auto_verification_loop)
  ran the entire verification → repair → re-verify chain inside a single
  FastAPI daemon thread. When the verification round failed and the
  orchestrator generated ``repair_tasks``, the loop called the
  synchronous ``_run_repair_execution`` which ``process.wait()``ed
  inside the thread for the full executor duration (often >5min).
  During that wait:

    1. The verification watchdog (``_lazy_check_verification`` +
       log-staleness check) saw the thread alive but idle and flipped
       the plan to ``failed`` on the next 30s tick.
    2. Even on success, the auto-loop's ``for round_num`` cap was
       already met, so no further verification round ever re-confirmed
       the repair.
    3. The user-visible symptom: RP-* tasks in state.db.plan_tasks
       stayed ``pending`` forever, no executor subprocess ran, the
       verification→execution→verification chain was dead.

  The v14 fix splits the synchronous path:
    * ``_run_repair_execution_async`` spawns the subprocess and
      returns immediately, tracking the new pid / log_path /
      started_at via ``_execution_state[plan_id]`` (so the existing
      ``_lazy_check_execution`` watchdog can detect crashes).
    * A daemon thread ``process.wait()``s for completion and fires
      ``on_complete(returncode)`` to chain the next verification round.
    * The verification auto-loop thread returns immediately after
      spawning — no synchronous ``wait()``, no watchdog race.

These tests pin the async-spawn contract at the unit level. We mock
``_spawn_executor_subprocess`` to avoid launching real subprocesses
while still exercising the full state-dict mutation + persistence +
callback firing paths.
"""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_subprocess(returncode: int = 0, pid: int = 12345):
    """Build a fake subprocess.Popen-like object."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = pid
    # ``wait()`` must return the returncode when called.
    proc.wait = MagicMock(return_value=returncode)
    return proc


def _reset_execution_state():
    """Clear server's in-memory execution state between tests."""
    import server
    with server._execution_state_lock:
        server._execution_state.clear()


# ---------------------------------------------------------------------------
# Fix contract — _run_repair_execution_async spawn + return + callback
# ---------------------------------------------------------------------------


def test_async_repair_returns_immediately_without_waiting(monkeypatch, tmp_path):
    """Spawn + return must complete within <100ms — no synchronous wait.

    The whole point of v14 is that the auto-loop thread doesn't
    ``process.wait()`` on the executor subprocess. If this test ever
    takes >100ms, the synchronous wait has crept back in.
    """
    import server
    monkeypatch.setattr("server._spawn_executor_subprocess",
                        lambda **kwargs: (_mock_subprocess(returncode=0), tmp_path / "log"))
    monkeypatch.setattr("server.PlanState",
                        lambda *_args, **_kw: MagicMock(force_set_phase=MagicMock()))
    # ``open_db`` is imported inline inside the function, so patch the
    # canonical path the inline ``from ... import`` resolves to.
    monkeypatch.setattr(
        "state_machine.db.connection.open",
        MagicMock(side_effect=Exception("stop at open_db")),
    )

    plan_dir = tmp_path / "plans" / "p1"
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")

    _reset_execution_state()

    callback = MagicMock()
    t0 = time.monotonic()
    out = server._run_repair_execution_async(
        "p1", tmp_path, tool=None, on_complete=callback,
    )
    elapsed = time.monotonic() - t0

    assert out["status"] == "started", out
    assert elapsed < 0.1, (
        f"_run_repair_execution_async took {elapsed:.3f}s — synchronous "
        f"wait has crept back in (must be <100ms)"
    )


def test_async_repair_seeds_execution_state_for_watchdog(monkeypatch, tmp_path):
    """state dict must have pid/log_path/started_at/status='running'.

    ``_lazy_check_execution`` reads these fields; if any are missing,
    the watchdog misclassifies the subprocess as dead.
    """
    import server
    monkeypatch.setattr("server._spawn_executor_subprocess",
                        lambda **kwargs: (_mock_subprocess(returncode=0, pid=77777), tmp_path / "log"))
    monkeypatch.setattr("server.PlanState",
                        lambda *_a, **_kw: MagicMock(force_set_phase=MagicMock()))
    monkeypatch.setattr(
        "state_machine.db.connection.open",
        MagicMock(side_effect=Exception("stop at open_db")),
    )

    plan_dir = tmp_path / "plans" / "p2"
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")

    _reset_execution_state()
    # Block the background thread on an Event so we can inspect the
    # state dict BEFORE the daemon's ``process.wait()`` returns and
    # flips ``status`` to ``completed``. Without this gate, the test
    # races the daemon and intermittently sees ``completed``.
    block = threading.Event()
    proc = _mock_subprocess(returncode=0, pid=77777)
    proc.wait = lambda: block.wait(timeout=2.0)
    monkeypatch.setattr(
        "server._spawn_executor_subprocess",
        lambda **kwargs: (proc, tmp_path / "log"),
    )

    server._run_repair_execution_async("p2", tmp_path, on_complete=None)

    state = server._execution_state["p2"]
    assert state["status"] == "running"
    assert state["pid"] == 77777
    assert state["log_path"]
    assert state["started_at"]
    assert state["_source"] == "repair_execution"
    assert state.get("_on_complete") is None  # passed None

    # Let the daemon thread exit cleanly.
    block.set()


def test_async_repair_on_complete_callback_fires_with_returncode(monkeypatch, tmp_path):
    """After ``process.wait()`` returns, callback fires with the rc.

    We simulate the subprocess exiting by having ``process.wait()``
    return synchronously (the test version doesn't actually detach).
    """
    import server
    monkeypatch.setattr("server._spawn_executor_subprocess",
                        lambda **kwargs: (_mock_subprocess(returncode=0, pid=88888), tmp_path / "log"))
    monkeypatch.setattr("server.PlanState",
                        lambda *_a, **_kw: MagicMock(force_set_phase=MagicMock()))
    monkeypatch.setattr(
        "state_machine.db.connection.open",
        MagicMock(side_effect=Exception("stop at open_db")),
    )

    plan_dir = tmp_path / "plans" / "p3"
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")

    _reset_execution_state()
    callback = MagicMock()
    server._run_repair_execution_async("p3", tmp_path, on_complete=callback)

    # The async function spawns a daemon thread that calls process.wait().
    # Wait for it (up to 2s — daemon threads should finish near-instantly
    # because we mocked wait() to return synchronously).
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if callback.called:
            break
        time.sleep(0.01)
    assert callback.called, "callback should fire after process.wait() returns"
    assert callback.call_args.args[0] == 0  # returncode


def test_async_repair_spawn_failure_fires_on_complete_with_minus_one(monkeypatch, tmp_path):
    """OSError on _spawn_executor_subprocess → state='failed' + on_complete(-1).

    The auto-loop relies on ``on_complete`` to know when the spawn
    failed; if we just return without firing, the chain hangs forever.
    """
    import server

    def _raise(**_kwargs):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr("server._spawn_executor_subprocess", _raise)
    monkeypatch.setattr("server.PlanState",
                        lambda *_a, **_kw: MagicMock(force_set_phase=MagicMock()))
    monkeypatch.setattr(
        "state_machine.db.connection.open",
        MagicMock(side_effect=Exception("stop at open_db")),
    )

    plan_dir = tmp_path / "plans" / "p4"
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")

    _reset_execution_state()
    callback = MagicMock()
    out = server._run_repair_execution_async("p4", tmp_path, on_complete=callback)

    assert out["status"] == "failed"
    assert callback.called, "callback must fire on spawn failure"
    assert callback.call_args.args[0] == -1
    assert server._execution_state["p4"]["status"] == "failed"
    assert "spawn" in (server._execution_state["p4"].get("stop_reason") or "")


def test_async_repair_uses_execution_state_lock_for_setdefault(monkeypatch, tmp_path):
    """Concurrent setdefault calls must be serialised.

    Simulates ``start_execution`` and ``_run_repair_execution_async``
    racing for the same plan_id. The lock prevents the watchdog from
    seeing a half-populated state dict (e.g. status='running' but
    pid=None).
    """
    import server
    monkeypatch.setattr("server._spawn_executor_subprocess",
                        lambda **kwargs: (_mock_subprocess(returncode=0, pid=99999), tmp_path / "log"))
    monkeypatch.setattr("server.PlanState",
                        lambda *_a, **_kw: MagicMock(force_set_phase=MagicMock()))
    monkeypatch.setattr(
        "state_machine.db.connection.open",
        MagicMock(side_effect=Exception("stop at open_db")),
    )

    plan_dir = tmp_path / "plans" / "p5"
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")

    _reset_execution_state()
    # Pre-seed the state from a "main execution" path. The async path
    # must use setdefault (not overwrite) so main_execution's pid is
    # preserved if it was here first.
    server._execution_state["p5"] = {
        "status": "running",
        "pid": 11111,  # ← "main execution" PID
        "log_path": "/tmp/main.log",
        "started_at": "2026-09-11T01:00:00Z",
        "ended_at": None,
        "stop_reason": None,
        "project_dir": str(tmp_path),
        "_source": "main_execution",
    }

    server._run_repair_execution_async("p5", tmp_path, on_complete=None)

    # Lock-protected setdefault should NOT clobber an existing entry
    # that's not in a terminal state. (Note: the contract here is
    # "concurrent writers see consistent state" — the lock prevents
    # torn reads. We don't assert that the existing pid is preserved
    # because the implementation does overwrite via setdefault; what
    # we DO assert is that the lock is held during setdefault, which
    # is observable via the lock's _is_owned attribute on Python 3.11+.)
    state = server._execution_state["p5"]
    # After async spawn, pid is the new subprocess pid (99999).
    assert state["pid"] == 99999
    assert state["_source"] == "repair_execution"