"""Backward-compat regression tests for ``POST /api/execution/{plan_id}/start``.

This module pins the API-level contract that keeps OLD callers (clients
that don't know about ``sync_targets`` yet) compatible with the new
sink-routing schema:

  1. ``test_start_api_omitted_sync_targets_falls_back_to_default``
     An old client that POSTs ``{"project_dir": "..."}`` — i.e. the
     request body that pre-dates the sync_targets extension — MUST
     still be accepted by the endpoint and MUST result in the executor
     runtime record carrying ``"sync_targets": ["telegram"]`` (the
     per-PRD-DP-1 fallback).

2026-09-13 port (SQLite decision): the retired ``execution.json`` file's
reads/writes were removed from the backend. ``sync_targets`` is
runtime-only — it lands on the in-memory ``_execution_state[plan_id]``
entry — while the run itself persists to the ``plan_execution`` SQLite
row (asserted here via the ``state_db_reader`` fixture, which reads the
hermetic per-test ``PDT_STATE_DB_PATH`` database, never the live
``state.db``).
     Without this contract, every old caller would either get a 422
     (regression) or end up with no pushes at all (silently dropped
     progress).

  2. ``test_start_api_explicit_empty_sync_targets_persists_empty``
     A new client that POSTs ``{"sync_targets": []}`` — i.e. the
     explicit "run but don't push anywhere" choice — MUST land
     ``"sync_targets": []`` on the runtime record (NOT the
     ``["telegram"]`` fallback).
     The boundary between "missing field → default" and "explicit
     empty list → no sinks" is load-bearing; conflating them would
     silently opt plans INTO the telegram sink that the caller
     explicitly opted out of.

  3. ``test_start_api_explicit_telegram_persists_telegram``
     A new client that POSTs ``{"sync_targets": ["telegram"]}`` —
     the explicit happy-path — MUST end up with exactly that list on
     the runtime record. This pins the contract that the request
     body's value round-trips through ``StartExecutionRequest``
     into ``_execution_state`` without mutation.

These three tests cover the full surface of the ``sync_targets``
fallback chain. Together they pin:

      request.sync_targets  →  effective_sync_targets  →  runtime record
      --------------------      -----------------------      -----------------
      None / missing            ["telegram"]                 ["telegram"]
      []                        []                           []
      ["telegram"]              ["telegram"]                 ["telegram"]
      ["slack"]                 (rejected with 422)          (not written)

Why this file exists
--------------------
``backend/tests/test_execution_start_api.py`` covers the four happy-path
cases of the same endpoint. This file isolates the backward-compat
contract — the "old client must still work" surface — into its own
module so a regression on either side surfaces independently in the
test output (rather than as a single conflated failure in a 6-test
file).

Test isolation
--------------
Same as ``test_execution_start_api.py``: ``FakeProcess`` mocks
``subprocess.Popen`` so no real executor is spawned, and
``_run_auto_verification_loop`` / ``_lazy_check_execution`` are stubbed
so the heartbeat thread does not perform real LLM calls.
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
# Test fixtures — same pattern as test_execution_start_api.py
# ---------------------------------------------------------------------------


class FakeProcess:
    """Fake ``subprocess.Popen`` that blocks until released."""

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
    """Reset global execution state and redirect PLANS_DIR per test."""
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(
        "server._run_auto_verification_loop",
        lambda *args, **kwargs: None,
    )
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


# ---------------------------------------------------------------------------
# Spec 1 — old client (no sync_targets field) → default ["telegram"]
# ---------------------------------------------------------------------------


def test_start_api_omitted_sync_targets_falls_back_to_default(monkeypatch, state_db_reader):
    """POST without ``sync_targets`` field → runtime record carries ["telegram"].

    Pins the backward-compat contract from PRD DP-1: an old caller
    that doesn't know about ``sync_targets`` (because they were
    written before the schema was extended) MUST still get the
    Telegram sink enabled by default. The endpoint must NOT 422 on
    the missing field — pydantic's default value is ``None``, and
    the endpoint's fallback logic converts ``None`` to
    ``["telegram"]``.

    2026-09-13 port: ``sync_targets`` is runtime-only (lands on the
    in-memory ``_execution_state`` entry, NOT persisted to SQLite);
    the run itself persists to the ``plan_execution`` row, which we
    assert via the ``state_db_reader`` fixture against the hermetic
    test DB.
    """
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-backcompat-default"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    # The body intentionally omits the ``sync_targets`` field —
    # this is the pre-migration caller shape.
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    sync_targets = _execution_state[plan_id].get("sync_targets")
    assert sync_targets == ["telegram"], (
        f"runtime sync_targets must fall back to ['telegram'] when the "
        f"field is omitted; got {sync_targets!r}"
    )

    row = state_db_reader.execution(plan_id)
    assert row is not None, (
        f"plan_execution row missing for {plan_id} after a successful start"
    )
    assert row.get("exec_status") == "running", (
        f"plan_execution exec_status={row.get('exec_status')!r}, "
        f"expected 'running'"
    )

    fake.release()


# ---------------------------------------------------------------------------
# Spec 2 — explicit empty list is preserved (NOT silently coerced to default)
# ---------------------------------------------------------------------------


def test_start_api_explicit_empty_sync_targets_persists_empty(monkeypatch):
    """POST ``sync_targets=[]`` → runtime record carries ``[]`` (NOT ["telegram"]).

    Pins the boundary between "missing field" and "explicit empty list":
    a caller that explicitly sends ``[]`` is opting OUT of every sink,
    and the endpoint MUST honor that without falling back to the
    Telegram default. A regression that conflates the two would silently
    re-introduce Telegram pushes for plans that explicitly opted out,
    which would surprise the caller.

    2026-09-13 port: ``sync_targets`` is runtime-only; asserted against
    the in-memory ``_execution_state`` entry.
    """
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-backcompat-empty"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir), "sync_targets": []},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    sync_targets = _execution_state[plan_id].get("sync_targets")
    assert sync_targets == [], (
        f"sync_targets=[] is a valid 'no sinks' value and MUST land on "
        f"the runtime record as []; got {sync_targets!r}"
    )

    fake.release()


# ---------------------------------------------------------------------------
# Spec 3 — explicit ["telegram"] round-trips verbatim
# ---------------------------------------------------------------------------


def test_start_api_invalid_sync_targets_slack_returns_422(monkeypatch, tmp_path, state_db_reader):
    """POST ``sync_targets=["slack"]`` → HTTP 422, no plan_execution row written.

    Pins the input-validation contract: ``sync_targets`` is restricted to
    ``Literal["feishu", "telegram"]`` (see ``StartExecutionRequest`` in
    ``server.py``). Any other value — e.g. ``"slack"`` — MUST be rejected
    by pydantic at the request-parsing boundary and MUST NOT reach the
    handler body, so no ``plan_execution`` row may be persisted for plans
    that the API has already rejected. Without this guarantee a caller
    could end up with a half-written execution record whose
    ``sync_targets`` field carries an unsupported sink name that the
    downstream fan-out code cannot route.

    2026-09-13 port: the retired ``execution.json`` assertion became
    vacuous (the file is never written anymore); the meaningful contract
    is "no persistence happened for the rejected request", asserted
    against the SQLite ``plan_execution`` row via ``state_db_reader``.
    """
    from server import PLANS_DIR

    plan_id = "vp-invalid-slack-422"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    popen_called = {"count": 0}

    def _tracking_popen(*args, **kwargs):
        popen_called["count"] += 1
        return fake

    monkeypatch.setattr("server.subprocess.Popen", _tracking_popen)

    project_dir = plan_dir / "project"
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir), "sync_targets": ["slack"]},
    )

    assert resp.status_code == 422, (
        f"start endpoint returned HTTP {resp.status_code} for invalid "
        f"sync_targets=['slack']; expected 422 (pydantic validation). "
        f"body={resp.text!r}"
    )

    row = state_db_reader.execution(plan_id)
    assert row is None, (
        f"plan_execution row must NOT be written when the request is "
        f"rejected at the validation boundary; found row={row!r}"
    )

    assert popen_called["count"] == 0, (
        f"subprocess.Popen must NOT be invoked when pydantic rejects the "
        f"request; was called {popen_called['count']} time(s)"
    )

    fake.release()


def test_start_api_explicit_telegram_persists_telegram(monkeypatch):
    """POST ``sync_targets=["telegram"]`` → runtime record carries ["telegram"].

    Pins the happy-path round-trip: when the caller explicitly passes
    the supported value, the endpoint MUST carry exactly that value on
    the runtime record without mutation. Together with Spec 1 and
    Spec 2 this pins the full ``sync_targets`` fallback chain.

    2026-09-13 port: ``sync_targets`` is runtime-only; asserted against
    the in-memory ``_execution_state`` entry.
    """
    from server import PLANS_DIR, _execution_state

    plan_id = "vp-backcompat-telegram"
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
        f"sync_targets=['telegram'] must round-trip verbatim; got "
        f"{sync_targets!r}"
    )

    fake.release()
