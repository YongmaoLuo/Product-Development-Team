"""VP-005: After a successful POST /api/execution/{plan_id}/start, the
backend MUST persist execution state to the ``plan_execution`` SQLite
row and the row MUST satisfy:

  * ``exec_pid`` field present, type ``int``, value not ``None``
  * ``exec_status`` field equals ``"running"``
  * ``started_at`` field is an ISO-8601 timestamp

Production code path (backend/server.py ``@app.post(
"/api/execution/{plan_id}/start")``) calls ``subprocess.Popen`` synchronously,
captures ``process.pid`` into the in-memory state, then invokes
``ExecutionRepository.update_phase(..., create_if_missing=True)`` which
writes the following columns on the ``plan_execution`` row::

    current_phase = "executing"
    project_dir   = <requested project dir>
    exec_pid      = <subprocess pid>
    exec_status   = "running"
    started_at    = <ISO-8601 timestamp>

2026-09-13 port (everything depends on SQLite, so the test side moved
to SQLite too): this module used to assert the retired
``plans/{id}/execution.json`` file. That file's reads AND writes were
removed from the backend (see the three "legacy ``execution.json`` read
was removed" notes in ``server.py``) because they bypassed the SQLite
CAS layer and desynced from the rest of the state machine. The
``plan_execution`` row is now the single source of truth, and
``_recover_execution_states`` restores from it on restart. The tests
below assert the SAME contract against that row.

The operator's other requirement ("测试的时候不要去动 state.db") is
handled by the suite-wide isolation: the ``isolated_plans_dir`` autouse
fixture points both ``PDT_STATE_DB_PATH`` and ``server._state_db_path``
at a per-test ``tmp_path/state.db``, and the ``state_db_reader`` fixture
reads from that same file, so these tests can never touch the live
database.

Tests in this module use FastAPI's ``TestClient`` and stub
``subprocess.Popen`` with a deterministic fake that reports a known PID.
They assert each of the contract clauses by reading the on-disk
``plan_execution`` row AFTER the HTTP response returns.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app, _execution_state, _execution_locks


client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset global execution state and redirect PLANS_DIR per test."""
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan(plan_id: str) -> Path:
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


@pytest.fixture
def stub_subprocess(monkeypatch):
    """Stub ``subprocess.Popen`` to a deterministic fake process.

    The fake reports a known PID (7777) and an empty stdout iterator
    so the daemon reader thread exits immediately. This keeps the
    contract test focused on the on-disk ``plan_execution`` row.
    """

    class _StubProcess:
        PID = 7777

        def __init__(self, *args, **kwargs):
            self.pid = _StubProcess.PID
            self.returncode = 0
            # The watcher thread is parked on this event for the duration
            # of the test and released at teardown — see ``release``.
            self._block = threading.Event()
            # A blocking iterator: the daemon thread parks here forever,
            # so ``state["status"]`` remains "running" for the duration
            # of the test and the persisted ``plan_execution`` row
            # reflects the initial "running" snapshot.
            self.stdout = _BlockingIterator()

        def wait(self, timeout=None):
            # Park the ``_run()`` watcher thread inside ``process.wait()``
            # so the plan stays "running" for the duration of the test —
            # exactly what the status contract asserts. (The old JSON-era
            # test relied on the stdout iterator blocking the drain
            # thread; the 2026-09-06 spawn refactor redirects stdout to a
            # log file, so ``wait()`` is now the only place the watcher
            # can be parked.)
            #
            # Parking must not outlive the test: ``release`` is what
            # ``tests/conftest.py::clean_execution_state`` calls at
            # teardown, before it joins the watcher. A test may leave no
            # live resource behind for the next one to trip over.
            self._block.wait()
            return 0

        def poll(self):
            return 0

        def terminate(self):
            self._block.set()

        def release(self):
            self._block.set()

    monkeypatch.setattr("server.subprocess.Popen", _StubProcess)
    monkeypatch.setattr(
        "server._run_auto_verification_loop", lambda *a, **kw: None
    )
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)
    return _StubProcess


class _BlockingIterator:
    """An iterator that blocks forever on ``__next__``.

    Used to keep the daemon reader thread parked inside
    ``for line in process.stdout`` so the persisted ``exec_status``
    remains ``"running"`` while the test reads the ``plan_execution``
    row.
    """

    def __iter__(self):
        return self

    def __next__(self):
        import threading

        ev = threading.Event()
        ev.wait()
        raise StopIteration()


# ---------------------------------------------------------------------------
# VP-005: plan_execution persistence contains exec_pid (non-null) +
#         exec_status 'running' + ISO started_at.
# ---------------------------------------------------------------------------


