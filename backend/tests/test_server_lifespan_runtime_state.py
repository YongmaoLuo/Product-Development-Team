"""TDD spec for ``backend/server.py`` lifespan injection of
:class:`backend.runtime_state.RuntimeState`.

The verification executor persists its in-memory bookkeeping (the
``pending_vps`` queue, etc.) into the SQLite
``plan_verification.runtime_state`` JSON column. The ``RuntimeState``
dataclass gives callers attribute access instead of
``runtime_state.get("pending_vps", [])`` patterns.

2026-09-06 TC-006 dependency-injection refactor: the FastAPI
lifespan is now the canonical constructor for ``RuntimeState``. It
builds a fresh instance (carrying the four DI collaborators —
``config``, ``provider_order_file``, ``in_flight_guard``,
``dynamic_tracker``) and attaches it to ``app.state.runtime_state``.
A previous revision exposed a module-level ``RUNTIME_STATE``
singleton; the refactor removed it, so the ONLY handle on the live
state is ``app.state.runtime_state``. These tests pin the new
contract (and pin the absence of the old singleton so it cannot be
reintroduced silently).

Pinned contracts:

  1. **No module-level singleton.** ``server.py`` does NOT expose
     ``RUNTIME_STATE`` — downstream code must read the handle from
     ``app.state.runtime_state`` (the lifespan is the sole owner).
  2. **Lifespan attaches ``app.state.runtime_state``.** After
     :func:`server._lifespan` runs, ``app.state.runtime_state``
     MUST be a ``RuntimeState`` instance.
  3. **Fresh instance per startup.** Each lifespan entry constructs
     a NEW instance — two consecutive startups must not share the
     object (no hidden global state leaking across restarts).
  4. **DI collaborators populated.** The injected instance carries
     the parsed ``config.yaml`` dict, the resolved
     ``provider_order_file`` Path, and live ``in_flight_guard`` /
     ``dynamic_tracker`` instances.
  5. **Default / round-trip ``pending_vps``.** A fresh
     ``RuntimeState`` defaults to ``pending_vps == []`` and the
     list is mutable in place (the executor's read-and-mutate
     pattern depends on it).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture
def server_module():
    """Import the :mod:`backend.server` module on demand."""
    import importlib

    return importlib.import_module("backend.server")


@pytest.fixture
def runtime_state_class(server_module):
    """Return the ``RuntimeState`` class as server.py sees it.

    ``server.py`` is loaded via ``backend.server`` in production
    but via the bare top-level name ``server`` in this test (the
    pytest path puts ``backend/`` on ``sys.path``). That means
    server.py's ``from runtime_state import RuntimeState``
    resolves to the bare ``runtime_state`` module — NOT the
    package-qualified ``backend.runtime_state``. Looking the
    class up via the server module's own namespace guarantees
    we compare against the same class object the production
    code uses.
    """
    return server_module.RuntimeState


def _drive_lifespan(server_module, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Enter ``server._lifespan`` against a fake app and return the
    injected ``app.state.runtime_state``.

    The recovery helpers + heartbeat are stubbed so the lifespan body
    does not depend on the live state-machine SQLite database; the
    contract under test is purely the injection, not the recovery
    semantics.
    """
    monkeypatch.setattr(server_module, "_recover_execution_states", lambda plans_dir: None)
    monkeypatch.setattr(server_module, "_recover_verification_states", lambda plans_dir: None)
    monkeypatch.setattr(server_module, "load_providers", lambda: [])
    fake_heartbeat = MagicMock()
    monkeypatch.setattr(server_module, "heartbeat_monitor", fake_heartbeat)

    fake_app = MagicMock()
    fake_app.state = MagicMock()

    async def _enter() -> Any:
        async with server_module._lifespan(fake_app):
            return fake_app.state.runtime_state

    return asyncio.run(_enter())


# ---------------------------------------------------------------------------
# Contract 1: the module-level singleton stays removed
# ---------------------------------------------------------------------------


