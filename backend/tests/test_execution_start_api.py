"""TDD tests for StartExecutionRequest.sync_targets + execution.json
persistence.

These tests pin the contract pinned by PRD DP-1 + architecture DP-7:

  * ``POST /api/execution/{plan_id}/start`` accepts an optional
    ``sync_targets`` list whose elements must be ``"feishu"`` or
    ``"telegram"``.
  * When the caller passes ``sync_targets=["telegram"]``, the request
    body is accepted and ``plans/{plan_id}/execution.json`` has
    ``sync_targets == ["telegram"]`` at the top level.
  * When the caller passes ``sync_targets=null`` (or omits the field
    entirely), the endpoint falls back to ``["telegram"]`` — Feishu is
    owned by the caller and is NOT re-pushed here, so a no-arg caller
    still gets the Telegram sink enabled by default.
  * Unknown sink names (``"slack"``) are rejected by pydantic with HTTP
    422, surfacing typos up-front instead of silently dropping the
    message.
  * An empty list (``sync_targets=[]``) is a valid value: the executor
    runs but pushes are routed nowhere.

Why this file exists
--------------------
``tests/integration/test_execution_api.py`` covers the happy path of the
``POST /api/execution/{plan_id}/start`` endpoint (status=started,
project_dir persisted, status=running within 5s). The 4 TDD tests below
extend that contract with the ``sync_targets`` dimension.

Test isolation
--------------
Like the existing integration test, we patch
``server.subprocess.Popen`` to a ``FakeProcess`` so no real executor
subprocess is spawned. ``_run_auto_verification_loop`` and
``_lazy_check_execution`` are also stubbed so background threads do not
make LLM calls.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

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
    """Fake ``subprocess.Popen`` that blocks until released.

    Mirrors the helper in ``tests/integration/test_execution_api.py``.
    Exists in this file too so the test file is self-contained (importing
    across test files is fragile in pytest discovery).
    """

    def __init__(self):
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


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset global execution state and redirect PLANS_DIR per test.

    Also stubs auto-verification + heartbeat probes so background
    threads don't perform real LLM calls.
    """
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    # Stub auto-verification so background threads do not spin up real
    # LLM work.
    monkeypatch.setattr(
        "server._run_auto_verification_loop",
        lambda *args, **kwargs: None,
    )
    # Stub _lazy_check_execution: FakeProcess.pid is not a real OS PID,
    # so os.kill(pid, 0) would raise ProcessLookupError and flip
    # status to 'failed'. We want the test to observe status='running'
    # and the test to exit cleanly, so disable the lazy heartbeat probe
    # entirely.
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan_dir(plan_id: str, plans_dir: Path) -> Path:
    """Create a minimal plan directory with tasks.json + plan_state.json."""
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


