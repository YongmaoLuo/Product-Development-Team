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

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

import notifications.feishu_notifier as feishu_notifier_module
import server as server_mod
from credentials import _FD_ENV_PREFIX as _DESCRIPTOR_ENV_PREFIX
from service_declaration import ServiceDeclaration
from tests import _readiness
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
#: still).  The ceiling is now 300s, which is an order of magnitude
#: above the slowest measurement ever taken (the same case run eight
#: times in parallel went from 11.89s to 68.6s) and comfortably above
#: the backend's own 60s self-check ceiling
#: (``server._start_request_guard_selfcheck`` stops an instance that
#: has not begun listening by then).  Above that ceiling a spawn that
#: is going to come up always gets the chance, and one that is not
#: going to come up dies on its own -- the reproducer records a
#: non-``None`` return code, and the assertion below still fails.
#: What this suite guards is "the child does not die before it is
#: ready" (the EBADF defect), not how fast the host starts a process.
_BACKEND_READY_TIMEOUT = 300

#: How long the one-route service gets to answer.  The same ceiling
#: applies: it bounds how long a doomed child is waited for, and it is
#: not an assertion about how quickly a one-route process binds a port.
#: A cold start of that target measured 3.03s, and eight copies of this
#: case in parallel multiply that by the same factor the backend does.
_LIGHT_READY_TIMEOUT = 60


def _backend_ready_timeout() -> int:
    """How long the backend is given to come up, on this host.

    The ceiling above is what a quiet host gets; the budget the
    reproducer is actually given follows the host's load, because the
    backend's own time-to-ready drifts with it.  A budget that does not
    drift measures the host instead of the defect this suite guards.
    """
    return int(
        _readiness.readiness_budget_seconds(
            float(_BACKEND_READY_TIMEOUT), ceiling=120.0,
        )
    )


def _light_ready_timeout() -> int:
    """How long the one-route service is given to answer, on this host.

    Eight seconds was written for an idle runner, and it is the number
    that made this suite red on a busy host for a reason that had
    nothing to do with the teardown defect it guards.
    """
    return int(
        _readiness.readiness_budget_seconds(
            float(_LIGHT_READY_TIMEOUT), ceiling=120.0,
        )
    )

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


@pytest.fixture(autouse=True)
def _no_provider_fleet_to_wait_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawned backend must not wait for a CC Switch that is not there.

    ``server.load_providers`` asks CC Switch for the provider chain when
    the mode says ``cc-switch``, and the hermetic suite pins that mode
    because the *provider-contract* tests need it.  On a machine without
    CC Switch the spawned backend therefore asks ``127.0.0.1:4000`` for
    a chain nobody serves, and the answer is a connect timeout: the
    service is up but not *ready* yet, and the reproducer's readiness
    budget runs out before the boot does.

    This file is about the spawn, not about provider routing, so it pins
    the mode to ``inherit`` — the configuration this codebase itself
    provides for "there is no provider fleet here" — the same way the
    other live-server tests do.
    """
    monkeypatch.setenv("PDT_PROVIDER_MODE", "inherit")


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
            _REPRODUCING_TARGET, _backend_ready_timeout(),
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
    # The batch waits for a service to answer; on a host that cannot
    # keep time the wait has no deadline to meet, so the case is
    # skipped rather than failed on the machine's answer.
    _readiness.skip_if_host_unmeasurable()

    ports: List[int] = []
    _record_ports(monkeypatch, ports)

    rc, measurement = _run_batch(
        tmp_path, "batch", spawned_groups,
        _LIGHT_TARGET, _light_ready_timeout(),
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


# ---------------------------------------------------------------------------
# The spawn shape: what a spawned backend may do to the machine
# ---------------------------------------------------------------------------

#: A deployment-shaped plans root, derived from this file so the test
#: never depends on where the checkout lives.
_DEPLOYMENT_PLANS_DIR = Path(__file__).resolve().parents[3] / "plans"


def _reaper_calls(monkeypatch: pytest.MonkeyPatch, config: Any) -> List[str]:
    """Run ``_start_orphan_reaper`` against *config*; report what it did.

    The reaper's own entry points are replaced, so the chain is
    exercised without scanning the machine and without starting a
    daemon -- the question under test is *whether the backend reaches
    them*, not what they do.

    The scan and the daemon start run on a thread of their own, so that
    the machine-wide scan is not on the boot's readiness path; the
    thread is joined here, which is also what keeps this test from
    leaving a scan running behind it.
    """
    calls: List[str] = []
    monkeypatch.setattr(server_mod.orphan_reaper, "config_from_env", lambda: config)
    monkeypatch.setattr(
        server_mod.orphan_reaper, "run_once", lambda cfg: calls.append("run_once")
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "ensure_daemon",
        lambda cfg, **kwargs: calls.append("ensure_daemon"),
    )
    thread = server_mod._start_orphan_reaper()
    if isinstance(thread, threading.Thread):
        thread.join(timeout=60)
        assert not thread.is_alive(), (
            "the reaper start-up thread outlived the test -- it must be joined"
        )
    return calls


def test_scratch_plans_root_does_not_license_the_machine_wide_reaper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend whose plans root is scratch must not act on the machine.

    The reaper is a **machine-wide** agent: its scan reads every process
    on the box, and its daemon is a detached process that outlives the
    backend that started it.  A backend that was spawned for a scratch
    run -- a test harness's copy of the plans tree, a one-off
    reproducer -- has no business installing one.  The daemon it starts
    survives it, keeps a core busy for as long as it lives, and is
    reclaimed by nobody; the scan it waits for is a machine-wide sweep
    that costs the boot tens of seconds on a loaded box, and the answer
    it would give ("what did the *machine's* PDT leave behind?") is not
    a scratch instance's business.

    The question is asked of the reaper's **own** configuration -- the
    root it would work under -- because that is the root the scan and
    the daemon would use.
    """
    config = SimpleNamespace(
        enabled=True,
        plans_root=Path(tempfile.gettempdir()) / "scratch-plans",
    )

    assert _reaper_calls(monkeypatch, config) == []


