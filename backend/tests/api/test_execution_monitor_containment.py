"""An execution monitor must not outlive the test that started it.

Regression pin for the 2026-09-25 suite-wide flake.

``POST /api/execution/{id}/start`` spawns a daemon *monitor* thread. The
monitor resolves ``_state_db_path()`` / ``PDT_STATE_DB_PATH`` **on every
call**, and the autouse ``isolated_plans_dir`` fixture re-points those at
whichever test is running at that moment — so a monitor that survives its
own test goes on writing into the *next* test's database. That is how
unrelated tests started failing at random with

  * ``[I2] plan_id='vp-003-explicit' ... (orphan CAS)`` in the
    crash-recovery suite, and
  * ``sqlite3.OperationalError: table plan_routing already exists`` in
    ``test_state_from_db.py``.

The pin is deliberately timing-free. The first test leaks a monitor whose
process is never released — the shape of the original defect, where
``FakeProcess.wait()`` merely caps at 30 s, so the monitor outlives the
test by half a minute and by then the test's stubs are gone and the real
auto-verification loop runs against a stranger's state.db. The second test
runs immediately afterwards and asserts the thread is gone.

Without the containment in ``tests/conftest.py::clean_execution_state``
(release the monitor's process, then join it before ``monkeypatch``
unwinds) the thread is still parked in ``wait()`` here, so this fails
deterministically instead of flakily.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from server import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def _stub_auto_verification(monkeypatch):
    """Keep the monitor's completion path cheap.

    The subject of this module is the monitor *thread*, not what the
    verified loop does; the sibling suites stub it for the same reason.
    """
    monkeypatch.setattr(
        "server._run_auto_verification_loop", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)


#: Monitors leaked by the first test, inspected by the second.
_LEAKED: "list[threading.Thread]" = []


class _NeverReleasedProcess:
    """``Popen`` stand-in that "exits 0" only when its 30 s cap expires.

    Mirrors ``tests/api/test_start_endpoint.py::FakeProcess``, including
    the ``release`` hook the leaking test never calls.
    """

    pid = 4242

    def __init__(self):
        self._block = threading.Event()

    @property
    def returncode(self):
        return 0

    @property
    def stdout(self):
        return iter([])

    def wait(self, timeout=None):
        self._block.wait(timeout=timeout or 30)
        return 0

    def poll(self):
        return 0 if self._block.is_set() else None

    def terminate(self):
        self._block.set()

    def release(self):
        self._block.set()


def _monitor_threads() -> "list[threading.Thread]":
    """Every live execution-monitor thread in this process."""
    import server

    return [
        thread
        for thread in threading.enumerate()
        if getattr(thread, "_target", None) is server._run_in_plan_ctx
    ]


def test_a_leaked_monitor_is_contained_by_its_own_test(monkeypatch, tmp_path):
    """Start an execution and leave its monitor running (the original bug)."""
    from server import PLANS_DIR

    plan_id = "monitor-containment-leak"
    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    monkeypatch.setattr(
        "server.subprocess.Popen", lambda *args, **kwargs: _NeverReleasedProcess()
    )

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )
    assert resp.status_code == 200, resp.text

    _LEAKED[:] = _monitor_threads()
    assert _LEAKED, (
        "the start endpoint must have spawned an execution monitor thread"
    )


def test_no_monitor_outlives_the_test_that_started_it():
    """The fixture released and joined every monitor the previous test left."""
    if not _LEAKED:
        pytest.skip(
            "the leaking test did not run in this session — nothing leaked"
        )
    alive = [thread for thread in _LEAKED if thread.is_alive()]
    assert not alive, (
        f"{len(alive)} execution monitor thread(s) outlived their own test. "
        f"Such a monitor keeps resolving the DB path per call and writes "
        f"into whichever test runs next (see "
        f"tests/conftest.py::clean_execution_state)."
    )