def _read_execution_json(plan_dir: Path) -> dict:
    """Load execution.json into a dict; raise AssertionError if missing."""
    exec_file = plan_dir / "execution.json"
    assert exec_file.exists(), (
        f"execution.json was not written to {exec_file} after start"
    )
    return json.loads(exec_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# TDD test 1 — explicit ``["telegram"]`` round-trips into execution.json
# ---------------------------------------------------------------------------


def test_start_with_telegram_only_persists_to_execution_json(monkeypatch, state_db_reader):
    """POST sync_targets=["telegram"] → the executor's in-memory record carries ["telegram"].

    2026-09-13 port (SQLite decision): ``sync_targets`` is a runtime-only
    field — the server stores it in ``_execution_state`` and does NOT
    persist it to SQLite (see the "``sync_targets`` lives in the
    in-memory ``_execution_state`` dict" note in ``server.py``). The old
    assertion read the retired ``execution.json``; the contract being
    pinned here is really "the explicit sink list is accepted and lands
    on the executor record", which we now assert against
    ``_execution_state`` plus the persisted ``plan_execution`` row.
    """
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-sync-targets-telegram"
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
    body = resp.json()
    assert body.get("status") == "started", (
        f"start endpoint returned status={body.get('status')!r}, expected 'started'"
    )

    # The sync_targets fallback lands on the executor's runtime record.
    assert _execution_state[plan_id]["sync_targets"] == ["telegram"], (
        f"in-memory sync_targets={_execution_state[plan_id].get('sync_targets')!r}, "
        f"expected ['telegram']"
    )
    # And the run itself persists to the SQLite plan_execution row.
    row = state_db_reader.execution(plan_id)
    assert row is not None, f"plan_execution row missing for {plan_id} after start"
    assert row.get("exec_status") == "running", (
        f"plan_execution exec_status={row.get('exec_status')!r}, expected 'running'"
    )

    fake.release()


# ---------------------------------------------------------------------------
# TDD test 2 — ``sync_targets=null`` falls back to ``["telegram"]``
# ---------------------------------------------------------------------------


def test_start_with_none_defaults_to_telegram(monkeypatch):
    """POST sync_targets=null → the executor record falls back to ["telegram"]."""
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-sync-targets-null"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir), "sync_targets": None},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    # sync_targets is runtime-only (not persisted to SQLite) — see the
    # test_start_with_telegram_only_persists_to_execution_json docstring.
    assert _execution_state[plan_id]["sync_targets"] == ["telegram"], (
        f"sync_targets=null did NOT fall back to ['telegram']; "
        f"got sync_targets={_execution_state[plan_id].get('sync_targets')!r}"
    )

    fake.release()


# ---------------------------------------------------------------------------
# TDD test 3 — omitting sync_targets entirely is backward-compatible
# ---------------------------------------------------------------------------


def test_start_omitting_sync_targets_defaults_to_telegram(monkeypatch):
    """No sync_targets field at all → the executor record falls back to ["telegram"].

    This pins backward compatibility: an old caller that doesn't know
    about sync_targets yet still gets the Telegram sink enabled by
    default (the per-PRD-DP-1 fallback). sync_targets is runtime-only —
    see test_start_with_telegram_only_persists_to_execution_json.
    """
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-sync-targets-omitted"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    # Deliberately do NOT include ``sync_targets`` in the request body
    # — this is the oldest-caller shape, pre sync_targets extension.
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    assert _execution_state[plan_id]["sync_targets"] == ["telegram"], (
        f"omitted sync_targets did NOT fall back to ['telegram']; "
        f"got sync_targets={_execution_state[plan_id].get('sync_targets')!r}"
    )

    fake.release()


# ---------------------------------------------------------------------------
# TDD test 4 — unknown sink name is rejected with 422
# ---------------------------------------------------------------------------


def test_start_with_invalid_sync_targets_returns_422(monkeypatch):
    """POST sync_targets=["slack"] → 422 with an error mentioning
    'feishu' or 'telegram'.

    Pydantic validates the ``Literal["feishu", "telegram"]`` type and
    rejects any other value with HTTP 422. The error message must
    surface the supported enum values so a typo like ``"slack"`` or
    ``"feishu "`` (trailing space) is debuggable from the response
    body alone.
    """
    from server import PLANS_DIR

    plan_id = "vp-sync-targets-unknown"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir), "sync_targets": ["slack"]},
    )

    assert resp.status_code == 422, (
        f"start endpoint returned HTTP {resp.status_code} for unknown sink "
        f"'slack', expected 422; body={resp.text!r}"
    )

    # The 422 body must mention at least one of the supported enum values
    # so the caller can self-correct.
    body_text = resp.text
    assert ("feishu" in body_text) or ("telegram" in body_text), (
        f"422 response did NOT mention either 'feishu' or 'telegram' so the "
        f"caller cannot discover the supported enum values from the error "
        f"alone; body={body_text!r}"
    )

    # And critically, no execution.json should have been written — the
    # request was rejected before the executor started.
    exec_file = plan_dir / "execution.json"
    assert not exec_file.exists(), (
        f"execution.json was unexpectedly written at {exec_file} even though "
        f"the request was rejected with 422"
    )

    fake.release()


# ---------------------------------------------------------------------------
# TDD test 5 — missing tasks.json → 404 with 'Tasks not found' detail
# ---------------------------------------------------------------------------