def test_deployment_plans_root_may_install_the_machine_wide_reaper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment's plans root is the other half of the rule.

    The guard above must not become "the reaper never runs": the
    machine's PDT instance is exactly the case the reaper exists for,
    and it is the only one that is owed the orphans a previous life
    left behind.
    """
    config = SimpleNamespace(enabled=True, plans_root=_DEPLOYMENT_PLANS_DIR)

    assert _reaper_calls(monkeypatch, config) == ["run_once", "ensure_daemon"]


def test_a_config_without_a_plans_root_leaves_the_reaper_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unanswerable question is not a permission -- and not a veto.

    The reaper's configuration is what says where the plans live.  A
    configuration that does not answer the question cannot forbid the
    reaper either: the backend must not end up with a reaper that
    nobody can switch on and nobody can explain.
    """
    config = SimpleNamespace(enabled=True)

    assert _reaper_calls(monkeypatch, config) == ["run_once", "ensure_daemon"]


# ---------------------------------------------------------------------------
# The readiness path
# ---------------------------------------------------------------------------


#: How long the stand-in notifier takes to start.  Longer than the
#: budget the boot has to meet, so a boot that waits for it is red.
_NOTIFIER_BUILD_SECONDS = 3.0

#: The budget the boot has to meet.  Half the stand-in's start time: a
#: boot that waits for the notifier cannot meet it.
_BOOT_BUDGET_SECONDS = 1.5


class _SlowNotifier:
    """A notifier that takes its time to start.

    The real notifier's slow part is ``start()``: it builds the
    client, and building the client imports the SDK -- tens of seconds
    on a loaded machine.  The constructor does no I/O; it only stores
    configuration.  The same shape as the boot sweeps, and with the
    same consequence: uvicorn binds the socket only after the lifespan
    returns, so every one of those seconds is a second in which the API
    does not answer.

    The stand-in stands in for that cost, so the property under test
    does not depend on how busy the machine is.
    """

    _min_interval_seconds = 60.0

    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        time.sleep(_NOTIFIER_BUILD_SECONDS)
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        """There is no worker to stop."""


class _AppState:
    """The ``app.state`` the lifespan reads and writes.

    A namespace of its own rather than a mock: the test has to be able
    to tell "the notifier has not started yet" from "the notifier
    started", and a mock answers both questions with itself.
    """

    def __init__(self) -> None:
        self.feishu_notifier = None

    def __getattr__(self, name: str) -> None:
        return None


