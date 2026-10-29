"""The service-spawn reproduction, frozen into an integration test.

Why this suite exists
---------------------
The defect this guards is a spawn that dies before it is ready: the
service inherits an environment variable that names a descriptor, closes
a descriptor number that means something else in the child, and takes the
event loop's own selector down with it (``Bad file descriptor``).  The
root cause, the measurements and the shape of the fix are written down in
``docs/development/service-spawn-root-cause.md``.

``tests/manual/spawn_repro.py`` is the tool that found the defect and the
tool that measures it.  A tool nobody runs is not a guard.  So the
reduced version of the reproduction -- three spawns instead of ten -- is
an integration test here: the production spawn shape is exercised on
every run, and a regression in it is a red build rather than a page of
notes nobody rereads.

Which service the test starts, and why it is that one
------------------------------------------------------
The reproduction needs a service that *reads* the descriptor variables:
that read is what closes the number, and the number belongs to the event
loop's selector in the child.  The service that reads them is the
backend itself, so the batches below start the backend.

The one-route service the reproducer also knows how to start is not
enough: it never looks at those variables, so it cannot die of them, and
a test built on it stays green with the fix and stays green without it.
That is the "always green" guard the materials forbid, so the port
property is measured on the cheap service, and the reproduction itself is
measured on the backend.

What the test needs from the environment
-----------------------------------------
The collision needs the launcher's handoff: a ``PDT_SECRET_FD_*``
variable naming a descriptor number.  The launcher publishes the secret
on descriptor 3, and the child -- which starts with its inherited
descriptors closed -- gives descriptor 3 to its own event loop selector.
So the same number means "a pipe" in the parent and "the selector" in the
child, and the child closes it.

Where the environment does not carry that handoff the defect cannot
occur at all, and the batch is green because nothing is broken there --
not because the test is blind to it.  The launcher publishes it, so a run
started by the launcher reproduces; a plain shell without it does not.

The shape under test is the production one, reached through the
reproducer's ``service-manager`` profile: the declaration is handed to
``service_manager.start_service``, and the child is started by the same
wrapper production starts it with.  Nothing in this file assembles a
spawn of its own, so a change to the production shape cannot be missed
here by having been re-implemented around.

The equivalent command lines, for a reader who wants to run them by hand
(relative paths, so they do not depend on where the checkout lives)::

    cd backend || exit 1
    .venv/bin/python3 tests/manual/spawn_repro.py --profile service-manager \
        --target pdt --runs 3 --ready-timeout 90 --assert-failure-rate 0
    .venv/bin/python3 tests/manual/spawn_repro.py --profile service-manager \
        --target tiny --runs 3 --ready-timeout 8 --assert-failure-rate 0

Three properties are pinned
---------------------------
* **Three consecutive spawns all come up.**  "Ready" is not "the port
  answers": the reproducer's readiness judgement is the port answering
  *and* the child still being alive, which is the distinction that lets
  a dead service be caught.
* **The ports are free afterwards.**  A spawn that leaves a listener
  behind breaks the next service on that port, and it does so silently:
  the next start fails with ``EADDRINUSE`` and nobody connects the two
  events.
* **The skip is honest.**  Off macOS the reproduction does not happen, so
  the test is skipped *with a reason that says so and points at the
  measurements* -- rather than left running as a guard that can never
  fail and therefore protects nothing.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from credentials import _FD_ENV_PREFIX as _DESCRIPTOR_ENV_PREFIX
from tests.manual import spawn_repro

pytestmark = pytest.mark.integration


#: How many spawns one batch makes.  The materials ask for the reduced
#: version of the reproduction: three, not ten.
_RUNS = 3

#: How many batches one test runs.  A batch reuses one port for all of
#: its spawns, so three ports means three batches.
_BATCHES = 3

#: The machine name of the platform the reproduction depends on.
_SPAWN_REPRO_PLATFORM = "darwin"

#: The same platform as the skip reason spells it.
_PLATFORM_NAME = "macOS"

#: Why the spawning tests are skipped off macOS.
#:
#: The reproduction is a macOS failure: the child closes a descriptor
#: number that the event loop's kqueue selector is holding, and a
#: selector of that kind is macOS's.  The same batch on Linux measures
#: zero failures -- there is nothing there to detect, which is why the
#: test is skipped with a reason instead of being left to run as a
#: guard that can never fail.
_SKIP_REASON = (
    "EBADF 复现依赖 macOS 的 spawn 路径；该平台 N=3 未观察到失败 "
    "—— 见 docs/development/service-spawn-root-cause.md"
)

#: The document the skip reason points at, resolved from this file.
_ROOT_CAUSE_DOC = (
    Path(__file__).resolve().parents[3]
    / "docs" / "development" / "service-spawn-root-cause.md"
)

#: How many times the port allocator is asked for a number this test has
#: not used yet, before the test gives up on distinctness.
_PORT_PICK_ATTEMPTS = 10

#: The production allocator, kept before anything patches it.
_ALLOCATE_FREE_PORT = spawn_repro.sm.allocate_free_port

#: The service that reads the descriptor variables, and therefore the
#: one that can die of them: the backend.
_REPRODUCING_TARGET = "pdt"

#: The one-route service: cheap to start, and it never reads the
#: descriptor variables -- so it measures the port and teardown
#: properties without standing in for a reproduction it cannot make.
_LIGHT_TARGET = "tiny"

#: How long the backend gets to answer.  It has a lifespan, a state
#: database and a watchdog to start, so it needs more room than the
#: one-route service; a service that is going to die dies at once, so
#: the larger budget costs time only in the case that is already red.
#:
#: The budget is a wall-clock ceiling, not a sleep: a spawn that comes
#: up in 7s is measured as 7s however large the ceiling is.  It used to
#: be 12 -- a figure taken from an idle runner -- and on a loaded host
#: that made this suite red for a reason that has nothing to do with
#: the defect it guards: the backend's time-to-ready drifts with the
#: host's load (measured here: 6.9s on a quiet run, 27.8s for the
#: first spawn of a batch on a loaded one, 40s+ under heavier load
#: still).  The budget is now 90s, which is more than twice the
#: slowest measurement and comfortably above the backend's own
#: 60s self-check ceiling (``server._start_request_guard_selfcheck``
#: stops an instance that has not begun listening by then).  Above that
#: ceiling a spawn that is going to come up always gets the chance,
#: and one that is not going to come up dies on its own -- the
#: reproducer records a non-``None`` return code, and the assertion
#: below still fails.  What this suite guards is "the child does not
#: die before it is ready" (the EBADF defect), not how fast the host
#: starts a process.
_BACKEND_READY_TIMEOUT = 90

#: How long the one-route service gets to answer.
_LIGHT_READY_TIMEOUT = 8

#: The guard for the platform the reproduction needs.  It is a marker
#: rather than a module-level ``pytestmark`` entry on purpose: the test
#: that checks the skip's reason has to run on the platforms the skip
#: excludes, or nothing would ever check it.
requires_macos_spawn = pytest.mark.skipif(
    sys.platform != _SPAWN_REPRO_PLATFORM,
    reason=_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_ports(monkeypatch: pytest.MonkeyPatch, used: List[int]) -> None:
    """Make the reproducer's port choice visible to the test.

    The reproducer picks its own port, and "the port is free again" is
    one of the things this suite asserts -- so the test has to know
    which port was used.  The production allocator still chooses it:
    this only records the answer, and asks again when the kernel hands
    out a number this test has already used, so that separate batches
    do not end up sharing one port.
    """
    def spy() -> Optional[int]:
        for _ in range(_PORT_PICK_ATTEMPTS):
            port = _ALLOCATE_FREE_PORT()
            if port is not None and port not in used:
                used.append(port)
                return port
        return _ALLOCATE_FREE_PORT()

    monkeypatch.setattr(spawn_repro.sm, "allocate_free_port", spy)


def _run_batch(
    tmp_path: Path,
    label: str,
    groups: List[Tuple[Optional[int], Optional[int]]],
    target: str,
    ready_timeout: int,
) -> Tuple[int, Dict[str, Any]]:
    """Run the reproducer's production-shape batch once.

    The batch is started through ``spawn_repro.main`` with the
    ``service-manager`` profile, so the child is started the way
    production starts it -- through ``service_manager.start_service``
    and the wrapper that does the stripping -- and this file never
    assembles a spawn of its own.

    The measurement is written under *tmp_path* rather than into the
    repository: the reproducer's default is a path below the working
    directory, and a test that litters the checkout is a test whose
    litter shows up in the next diff.

    The process groups the batch started are collected into *groups* so
    that the teardown can kill whatever the batch left behind.
    """
    json_out = tmp_path / f"{label}.json"
    rc = spawn_repro.main([
        "--profile", "service-manager",
        "--target", target,
        "--runs", str(_RUNS),
        "--ready-timeout", str(ready_timeout),
        "--assert-failure-rate", "0",
        "--json-out", str(json_out),
    ])
    measurement: Dict[str, Any] = {}
    if json_out.is_file():
        measurement = json.loads(json_out.read_text(encoding="utf-8"))
    for result in measurement.get("results", []):
        groups.append((result.get("pid"), result.get("pgid")))
    return rc, measurement


def _failure_detail(measurement: Dict[str, Any]) -> str:
    """What each run of a batch did, for an assertion message.

    The log tail is the only thing that says *why* a spawn did not come
    up, so it goes into the message instead of being left in a
    measurement file nobody reads.
    """
    lines = []
    for index, result in enumerate(measurement.get("results", []), start=1):
        lines.append(
            f"    run {index}: ready={result.get('ready')} "
            f"pid={result.get('pid')} rc={result.get('rc')} "
            f"tail={str(result.get('log_tail', ''))[:400]!r}"
        )
    return "\n".join(lines) if lines else "    (no measurement was written)"


def _can_bind(port: int) -> Tuple[bool, str]:
    """Whether *port* can be bound right now, and why not if it cannot."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def spawned_groups() -> Iterator[List[Tuple[Optional[int], Optional[int]]]]:
    """Kill the process groups the batch started, whatever happened.

    The reproducer stops each service itself; this is the net for the
    case where it did not.  A leaked process group keeps its port bound,
    and the next batch then fails for a reason that has nothing to do
    with what it is testing.
    """
    groups: List[Tuple[Optional[int], Optional[int]]] = []
    yield groups
    for pid, pgid in groups:
        spawn_repro.stop_tree(pid, pgid)


