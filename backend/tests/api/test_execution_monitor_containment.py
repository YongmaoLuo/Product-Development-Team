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

The pin is deliberately timing-free. Both tests build their own leak via
``_start_leaking_execution``: a monitor whose process is never released —
the shape of the original defect, where ``FakeProcess.wait()`` merely caps
at 30 s, so the monitor outlives the test by half a minute and by then the
test's stubs are gone and the real auto-verification loop runs against a
stranger's state.db. The first asserts the start endpoint spawns such a
monitor at all; the second replays the containment from
``tests/conftest.py::clean_execution_state`` (release the monitor's
process, then join it) and asserts nothing is left alive.

Neither test reads state from the other, so either can be run alone —
``-k no_monitor`` used to skip, which meant the containment was verified
in one configuration only.

Without that containment the thread is still parked in ``wait()`` here, so
this fails deterministically instead of flakily.
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


#: Plan id both tests start an execution under.
_PLAN_ID = "monitor-containment-leak"


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


def _start_leaking_execution(monkeypatch, tmp_path):
    """Start an execution whose monitor is left parked, and return the threads.

    Both tests below need a live, unreclaimed monitor to exist, and each
    has to produce its own. The second used to read a module-level list
    the first one filled, which made it a claim about *test ordering*:
    run on its own (``-k no_monitor``) or under ``-p no:randomly`` it had
    nothing to inspect and skipped, so the containment it exists to pin
    was verified in exactly one configuration.

    ``Popen`` is replaced with a process whose ``wait()`` merely caps at
    30 s and is never released, which is the shape of the original
    defect: the monitor outlives the test by half a minute, and by then
    the test's stubs are gone and the real auto-verification loop runs
    against a stranger's state.db.
    """
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / _PLAN_ID
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
        f"/api/execution/{_PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )
    assert resp.status_code == 200, resp.text

    return _monitor_threads()


def test_a_leaked_monitor_is_contained_by_its_own_test(monkeypatch, tmp_path):
    """Start an execution and leave its monitor running (the original bug)."""
    assert _start_leaking_execution(monkeypatch, tmp_path), (
        "the start endpoint must have spawned an execution monitor thread"
    )


def test_no_monitor_outlives_the_test_that_started_it(monkeypatch, tmp_path):
    """The fixture's release/join reclaimed every monitor the test started.

    The two halves replayed here are the ones
    ``tests/conftest.py::clean_execution_state`` performs at teardown —
    release the process the monitor is parked on, then join the thread —
    so this asserts the containment itself rather than that the *previous*
    test happened to run first.
    """
    import server

    leaked = _start_leaking_execution(monkeypatch, tmp_path)
    assert leaked, (
        "the start endpoint must have spawned an execution monitor thread"
    )
    assert [thread for thread in leaked if thread.is_alive()], (
        "the monitor must still be running before containment is applied, "
        "or this test asserts nothing"
    )

    # ``_execution_state[plan_id]["process"]`` is where the fixture reads
    # the monitor's process from; same key, same lookup.
    state = server._execution_state.get(_PLAN_ID) or {}
    process = state.get("process")
    hook = getattr(process, "release", None) or getattr(
        process, "terminate", None
    )
    if callable(hook):
        try:
            hook()
        except Exception:  # noqa: BLE001 - best-effort unblock only
            pass
    for thread in leaked:
        thread.join(timeout=10)

    alive = [thread for thread in leaked if thread.is_alive()]
    assert not alive, (
        f"{len(alive)} execution monitor thread(s) outlived their own test. "
        f"Such a monitor keeps resolving the DB path per call and writes "
        f"into whichever test runs next (see "
        f"tests/conftest.py::clean_execution_state)."
    )
