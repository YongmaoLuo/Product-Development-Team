"""Pins for the process budget of the service spawn path.

What is under test
------------------
How many processes :func:`service_manager.start_service` creates, and
what it writes down about the one it creates.

Why the budget needs a pin
--------------------------
The spawn path used to answer "when did this process start?" by asking
the operating system **twice** — once about itself, once about the
child it had just created — and on a machine without ``/proc`` both
answers cost a ``ps`` subprocess.

That is invisible in the production code and loud in the tests.  The
spawn-shape pins record every ``subprocess.Popen`` the spawn path makes
and assert that the *last* one is the service itself
(``test_service_spawn_shape.py``) and that there is exactly one of them
(``test_spawn_repro_harness.py``).  A probe inside the spawn path is
neither: it is an extra process, and the last one is no longer the
service.  The pins were right and the code was wrong — what the pins
reported as "the spawn is broken" was in fact "the spawn path measures
itself and its child by spawning processes".

The registry makes it worse than a red test.  ``scripts/run_tests.sh``
exports ``PDT_SPAWN_REGISTRY`` for every run, so the registration path
is live under the gate — and the registry entry is where the child's
start time is recorded.  Recording it must not cost a process.

The pins
--------
1. The spawn path creates exactly one process, and it is the service.
2. The registry entry carries a start time an observer can compare.
3. The recorded start time and the observed one are the same instant.
4. Two readings of one start time may straddle a second; a recycled
   pid may not.

Run them with:

    cd backend && .venv/bin/python3 -m pytest tests/unit/test_service_spawn_probe_budget.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, List, Tuple

import pytest

import orphan_reaper
import service_manager
from service_declaration import ServiceDeclaration

#: The environment variable that names the run's spawn registry.
REGISTRY_ENV = "PDT_SPAWN_REGISTRY"


def _free_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FakePopen:
    """A stand-in for :class:`subprocess.Popen` that records the call."""

    def __init__(self, args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = os.getpid()
        self.returncode = None

    def poll(self) -> None:
        return None

    def wait(self, timeout: Any = None) -> int:
        return 0


@pytest.fixture
def spawn_calls(monkeypatch, tmp_path):
    """Record every process the spawn path creates.

    The registry is named, because a run that names no registry does not
    register anything — and the registration is where the second probe
    used to live.  Without it, the budget this file pins is not the
    budget the gate runs with.
    """
    calls: List[Tuple[Any, dict]] = []

    def _fake_popen(args: Any, **kwargs: Any) -> _FakePopen:
        calls.append((args, kwargs))
        return _FakePopen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setenv(REGISTRY_ENV, str(tmp_path / "spawn-registry.jsonl"))
    monkeypatch.setattr(
        service_manager, "wait_until_ready",
        lambda decl, timeout_seconds=None: True,
    )
    monkeypatch.setattr(
        service_manager, "_await_exit", lambda proc, timeout: (False, None),
    )
    return calls


def _start_a_service(project_dir):
    """Start one declared service and return it."""
    decl = ServiceDeclaration(
        name="probe-budget", port=_free_port(), start_cmd="true", cwd=".",
    )
    return service_manager.start_service(decl, project_dir)


# ---------------------------------------------------------------------------
# 1. The spawn path creates exactly one process
# ---------------------------------------------------------------------------


def test_spawn_path_creates_exactly_one_process(spawn_calls, tmp_path):
    """Starting a service creates the service and nothing else.

    The registry is live (the fixture names one), so this is the budget
    the gate runs under: one process, the service, described by the argv
    the spawn-shape pins expect.
    """
    _start_a_service(tmp_path)

    assert len(spawn_calls) == 1, (
        "the spawn path created %d processes; the service is the only one "
        "it is allowed to create" % len(spawn_calls)
    )
    argv, _kwargs = spawn_calls[0]
    assert argv[0] == (sys.executable or "/usr/bin/python3"), (
        f"the process that was created is not the service: {argv[0]!r}"
    )
    assert argv[1] == "-c", (
        f"the service was not started through the wrapper: {argv[1:2]!r}"
    )


def test_spawn_path_creates_no_process_without_a_registry(
    spawn_calls, monkeypatch, tmp_path,
):
    """The budget does not depend on whether a registry was named.

    A run that names no registry registers nothing, and the spawn path
    must not measure the child either — otherwise the budget would be
    one process for some runs and three for others, and the pins would
    only ever catch half of it.
    """
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    _start_a_service(tmp_path)

    assert len(spawn_calls) == 1, (
        "the spawn path created %d processes without a registry; the "
        "service is the only one" % len(spawn_calls)
    )


# ---------------------------------------------------------------------------
# 2. The registry entry carries a comparable start time
# ---------------------------------------------------------------------------


def test_registry_entry_records_the_child_start_epoch(spawn_calls, tmp_path):
    """The entry written down for a service says when it started.

    A pid on its own is not an identity — pids are recycled — so the
    entry has to carry the start time next to it.  Without it the
    reaper cannot tell its own service from a process that inherited the
    number, and it leaves the service running.
    """
    _start_a_service(tmp_path)

    registry = tmp_path / "spawn-registry.jsonl"
    assert registry.exists(), "the service was not written down"
    entries = [
        line for line in registry.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries, "the registry is empty"
    entry = json.loads(entries[-1])
    assert entry.get("start_epoch"), (
        "the registry entry carries no start time: %r" % entry
    )


def test_recorded_start_epoch_is_the_same_instant_as_the_observed_one():
    """What the spawn writes down is what an observer later reads.

    The two have to be the same instant **and in the same unit**: a
    value recorded in one unit and compared against the other reads as
    a different process every time, and the reaper then leaves every
    live service alone.
    """
    proc = subprocess.Popen(
        [sys.executable or "/usr/bin/python3", "-c",
         "import time; time.sleep(30)"]
    )
    try:
        recorded = orphan_reaper.spawn_start_epoch(
            proc.pid, spawned_at=time.time(),
        )
        observed = orphan_reaper.process_start_epoch(proc.pid)
        assert recorded, "no start time was recorded for a live process"
        assert observed, "the observer could not read the start time"
        assert orphan_reaper._same_start(
            recorded, observed, orphan_reaper._start_tolerance(),
        ), (
            "the recorded start time %r and the observed one %r are not "
            "the same instant" % (recorded, observed)
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_recording_a_start_epoch_costs_no_process(monkeypatch):
    """Recording a start time does not cost a process.

    This is the whole point: the spawn path is not allowed to ask the
    operating system by spawning a process to ask it.
    """
    calls: List[Tuple[Any, dict]] = []

    def _fake_popen(args: Any, **kwargs: Any) -> _FakePopen:
        calls.append((args, kwargs))
        return _FakePopen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    recorded = orphan_reaper.spawn_start_epoch(
        os.getpid(), spawned_at=time.time(),
    )
    assert recorded, "no start time was recorded"
    assert not calls, (
        "recording a start time created %d processes" % len(calls)
    )


# ---------------------------------------------------------------------------
# 3. Two readings of one start time
# ---------------------------------------------------------------------------


def test_two_readings_of_one_start_time_may_straddle_a_second():
    """A one-second difference is the same instant; a large one is not.

    ``ps -o lstart=`` has one-second granularity and a value derived
    from the spawn clock can land on the other side of a second
    boundary from the value an observer reads.  Two seconds of
    tolerance is what makes the comparison survive that without making
    it blind to a recycled pid.
    """
    assert orphan_reaper._same_start("100", "101", 2.0)
    assert not orphan_reaper._same_start("100", "103", 2.0)
    assert not orphan_reaper._same_start("100", "999999", 2.0)


def test_start_times_are_compared_exactly_unless_a_tolerance_is_given():
    """The exact comparison is the default; the tolerance is opt-in.

    A caller that has two readings of the same field does not need a
    tolerance, and one that does not have them must not be given one
    by accident.
    """
    assert orphan_reaper._same_start("100", "100")
    assert not orphan_reaper._same_start("100", "101")


def test_start_tolerance_is_expressed_in_the_platforms_own_unit():
    """The tolerance is zero where the reading is exact, two seconds elsewhere.

    ``/proc`` answers with the kernel's own number, so two differing
    readings there describe two processes and nothing may be widened —
    a tolerance on that path would call a recycled pid ours, which is
    the one mistake this comparison exists to prevent.  ``ps -o
    lstart=`` answers in whole seconds, and that is the only path that
    needs slack.
    """
    tolerance = orphan_reaper._start_tolerance()
    if orphan_reaper._PROC_START_UNIT_IS_TICKS:
        assert tolerance == 0.0, (
            "the /proc reading is exact; a tolerance of %r would let a "
            "recycled pid be mistaken for ours" % (tolerance,)
        )
    else:
        assert tolerance >= 2.0, (
            "the tolerance is smaller than the second it is meant to cover: %r"
            % (tolerance,)
        )


# ---------------------------------------------------------------------------
# 4. The process learns its own identity once
# ---------------------------------------------------------------------------


def test_the_process_resolved_its_own_start_time_before_the_first_spawn():
    """The reaper learns who it is when it starts, not at every spawn.

    The spawn path asks for the owner's start time on every spawn.  The
    answer cannot change while the process lives, and asking costs a
    process on a machine without ``/proc`` — so the answer is resolved
    once, and the spawn path reads the answer.
    """
    if not orphan_reaper.process_start_epoch(os.getpid()):
        pytest.skip("this platform cannot report a process' start time")
    assert orphan_reaper._SELF_START_EPOCH, (
        "this process never resolved its own start time"
    )
