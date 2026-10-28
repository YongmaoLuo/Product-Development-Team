"""Pins for the spawn marker a service child inherits.

What is under test
------------------
The environment :func:`service_manager.start_service` hands to the
process it starts: the three marker variables the orphan reaper reads
(``PDT_SPAWN_OWNER_PID``, ``PDT_SPAWN_OWNER_START``,
``PDT_SPAWN_KIND``).

Why the marker needs a pin
--------------------------
A service is started **detached** -- on purpose it outlives the
process that started it.  That is also what makes it an orphan when
the backend dies, and it is why the reaper exists: the reaper can only
reclaim a process it can recognise as PDT's.  The recognition *is* the
marker, and the marker can only be written at the spawn point -- there
is no other place from which it can be written.

Three properties are load-bearing, and each has its own pin:

1. the marker is **present** and names *this* process as the owner;
2. the marker **replaces** an inherited one -- a child inherits its
   parent's marker, and a marker that survived a new owner would
   attribute the service to an ancestor that may long since be gone;
3. a **missing reaper module never blocks the spawn**.  The marker is
   how the reaper finds the process; it is not what makes the process
   run, so a service that cannot be marked must still be started.

Run them with:

    cd backend && .venv/bin/python3 -m pytest tests/unit/test_service_spawn_tags.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402

#: The marker keys, spelled out here so a pin can talk about them
#: without importing the reaper -- one of the pins below deliberately
#: takes the reaper away.
_MARKER_KEYS = (
    "PDT_SPAWN_OWNER_PID",
    "PDT_SPAWN_OWNER_START",
    "PDT_SPAWN_KIND",
)


def _decl(**overrides) -> ServiceDeclaration:
    base = dict(
        name="chart",
        port=3000,
        start_cmd="npx next dev -p 3000",
        cwd=".",
    )
    base.update(overrides)
    return ServiceDeclaration(**base)


class _FakeProc:
    """A child that was never really started.

    ``start_service`` polls the process once after the readiness wait;
    a child that is still running is the boring case, and what these
    pins are about is the environment, not the readiness verdict.
    """

    pid = 999999

    def poll(self) -> None:
        return None


@pytest.fixture
def project_dir(tmp_path) -> Path:
    """A project directory a declared service can be started in."""
    return tmp_path


@pytest.fixture
def start(monkeypatch, project_dir) -> Callable[..., Dict[str, Any]]:
    """Start a declared service and hand back what it was given.

    ``Popen`` is replaced, so nothing is really started -- but the
    replacement lets the reaper's own probe run for real: it asks the
    operating system when this process started (``ps``), and a
    replacement that swallowed that call would leave the pins asserting
    against a degraded code path instead of the one production runs.

    The fixture also gives every test a clean warning state, so the
    "warns once" pin does not depend on the order the tests ran in.
    """
    captured: Dict[str, Any] = {}
    real_popen = subprocess.Popen

    def _fake_popen(argv, **kwargs):
        if argv and os.path.basename(str(argv[0])) == "ps":
            # Not the service -- the reaper asking the system when
            # this process started.  Let it run.
            return real_popen(argv, **kwargs)
        captured["argv"] = argv
        captured["env"] = dict(kwargs.get("env") or {})
        captured["started"] = True
        return _FakeProc()

    monkeypatch.setattr(sm.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    # The child never existed, so there is nothing to wait for.
    monkeypatch.setattr(
        sm, "_await_exit", lambda proc, timeout=2.0: (False, None)
    )
    # ``raising=False``: the warning state is an implementation detail,
    # and a pin that cannot be set up is a pin that says nothing.
    monkeypatch.setattr(sm, "_SPAWN_MARKER_WARNED", set(), raising=False)

    def _start(**kwargs) -> Dict[str, Any]:
        decl = kwargs.pop("decl", None) or _decl()
        sm.start_service(decl, project_dir, **kwargs)
        assert captured.get("started"), "the service was never started"
        return captured

    return _start


# ---------------------------------------------------------------------------
# The marker is there, and it names this process
# ---------------------------------------------------------------------------


def test_start_service_env_has_spawn_tags(start):
    """The service's environment carries all three marker keys.

    The owner is the process that started the service -- this one.
    """
    env = start()["env"]

    assert env["PDT_SPAWN_OWNER_PID"] == str(os.getpid())
    assert "PDT_SPAWN_OWNER_START" in env
    assert env["PDT_SPAWN_KIND"] == "service"


def test_start_service_kind_is_service(start):
    """The marker says what the process is: a service.

    The reaper reads the kind to tell a service from a fixture or a
    sub-agent; a marker without one cannot be acted on.
    """
    env = start()["env"]

    assert env["PDT_SPAWN_KIND"] == "service"


def test_start_service_replaces_an_inherited_marker(start, monkeypatch):
    """An inherited marker is replaced, not merged.

    A child inherits its parent's marker.  The owner of a process is
    the process that actually started it, so a marker inherited from a
    grandparent -- whose pid says nothing about who started this
    service -- has to go.
    """
    monkeypatch.setenv("PDT_SPAWN_OWNER_PID", "424242")
    monkeypatch.setenv("PDT_SPAWN_OWNER_START", "1")
    monkeypatch.setenv("PDT_SPAWN_KIND", "agent")

    env = start()["env"]

    assert env["PDT_SPAWN_OWNER_PID"] == str(os.getpid())
    assert env["PDT_SPAWN_KIND"] == "service"


# ---------------------------------------------------------------------------
# A missing reaper never blocks the spawn
# ---------------------------------------------------------------------------


def test_start_service_survives_missing_reaper_module(start, monkeypatch):
    """No reaper module: the service still starts, and it is unmarked.

    The marker is how the reaper finds a process; it is not what makes
    the process run.  A service that cannot be marked must still be
    started -- and it must not keep a marker inherited from a
    grandparent, because that marker names an owner that did not start
    it.
    """
    monkeypatch.setattr(sm, "orphan_reaper", None)
    monkeypatch.setenv("PDT_SPAWN_OWNER_PID", "424242")
    monkeypatch.setenv("PDT_SPAWN_KIND", "agent")

    env = start()["env"]

    assert [key for key in _MARKER_KEYS if key in env] == []


def test_start_service_warns_once_about_the_missing_module(
    start, monkeypatch, caplog,
):
    """The missing module is worth one warning, not one per service."""
    monkeypatch.setattr(sm, "orphan_reaper", None)

    with caplog.at_level(logging.WARNING):
        start()
        start()

    warnings = [
        record.getMessage() for record in caplog.records
        if "orphan_reaper" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_start_service_marks_the_service_when_the_probe_fails(
    start, monkeypatch,
):
    """A marker that could not be built is degraded, not dropped.

    The owner start is read from the operating system, and that read
    can fail.  When it does, the marker is still written -- with an
    unknown owner start, which the reaper reads as "cannot tell" and
    not as "a different process".  An unmarked service is one the
    reaper never looks at.
    """
    def _boom(*args, **kwargs):
        raise OSError("the probe did not answer")

    monkeypatch.setattr(sm.orphan_reaper, "spawn_tags", _boom)

    env = start()["env"]

    assert env["PDT_SPAWN_OWNER_PID"] == str(os.getpid())
    assert env["PDT_SPAWN_OWNER_START"] == ""
    assert env["PDT_SPAWN_KIND"] == "service"


# ---------------------------------------------------------------------------
# The marker does not collide with the rest of the environment
# ---------------------------------------------------------------------------


def test_start_service_preserve_data_keeps_the_marker(start):
    """A restart inside a round carries the marker as well.

    The skip-seed flag and the spawn marker are independent: one says
    "do not re-create the data", the other says "who started this".
    """
    env = start(preserve_data=True)["env"]

    assert env[sm.SKIP_SEED_ENV] == "1"
    assert env["PDT_SPAWN_KIND"] == "service"
    assert env["PDT_SPAWN_OWNER_PID"] == str(os.getpid())


def test_start_service_keeps_the_service_environment(start):
    """The marker is added, not substituted: the service's own
    environment is untouched."""
    env = start()["env"]

    assert env["PDT_SVC_CHART_PORT"] == "3000"
    assert env["PDT_SVC_CHART_HOST"] == "127.0.0.1"
