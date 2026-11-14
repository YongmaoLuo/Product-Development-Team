"""The service child's environment always disables the machine-wide sweep.

What is under test
------------------
The environment :func:`service_manager.start_service` hands to the child
process it starts -- and specifically one key in it: the switch
``utils.secret_sweep.DISABLE_ENV`` (``PDT_DISABLE_TEMP_SWEEP``).

Why the key has to be pinned
----------------------------
``utils.secret_sweep.sweep_default_roots()`` rewrites the machine's real
temp roots -- a **machine-wide** credential rewrite that takes 60-100
seconds when it fires.  It is a no-op when the switch holds any
non-empty value, and it is read from the environment of the process that
boots the application.

A declared service is started by ``start_service()`` as a **child
process**, and the child inherits the parent's environment unless the
parent says otherwise.  The parent (the backend) is started by the
supervisor and does not set the switch, so a service child booted with
the inherited environment ran the full sweep: 60-100 seconds walking the
machine's temp roots before it ever answered a readiness probe -- longer
than the 60-second request-guard self-check, so the verification round
was killed and reported ``service_preflight_failed``.

The fix belongs at the spawn site, not in the sweep: a declared service
is a one-shot fixture for the round, and the **main server** does not
come through this path (the supervisor starts it), so it keeps sweeping
when the switch is unset.

The rule these pins hold
------------------------
The child's environment **always** carries ``PDT_DISABLE_TEMP_SWEEP=1``
-- whatever the parent holds (``"0"``, ``"1"``, or nothing at all).
The parent's value is deliberately **not** passed through: the parent
here is the backend, and the backend's opinion about *its own* sweep is
not the child's business.  ``"0"`` asks for the machine-wide rewrite and
counts as "disabled" for the parent, but for the child it must not be
inherited verbatim.

None of these tests starts a process: ``subprocess.Popen`` is replaced,
so what is asserted is the environment the child *would* have been
given.

Run them with:

    cd backend && .venv/bin/python3 -m pytest \
        tests/unit/test_service_manager_temp_sweep_env.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from utils.secret_sweep import DISABLE_ENV  # noqa: E402


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
def project(tmp_path: Path) -> Path:
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _is_the_service_spawn(argv) -> bool:
    """Is this argv the spawn of the declared service?

    ``subprocess.Popen`` is what *every* subprocess in this process goes
    through -- including the ``ps`` probe that writing the CI gate's
    spawn registry costs.  A recorder that read "the call it caught"
    would read the probe instead of the spawn, and the probe hands on
    the *parent's* environment -- which is exactly what these pins are
    about.  So the spawn is told apart by its shape: the detached
    wrapper runs under this interpreter and is a program of its own --
    that is the shape ``service_manager._detached_argv`` builds, and it
    is the shape no other subprocess in this process has.
    """
    argv = list(argv or [])
    return (
        len(argv) > 2
        and argv[0] == (sys.executable or "/usr/bin/python3")
        and argv[1] == "-c"
        and argv[2] == sm._DETACHED_WRAPPER_SOURCE
    )


@pytest.fixture
def spawn(monkeypatch, project):
    """Start a declared service and hand back what it was given.

    ``Popen`` is replaced, so nothing is really started -- but the
    replacement captures the ``env`` the child would have inherited,
    which is the only thing these pins are about.

    The capture is of the spawn that carries the **declared command**.
    The spawn path is allowed to start other processes of its own, and
    a recorder that took "the last one" would hand back the
    environment of one of those -- which is how a whole family of
    these pins once read the environment of a ``ps`` census instead of
    the environment of the service.

    The replacement takes the **service's** spawn and nothing else.
    The start path spawns subprocesses of its own besides the service:
    the ``ps`` probes that read a pid's start time -- the spawn marker
    before the spawn, the registry entry after it -- and the machine's
    process table.  A capture that takes those as well leaves the pins
    below reading a probe instead of the service, and the probe carries
    the *inherited* environment, not the one the spawn site wrote.
    The probes are the production's business, so they are handed to the
    real ``Popen``.

    Every call the replacement caught is kept as well, and the
    service's own spawn is picked out of them by its shape.
    """
    captured = {}
    service_spawns = []
    calls = []
    real_popen = sm.subprocess.Popen

    def _fake_popen(argv, **kwargs):
        # Every call the replacement caught is kept -- the probe included
        # -- because a recorder that threw the others away would be
        # hiding them instead of telling them apart.
        calls.append((list(argv), dict(kwargs)))
        if not _is_the_service_spawn(argv):
            # Not the service: a probe of the production's own.  It is
            # handed to the real ``Popen`` so that the production sees
            # what it would see.
            return real_popen(argv, **kwargs)
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        captured["env"] = dict(kwargs.get("env") or {})
        service_spawns.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(sm.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    # The child never existed, so there is no process group to wait for
    # and no system clock to ask about.
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    # Nothing is listening, so the child cannot have exited; without
    # this the readiness check would spin for its full two-second
    # timeout on every test.
    monkeypatch.setattr(sm, "_await_exit", lambda proc, timeout=2.0: (False, None))

    def _start(**kwargs):
        decl = _decl()
        runtime = sm.start_service(decl, project, **kwargs)
        # The service's spawn is picked out of every call the recorder
        # caught.  The start path's own probes are calls too, and a probe
        # is not the service: the spawn under test is the one that
        # carries the declared command, and it is also the one whose
        # shape says it is the service's own.
        service = [
            call for call in calls
            if call[0] and call[0][-1] == decl.start_cmd
        ]
        assert len(service) == 1, (
            f"the declared command reached {len(service)} spawns, not "
            f"one: {[c[0][-1] for c in calls]!r}"
        )
        spawn_kwargs = service[0][1]
        assert captured.get("env") is not None, "the service was never started"
        assert dict(spawn_kwargs.get("env") or {}) == captured["env"], (
            "the capture does not hold the environment the service's own "
            "spawn was given"
        )
        # Exactly one spawn is the service's own.  A start that spawned
        # the service twice, or none at all, would leave the pins below
        # reading something that is not the service.
        assert len(service_spawns) == 1, (
            "the capture took %d service spawns; the start path spawns "
            "the service exactly once" % len(service_spawns)
        )
        return runtime, captured

    return _start


def test_child_env_forces_temp_sweep_off_when_parent_is_zero(spawn, monkeypatch):
    """A parent that says "do sweep" does not get its way.

    ``"0"`` is the value that *asks* for the machine-wide credential
    rewrite.  For the backend that value is legitimate -- the main
    server's boot sweeps the temp roots on purpose.  For a **service
    child** it is not: the child is a one-shot fixture for the round,
    and the sweep it would run takes longer than the request guard's
    self-check window, so the round is killed before the service is ever
    used.
    """
    monkeypatch.setenv(DISABLE_ENV, "0")

    runtime, captured = spawn()

    assert captured["env"][DISABLE_ENV] == "1", (
        "the service child inherited {0}={1!r}; the sweep would run inside "
        "the child and burn the round's readiness budget".format(
            DISABLE_ENV, captured["env"].get(DISABLE_ENV)
        )
    )
    assert runtime.ready is True


def test_child_env_forces_temp_sweep_off_when_parent_is_one(spawn, monkeypatch):
    """A parent that already says "do not sweep" is not changed.

    The value the child ends up with is the same one the parent had --
    but the child gets it because the spawn site writes it, not because
    it happened to inherit it.
    """
    monkeypatch.setenv(DISABLE_ENV, "1")

    runtime, captured = spawn()

    assert captured["env"][DISABLE_ENV] == "1"
    assert runtime.ready is True


def test_child_env_forces_temp_sweep_off_when_parent_unset(spawn, monkeypatch):
    """A parent with no opinion still gets a child that does not sweep.

    This is the case that actually bites: the backend is started by the
    supervisor, which sets no sweep switch, so the parent environment
    carries no key at all -- and the child used to inherit exactly
    that, and swept.
    """
    monkeypatch.delenv(DISABLE_ENV, raising=False)

    runtime, captured = spawn()

    assert captured["env"][DISABLE_ENV] == "1", (
        "the service child inherited no {0} at all; the machine-wide "
        "credential sweep would run inside the child".format(DISABLE_ENV)
    )
    assert runtime.ready is True
