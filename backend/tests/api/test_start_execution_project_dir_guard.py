"""``POST /api/execution/{plan_id}/start`` refuses an unusable ``project_dir``.

Background
----------
The start route used to answer a mistyped (or stale) ``project_dir`` with
``mkdir(parents=True, exist_ok=True)`` — it silently *created* the
directory and let the run proceed inside an empty tree. The real error
only surfaced much later, as a confusing "no such file" from the executor
subprocess, with nothing pointing back at the typo that caused it.

``project_dir_guard.validate_project_dir`` is the single check (see
``backend/project_dir_guard.py``): the directory must already exist and
must really be a directory. The route now calls it *before* the routing
CAS, so a refused start never moves the plan into ``executing`` and never
reaches the spawn.

What these tests pin:

  1. a missing directory is refused with HTTP 400 and the executor is
     never spawned;
  2. the refusal leaves a diagnosable trace — the ``plan_execution`` row
     says ``failed`` / ``stop_reason='project_dir_invalid'`` / no pid, and
     ``<plan_dir>/logs/execution_start.log`` names the offending path;
  3. a directory that *does* exist still reaches the spawn (the guard
     must not break the happy path).

Test isolation
--------------
Same harness as ``tests/api/test_start_endpoint.py``: ``PLANS_DIR`` is
redirected to ``tmp_path`` per test, the auto-verification loop and the
heartbeat are stubbed, and the executor spawn is replaced by a fake that
records its arguments. The fake process blocks in ``wait()`` until the
test releases it, and the watcher thread the route starts is captured so
the test can join it before pytest tears ``tmp_path`` down — no real
subprocess is ever started.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import (
    _execution_locks,
    _execution_state,
    app,
)


client = TestClient(app)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class FakeProcess:
    """Stand-in for the executor subprocess.

    ``wait()`` blocks until the test releases it, so the watcher thread
    the route starts stays parked instead of racing the assertions.
    """

    def __init__(self, pid: int = 4242):
        self.pid = pid
        self._released = threading.Event()

    @property
    def returncode(self):
        return 0

    @property
    def stdout(self):
        return iter([])

    def wait(self, timeout=None):
        self._released.wait(timeout=timeout or 30)
        return 0

    def poll(self):
        return 0 if self._released.is_set() else None

    def terminate(self):
        self._released.set()

    def release(self):
        self._released.set()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset global execution state and redirect PLANS_DIR per test."""
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(
        "server._run_auto_verification_loop", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)
    yield
    _execution_state.clear()
    _execution_locks.clear()


@pytest.fixture
def spawner(monkeypatch, tmp_path):
    """Replace the executor spawn with a recording fake.

    The route calls ``_server._spawn_executor_subprocess`` (late-bound
    through the ``server`` module), so patching ``server``'s attribute is
    what makes the fake effective.

    Returns:
        ``(calls, process, threads)`` — the recorded spawn calls, the fake
        process, and the watcher threads the route started.
    """
    calls: list = []
    threads: list = []
    process = FakeProcess()

    def _fake_spawn(**kwargs):
        calls.append(kwargs)
        return process, tmp_path / "logs" / "executor_fake.log"

    monkeypatch.setattr("server._spawn_executor_subprocess", _fake_spawn)

    # The route starts its watcher through ``threading.Thread`` in the
    # ``routes.execution`` namespace. Swap that one name for a recording
    # subclass so the test can join the thread before pytest removes
    # ``tmp_path`` — a daemon thread still reading a deleted directory is
    # how these suites grow flaky.
    real_thread = threading.Thread

    class _RecordingThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    class _ThreadingShim:
        Thread = _RecordingThread

    monkeypatch.setattr("routes.execution.threading", _ThreadingShim)

    yield calls, process, threads

    # Release the fake process, then let the watcher thread finish.
    process.release()
    for thread in threads:
        thread.join(timeout=15)


def _setup_plan_dir(plan_id: str) -> Path:
    """Create a minimal plan directory with tasks.json + plan_state.json."""
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
                "current_phase": "ready",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# 1. A missing project_dir is refused — and nothing is spawned
# ---------------------------------------------------------------------------