@pytest.fixture
def descriptor_hazard(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Put a descriptor variable into the environment the child inherits.

    The launcher hands the backend the descriptors it published on, and
    the environment says so: a ``PDT_SECRET_FD_*`` variable names a
    descriptor number.  That number means something in the process that
    published it and nothing in the child -- the child inherits the
    *variable* without inheriting the pipe behind it -- which is the
    whole defect.

    A spawn made without such a variable in the environment never runs
    the part of the fix that removes it -- the removal loop has nothing
    to remove and its body never executes -- and a removal that raised
    would take the wrapper, and with it the service, down without this
    suite ever noticing.  So the variable is here.

    What this does *not* do is reproduce the crash: the number it names
    is not the one the child's event loop ends up holding, so the child
    closes a descriptor of its own that was never meant to be closed.
    The collision that does reproduce it is the launcher's own handoff,
    which the reproducer's parent process inherits.
    """
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv(
            _DESCRIPTOR_ENV_PREFIX + "integration_test", str(read_fd),
        )
        yield read_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@requires_macos_spawn
# The watchdog this test needs is not the suite default.  Nine backend
# spawns at the per-spawn ceiling above is 810s of worst case, and the
# default 300s watchdog would kill the test on a loaded host -- turning
# "the host is slow" into a red suite, which is the very thing the
# budget above exists to stop.  The budget guards against a hung
# pytest; it is not one of the criteria this suite asserts.
@pytest.mark.timeout(1800)
def test_three_consecutive_spawns_all_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_hazard: int,
    spawned_groups: List[Tuple[Optional[int], Optional[int]]],
) -> None:
    """Three batches of three consecutive spawns: every one of them comes up.

    The service is the backend, because the backend is the service that
    reads the descriptor variables -- the read is what closes the
    number, and the number belongs to the child's event loop selector.
    With the fix the child starts with those variables removed, and the
    three spawns of a batch all come up; without it the child closes
    its own selector and the batch comes up empty.

    The batch is the reproducer's own, with its own zero-failure
    assertion: the exit code is zero only when not one of the three
    spawns failed to come up, and "came up" is the port answering while
    the child is still alive.

    Three batches rather than one, because a batch reuses a single port
    for all of its spawns -- three ports is three batches, and each of
    those ports has to be free again at the end.
    """
    ports: List[int] = []
    _record_ports(monkeypatch, ports)

    for index in range(_BATCHES):
        rc, measurement = _run_batch(
            tmp_path, f"batch-{index}", spawned_groups,
            _REPRODUCING_TARGET, _BACKEND_READY_TIMEOUT,
        )
        runs = measurement.get("runs")
        ready = measurement.get("ready")
        failed = measurement.get("failed")
        # The measurement itself, not only the exit code that judges it.
        # The batch is three spawns, and the shape of the result is the
        # contract: three measured, three up, none failed.  The profile
        # is checked too, because "three spawns came up" means something
        # only if they came up through the production shape.
        assert measurement.get("profile") == "service-manager", (
            f"batch {index + 1} did not run through the production "
            f"profile: {measurement.get('profile')!r}"
        )
        assert runs == _RUNS, (
            f"batch {index + 1} measured {runs!r} spawns, "
            f"expected {_RUNS}\n{_failure_detail(measurement)}"
        )
        # The failure count comes before the readiness count on purpose:
        # it is the number that names the defect, and it is the line a
        # reader of a red run looks for first.
        assert failed == 0, (
            f"batch {index + 1} of {_BATCHES}: failed={failed} of {runs} "
            f"spawns\n{_failure_detail(measurement)}"
        )
        assert ready == _RUNS, (
            f"batch {index + 1} brought up {ready!r} of {_RUNS} "
            f"services\n{_failure_detail(measurement)}"
        )
        assert rc == 0, (
            f"batch {index + 1} of {_BATCHES} did not pass the "
            f"reproducer's own zero-failure assertion "
            f"(rc={rc}, failed={failed})\n{_failure_detail(measurement)}"
        )

    assert len(ports) == _BATCHES, (
        f"the batches used {len(ports)} ports, expected {_BATCHES}: "
        f"{ports}"
    )
    for port in ports:
        bound, why = _can_bind(port)
        assert bound, (
            f"port {port} is still held after the batch that used it "
            f"({why}); a spawn that leaves a listener behind breaks the "
            f"next service on that port"
        )


@requires_macos_spawn
def test_ports_freed_after_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_hazard: int,
    spawned_groups: List[Tuple[Optional[int], Optional[int]]],
) -> None:
    """The port a batch used can be bound again once the batch is over.

    The reproducer stops each service after it has measured it, and a
    stop that leaves the listener behind is invisible in the
    measurement: the run counted as ready, and the port stays taken
    until something else gives up on it.  So the port is bound here
    after the fact -- the one check that a listener left behind cannot
    pass.

    The service here is the one-route one: it starts and stops fast,
    and the property under test is about the teardown rather than about
    the reproduction.
    """
    ports: List[int] = []
    _record_ports(monkeypatch, ports)

    rc, measurement = _run_batch(
        tmp_path, "batch", spawned_groups,
        _LIGHT_TARGET, _LIGHT_READY_TIMEOUT,
    )
    assert rc == 0, (
        f"the batch did not pass the reproducer's own zero-failure "
        f"assertion (rc={rc})\n{_failure_detail(measurement)}"
    )
    assert ports, "the reproducer allocated no port to check"

    for port in ports:
        bound, why = _can_bind(port)
        assert bound, (
            f"port {port} is still held after the batch that used it "
            f"({why})"
        )


def test_skip_reason_names_platform() -> None:
    """The skip says which platform it is about and where the evidence is.

    A skip without a reason is a hole in the suite; a skip whose reason
    does not say *what* was observed is a hole with a label on it.  So
    the reason has to name the platform and point at the document that
    holds the measurements -- and that document has to still be there,
    or the pointer is a dead end.
    """
    assert _PLATFORM_NAME in _SKIP_REASON, (
        f"the skip reason does not name the platform it is about: "
        f"{_SKIP_REASON!r}"
    )
    assert "docs/development/service-spawn-root-cause.md" in _SKIP_REASON, (
        f"the skip reason does not point at the document holding the "
        f"measurements: {_SKIP_REASON!r}"
    )
    assert _ROOT_CAUSE_DOC.is_file(), (
        f"the skip reason points at {_ROOT_CAUSE_DOC}, which does not "
        f"exist; a reader who follows the pointer finds nothing"
    )

    for func in (
        test_three_consecutive_spawns_all_ready,
        test_ports_freed_after_batch,
    ):
        marks = [
            mark for mark in getattr(func, "pytestmark", [])
            if mark.name == "skipif"
        ]
        assert marks, (
            f"{func.__name__} spawns services, but carries no skipif "
            f"marker: off the platform the reproduction needs, it would "
            f"run as a guard that cannot fail"
        )
        assert marks[0].kwargs.get("reason") == _SKIP_REASON, (
            f"{func.__name__} skips with a different reason than the one "
            f"this suite documents: {marks[0].kwargs.get('reason')!r}"
        )