def test_start_with_missing_tasks_returns_404(monkeypatch):
    """POST /api/execution/{plan_id}/start with no tasks.json → HTTP 404.

    Pins VP-003-07: when ``tasks.json`` does not exist under the plan
    directory (either because the plan_id is unknown, or because the
    task-generation phase has not run yet), the start endpoint must
    reject the request with HTTP 404 and a ``detail`` field that
    contains the substring ``'Tasks not found'`` so the caller can
    distinguish "plan doesn't exist" from "plan exists but tasks not
    generated yet". No executor subprocess must be spawned and no
    ``execution.json`` must be written.
    """
    from server import PLANS_DIR

    plan_id = "vp-missing-tasks-404"
    plans_dir = PLANS_DIR
    # Deliberately do NOT call _setup_plan_dir — the whole point is that
    # tasks.json is absent. We only create the (empty) plan dir so that
    # any later write of execution.json has a parent directory.
    (plans_dir / plan_id).mkdir(parents=True, exist_ok=True)

    # If a real subprocess were spawned, the test would hang and
    # subprocess.Popen would be visible. Patch it so any leak of the
    # 404 guard is observable as a test failure.
    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    # Hit the endpoint with a plan_id whose plan directory has no
    # tasks.json. ``project_dir`` can point to any existing directory;
    # the 404 guard fires before project_dir is validated.
    project_dir = plans_dir / plan_id / "project"
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir), "sync_targets": ["telegram"]},
    )

    assert resp.status_code == 404, (
        f"start endpoint returned HTTP {resp.status_code} for missing "
        f"tasks.json, expected 404; body={resp.text!r}"
    )

    body = resp.json()
    # FastAPI renders HTTPException(detail=str) as {"detail": str}.
    # The PRD-level guard message starts with "Tasks not found" — the
    # caller-facing substring we pin is "Tasks not found".
    detail = body.get("detail", "")
    assert isinstance(detail, str), (
        f"404 response 'detail' field must be a string; got "
        f"{type(detail).__name__}: {body!r}"
    )
    assert "Tasks not found" in detail, (
        f"404 detail did not contain 'Tasks not found' substring; "
        f"got detail={detail!r}, body={body!r}"
    )

    # And critically: no executor subprocess was spawned, and no
    # execution.json was written. The 404 fired before _run() started.
    exec_file = plans_dir / plan_id / "execution.json"
    assert not exec_file.exists(), (
        f"execution.json was unexpectedly written at {exec_file} even "
        f"though the request was rejected with 404 for missing tasks.json"
    )
    # Also: the in-memory _execution_state must NOT have been populated
    # for this plan_id, otherwise a later "stop" / "status" call would
    # see phantom state.
    from server import _execution_state

    assert plan_id not in _execution_state, (
        f"_execution_state unexpectedly contains plan_id={plan_id!r} "
        f"after a 404 for missing tasks.json; state={_execution_state!r}"
    )

    fake.release()


# ---------------------------------------------------------------------------
# TDD test 6 — refuses to execute inside backend/ directory with HTTP 400
# ---------------------------------------------------------------------------


def test_start_inside_backend_dir_refuses_with_400(monkeypatch, tmp_path):
    """VP-003-08 safety guard: project_dir == <root>/backend must be rejected.

    Pins the safety guard in ``start_execution``: if a caller posts
    ``project_dir`` pointing at (or under) the running server's own
    ``backend/`` directory, the endpoint must refuse with HTTP 400 and a
    ``detail`` field containing the substring ``"拒绝在 backend/ 目录内执行"``
    so a misconfigured CLI / dashboard can never accidentally spawn an
    executor subprocess that would clobber the running service code.

    No subprocess must be spawned (Popen must not have been called) and
    no ``execution.json`` may be written.
    """
    from server import PLANS_DIR

    plan_id = "vp-refuses-backend-dir-400"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    # Patch Popen. If the guard leaks, this fake would be spawned and
    # the test would either hang or assert below would catch the leak.
    fake = FakeProcess()
    popen_calls = []

    def _tracking_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return fake

    monkeypatch.setattr("server.subprocess.Popen", _tracking_popen)

    # The guard resolves _self_root as Path(__file__).parent.parent, so
    # for server.py under <root>/backend/server.py, _backend_dir ==
    # <root>/backend. Pointing project_dir at the same path triggers the
    # == branch of the guard.
    backend_root = Path(__file__).resolve().parent.parent
    backend_dir = (backend_root / "backend").resolve()

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(backend_dir), "sync_targets": ["telegram"]},
    )

    assert resp.status_code == 400, (
        f"start endpoint returned HTTP {resp.status_code} for project_dir "
        f"pointing inside backend/, expected 400; body={resp.text!r}"
    )

    body = resp.json()
    detail = body.get("detail", "")
    assert isinstance(detail, str), (
        f"400 response 'detail' field must be a string; got "
        f"{type(detail).__name__}: {body!r}"
    )
    assert "拒绝在 backend/ 目录内执行" in detail, (
        f"400 detail did NOT contain the substring "
        f"'拒绝在 backend/ 目录内执行'; got detail={detail!r}, body={body!r}"
    )

    # And critically: the guard fired before _run() started, so
    # subprocess.Popen must not have been called.
    assert popen_calls == [], (
        f"subprocess.Popen was called even though the backend_dir guard "
        f"fired; calls={popen_calls!r}"
    )

    # No execution.json must have been written either.
    exec_file = plan_dir / "execution.json"
    assert not exec_file.exists(), (
        f"execution.json was unexpectedly written at {exec_file} even "
        f"though the request was rejected with 400 for backend_dir guard"
    )

    # And the in-memory state must NOT have been populated.
    from server import _execution_state

    assert plan_id not in _execution_state, (
        f"_execution_state unexpectedly contains plan_id={plan_id!r} "
        f"after the 400 backend_dir refusal; state={_execution_state!r}"
    )

    fake.release()