class TestExecutionStatePersistedToDisk:
    """Contract: a successful /api/execution/{plan_id}/start MUST persist
    a fully populated ``plan_execution`` row (see the module docstring).
    """

    def test_persist_execution_state_row_is_written_on_successful_start(
        self, stub_subprocess, tmp_path, state_db_reader
    ):
        """After 200 start, a ``plan_execution`` row MUST exist."""
        plan_id = "vp005-persist-file-written"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp005-file"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text

        row = state_db_reader.execution(plan_id)
        assert row is not None, (
            f"plan_execution row was not written for {plan_id} after a "
            f"successful start; resp={resp.json()!r}"
        )

    def test_persist_execution_state_pid_field_is_integer(
        self, stub_subprocess, tmp_path, state_db_reader
    ):
        """The ``exec_pid`` column MUST contain an int."""
        plan_id = "vp005-persist-pid-int"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp005-pid-int"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text

        row = state_db_reader.execution(plan_id)
        assert row is not None, f"plan_execution row missing for {plan_id}"

        pid = row.get("exec_pid")
        assert pid is not None, (
            f"plan_execution row missing 'exec_pid'; row={row!r}"
        )
        assert isinstance(pid, int), (
            f"plan_execution 'exec_pid' must be int, got "
            f"{type(pid).__name__} with value={pid!r}"
        )
        assert not isinstance(pid, bool), (
            f"plan_execution 'exec_pid' must be a real int, not bool; "
            f"value={pid!r}"
        )

    def test_persist_execution_state_pid_is_not_none(
        self, stub_subprocess, tmp_path, state_db_reader
    ):
        """``exec_pid`` MUST NOT be null/None."""
        plan_id = "vp005-persist-pid-not-none"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp005-pid-not-none"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text

        row = state_db_reader.execution(plan_id)
        assert row is not None, f"plan_execution row missing for {plan_id}"

        assert row.get("exec_pid") is not None, (
            f"plan_execution 'exec_pid' MUST be non-null after a "
            f"successful start; row={row!r}"
        )

    def test_persist_execution_state_pid_matches_subprocess_pid(
        self, stub_subprocess, tmp_path, state_db_reader
    ):
        """``exec_pid`` MUST equal the OS PID reported by subprocess.Popen."""
        plan_id = "vp005-persist-pid-matches"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp005-pid-match"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text

        row = state_db_reader.execution(plan_id)
        assert row is not None, f"plan_execution row missing for {plan_id}"

        assert row.get("exec_pid") == stub_subprocess.PID, (
            f"plan_execution exec_pid {row.get('exec_pid')!r} != "
            f"subprocess pid {stub_subprocess.PID!r}"
        )

    def test_persist_execution_state_status_is_running(
        self, stub_subprocess, tmp_path, state_db_reader
    ):
        """``exec_status`` MUST equal 'running' immediately after start."""
        plan_id = "vp005-persist-status-running"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp005-status"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text

        row = state_db_reader.execution(plan_id)
        assert row is not None, f"plan_execution row missing for {plan_id}"

        assert row.get("exec_status") == "running", (
            f"plan_execution 'exec_status' must be 'running' after "
            f"start; got {row.get('exec_status')!r}; full row={row!r}"
        )

    def test_persist_execution_state_started_at_is_iso_timestamp(
        self, stub_subprocess, tmp_path, state_db_reader
    ):
        """``started_at`` MUST be a parseable ISO-8601 timestamp."""
        plan_id = "vp005-persist-started-at-iso"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp005-started-at"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text

        row = state_db_reader.execution(plan_id)
        assert row is not None, f"plan_execution row missing for {plan_id}"

        started_at = row.get("started_at")
        assert started_at, f"plan_execution missing 'started_at'; row={row!r}"
        assert isinstance(started_at, str), (
            f"plan_execution 'started_at' must be a string; "
            f"got {type(started_at).__name__}"
        )
        # Accept either ISO with or without 'T' separator / microseconds / tz.
        iso_re = re.compile(
            r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
        )
        assert iso_re.match(started_at), (
            f"plan_execution 'started_at' is not ISO-8601 format: "
            f"{started_at!r}"
        )
        # And it MUST round-trip through datetime.fromisoformat.
        # Note: Python 3.9 fromisoformat does NOT accept the 'Z' suffix;
        # normalize for the parse check.
        normalized = started_at.replace("Z", "+00:00") if started_at.endswith("Z") else started_at
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            pytest.fail(
                f"plan_execution 'started_at'={started_at!r} not parseable "
                f"by datetime.fromisoformat: {exc}"
            )
        assert isinstance(parsed, datetime), (
            f"parsed started_at is not a datetime: {parsed!r}"
        )