def test_the_boot_does_not_wait_for_the_notification_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API must answer while the notification channels are waking up.

    Building the notifier's client imports the Feishu SDK, and that
    import takes tens of seconds on a loaded machine.  A notifier that
    is started inline therefore makes the server *up but not ready*:
    the port answers a connect and then answers nothing for a minute.
    A client with a readiness budget gives up first, and the backend
    it spawned looks like a spawn that never came up -- which is
    exactly what ``test_three_consecutive_spawns_all_ready`` saw.

    The notifier is a side channel.  A plan card is worth pushing, but
    the API is worth serving, so a configured transport is started on
    the notifier's own thread and the boot does not wait for it.

    Two properties are pinned here: the boot does not wait for the
    notifier, and the notifier still starts -- off the readiness path
    is not the same thing as off the run.
    """
    monkeypatch.setattr(server_mod, "_recover_execution_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "_recover_verification_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "load_providers", lambda: [])
    monkeypatch.setattr(server_mod, "heartbeat_monitor", MagicMock())
    monkeypatch.setattr(server_mod, "_shutdown_all_executions", lambda: None)
    # The transport is configured, so the notifier builds its client --
    # and building the client is the slow part this test is about.
    monkeypatch.setattr(
        server_mod, "_feishu_transport_is_configured", lambda: True
    )
    monkeypatch.setattr(
        feishu_notifier_module, "FeishuNotifier", _SlowNotifier
    )

    state = _AppState()
    fake_app = MagicMock()
    fake_app.state = state
    measured: Dict[str, float] = {}
    threads: List[threading.Thread] = []

    async def _enter() -> None:
        began = time.monotonic()
        async with server_mod._lifespan(fake_app):
            # The body runs once the lifespan has entered, so this is
            # the boot: everything the startup did has happened by now.
            measured["boot"] = time.monotonic() - began
            # The notifier thread is caught while it is still starting
            # the notifier -- once the notifier is up, the thread has
            # nothing left to do and it is gone.
            threads.extend(
                thread for thread in threading.enumerate()
                if thread.name == "feishu-notifier-start"
            )
            # The notifier is attached before the boot ends: a server
            # that is ready can already answer questions about the
            # notification channel.  It is the *starting* of the
            # notifier that takes its time, not the attaching of it.
            assert state.feishu_notifier is not None, (
                "the notifier was not attached when the boot ended — a "
                "ready server must be able to answer questions about the "
                "notification channel"
            )

    asyncio.run(_enter())
    elapsed = measured["boot"]

    assert elapsed < _BOOT_BUDGET_SECONDS, (
        f"the boot waited {elapsed:.3f}s for the notification channels — "
        f"the lifespan must not block on a side channel"
    )

    assert len(threads) == 1, (
        f"the notifier must start on exactly one thread, found {len(threads)}"
    )
    thread = threads[0]
    try:
        assert thread.daemon is True, (
            "the notifier thread must be a daemon — a non-daemon thread "
            "would keep the interpreter alive at shutdown"
        )
    finally:
        thread.join(timeout=30)
        assert not thread.is_alive(), (
            "the notifier thread outlived the test — it must be joined"
        )

    assert state.feishu_notifier.started is True, (
        "the notifier was never started — moving it off the readiness "
        "path must not turn into not running at all"
    )

# ---------------------------------------------------------------------------
# The readiness decision (2026-10-29)
# ---------------------------------------------------------------------------
#
# The batches above measure the backend's own boot.  The tests below
# measure the *decision* the service manager makes about a child that
# came up, because that decision is what the readiness budget is spent
# on:
#
#   * the evidence has to name the process that answered, so that
#     "my child is serving" can be told from "something else is
#     holding my port";
#   * a child that is provably our own must not be waited on again --
#     the wait costs two seconds of every start, and the proof is
#     already in hand;
#   * a stranger on the port must still be caught.
#
# They start a minimal child rather than the backend because they are
# about the decision and not about the backend's boot: the backend's
# boot is measured above, where it belongs.

#: The minimal child: one route, one routing row, and a published
#: process group.  It is a script rather than an inline command
#: because it is a process of its own.
_MINIMAL_CHILD_SOURCE = '''
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
OWN_PGID = int(sys.argv[2])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0].endswith("/health/data"):
            body = json.dumps({
                "status": "ok",
                "plan_routing_rows": 1,
                "instance": {
                    "boot_id": "boot-minimal",
                    "pid": os.getpid(),
                    "pgid": os.getpgid(0) if OWN_PGID == 0 else OWN_PGID,
                    "state_db_path": "",
                },
            }).encode()
        else:
            body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

#: A child that tries to take a port and cannot: it is the shape of
#: the process that loses the race for a port somebody else holds.
_STRUGGLING_CHILD_SOURCE = '''
import socket
import sys

sock = socket.socket()
sock.bind(("127.0.0.1", int(sys.argv[1])))
print("bound", flush=True)
'''

#: The process group a stranger publishes: one that cannot be the
#: child's own, because no child of this test has it.
_STRANGER_PGID = 4242424


def _write_child(tmp_path: Path, name: str, source: str) -> Path:
    """Write a child's source into *tmp_path* and return its path."""
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return script


def _minimal_child_declaration(
    tmp_path: Path, port: int, pgid: int = 0,
) -> "ServiceDeclaration":
    """A declaration that starts the minimal child on *port*."""
    script = _write_child(tmp_path, "minimal_child.py", _MINIMAL_CHILD_SOURCE)
    return ServiceDeclaration(
        name="minimal-child",
        port=port,
        start_cmd=f'{sys.executable} "{script}" {port} {pgid}',
        cwd=str(tmp_path),
        health_url="",
        ready_timeout_seconds=20,
        log_path=str(tmp_path / "minimal.log"),
    )


def _struggling_child_declaration(
    tmp_path: Path, port: int,
) -> "ServiceDeclaration":
    """A declaration whose child cannot bind *port*."""
    script = _write_child(
        tmp_path, "struggling_child.py", _STRUGGLING_CHILD_SOURCE,
    )
    return ServiceDeclaration(
        name="struggling-child",
        port=port,
        start_cmd=f'{sys.executable} "{script}" {port}',
        cwd=str(tmp_path),
        health_url="",
        ready_timeout_seconds=20,
        log_path=str(tmp_path / "struggling.log"),
    )


