"""The lifespan wiring of the PDT-native scheduler engine.

Architecture decision point 1 pins the order the engine is assembled
in:

* the engine is built **after** the startup migration and the recovery
  of the plans that were running — its first tick decides on the plan
  facts, and facts that have not been recovered yet are facts the
  scheduler would decide against;
* it is hung on ``app.state.scheduler_engine``;
* on the way out it is stopped and joined **before** the application
  state is taken apart;
* ``enabled: false`` starts no worker at all — "not running" has to
  mean "no thread", or "there is exactly one scheduler at any moment"
  is a claim about a thread that is still running.

The cases drive the real ``server._lifespan`` against a fake app and
watch the worker thread it leaves behind.  The engine itself is the
production one: only the class is wrapped, so that the moment it is
constructed can be recorded.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import yaml


_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The name the scheduler's worker thread carries.  It is the engine's
#: own name, spelled out here so that a change to it shows up as a
#: failure in this file rather than as a silent leak.
THREAD_NAME = "PlanSchedulerEngine"

#: The markers that identify the server's recovery helpers.  They are
#: private and they have moved before, so a case that pinned their
#: names would turn a rename into five *errors* instead of one failure
#: with a readable message.
_RECOVERY_MARKERS = ("recover", "reconcile")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _engine_threads() -> List[threading.Thread]:
    """The scheduler worker threads that are alive right now."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == THREAD_NAME and thread.is_alive()
    ]


def _recovery_helpers(server_module: Any) -> List[str]:
    """The server's recovery helpers, by whatever they are called today.

    They are found by the shape of their names rather than pinned: a
    helper that has been renamed away is a fact the case reports, not
    an ``AttributeError`` it raises while setting up.
    """
    return [
        name
        for name in dir(server_module)
        if name.startswith("_")
        and any(marker in name for marker in _RECOVERY_MARKERS)
        and callable(getattr(server_module, name, None))
    ]


class _FakeState:
    """A real attribute bag.

    A ``MagicMock`` would answer every attribute with another mock, so
    "the engine is ``None``" could not be told apart from "the engine
    is something".
    """


class _FakeApp:
    """The app the lifespan is handed."""

    def __init__(self) -> None:
        self.state = _FakeState()


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_module():
    """The ``server`` module — the flat one, the way production imports it.

    The flat name is the one the suite's own fixtures patch (they set
    ``server.PLANS_DIR`` and ``server._state_db_path``), so a case that
    imported ``backend.server`` would be looking at a second module
    object with a second copy of everything.
    """
    import importlib

    return importlib.import_module("server")


@pytest.fixture
def startup(server_module, monkeypatch):
    """The lifespan, with the parts that are not under test stubbed out.

    The recovery helpers are replaced by recorders: the case has to
    know *when* the recovery happened, because the order is the
    contract.  The heartbeat monitor is replaced by a mock so that no
    second thread is started behind the case's back.
    """
    events: List[str] = []

    def _record(*args: Any, **kwargs: Any) -> None:
        events.append("recover")

    for name in _recovery_helpers(server_module):
        monkeypatch.setattr(server_module, name, _record, raising=False)

    monkeypatch.setattr(
        server_module, "load_providers", lambda: [], raising=False
    )
    monkeypatch.setattr(
        server_module, "heartbeat_monitor", MagicMock(), raising=False
    )
    return events


@pytest.fixture
def settings(tmp_path, monkeypatch, server_module):
    """The scheduler's settings file — owned by this case.

    The server reads the ``scheduler:`` section of its own
    ``config.yaml``; the path it reads is a seam, so a case can say
    what the scheduler is told without touching the repository's file.
    """
    path = tmp_path / "config.yaml"

    def _write(**scheduler: Any) -> Path:
        path.write_text(
            yaml.safe_dump({"scheduler": scheduler}), encoding="utf-8"
        )
        return path

    monkeypatch.setattr(
        server_module, "_scheduler_settings_path", lambda: path, raising=False
    )
    return _write