def test_server_module_does_not_expose_module_level_RUNTIME_STATE(
    server_module,
) -> None:
    """``server.RUNTIME_STATE`` must NOT exist (TC-006 refactor).

    The pre-refactor singleton let tests / helpers bypass the
    lifespan, which is exactly how divergent copies of the state
    crept in. The lifespan is the sole owner; reintroducing a
    module-level handle must fail here.
    """
    assert not hasattr(server_module, "RUNTIME_STATE"), (
        "server.py must NOT expose a module-level RUNTIME_STATE "
        "singleton — the only handle on the live state is "
        "app.state.runtime_state (TC-006 dependency-injection "
        "refactor)."
    )


# ---------------------------------------------------------------------------
# Contract 2: lifespan attaches a RuntimeState instance
# ---------------------------------------------------------------------------


def test_lifespan_attaches_RuntimeState_to_app_state(
    server_module, runtime_state_class, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifespan handler MUST inject a ``RuntimeState`` instance
    onto ``app.state.runtime_state``.

    Verified by directly entering ``server._lifespan`` as an async
    context manager against a fake app.
    """
    injected = _drive_lifespan(server_module, monkeypatch)
    assert isinstance(injected, runtime_state_class), (
        "app.state.runtime_state must be a RuntimeState instance "
        "after the FastAPI lifespan handler has run."
    )


# ---------------------------------------------------------------------------
# Contract 3: fresh instance per startup — no shared global
# ---------------------------------------------------------------------------


def test_lifespan_constructs_fresh_instance_per_startup(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive lifespan entries must produce DISTINCT objects.

    Pins "the lifespan is the canonical constructor": holding a
    reference to a previous startup's state must never leak into a
    new startup (no module-global mutable handle).
    """
    first = _drive_lifespan(server_module, monkeypatch)
    second = _drive_lifespan(server_module, monkeypatch)
    assert first is not second, (
        "each lifespan startup must construct a fresh RuntimeState — "
        "a shared instance means a hidden global handle survived the "
        "TC-006 refactor."
    )


# ---------------------------------------------------------------------------
# Contract 4: DI collaborators populated
# ---------------------------------------------------------------------------


def test_lifespan_injected_state_carries_di_collaborators(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected RuntimeState carries the four DI collaborators.

    * ``config`` — the parsed ``backend/config.yaml`` dict (``{}``
      when the file is absent is acceptable; the loader never raises).
    * ``provider_order_file`` — an absolute :class:`~pathlib.Path`.
    * ``in_flight_guard`` — a live ``InFlightFileGuard`` instance.
    * ``dynamic_tracker`` — a live ``ActiveConcurrencyTracker``.
    """
    injected = _drive_lifespan(server_module, monkeypatch)

    assert isinstance(injected.config, dict), (
        f"config must be the parsed config.yaml dict, got "
        f"{type(injected.config).__name__}"
    )
    assert isinstance(injected.provider_order_file, Path), (
        f"provider_order_file must be a resolved Path, got "
        f"{type(injected.provider_order_file).__name__}"
    )
    assert isinstance(injected.in_flight_guard, server_module.InFlightFileGuard), (
        f"in_flight_guard must be an InFlightFileGuard, got "
        f"{type(injected.in_flight_guard).__name__}"
    )
    assert isinstance(injected.dynamic_tracker, server_module.ActiveConcurrencyTracker), (
        f"dynamic_tracker must be an ActiveConcurrencyTracker, got "
        f"{type(injected.dynamic_tracker).__name__}"
    )


def test_lifespan_publishes_the_tracker_to_the_scene_dispatch_path(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ClaudeCodingTool`` must reach the SAME tracker the executor uses.

    The executor gets it by DI, but the tool is constructed in ~25 places
    that have no app state in scope and picks its provider inside
    ``_run_claude_interactive``. If the lifespan does not publish the
    instance, each tool builds a private tracker, counts only its own
    work, and the per-provider caps never bind — the state that let every
    verification VP pile onto Vendor A Pro while its sibling rows
    idled (2026-09-17).
    """
    from dynamic_provider_concurrency import get_shared_tracker

    original = get_shared_tracker()
    try:
        injected = _drive_lifespan(server_module, monkeypatch)
        assert get_shared_tracker() is injected.dynamic_tracker, (
            "the lifespan must register RuntimeState.dynamic_tracker as "
            "the process-wide tracker the scene walk consults"
        )
    finally:
        from dynamic_provider_concurrency import set_shared_tracker

        set_shared_tracker(original)


# ---------------------------------------------------------------------------
# Contract 5: default / round-trip pending_vps on the injected handle
# ---------------------------------------------------------------------------


def test_injected_RuntimeState_defaults_to_empty_pending_vps(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh server has no in-flight verification VPs to resume.

    The freshly-constructed ``RuntimeState()`` must default to
    ``pending_vps == []`` — mirroring the executor's fallback when
    the SQLite column is missing the key.
    """
    injected = _drive_lifespan(server_module, monkeypatch)
    assert injected.pending_vps == []


def test_injected_RuntimeState_round_trip_preserves_mutations(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pending_vps`` is a mutable list — in-place mutation persists.

    Mirrors the executor's read-and-mutate pattern: appending to the
    injected handle's list must be observable on re-read (no
    ``@property`` returning a fresh copy), and the mutation must not
    leak into a subsequent startup's fresh instance.
    """
    injected = _drive_lifespan(server_module, monkeypatch)
    injected.pending_vps.append("VP-001")
    try:
        assert injected.pending_vps == ["VP-001"]
        # A second startup gets a FRESH instance — the mutation must
        # not leak across lifespans.
        fresh = _drive_lifespan(server_module, monkeypatch)
        assert fresh.pending_vps == [], (
            "pending_vps mutations on one startup's handle leaked "
            "into the next startup's fresh RuntimeState."
        )
    finally:
        injected.pending_vps.remove("VP-001")


# ---------------------------------------------------------------------------
# Contract 7: the startup credential-residue sweep
# ---------------------------------------------------------------------------


def _fake_sweep(findings, failures=0, pruned=()):
    """A stand-in for ``secret_sweep.sweep_default_roots``.

    Returns the callable and a dict the callable records its arguments
    into, so a test can assert *how* the lifespan invoked the sweep. The
    real function returns ``(findings, failures, pruned_dirs)`` — the
    prune half is what keeps the temp-root population bounded, so the
    stand-in has to carry it too.
    """

    class _Finding:
        def __init__(self, path, status):
            self.path = path
            self.status = status

    calls: dict = {}

    def _run(apply=False):
        calls["apply"] = apply
        return [_Finding(p, s) for p, s in findings], failures, list(pruned)

    return _run, calls


def test_lifespan_runs_the_credential_residue_sweep(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Booting sweeps the temp roots, and it does so in **apply** mode.

    A dispatch's settings payload is redacted when the child is reaped;
    a backend killed mid-dispatch never gets there. Boot is the one
    moment when nothing of ours can legitimately be running, so it is
    where the residue is collected — and a dry run at boot would collect
    nothing, which is why ``apply`` is asserted rather than assumed.
    """
    run, calls = _fake_sweep([("/tmp/subagent_settings_x.json", "redacted")])
    monkeypatch.setattr(server_module, "_sweep_credential_residue", run)

    _drive_lifespan(server_module, monkeypatch)

    assert calls == {"apply": True}, (
        "the startup sweep ran in dry-run mode, so residue from a "
        "previous life survives every boot"
    )


def test_lifespan_survives_a_failing_credential_sweep(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep that cannot run must not take the backend down with it.

    Cleaning up credentials is housekeeping; refusing to serve because a
    temp root was unreadable would make the remedy worse than the leak.
    The failure is logged and boot continues — the same contract the
    service-orphan sweep next to it carries.
    """
    def _boom(apply=False):
        raise OSError("temp root unreadable")

    monkeypatch.setattr(server_module, "_sweep_credential_residue", _boom)

    injected = _drive_lifespan(server_module, monkeypatch)

    assert injected is not None, (
        "a raising credential sweep aborted startup; the sweep is "
        "housekeeping and must never be able to do that"
    )


def test_lifespan_tolerates_a_missing_sweep_module(
    server_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` is the "module missing" sentinel resolved at import time.

    It has to be a no-op rather than an ``AttributeError`` in the middle
    of the lifespan — a checkout without ``utils/secret_sweep.py`` must
    still start.
    """
    monkeypatch.setattr(server_module, "_sweep_credential_residue", None)

    injected = _drive_lifespan(server_module, monkeypatch)

    assert injected is not None