def _start_stranger(port: int, pgid: int) -> Tuple[Any, threading.Thread]:
    """A server that holds *port* and is not the child we started.

    It answers the data-plane probe the way a real instance does, and
    publishes a process group that is not the child's -- so the
    manager has to be able to tell the two apart.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            body = json.dumps({
                "status": "ok",
                "plan_routing_rows": 1,
                "instance": {
                    "boot_id": "boot-stranger",
                    "pid": os.getpid(),
                    "pgid": pgid,
                    "state_db_path": "",
                },
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="stranger-server", daemon=True,
    )
    thread.start()
    return server, thread


def test_the_evidence_names_the_process_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evidence has to say *who* answered, not only *what*.

    "Something answers on this port" and "our child is answering on
    this port" are two different claims, and the second one is the
    one the manager acts on.  The instance publishes its own process
    and process group in its ``instance`` block, and the evidence has
    to carry them: without them there is nothing to compare our
    child's group against, and the proof that the port is held by our
    own child cannot be made at all.
    """
    body = json.dumps({
        "status": "ok",
        "plan_routing_rows": 3,
        "instance": {
            "boot_id": "boot-1",
            "pid": 4242,
            "pgid": 4242,
            "state_db_path": "/tmp/state.db",
        },
    }).encode()

    monkeypatch.setattr(
        spawn_repro.sm, "http_probe", lambda *a, **k: (200, body),
    )

    evidence = spawn_repro.sm.data_plane_evidence(8123)

    assert evidence.pid == 4242, (
        f"the evidence does not name the process that answered: {evidence!r}"
    )
    assert evidence.pgid == 4242, (
        f"the evidence does not name the process group that answered: "
        f"{evidence!r}"
    )
    assert evidence.to_dict()["pgid"] == 4242, (
        f"the recorded evidence does not carry the process group: "
        f"{evidence.to_dict()!r}"
    )


