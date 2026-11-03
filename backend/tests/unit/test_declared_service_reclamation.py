"""Regression: a declared service a case starts has to be given back.

Why this file exists
--------------------
``tests/unit/test_verification_service_freshness.py`` starts a **real**
``backend.server`` -- through the preflight, the way the round starts a
declared service -- and used to leave it running: the case's own
docstring said the service "is left running".

A case that starts a service has to stop it.  The rules this file pins
are the ones the repository holds every other test to:

  * a test reclaims every resource it started, **before it returns**;
  * the reclaim is identified by the **pid the preflight handed back**,
    not by the declaration's command line -- the declared command of the
    pdt service is ``exec python -m backend.server``, which names no
    script for a token match to find, so the leftover sweep cannot see
    it and the service outlives the suite;
  * a reclaim that finds nothing left to stop is not an error: the
    second reclaim of an already-released service is a no-op, not a
    failure;
  * a service the preflight **adopted** -- a live, fresh, matching
    listener that was already there -- is not this file's to stop: its
    pid belongs to a server nobody here started.

The case under test is the one in the freshness module, and it is
**called directly** here -- with the declaration pinned -- so what runs
is the real case and not a copy of it.

The module the case lives in is imported under the name pytest gives it
(``backend.tests.unit.test_verification_service_freshness``), so this
file and the freshness module share **one** module object: a second
copy would be a second set of module-level names, and pinning one
would not pin the other.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from backend.tests.unit import (  # noqa: E402
    test_verification_service_freshness as target,
)


#: The tiny service the reclaim case starts: a real process on a real
#: port, but a small one.  The point of that case is the *reclaim*,
#: not the service behind it -- and a small service keeps the case
#: about what it is about.
_TINY_SERVICE = _BACKEND_DIR / "tests" / "manual" / "tiny_service.py"


def _pdt_declaration():
    """The pdt declaration, mirrored around a free port.

    The same declaration the target case uses when the plan under test
    is not on this machine.  A written-down port is a port another
    checkout may already hold, and the case would then fail for a
    reason that has nothing to do with what it is about.
    """
    from service_declaration import parse_declarations

    mirror = target._pdt_service_mirror()
    return parse_declarations(
        {"services": [mirror]}, target._REPO_ROOT,
    ).by_name()["pdt"]


def _tiny_declaration():
    """A declaration for the tiny service, on a free port."""
    from service_declaration import ServiceDeclaration

    import service_manager as sm

    port = sm.allocate_free_port()
    assert port is not None, "no free port for the reclaim case"
    return ServiceDeclaration(
        name="reclaim-probe",
        port=port,
        start_cmd=(
            f"{shlex.quote(sys.executable)} "
            f"{shlex.quote(str(_TINY_SERVICE))} --port {port}"
        ),
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=30,
    )


@pytest.mark.integration
def test_target_case_reclaims_its_service(tmp_path: Path, monkeypatch):
    """The target case must give the service back before it returns.

    The case is called directly, with the declaration pinned to one
    this file owns, so the case that runs is the real one: the real
    preflight, the real spawn, the real assertions.

    The port is the witness.  It was held by the service the case
    started, and the case is not allowed to return while it still is.
    """
    import service_manager as sm

    declaration = _pdt_declaration()
    monkeypatch.setattr(
        target, "_pdt_declaration", lambda project_dir: declaration,
    )

    target.test_declared_pdt_service_ready_in_harness_runtime(tmp_path)

    probe = sm.probe_port(declaration.port)
    assert probe.listening is False, (
        f"the case returned with port {declaration.port} still held by "
        f"{probe.pids}; a case that starts a service has to stop it"
    )


@pytest.mark.integration
def test_an_adopted_service_is_not_stopped():
    """A service this round did not start is not this round's to stop.

    The preflight adopts a live, fresh, matching listener instead of
    starting one -- that is how the round avoids taking down a service
    somebody else is serving.  The pid in an adopted runtime is *that*
    service's pid, so a reclaim that stops what the pid names stops a
    server this case never started.

    The process here is a real one, and the witness is that it is still
    running when the reclaim returns.
    """
    import service_manager as sm

    declaration = _tiny_declaration()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        runtime = sm.ServiceRuntime(
            name=declaration.name,
            port=declaration.port,
            declared_port=declaration.port,
            pid=sleeper.pid,
            origin="adopted",
            ready=True,
        )

        result = target._reclaim_service(
            runtime, declaration,
            caller="test_an_adopted_service_is_not_stopped",
        )

        assert result is None, (
            f"the reclaim acted on an adopted service: {result}"
        )
        assert sleeper.poll() is None, (
            "the reclaim stopped a service the round had adopted -- "
            "that service was not this round's to stop"
        )
    finally:
        sleeper.terminate()
        try:
            sleeper.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - cleanup only
            sleeper.kill()


@pytest.mark.integration
def test_reclaim_is_idempotent():
    """Reclaiming a service that is already gone must not raise.

    A reclaim runs in a ``finally``, so it runs whether the case passed
    or failed -- and it can run twice over the same service: once by
    the case, once by the sweep that finds what the case left behind.
    The second reclaim has nothing to stop, and that is not an error:
    it is the shape every other cleanup in this repository has.
    """
    import service_manager as sm

    declaration = _tiny_declaration()
    runtime = sm.start_service(declaration, _BACKEND_DIR)
    assert runtime.ready is True, runtime.detail

    first = target._reclaim_service(
        runtime, declaration, caller="test_reclaim_is_idempotent",
    )
    assert first is not None
    assert first["port_free"] is True, first

    # The port is free now, and the service is gone.  Reclaiming it a
    # second time must find nothing to do and must not raise.
    second = target._reclaim_service(
        runtime, declaration, caller="test_reclaim_is_idempotent",
    )
    assert second is not None
    assert second["port_free"] is True, second

    assert sm.probe_port(declaration.port).listening is False


def test_freshness_module_regression():
    """The two files belong to one pytest session, and both collect.

    The reclaim is only worth anything if the case it protects still
    runs beside it: two files that cannot be collected into one
    session are two files that never run together, and the pair the
    suite ships is the pair the command has to be able to run.

    What this proves is the *pairing* -- both modules collect into one
    session, and the cases the contract names are in it.  That both
    files are **green** in one run is what the suite's own command
    does (``pytest tests/unit/test_declared_service_reclamation.py
    tests/unit/test_verification_service_freshness.py``); a test cannot
    assert on the session it is running in, so it asserts on the part
    of the claim it *can* reach.
    """
    freshness = (
        _BACKEND_DIR / "tests" / "unit" / "test_verification_service_freshness.py"
    )
    reclamation = Path(__file__).resolve()

    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            str(freshness), str(reclamation),
            "--collect-only", "-q", "-p", "no:cacheprovider",
        ],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, (
        f"the two modules do not collect into one session:\n"
        f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
    )
    collected = completed.stdout
    for node_id in (
        "test_target_case_reclaims_its_service",
        "test_reclaim_is_idempotent",
        "test_freshness_module_regression",
        "test_declared_pdt_service_ready_in_harness_runtime",
    ):
        assert node_id in collected, (
            f"{node_id} is not in the collected set:\n{collected[-4000:]}"
        )