def test_missing_project_dir_returns_400_and_never_spawns(spawner):
    """A ``project_dir`` that does not exist is refused with HTTP 400.

    The refusal happens before the routing CAS and before the spawn, so
    the answer is a 400 with the offending path in it — not a run that
    dies somewhere inside the subprocess.
    """
    calls, _process, _threads = spawner
    plan_id = "p-guard-missing"
    plan_dir = _setup_plan_dir(plan_id)
    missing = plan_dir.parent / "removed-worktree"
    assert not missing.exists(), (
        f"the fixture path {missing} must not exist for this test"
    )

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(missing)},
    )

    assert resp.status_code == 400, (
        f"start endpoint returned HTTP {resp.status_code}, expected 400; "
        f"body={resp.text!r}"
    )
    detail = resp.json().get("detail", "")
    assert str(missing.resolve()) in detail, (
        f"the 400 detail must name the offending path; got detail={detail!r}"
    )
    assert "does not exist" in detail, (
        f"the 400 detail must say why the path was refused; "
        f"got detail={detail!r}"
    )
    assert calls == [], (
        f"the executor must not be spawned for an invalid project_dir; "
        f"spawn was called {len(calls)} time(s)"
    )


# ---------------------------------------------------------------------------
# 2. The refusal leaves a diagnosable trace
# ---------------------------------------------------------------------------


def test_rejection_persists_failed_status_and_log(state_db_reader, spawner):
    """A refused start is recorded as a failed run, with the path on disk.

    Without this, a rejected start looks like a plan that was never
    started — no status, no reason, nothing to grep.
    """
    calls, _process, _threads = spawner
    plan_id = "p-guard-rejected"
    plan_dir = _setup_plan_dir(plan_id)
    missing = plan_dir.parent / "removed-worktree"

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(missing)},
    )
    assert resp.status_code == 400, (
        f"start endpoint returned HTTP {resp.status_code}, expected 400; "
        f"body={resp.text!r}"
    )

    row = state_db_reader.execution(plan_id)
    assert row is not None, (
        f"the refusal must leave a plan_execution row for {plan_id}"
    )
    assert row["exec_status"] == "failed", (
        f"exec_status must record the refusal; got {row['exec_status']!r}"
    )
    assert row["exec_pid"] is None, (
        f"exec_pid must stay NULL — nothing was spawned; got {row['exec_pid']!r}"
    )
    assert row["stop_reason"] == "project_dir_invalid", (
        f"stop_reason must say why; got {row['stop_reason']!r}"
    )
    assert row["project_dir"] == str(missing.resolve()), (
        f"the row must carry the offending path verbatim; "
        f"got {row['project_dir']!r}"
    )

    log_path = plan_dir / "logs" / "execution_start.log"
    assert log_path.exists(), (
        f"the refusal must leave a log at {log_path}"
    )
    log_text = log_path.read_text(encoding="utf-8")
    assert str(missing.resolve()) in log_text, (
        f"the log must name the offending path verbatim; got {log_text!r}"
    )

    # The refusal happens before the routing CAS: the plan never entered
    # 'executing'.
    routing = state_db_reader.routing(plan_id)
    assert routing is None or routing.get("current_phase") != "executing", (
        f"a refused start must not move the plan into 'executing'; "
        f"routing row={routing!r}"
    )
    plan_state = json.loads(
        (plan_dir / "plan_state.json").read_text(encoding="utf-8")
    )
    assert plan_state["current_phase"] == "ready", (
        f"a refused start must leave the plan phase at 'ready'; "
        f"got {plan_state['current_phase']!r}"
    )


# ---------------------------------------------------------------------------
# 3. The happy path still works
# ---------------------------------------------------------------------------


def test_existing_project_dir_still_starts(spawner):
    """An existing directory still reaches the spawn."""
    calls, _process, _threads = spawner
    plan_id = "p-guard-existing"
    plan_dir = _setup_plan_dir(plan_id)
    workdir = plan_dir.parent / "existing-worktree"
    workdir.mkdir()

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(workdir)},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )
    assert len(calls) == 1, (
        f"an existing project_dir must still reach the spawn; "
        f"spawn was called {len(calls)} time(s)"
    )
    assert str(calls[0]["project_dir"]) == str(workdir.resolve()), (
        f"the spawn must carry the requested directory; "
        f"got {calls[0]['project_dir']!r}"
    )