def test_a_child_that_answered_is_not_waited_on_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our own child does not have to be waited on a second time.

    The manager waits two seconds after the readiness probe, to catch
    a child that died on the way up.  That wait is worth it while the
    holder of the port is a stranger -- but when the instance that
    answered publishes *our child's* process group, the child is
    provably ours and provably alive, and the two seconds are spent
    on a question that has already been answered.
    """
    port = spawn_repro.sm.allocate_free_port()
    decl = _minimal_child_declaration(tmp_path, port)
    waits: List[float] = []
    real_await_exit = spawn_repro.sm._await_exit

    def spy(proc: Any, timeout: float) -> Tuple[bool, Any]:
        waits.append(timeout)
        return real_await_exit(proc, timeout)

    monkeypatch.setattr(spawn_repro.sm, "_await_exit", spy)

    runtime = spawn_repro.sm.start_service(decl, tmp_path, timeout_seconds=20)
    try:
        assert runtime.ready is True, (
            f"the minimal child did not come up: {runtime.detail!r}"
        )
        assert waits == [], (
            f"the manager waited {waits!r} on a child that had already "
            f"answered — the answer names our own child's process group, "
            f"so there is nothing left to wait for"
        )
    finally:
        spawn_repro.stop_tree(runtime.pid, runtime.pgid)


def test_a_stranger_on_the_port_is_still_caught(
    tmp_path: Path,
) -> None:
    """The proof must not fire for a process that is not ours.

    A port taken by something else in the window between choosing it
    and binding it is the case the two-second wait exists for: the
    readiness probe finds the other listener, and without the wait a
    dead child would be reported as a started service.
    """
    port = spawn_repro.sm.allocate_free_port()
    server, thread = _start_stranger(port, _STRANGER_PGID)
    try:
        decl = _struggling_child_declaration(tmp_path, port)
        runtime = spawn_repro.sm.start_service(
            decl, tmp_path, timeout_seconds=20,
        )
        assert runtime.ready is False, (
            f"a stranger on the port was reported as our service: "
            f"{runtime.detail!r}"
        )
        assert "not ready" in (runtime.detail or ""), (
            f"the failure does not say the service was not ready: "
            f"{runtime.detail!r}"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _descriptor_types(pid: int) -> Optional[List[str]]:
    """The kinds of descriptor *pid* holds, or ``None`` if lsof cannot say.

    The *kind* is what matters here, not the number: a pipe inherited
    from the parent is a FIFO whatever number it happens to sit at, and
    the number is exactly the thing the two processes disagree about.
    """
    try:
        completed = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except Exception:  # noqa: BLE001 - a probe is best-effort
        return None
    if completed.returncode != 0:
        return None
    kinds: List[str] = []
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            kinds.append(parts[4])
    return kinds


def test_the_child_inherits_no_pipe_from_its_parent(
    tmp_path: Path, descriptor_hazard: int,
) -> None:
    """The number in the environment does not give the child anything.

    The defect this suite was written for is a child that closes a
    descriptor number it inherited from its parent: the number means
    "a pipe" in the parent and "the selector" in the child, and the
    child closes its own.  So the child must start with nothing of the
    parent's descriptors -- and the observable form of that is a child
    that holds no pipe at all, whatever the environment says about
    descriptor numbers.
    """
    port = spawn_repro.sm.allocate_free_port()
    decl = _minimal_child_declaration(tmp_path, port)
    runtime = spawn_repro.sm.start_service(decl, tmp_path, timeout_seconds=20)
    try:
        assert runtime.ready is True, (
            f"the minimal child did not come up: {runtime.detail!r}"
        )
        kinds = _descriptor_types(runtime.pid)
        if kinds is None:
            pytest.skip("lsof is not available here, so the kinds are unknown")
        pipes = [kind for kind in kinds if kind in ("FIFO", "PIPE")]
        assert pipes == [], (
            f"the child holds {pipes!r} — a descriptor the parent "
            f"published is still open in the child, and the number that "
            f"names it is the number the child's own selector may take"
        )
    finally:
        spawn_repro.stop_tree(runtime.pid, runtime.pgid)


def test_the_readiness_poll_is_tighter_than_a_second() -> None:
    """The probe cannot be noticed a whole second late.

    The port opens, and the manager notices it on its next poll.  A
    poll interval of a second spends up to a second of the readiness
    budget on the *detection* of a port that is already open — a
    second the child cannot buy back.
    """
    interval = spawn_repro.sm._READY_POLL_INTERVAL
    assert interval <= 0.25, (
        f"the readiness poll waits {interval}s between probes — up to "
        f"that much of the readiness budget is spent noticing a port "
        f"that is already open"
    )


def test_the_readiness_question_costs_one_connect_not_a_wait(
    tmp_path: Path,
) -> None:
    """Asking "is the port answering?" must not cost a whole second.

    The readiness loop paces itself: it sleeps
    :data:`service_manager._READY_POLL_INTERVAL` between rounds, so the
    question it asks has to be cheap.  Asking it through a *wait* means
    every round pays a full second before the next one starts, and a
    child that binds its port in the middle of that second is noticed a
    second late -- a second of the readiness budget spent on the
    *detection* of a port that is already open, which is a second the
    child cannot buy back.

    Measured on a port nobody listens on: **1.15 s**.  A connect to a
    port on this machine is either refused at once or accepted at once,
    so the whole second is the wait's own sleep.
    """
    port = spawn_repro._free_port()
    decl = ServiceDeclaration(
        name="never-listening",
        port=port,
        start_cmd="",
        cwd=str(tmp_path),
        health_url="",
        ready_timeout_seconds=20,
        log_path=str(tmp_path / "never.log"),
    )

    started = time.monotonic()
    answering = spawn_repro.sm._service_is_answering(decl)
    elapsed = time.monotonic() - started

    assert answering is False, (
        f"a port nobody listens on was reported as answering: {decl!r}"
    )
    assert elapsed < 0.5, (
        f"the readiness question took {elapsed:.2f}s on a port that is "
        f"not listening -- it went through a wait, and that wait sleeps "
        f"a full second before it gives up"
    )


def test_a_service_that_answers_after_the_budget_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness budget is a ceiling, not a suggestion.

    A service that answers *after* the budget did not come up in time.
    The manager's success line says "ready within 12s" about the service
    it accepted, so a service that took 16 s is reported as having come
    up within 12 s -- a sentence that is false, in the one place where
    the reader is told what happened.

    Measured on a loaded host: the backend child binds its port at
    15.99 s under a 12 s budget, and the line reads "ready within 12s".

    The budget is what the caller asked for.  A manager that quietly
    extends it has answered a different question than the one it was
    asked, and the caller is the one who has to decide whether the
    round can go on.
    """
    port = spawn_repro._free_port()
    decl = ServiceDeclaration(
        name="late",
        port=port,
        start_cmd="",
        cwd=str(tmp_path),
        health_url="",
        ready_timeout_seconds=20,
        log_path=str(tmp_path / "late.log"),
    )

    def _answers_only_after_the_budget(_decl: ServiceDeclaration) -> bool:
        """The service answers -- but only once the budget is gone."""
        time.sleep(1.2)
        return True

    monkeypatch.setattr(
        spawn_repro.sm, "_service_is_answering", _answers_only_after_the_budget
    )

    ready, _evidence = spawn_repro.sm.wait_until_ready_with_evidence(
        decl, timeout_seconds=1
    )

    assert ready is False, (
        f"a service that answered {1.2}s into a 1s budget was reported "
        f"as ready -- the budget is the caller's, and a manager that "
        f"extends it has answered a different question"
    )


