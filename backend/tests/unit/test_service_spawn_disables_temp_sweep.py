"""A service child must never run the machine-wide credential sweep.

What is under test
------------------
The environment :func:`service_manager.start_service` hands to the
process it starts, and specifically one key in it:
``PDT_DISABLE_TEMP_SWEEP``.

Why the key has to be pinned
----------------------------
``utils.secret_sweep.sweep_default_roots()`` rewrites the machine's
real temp roots -- it is a **machine-wide** credential rewrite, and it
takes 60-100 seconds when it fires.  It is a no-op when
``PDT_DISABLE_TEMP_SWEEP`` is set, and it is read from the environment
of the process that boots the application.

A declared service is started by ``start_service()`` as a **child
process**, and the child inherits the parent's environment unless the
parent says otherwise.  The parent (``PDT``'s backend) is started by the
supervisor and does not set the key, so a service child booted with the
inherited environment ran the full sweep: it spent 60-100 seconds
walking the machine's temp roots before it ever answered a readiness
probe, which is longer than the 60-second request-guard self-check --
so the verification round was killed and reported
``service_preflight_failed``.

The fix belongs at the spawn site, not in the sweep: the service is a
one-shot fixture for this round, and the **main server** does not come
through this path (the supervisor starts it), so it keeps sweeping when
the variable is unset.

The rule this pins
------------------
The child's environment **always** carries ``PDT_DISABLE_TEMP_SWEEP=1``
-- whatever the parent holds (``"0"``, ``"1"``, or nothing at all).
The parent's value is deliberately **not** passed through: the parent
here is the backend, and the backend's opinion about *its own* sweep is
not the child's business.  (The other spawn sites -- the test-suite
``_child_env`` helpers -- do pass the parent's value through, and
``tests/unit/test_child_env_temp_sweep_passthrough.py`` pins that; the
two rules are about different spawners and do not contradict each
other.)

Run them with:

    cd backend && .venv/bin/python3 -m pytest \
        tests/unit/test_service_spawn_disables_temp_sweep.py
"""

from __future__ import annotations

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

#: The key ``utils.secret_sweep`` reads (``secret_sweep.DISABLE_ENV``).
#: A non-empty value makes ``sweep_default_roots`` a no-op.
SWEEP_KEY = "PDT_DISABLE_TEMP_SWEEP"


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

    The fixture also gives every test a clean warning state, so a
    pin never depends on the order the tests ran in.
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
    # Nothing is listening on the port, so there is no data plane to
    # take; the readiness verdict is not what these pins are about.
    monkeypatch.setattr(sm, "_take_data_plane", lambda port: None)
    # ``raising=False``: the warning state is an implementation detail,
    # and a pin that cannot be set up is a pin that says nothing.
    monkeypatch.setattr(sm, "_SPAWN_MARKER_WARNED", set(), raising=False)

    def _start(**kwargs) -> Dict[str, Any]:
        decl = kwargs.pop("decl", None) or _decl()
        sm.start_service(decl, project_dir, **kwargs)
        assert captured.get("started"), "the service was never started"
        return captured

    return _start


def test_service_child_env_disables_sweep_when_parent_zero(
    start, monkeypatch,
):
    """A parent that says "do sweep" does not get its way.

    ``"0"`` is the value that *asks* for the machine-wide credential
    rewrite.  For the backend that value is legitimate -- the main
    server's boot sweeps the temp roots on purpose.  For a **service
    child** it is not: the child is a one-shot fixture for this round,
    and the sweep it would run takes longer than the request guard's
    self-check window, so the round is killed before the service is
    ever used.
    """
    monkeypatch.setenv(SWEEP_KEY, "0")

    env = start()["env"]

    assert env[SWEEP_KEY] == "1", (
        "the service child inherited {0}={1!r}; the sweep would run inside "
        "the child and burn the round's readiness budget".format(
            SWEEP_KEY, env.get(SWEEP_KEY)
        )
    )


def test_service_child_env_disables_sweep_when_parent_one(
    start, monkeypatch,
):
    """A parent that already says "do not sweep" is not changed.

    The value the child ends up with is the same one the parent had --
    but the child gets it because the spawn site writes it, not because
    it happened to inherit it.
    """
    monkeypatch.setenv(SWEEP_KEY, "1")

    env = start()["env"]

    assert env[SWEEP_KEY] == "1"


def test_service_child_env_disables_sweep_when_parent_unset(
    start, monkeypatch,
):
    """A parent with no opinion still gets a child that does not sweep.

    This is the case that actually bites: the backend is started by the
    supervisor, which sets no sweep switch, so the parent environment
    carries no key at all -- and the child used to inherit exactly
    that, and swept.
    """
    monkeypatch.delenv(SWEEP_KEY, raising=False)

    env = start()["env"]

    assert env[SWEEP_KEY] == "1", (
        "the service child inherited no {0} at all; the machine-wide "
        "credential sweep would run inside the child".format(SWEEP_KEY)
    )