@pytest.fixture
def recording_engine(monkeypatch, startup):
    """The production engine, with its construction recorded.

    The class is wrapped rather than replaced: the case needs to know
    *when* the engine was built, and it needs the real engine — the
    real collaborators, the real worker thread.
    """
    from plan_scheduler import engine as engine_module

    real_engine = engine_module.PlanSchedulerEngine

    class _RecordingEngine(real_engine):  # type: ignore[misc, valid-type]
        """The engine, watched."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            startup.append("engine")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(engine_module, "PlanSchedulerEngine", _RecordingEngine)
    return _RecordingEngine


def _drive(server_module: Any, app: Any, inside: Callable[[], None]) -> None:
    """Enter the lifespan and run ``inside`` while it is up."""
    async def _main() -> None:
        async with server_module._lifespan(app):
            inside()

    asyncio.run(_main())


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_disabled_does_not_start_thread(
    server_module, startup, settings, recording_engine
) -> None:
    """``enabled: false`` — no worker thread, and no engine on the app.

    A scheduler that is switched off must leave no thread behind.  The
    thread is the scheduler as far as the rest of the system is
    concerned, and a thread that is still running is a scheduler that is
    still running.
    """
    settings(enabled=False)

    app = _FakeApp()
    _drive(server_module, app, lambda: None)

    assert hasattr(app.state, "scheduler_engine"), (
        "the lifespan must publish the engine handle even when the "
        "scheduler is off — a reader must be able to tell 'off' from "
        "'not wired yet'"
    )
    assert app.state.scheduler_engine is None, (
        "a disabled scheduler must leave no engine on the app"
    )
    assert _engine_threads() == [], (
        "a disabled scheduler started a worker thread: "
        f"{[t.name for t in _engine_threads()]!r}"
    )
    assert "engine" not in startup, (
        "the engine was constructed even though the configuration "
        "switched the scheduler off"
    )


def test_enabled_starts_after_recovery(
    server_module, startup, settings, recording_engine
) -> None:
    """The engine is built **after** the recovery of the running plans.

    The engine's first tick decides on the plan facts.  A worker that
    started before the recovery would decide on a half-recovered world
    — a plan that was running a moment ago and is not in the facts yet
    is a plan the scheduler would start a second time.
    """
    settings(
        enabled=True,
        dry_run=True,
        capacity=1,
        min_runtime_seconds=0,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    app = _FakeApp()
    published: Dict[str, Any] = {}

    def _inside() -> None:
        published["engine"] = getattr(app.state, "scheduler_engine", None)

    _drive(server_module, app, _inside)

    assert published["engine"] is not None, (
        "an enabled scheduler must publish its engine on the app"
    )
    from plan_scheduler.engine import PlanSchedulerEngine

    assert isinstance(published["engine"], PlanSchedulerEngine), (
        "app.state.scheduler_engine must be a PlanSchedulerEngine, got "
        f"{type(published['engine']).__name__}"
    )
    assert "engine" in startup, "the engine was never constructed"

    recoveries = [i for i, event in enumerate(startup) if event == "recover"]
    built = startup.index("engine")
    assert recoveries, (
        "the recovery did not run at all — the server module exposes no "
        f"recovery helper to compare the order against: "
        f"{_recovery_helpers(server_module)!r}"
    )
    assert built > max(recoveries), (
        "the engine was constructed before the recovery finished: "
        f"{startup!r}"
    )


def test_shutdown_joins_thread(
    server_module, startup, settings, recording_engine
) -> None:
    """The worker is stopped **and joined** on the way out.

    Stopping is a request and joining is the guarantee.  A thread that
    was asked to stop but is still running would tick into a world that
    is being taken apart.
    """
    settings(
        enabled=True,
        dry_run=True,
        capacity=1,
        min_runtime_seconds=0,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    app = _FakeApp()
    _drive(server_module, app, lambda: None)

    assert _engine_threads() == [], (
        "the worker thread outlived the lifespan: "
        f"{[t.name for t in _engine_threads()]!r}"
    )
    assert app.state.scheduler_engine is None, (
        "the engine handle was not taken off the app when the worker "
        "was stopped"
    )


def test_double_start_single_thread(
    server_module, startup, settings, recording_engine
) -> None:
    """A second start does not produce a second worker.

    Two workers are two schedulers, and "at any moment there is exactly
    one scheduler" is an invariant rather than a preference.  The
    lifespan that finds a worker already running leaves it alone: it
    does not start a second one, and it does not stop the one it did
    not start either.
    """
    settings(
        enabled=True,
        dry_run=True,
        capacity=1,
        min_runtime_seconds=0,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    app = _FakeApp()
    seen: Dict[str, List[threading.Thread]] = {}

    async def _main() -> None:
        async with server_module._lifespan(app):
            async with server_module._lifespan(app):
                seen["inside"] = _engine_threads()
            seen["after_inner"] = _engine_threads()
        seen["after_outer"] = _engine_threads()

    asyncio.run(_main())

    assert len(seen["inside"]) == 1, (
        "a second start produced a second worker: "
        f"{[t.name for t in seen['inside']]!r}"
    )
    assert len(seen["after_inner"]) == 1, (
        "the inner lifespan stopped the worker the outer one started — "
        "a lifespan that did not start the worker must not stop it"
    )
    assert seen["after_outer"] == [], (
        "the worker outlived the lifespan that started it: "
        f"{[t.name for t in seen['after_outer']]!r}"
    )


def test_construction_failure_is_logged_and_cleaned_up(
    server_module, startup, settings, monkeypatch, caplog
) -> None:
    """An engine that cannot be built does not take the API down.

    The failure is logged, the app carries no engine, and the resources
    that were created before the failure are given back — a connection
    that was opened and never closed is a file descriptor that is never
    returned.
    """
    settings(
        enabled=True,
        dry_run=True,
        capacity=1,
        min_runtime_seconds=0,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    from plan_scheduler import engine as engine_module

    class _BrokenEngine:
        """An engine that cannot be built."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the engine cannot be built")

    monkeypatch.setattr(engine_module, "PlanSchedulerEngine", _BrokenEngine)

    opened: List[sqlite3.Connection] = []
    from state_machine.db import connection as connection_module

    real_open = connection_module.open

    def _recording_open(db_path: Any) -> sqlite3.Connection:
        conn = real_open(db_path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(connection_module, "open", _recording_open)

    app = _FakeApp()
    _drive(server_module, app, lambda: None)

    assert app.state.scheduler_engine is None, (
        "a scheduler that could not be built must leave no engine on the app"
    )
    assert any(
        "could not be built" in record.getMessage() for record in caplog.records
    ), (
        "the construction failure was not logged — a scheduler that could "
        "not start has to say so: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    assert opened, "the database was never opened — nothing was cleaned up"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