def test_the_manager_reads_its_probe_header_before_the_child_is_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manager's own probe must be ready before the child's clock starts.

    The data-plane probe asks the child a question the request guard
    has to let through, so the probe needs the guard's header -- and
    the guard's module pulls the web framework in on the way.  That
    import costs **1.0s to 2.2s** on a loaded host, and it used to be
    paid inside the child's readiness window: the child answers, and
    the manager then spends two seconds importing a framework before it
    can judge the answer it already has.

    The child's budget is the caller's budget.  The manager's warm-up
    is the manager's own cost, so it is paid before the child is
    started -- the window the child is given is the window the caller
    named, and nothing else.
    """
    order: List[str] = []
    real_popen = subprocess.Popen

    def _spy_popen(*args: Any, **kwargs: Any) -> Any:
        order.append("spawn")
        return real_popen(*args, **kwargs)

    def _spy_guard_header() -> Tuple[str, str]:
        order.append("guard")
        return ("X-PDT-Request", "1")

    monkeypatch.setattr(
        spawn_repro.sm.subprocess, "Popen", _spy_popen, raising=False,
    )
    monkeypatch.setattr(
        spawn_repro.sm, "_guard_header", _spy_guard_header, raising=False,
    )

    port = spawn_repro.sm.allocate_free_port()
    decl = _minimal_child_declaration(tmp_path, port)
    runtime = spawn_repro.sm.start_service(decl, tmp_path, timeout_seconds=20)
    try:
        assert runtime.ready is True, (
            f"the minimal child did not come up: {runtime.detail!r}"
        )
        assert "guard" in order, (
            f"the manager never read the request-guard header its own "
            f"readiness probe needs -- it reads it inside the child's "
            f"readiness window, where a 1.0s-2.2s import is charged "
            f"to the child (order: {order!r})"
        )
        assert order.index("guard") < order.index("spawn"), (
            f"the manager started the child before its own probe was "
            f"ready (order: {order!r}) -- the child's window then "
            f"starts after the manager's warm-up, and the import is "
            f"charged to the child"
        )
    finally:
        spawn_repro.stop_tree(runtime.pid, runtime.pgid)


def test_the_readiness_budget_is_measured_from_the_child_s_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget the caller named is the child's, and the child starts at the fork.

    The manager does the child's bookkeeping before it starts asking
    whether the child is up: the process-group separation alone took
    **0.18s** measured on this host, and the child is alive and
    importing through all of it.  A budget that starts at the
    readiness wait charges the child for the manager's bookkeeping, so
    a service that came up inside the budget the caller named is
    reported as late -- and the one-route service, which is given eight
    seconds, is the one that cannot afford it.

    So the wait is asked for what is left of the budget, and the
    report still names the budget the caller named.
    """
    asked: List[Optional[float]] = []
    marks: Dict[str, float] = {}
    real_popen = subprocess.Popen
    real_pgid = spawn_repro.sm._detached_pgid_of

    def _spy_popen(*args: Any, **kwargs: Any) -> Any:
        marks["spawn"] = time.monotonic()
        return real_popen(*args, **kwargs)

    def _slow_pgid(pid: int) -> Optional[int]:
        """The bookkeeping the manager does before it starts asking.

        On this host the process-group separation takes 0.18s; here it
        is made to take half a second, so the gap the child pays for is
        a gap a test can see.
        """
        time.sleep(0.5)
        return real_pgid(pid)

    def _spy_wait(
        decl: ServiceDeclaration, timeout_seconds: Optional[float] = None,
    ) -> bool:
        marks["wait"] = time.monotonic()
        asked.append(timeout_seconds)
        return True

    monkeypatch.setattr(
        spawn_repro.sm.subprocess, "Popen", _spy_popen, raising=False,
    )
    monkeypatch.setattr(
        spawn_repro.sm, "_detached_pgid_of", _slow_pgid, raising=False,
    )
    monkeypatch.setattr(
        spawn_repro.sm, "wait_until_ready", _spy_wait, raising=False,
    )

    port = spawn_repro.sm.allocate_free_port()
    decl = _minimal_child_declaration(tmp_path, port)
    runtime = spawn_repro.sm.start_service(decl, tmp_path, timeout_seconds=20)
    try:
        assert asked, "the readiness wait was never asked anything"
        spent = marks["wait"] - marks["spawn"]
        assert spent > 0.4, (
            f"the bookkeeping this test makes slow was skipped: the wait "
            f"started {spent:.2f}s after the spawn"
        )
        assert asked[0] == pytest.approx(20.0 - spent, abs=0.2), (
            f"the wait was asked for {asked[0]!r}s of a 20s budget, but "
            f"the child had already spent {spent:.2f}s of it before the "
            f"wait even started -- the child is charged for the "
            f"manager's bookkeeping"
        )
    finally:
        spawn_repro.stop_tree(runtime.pid, runtime.pgid)


def test_a_child_that_wrote_nothing_says_so(tmp_path: Path) -> None:
    """An empty log has to say that it is empty.

    "The child wrote no output at all" is a fact about where the child
    died: it never reached its first line.  A bare "(empty)" reads like
    a formatting accident, and the operator who has to act on it
    cannot tell "the log is empty" from "the log was not read".
    """
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")

    detail = spawn_repro.sm._failure_detail(
        pid=1234,
        pgid=1234,
        fds=3,
        timeout_seconds=12,
        rc=None,
        log_file=empty,
    )

    assert "wrote no output at all" in detail, (
        f"an empty log is not reported as one: {detail!r}"
    )
    assert detail.endswith("(empty)"), (
        f"the log half is no longer the last thing on the line: {detail!r}"
    )


