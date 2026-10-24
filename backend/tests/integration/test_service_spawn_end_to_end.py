"""End-to-end acceptance for ``ensure_services`` under a backend-like parent.

Why this suite exists
---------------------
The unit tests around :mod:`service_manager` pin the *shape* of a spawn
-- that the child is detached, that it does not inherit the launcher's
descriptor variables, that a failure says which process died.  The
integration suite next door pins that the *reproduction* no longer
reproduces.  Neither of them answers the question the round actually
asks: **does ``ensure_services`` bring a declared service up, end to
end, under the conditions production runs it?**

This suite walks the whole chain, in one pass, with nothing stubbed:

    a threaded parent  ->  ``ensure_services``  ->  a spawned process
    ->  readiness  ->  a real HTTP request  ->  the ledger
    ->  reclamation (the port comes back)

Every step is a step the verification round takes, so a break anywhere
between "the plan declared a service" and "the VP could talk to it"
shows up here as a red test rather than as a blocked VP in a round
nobody re-reads.

What "under a backend-like parent" means
-----------------------------------------
The parent here is threaded: eight threads are alive and sleeping while
the service is started.  That is not decoration.  The defect this work
is about was a spawn that died before it was ready, and the
measurements that found it were taken under a parent with threads in
it -- the backend is a threaded server, and a spawn that only works
when the parent is idle is not a spawn that works.  The threads here do
no work; what is reproduced is the *shape* of the parent.

The service under test is the one-route service the reproducer also
starts (``tests/manual/tiny_service.py``): one route, no state, and a
port it binds.  Readiness therefore means something a reader can check
by hand -- the port answers ``GET /health`` with 200 -- instead of
"a process exists somewhere".

What this suite must not do
---------------------------
* **It must not touch a real plan.**  Every service is declared against
  a scratch directory under ``tmp_path``, every log goes there, and the
  ledger is written there.  Nothing lands in ``.pdt/`` and nothing
  reads a plan that belongs to a real round.
* **It must not leave a process behind.**  A test that starts a service
  and forgets it is a test that breaks the next test on the same port.
  Every service started here is stopped by the teardown, and one test
  asserts the reclamation half of that promise directly: after
  ``stop_service`` the port can be bound again.
* **It must not be a guard that cannot fail.**  Each assertion is on a
  value production produced -- ``ready``, ``detail``, the HTTP status
  and body, the ledger entry -- and the failure messages carry the
  production detail, so a red run says why.

The command the tests run
-------------------------
Relative paths, so they do not depend on where the checkout lives::

    cd backend || exit 1
    .venv/bin/python3 -m pytest tests/integration/test_service_spawn_end_to_end.py -v
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import requests

import service_manager as sm
from service_declaration import ServiceDeclaration

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The backend directory, resolved from this file rather than written
#: down.  A path into one machine's home directory is a shape this
#: repository does not carry, and the checkout may live anywhere.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The one-route service, relative to the backend directory.  Relative
#: because the service is started with the backend directory as its
#: working directory -- see :func:`_declaration`.
_TINY_SERVICE = "tests/manual/tiny_service.py"

#: The interpreter, relative to the backend directory.  ``backend/.venv``
#: is a symlink to the checkout's venv, so this resolves to the same
#: interpreter the tests themselves run under, wherever the checkout is.
_VENV_PYTHON = ".venv/bin/python3"

#: The name the declaration carries.  It is the key the runtime map is
#: read by, and the name the ledger records.
_SERVICE_NAME = "api"

#: How many rounds the end-to-end test runs.  Each round gets its own
#: port: a port that was already used cannot tell "the second spawn
#: worked" from "the first spawn was adopted".
_ROUNDS = 3

#: How long the service gets to come up.  A service that is going to
#: die dies at once, so a large budget costs time only in the case that
#: is already red.
_READY_TIMEOUT_SECONDS = 15

#: How many times the HTTP request is retried, and how long the test
#: waits between attempts.  The service is ready by the time
#: ``ensure_services`` returns, so the first attempt normally answers;
#: the retries are there for the moment where it does not, because a
#: request that raced the accept queue must not decide the verdict.
_HTTP_ATTEMPTS = 5
_HTTP_RETRY_SECONDS = 0.5

#: How many threads the parent runs while the service is started, and
#: how long they live.
_PARENT_THREADS = 8
_PARENT_THREAD_SECONDS = 3

#: How long the service that cannot come up is given, and how long it
#: stays alive before it exits.  It has to outlive the spawn by enough
#: to be seen as a process -- the group a service runs in is recorded
#: while it is alive -- and it has to be gone before the readiness
#: budget runs out, so the round learns it did not come up.
_FAILING_READY_TIMEOUT_SECONDS = 2
_FAILING_LIFETIME_SECONDS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _declaration(
    port: int,
    *,
    start_cmd: Optional[str] = None,
    ready_timeout_seconds: int = _READY_TIMEOUT_SECONDS,
    log_suffix: str = "",
) -> ServiceDeclaration:
    """The declaration one round is started from.

    The working directory is the backend directory, because the command
    is relative to it: ``.venv/bin/python3`` and
    ``tests/manual/tiny_service.py`` are both paths inside the
    backend, and a command that only resolves from one directory is a
    command that stops working the moment the directory moves.

    The log path is relative too, and lands inside the scratch project
    directory -- a test that writes its logs into the checkout is a
    test whose litter shows up in the next diff.
    """
    return ServiceDeclaration(
        name=_SERVICE_NAME,
        port=port,
        start_cmd=start_cmd or (
            f"{_VENV_PYTHON} {_TINY_SERVICE} --port {port}"
        ),
        cwd=str(_BACKEND_DIR),
        ready_timeout_seconds=ready_timeout_seconds,
        log_path=f"tmp/e2e_{_SERVICE_NAME}_{port}{log_suffix}.log",
    )


def _failing_command() -> str:
    """A command that starts, lingers, and then exits without serving.

    It is the shape of a service that dies on the way up.  The linger
    is what makes the diagnostic deterministic: the process has to
    still be there when the spawn records the group it runs in, so
    that the detail an operator reads names a real process group rather
    than a placeholder.
    """
    return (
        f'{_VENV_PYTHON} -c "import sys, time; '
        f'time.sleep({_FAILING_LIFETIME_SECONDS}); sys.exit(3)"'
    )


def _start_parent_threads() -> List[threading.Thread]:
    """Start the threads the backend would have running.

    The threads do nothing but stay alive, and that is the whole point:
    what is under test is the shape of the parent, not its work.  The
    defect this work is about was measured under a threaded parent, and
    a spawn that only survives an idle parent is not a spawn that
    survives the backend.
    """
    threads = [
        threading.Thread(
            target=time.sleep, args=(_PARENT_THREAD_SECONDS,), daemon=True,
        )
        for _ in range(_PARENT_THREADS)
    ]
    for thread in threads:
        thread.start()
    return threads


def _body_reports_ok(body: Dict[str, Any]) -> bool:
    """Whether *body* says the service is up.

    The repository spells that fact two ways: ``{"ok": true}`` and
    ``{"status": "ok"}`` -- the latter is what the backend's own
    ``/health`` answers with, and what the one-route service under test
    answers with.  The assertion is on the fact rather than on the
    spelling, and it is strict about the fact: ``ok`` has to be exactly
    ``True`` and ``status`` has to be exactly ``"ok"``.
    """
    if body.get("ok") is True:
        return True
    return body.get("status") == "ok"


def _health(port: int) -> Tuple[int, Dict[str, Any]]:
    """``GET /health`` on *port*, retried while it does not answer.

    Returns ``(status, body)``.  Raises ``AssertionError`` when no
    attempt produced a JSON object, with the last failure attached --
    a verdict without the reason is not a verdict a reader can act on.
    """
    url = f"http://127.0.0.1:{port}/health"
    last_error = "(no attempt was made)"
    for _ in range(_HTTP_ATTEMPTS):
        try:
            response = requests.get(url, timeout=5)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            try:
                body = response.json()
            except ValueError as exc:
                last_error = (
                    f"body was not JSON: {exc} ({response.text[:200]!r})"
                )
            else:
                if isinstance(body, dict):
                    return int(response.status_code), body
                last_error = f"body was not a JSON object: {body!r}"
        time.sleep(_HTTP_RETRY_SECONDS)
    raise AssertionError(
        f"GET {url} never answered with a JSON object after "
        f"{_HTTP_ATTEMPTS} attempts: {last_error}"
    )


def _can_bind(port: int) -> Tuple[bool, str]:
    """Whether *port* can be bound right now, and why not if it cannot.

    A plain bind, without ``SO_REUSEADDR``: the question being asked is
    "is this port free", and a socket that may reuse a port another
    socket still holds would answer "yes" to a question it was not
    asked.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _start_round(
    project_dir: Path,
    started: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]],
    *,
    port: Optional[int] = None,
    start_cmd: Optional[str] = None,
    ready_timeout_seconds: int = _READY_TIMEOUT_SECONDS,
    log_suffix: str = "",
) -> Tuple[sm.ServiceRuntimeMap, sm.ServiceRuntime, ServiceDeclaration]:
    """Start one declared service through ``ensure_services``.

    The runtime is appended to *started* so that the teardown stops it
    whether or not the test got far enough to stop it itself.
    """
    if port is None:
        port = sm.allocate_free_port()
    assert port is not None, "could not allocate a free port"
    declaration = _declaration(
        port,
        start_cmd=start_cmd,
        ready_timeout_seconds=ready_timeout_seconds,
        log_suffix=log_suffix,
    )
    runtimes = sm.ensure_services(project_dir, {_SERVICE_NAME: declaration})
    runtime = runtimes.runtimes[_SERVICE_NAME]
    started.append((runtime, declaration))
    return runtimes, runtime, declaration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A scratch project directory for one test.

    Everything the round writes -- the service log, the ledger -- lands
    here, so the test touches neither ``.pdt/`` nor a real plan.
    """
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def started_services() -> Iterator[List[Tuple[sm.ServiceRuntime, ServiceDeclaration]]]:
    """Stop every service the test started, whatever happened.

    A service left running keeps its port, and the next test that
    asks for a port then fails for a reason that has nothing to do with
    what it is testing.
    """
    started: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]] = []
    yield started
    for runtime, declaration in started:
        sm.stop_service(runtime, declaration)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_ensure_services_ready_for_three_rounds(
    project_dir: Path,
    started_services: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]],
) -> None:
    """Three rounds, three ports, three ready services.

    "Ready" is the runtime's own judgement, and it is the judgement
    the round acts on: a service that is not ready blocks every VP that
    depends on it.  A round that fails here is a round whose VPs were
    blocked for a reason the operator can read off the detail.
    """
    failures: List[str] = []
    for round_number in range(1, _ROUNDS + 1):
        threads = _start_parent_threads()
        port = sm.allocate_free_port()
        assert port is not None, "could not allocate a free port"
        declaration = _declaration(port)
        runtimes = sm.ensure_services(
            project_dir, {_SERVICE_NAME: declaration},
        )
        runtime = runtimes.runtimes[_SERVICE_NAME]
        started_services.append((runtime, declaration))
        if not runtime.ready:
            failures.append(
                f"round {round_number} (port {port}): {runtime.detail}"
            )
        else:
            assert runtime.origin == "spawned", (
                f"round {round_number}: origin was {runtime.origin!r}, "
                f"expected 'spawned'; detail: {runtime.detail}"
            )
        sm.stop_service(runtime, declaration)
        for thread in threads:
            thread.join(timeout=1.0)

    assert not failures, (
        f"{len(failures)} of {_ROUNDS} rounds did not come up:\n  "
        + "\n  ".join(failures)
    )


def test_real_http_request_succeeds(
    project_dir: Path,
    started_services: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]],
) -> None:
    """A real HTTP request against the service the round started.

    "The port accepts a connection" is not "the service answers": a
    port held by something else accepts connections too, and the round
    would then point a VP at a stranger.  So the request is a request,
    and the body has to say the service is up.
    """
    _runtimes, runtime, _declaration_ = _start_round(
        project_dir, started_services,
    )
    status, body = _health(runtime.port)
    assert status == 200, (
        f"GET /health answered {status}, expected 200; body: {body!r}"
    )
    assert _body_reports_ok(body), (
        f"GET /health answered 200 but the body does not report ok: "
        f"{body!r}"
    )


def test_detail_carries_pid_and_pgid(
    project_dir: Path,
    started_services: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]],
) -> None:
    """The detail names the process, and the group it belongs to.

    Two runtimes, because production writes the numbers in two places.
    A service that came up names the pid it started; a service that did
    not come up names the pid, the process group it ran in, and the
    descriptors it held when it died.

    Both numbers are what an operator needs to look at the service, and
    the second one is what decides how it may be signalled: the group
    is what a stop signals, so a detail without it describes a process
    that cannot be reclaimed as a unit.
    """
    _runtimes, ready_runtime, _declaration_ = _start_round(
        project_dir, started_services,
    )
    assert "pid " in ready_runtime.detail, (
        f"the detail of a started service does not name the pid: "
        f"{ready_runtime.detail!r}"
    )

    _runtimes, failed_runtime, _declaration_ = _start_round(
        project_dir,
        started_services,
        start_cmd=_failing_command(),
        ready_timeout_seconds=_FAILING_READY_TIMEOUT_SECONDS,
        log_suffix="_failing",
    )
    assert not failed_runtime.ready, (
        f"the service that exits at once was reported ready: "
        f"{failed_runtime.detail!r}"
    )
    assert "pid " in failed_runtime.detail, (
        f"the detail does not name the pid: {failed_runtime.detail!r}"
    )
    assert "pgid " in failed_runtime.detail, (
        f"the detail does not name the process group: "
        f"{failed_runtime.detail!r}"
    )


def test_ledger_records_spawned_service(
    project_dir: Path,
    started_services: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]],
) -> None:
    """The ledger names the service, its port and its pid.

    The ledger is what the exit-time reap works from: a service that
    is not in it is a service nobody will ever stop.  So the entry has
    to carry the name, the port and the pid of what was actually
    started.
    """
    runtimes, runtime, _declaration_ = _start_round(
        project_dir, started_services,
    )
    ledger_file = sm.ledger_path(project_dir)
    sm.record_runtimes(
        project_dir, "e2e-acceptance", runtimes, project_dir=project_dir,
    )
    assert ledger_file.is_file(), f"no ledger was written at {ledger_file}"
    ledger = sm.read_ledger(project_dir)
    entries = [
        entry for entry in ledger.get("services", [])
        if entry.get("name") == _SERVICE_NAME
    ]
    assert entries, (
        f"the ledger has no entry for {_SERVICE_NAME!r}: {ledger!r}"
    )
    entry = entries[0]
    assert entry.get("port") == runtime.port, (
        f"the ledger records port {entry.get('port')!r}, expected "
        f"{runtime.port!r}"
    )
    assert entry.get("pid") == runtime.pid, (
        f"the ledger records pid {entry.get('pid')!r}, expected "
        f"{runtime.pid!r}"
    )


def test_port_released_after_stop(
    project_dir: Path,
    started_services: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]],
) -> None:
    """After the service is stopped, its port can be bound again.

    This is the reclamation half of the chain: a service that is
    stopped but keeps its port breaks the next service on that port,
    and it does so silently -- the next start fails with
    ``EADDRINUSE`` and nobody connects the two events.
    """
    _runtimes, runtime, declaration = _start_round(
        project_dir, started_services,
    )
    port = runtime.port
    stop_info = sm.stop_service(runtime, declaration)
    assert stop_info.get("port_free") is True, (
        f"stop_service did not report the port free: {stop_info!r}"
    )
    bound, reason = _can_bind(port)
    assert bound, f"port {port} could not be bound after the stop: {reason}"