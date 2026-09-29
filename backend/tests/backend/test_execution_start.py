"""VP-001: Telegram bridge backwards compat — POST /api/execution start.

These tests pin the contract pinned by VP-001 of the verification plan:
when the caller explicitly passes ``sync_targets=["telegram"]`` (the
"Telegram bridge backwards compat" path) the endpoint must accept the
request and return HTTP 200; when no ``sync_targets`` is provided the
endpoint must also accept the request and return HTTP 200 (the default
sink path used by pre-migration callers).

The two test names below are chosen to match the
``-k 'test_start_api_explicit_telegram or test_start_api_default_channel'``
filter used by the verification runner.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import (
    _execution_locks,
    _execution_state,
    app,
)


client = TestClient(app)


class FakeProcess:
    def __init__(self):
        import threading
        self.pid = 4242
        self._block = threading.Event()

    @property
    def returncode(self):
        return 0

    @property
    def stdout(self):
        self._block.wait(timeout=30)
        return iter([])

    def wait(self, timeout=None):
        self._block.wait(timeout=timeout or 30)
        return 0

    def poll(self):
        if self._block.is_set():
            return 0
        return None

    def terminate(self):
        self._block.set()

    def release(self):
        self._block.set()


def _join_threads_started_during(before: set, timeout: float = 15.0) -> list:
    """Wait for every thread this test started to actually finish.

    Why this is not optional (2026-09-23)
    -------------------------------------
    ``POST /api/execution/{id}/start`` spawns a background worker. The
    worker resolves its SQLite path through ``_state_db_path()`` **at
    write time**, and that path is per-test (the autouse
    ``isolated_plans_dir`` fixture points ``PDT_STATE_DB_PATH`` at the
    running test's ``tmp_path``). A worker that outlives its test
    therefore writes its own plan's rows into the NEXT test's database.

    That is not theoretical. Measured: after these two tests ran,
    ``tests/crash_recovery/test_kill_after_commit_persists_change.py``
    failed its ``assert_db_consistent`` cold-replay check with

        [I2] plan_id='vp-001-default-channel' has
        plan_routing.current_phase='ready' but NO matching
        plan_execution row (orphan CAS)

    — a routing row written by this file's leftover worker, in a
    database that test owns and had just seeded with ``p1`` only. It
    reproduced on every sharded run and never standalone, which is the
    exact signature of a cross-test leak: alone, the late write lands in
    a directory nobody reads; in a shard, it lands in the next test.

    Clearing ``_execution_state`` (which the fixture already did) is not
    enough: it drops the Python references, not the running thread. The
    assertion is the join — after teardown no worker from this test is
    still alive to write anywhere.
    """
    deadline = time.monotonic() + timeout
    current = threading.current_thread()
    stragglers = []
    for thread in list(threading.enumerate()):
        if thread in before or thread is current or not thread.is_alive():
            continue
        stragglers.append(thread)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    return stragglers


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(
        "server._run_auto_verification_loop",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)

    threads_before = set(threading.enumerate())
    yield
    _execution_state.clear()
    _execution_locks.clear()
    # Must run BEFORE monkeypatch undoes anything: while it is still
    # installed, a worker that has not reached its loop yet is a no-op
    # and finishes immediately instead of doing real work against the
    # next test's database.
    _join_threads_started_during(threads_before)


def _setup_plan_dir(plan_id: str, plans_dir: Path) -> Path:
    plan_dir = plans_dir / plan_id
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


def test_start_api_explicit_telegram(monkeypatch):
    """VP-001a: POST sync_targets=['telegram'] → 200 + runtime record carries ['telegram'].

    The test name ``test_start_api_explicit_telegram`` is a substring
    match against the verifier's ``-k`` filter
    ``test_start_api_explicit_telegram or test_start_api_default_channel``.

    2026-09-13 port (SQLite decision): the retired ``execution.json``
    was removed; ``sync_targets`` is runtime-only (lands on the
    in-memory ``_execution_state`` entry), while the run persists to
    the ``plan_execution`` SQLite row (asserted via ``state_db_reader``
    against the hermetic test DB).
    """
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-001-explicit-telegram"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir), "sync_targets": ["telegram"]},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    sync_targets = _execution_state[plan_id].get("sync_targets")
    assert sync_targets == ["telegram"], (
        f"sync_targets=['telegram'] did NOT round-trip; got {sync_targets!r}"
    )

    fake.release()


def test_start_api_default_channel(monkeypatch):
    """VP-001b: POST without sync_targets → 200 (default-channel / no-channel path).

    The test name ``test_start_api_default_channel`` is a substring
    match against the verifier's ``-k`` filter
    ``test_start_api_explicit_telegram or test_start_api_default_channel``.
    Per-PRD-DP-1 the no-arg caller path must still return 200 and fall
    back to the default sync_targets.

    2026-09-13 port: sync_targets is runtime-only; asserted against the
    in-memory ``_execution_state`` entry.
    """
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-001-default-channel"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    # Deliberately do NOT include ``sync_targets`` — this is the
    # default-channel path used by pre-migration callers.
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    # The default-channel (no sync_targets) path must still land the
    # default sync_targets on the runtime record.
    sync_targets = _execution_state[plan_id].get("sync_targets")
    assert sync_targets == ["telegram"], (
        f"omitted sync_targets did NOT fall back to ['telegram']; "
        f"got sync_targets={sync_targets!r}"
    )

    fake.release()