def test_a_child_that_wrote_nothing_says_whether_it_was_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty log has to say whether the child was busy or stuck.

    "The service wrote no output at all" says where the child stopped
    -- it never reached its first line -- but not what it was doing
    there.  A child that is still importing burns CPU; a child that is
    waiting on something does not.  Those are two different problems,
    and the reader of the line has to be able to tell them apart.

    The guess that used to be printed there -- "it was still importing
    the application" -- is wrong exactly when it matters, which is
    when the child is *not* importing anything and is stuck instead.

    The CPU seconds alone do not settle it either: a child reading
    thousands of files spends its time waiting, not computing -- the
    ``lark_oapi`` import on this host is **35.5s of wall time and
    2.3s of CPU**.  So the line carries both numbers and lets the
    reader see which side of the line the child is on.
    """
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        spawn_repro.sm, "_child_cpu_seconds", lambda pid: 3.5, raising=False,
    )
    busy = spawn_repro.sm._failure_detail(
        pid=1234, pgid=1234, fds=3, timeout_seconds=12, rc=None,
        log_file=empty, elapsed_seconds=4.0,
    )
    assert "3.5s of CPU" in busy, (
        f"the line does not say how much CPU the child used: {busy!r}"
    )
    assert "4.0s of wall time" in busy, (
        f"the line does not say how long the child had: {busy!r}"
    )
    assert "computing" in busy, (
        f"a child that burnt its wall time is not reported as computing: "
        f"{busy!r}"
    )

    monkeypatch.setattr(
        spawn_repro.sm, "_child_cpu_seconds", lambda pid: 0.7, raising=False,
    )
    idle = spawn_repro.sm._failure_detail(
        pid=1234, pgid=1234, fds=3, timeout_seconds=12, rc=None,
        log_file=empty, elapsed_seconds=12.0,
    )
    assert "0.7s of CPU" in idle, (
        f"the line does not say how much CPU the child used: {idle!r}"
    )
    assert "12.0s of wall time" in idle, (
        f"the line does not say how long the child had: {idle!r}"
    )
    assert "waiting" in idle, (
        f"a child that used almost no CPU is not reported as waiting: {idle!r}"
    )


def test_a_child_that_stalled_in_the_import_says_where_it_stalled(
    tmp_path: Path,
) -> None:
    """A child that never got past the import has to say so.

    A log that stops before the child's first line is a fact about the
    step the child died on: it never printed the line that says the
    server is starting, so it never got past importing the
    application.  The reader of the failure line has the log open in
    another window, and the question they are asking is "where did it
    stop" -- not "what did it print".
    """
    log = tmp_path / "svc.log"
    log.write_text("", encoding="utf-8")

    detail = spawn_repro.sm._failure_detail(
        pid=1234,
        pgid=1234,
        fds=3,
        timeout_seconds=12,
        rc=None,
        log_file=log,
    )

    assert "importing" in detail, (
        f"a child that never reached its first line is not reported as "
        f"one: {detail!r}"
    )


def test_a_child_that_stalled_in_the_lifespan_says_where_it_stalled(
    tmp_path: Path,
) -> None:
    """A child that got as far as the lifespan has to say so.

    The lifespan is where the boot does its work, and it runs *after*
    the application's import: a child whose log says it is waiting for
    the application to start has already paid for the import and is
    stuck in the startup itself.  Those are two different problems with
    two different fixes, and the failure line has to tell them apart.
    """
    log = tmp_path / "svc.log"
    log.write_text(
        "[backend server] Starting uvicorn on 127.0.0.1:62991\n"
        "INFO:     Started server process [55164]\n"
        "INFO:     Waiting for application startup.\n",
        encoding="utf-8",
    )

    detail = spawn_repro.sm._failure_detail(
        pid=55164,
        pgid=55164,
        fds=3,
        timeout_seconds=12,
        rc=None,
        log_file=log,
    )

    assert "lifespan" in detail, (
        f"a child stuck in the startup is not reported as one: {detail!r}"
    )


def test_a_child_that_finished_starting_but_never_answered_says_so(
    tmp_path: Path,
) -> None:
    """A child that started and then went quiet has to say so.

    This is the case the readiness check cannot explain by itself: the
    application says it started, and the port still does not answer.
    The line has to name that, because "not ready" on its own reads as
    "it never came up".
    """
    log = tmp_path / "svc.log"
    log.write_text(
        "[backend server] Starting uvicorn on 127.0.0.1:62991\n"
        "INFO:     Started server process [61103]\n"
        "INFO:     Waiting for application startup.\n"
        "INFO:     Application startup complete.\n",
        encoding="utf-8",
    )

    detail = spawn_repro.sm._failure_detail(
        pid=61103,
        pgid=61103,
        fds=3,
        timeout_seconds=12,
        rc=None,
        log_file=log,
    )

    assert "never answered" in detail, (
        f"a child that started and then went quiet is not reported as "
        f"one: {detail!r}"
    )


def test_the_child_is_started_with_unbuffered_output() -> None:
    """A child's own log has to arrive while the child is running.

    A child's stdout is a file, not a terminal, so Python buffers it.
    The buffer is flushed when the process exits — which, for a child
    that died on the way up, never happens.  The log of a child that
    died before its first line is then empty, and an empty log says
    nothing about where the child died.
    """
    env = spawn_repro.sm._child_env({})
    assert env.get("PYTHONUNBUFFERED") == "1", (
        f"the child's output is buffered, so its log is empty until it "
        f"exits: {env!r}"
    )


def test_the_boot_path_does_not_import_the_notification_http_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notification client's HTTP library is not on the boot path.

    The notification stack imports its HTTP library at module level,
    and the notifier is imported *inside* the lifespan — which is on
    the readiness path, because uvicorn binds the socket only after
    the lifespan returns.  A library that costs seconds to import is
    therefore seconds in which the API does not answer.
    """
    monkeypatch.delitem(sys.modules, "httpx", raising=False)
    monkeypatch.delitem(sys.modules, "httpx._client", raising=False)

    stand_in = server_mod._install_lazy_httpx()

    assert stand_in is not None, (
        "the notification HTTP library was not deferred"
    )
    assert sys.modules.get("httpx") is stand_in, (
        f"the deferred library is not the one in sys.modules: "
        f"{sys.modules.get('httpx')!r}"
    )
    assert "httpx._client" not in sys.modules, (
        "the deferred library was imported anyway"
    )


def test_the_deferred_library_answers_when_the_client_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferring the import must not break the thing that imports it.

    The stand-in answers an attribute read by loading the real library
    and handing the attribute over — and the real library takes the
    place of the stand-in, so everything that imports it afterwards
    gets the real thing.
    """
    monkeypatch.delitem(sys.modules, "httpx", raising=False)
    monkeypatch.delitem(sys.modules, "httpx._client", raising=False)

    stand_in = server_mod._install_lazy_httpx()
    assert stand_in is not None

    client = stand_in.Client

    assert client is not None, (
        "the deferred library did not answer the attribute the client "
        "asks for"
    )
    assert sys.modules.get("httpx") is not stand_in, (
        "the real library did not take the place of the stand-in"
    )
    assert "httpx._client" in sys.modules, (
        "the real library was not imported when it was first used"
    )


#: The line the backend prints *before* it imports the application.  The
#: manager reads it to tell "the child is still importing" from "the
#: child is stuck in the start-up", so the wording is a contract between
#: the two files and not a log line anybody may reword.
_BOOT_IMPORT_LINE = "[backend server] importing the application"


def test_the_child_announces_the_import_before_it_imports(
    tmp_path: Path,
) -> None:
    """The boot has to say that it is importing, before it imports.

    The backend's log says nothing at all until the whole application
    graph has been imported, and that import is the slow part of the
    boot: measured on this host, 0.65s on a quiet machine and 7-10s
    under load.  A child that is stuck there therefore leaves an
    **empty** log, and the manager has to *deduce* from that emptiness
    where the child stopped.

    The deduction is wrong exactly when it matters: an empty log also
    says nothing about a child that died before it could write a
    line, and the reader of the failure line is the one who has to
    tell those two apart.  So the child announces the import, and the
    announcement is the **first** line of the log -- printed before the
    import, not after it.

    The announcement is asked for, not volunteered: a plain import in a
    test or in a REPL stays quiet, and the manager's child environment
    is what turns it on.
    """
    repo_root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env["PDT_BOOT_TRACE"] = "1"
    env["PDT_PLANS_DIR"] = str(tmp_path / "plans")

    completed = subprocess.run(
        [sys.executable, "-c", "import backend.server"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, (
        f"the backend did not import: {completed.stderr[-2000:]!r}"
    )
    said = [line for line in completed.stdout.splitlines() if line.strip()]
    assert said, (
        "the boot said nothing at all, so the manager cannot tell a "
        "child that is importing from a child that is dead"
    )
    assert said[0] == _BOOT_IMPORT_LINE, (
        f"the first line of the log is not the import announcement, so a "
        f"child that is stuck in the import still leaves a log that says "
        f"nothing about it: {said[0]!r}"
    )


def test_the_manager_tells_the_child_to_announce_its_boot() -> None:
    """The announcement has to be asked for.

    The child is started by the manager, and the manager is the one
    that reads the log: the variable that turns the announcement on is
    part of the child environment, next to the two the child already
    depends on (``PYTHONUNBUFFERED``, so the log is not stuck in a
    buffer, and the provider mode, so the child does not wait for a
    provider fleet that is not there).
    """
    env = spawn_repro.sm._child_env({})

    assert env.get("PDT_BOOT_TRACE") == "1", (
        f"the child is not asked to announce its boot, so its log stays "
        f"empty while it imports and the manager can only guess where "
        f"the child stopped: {env!r}"
    )


def test_a_child_that_announced_its_import_is_told_it_was_importing(
    tmp_path: Path,
) -> None:
    """A child that announced the import is reported as importing.

    The announcement in the log is a fact; the sentence in the failure
    line is what the reader reads.  Without the sentence the reader has
    to interpret the log's last line themselves, and the whole job of
    the failure line is to have already done that.

    The sentence names the step the child stopped on -- "still
    importing" -- and not a guess about it: a child that announced the
    import and then printed nothing else did not get past the import.
    """
    log = tmp_path / "svc.log"
    log.write_text(f"{_BOOT_IMPORT_LINE}\n", encoding="utf-8")

    detail = spawn_repro.sm._failure_detail(
        pid=1234,
        pgid=1234,
        fds=3,
        timeout_seconds=12,
        rc=None,
        log_file=log,
    )

    assert "it was still importing the application" in detail, (
        f"a child that announced its import is not reported as one: "
        f"{detail!r}"
    )
