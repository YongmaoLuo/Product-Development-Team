"""The blast radius of a verdict list: it cannot name a process outside the scope.

Why this file exists
--------------------
The reaper's zero-impact suite pins the *judgement*: an unmarked process is
never a kill, and a full unscoped pass decides and stops without a signal
leaving the process.  What that suite cannot pin is the layer that turns a
verdict into a signal.

The accident this file exists for had its shape in a file rather than in the
reaper.  A negative-control pytest plugin replaced the reaper's action layer
with one that sent ``SIGTERM`` to every verdict it was handed, and the
plugin was run against a full-machine scan with no ``pid_scope``
configured.  The judgement layer kept skipping every process it saw -- it
never once decided to kill anything -- and the signals went out anyway,
because the signals did not come from the judgement layer.  They came from
the replacement.  A ``SIGTERM`` aimed at one process' group took the
launchd jobs and the supervisor that shared it down with it: the machine
did not restart, and the whole user session started over.

So the property pinned here is not "the reaper is careful".  It is narrower
and stronger: **a verdict list produced by a scoped scan cannot name a
process outside that scope**, whatever the action layer does with the list
afterwards.  The scope is enforced at the scan, so it holds for the real
action layer, for a replaced one and for a broken one alike.

What each test pins
-------------------
``test_scoped_scan_cannot_reach_outside_the_scope``
    The positive control.  Two children inside the scope, two canaries
    outside it, one scan, one deliberately broken action layer.  The
    scan's verdict list names the in-scope pair and nothing else -- and
    that is asserted *before* any signal is sent -- so the broken action
    layer kills the in-scope pair and leaves both canaries running.

``test_unscoped_scan_would_have_reached_the_canaries``
    The negative control, and the reason the first test is a measurement
    rather than a tautology.  The same four children, the same scan, but
    no scope: the verdict list *does* name the canaries.  Drop the scope
    and the canaries come back into the list, which is exactly what the
    broken action layer would have signalled.  This test never sends a
    signal of any kind -- it scans and asserts, and that is the whole
    point of it.

The ordering rule
-----------------
The "who is in the list" assertion comes before the first signal, and the
"who died" assertion comes after it.  A test that signalled first and
asked afterwards would kill the canaries on the way to proving that it had
not killed them.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    SPAWN_KIND,
    SPAWN_OWNER_PID,
    SPAWN_OWNER_START,
    read_process_environ,
)

pytestmark = pytest.mark.integration


#: How many children the scope puts inside, and how many stay outside it
#: as canaries.  Two each, so that "the scope held" is a statement about a
#: set of processes and not about one lucky pid.
IN_SCOPE_COUNT = 2
CANARY_COUNT = 2

#: The children's program.  ``time.sleep(30)`` outlives every pass in this
#: file by a wide margin, and a leaked child cannot outlive the test run
#: by much.
CHILD_CODE = "import time; time.sleep(30)"

#: The environment the children are *launched* with: ``PATH`` and nothing
#: else.  It is built from scratch rather than inherited so that no
#: ``PDT_SPAWN_*`` marker can reach a child -- not even one the test
#: runner itself happens to carry, which it does whenever the runner is
#: itself a PDT-spawned process.
CHILD_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

#: The markers that make a process *not* unmarked.
MARKER_NAMES = (SPAWN_OWNER_PID, SPAWN_OWNER_START, SPAWN_KIND)

#: How long to wait for a child to become visible to the machine with a
#: readable environment.
READY_TIMEOUT = 30.0

#: How long to wait for a terminated child to go away.
TERMINATE_TIMEOUT = 15.0

#: How long to let a signal that was sent a moment ago land, before
#: asking "is it still running?".
#:
#: Without this the survival check would answer the wrong question: a
#: child that was signalled but has not died *yet* still reports
#: ``poll() is None``, so "still running" would be indistinguishable from
#: "not signalled".  A ``SIGTERM`` to a sleeping child lands in
#: milliseconds; two seconds is the margin.
SETTLE_SECONDS = 2.0

#: The exit code a child reports when it died of ``SIGTERM``: the
#: negative of the signal number, which is what ``subprocess`` hands back
#: for a process that was killed by a signal rather than by an exit.
SIGTERM_EXIT_CODE = -signal.SIGTERM


class _Children:
    """The children the fixture spawned, split by the side of the scope.

    ``in_scope`` are the pids the scan is allowed to judge; ``canary``
    are the pids it must never name.  Both lists hold the live
    :class:`subprocess.Popen` objects, so ``poll()`` on them answers "is
    it still running?".
    """

    def __init__(self, in_scope, canary):
        self.in_scope = in_scope
        self.canary = canary

    @property
    def in_scope_pids(self) -> Tuple[int, ...]:
        """The pids inside the scope, in the order they were spawned."""
        return tuple(proc.pid for proc in self.in_scope)

    @property
    def canary_pids(self) -> Tuple[int, ...]:
        """The pids outside the scope, in the order they were spawned."""
        return tuple(proc.pid for proc in self.canary)


def _wait_until_its_environment_can_be_read(pid: int, timeout: float = READY_TIMEOUT):
    """Return *pid*'s environment, or fail the test if it cannot be read.

    A child becomes visible to the machine a moment after it is spawned;
    the wait covers that window only.

    The environment has to come back **readable**.  An unreadable one
    would be judged ``env_unreadable``, which is a different claim from
    the one this file exists to prove, and on a machine whose process
    probe cannot run at all it is the difference between "the scope held"
    and "the scope held over an empty list".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        env = read_process_environ(pid)
        if env is not None:
            for name in MARKER_NAMES:
                assert name not in env, (
                    "the child %d carries %s, so it is not an unmarked "
                    "process" % (pid, name)
                )
            return env
        time.sleep(0.05)
    raise AssertionError(
        "the child %d never became readable. The reaper reads a process' "
        "environment through the platform's process probe, and on this "
        "machine that probe cannot answer -- so no process on the machine "
        "is visible to the reaper, and this file cannot measure anything "
        "here." % pid
    )


def _terminate_all(procs) -> None:
    """Stop every child and wait for it -- a fixture must not leak processes."""
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=TERMINATE_TIMEOUT)


def _settle(seconds: float = SETTLE_SECONDS) -> None:
    """Let a signal that was just sent land, so "still running" means it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)


def _wait_for_exit_codes(procs, timeout: float = TERMINATE_TIMEOUT) -> List[Optional[int]]:
    """Return each child's exit code, waiting up to *timeout* for it.

    ``None`` is what a child that is still running reports, so a ``None``
    in the result is a child the signal did not reach.
    """
    deadline = time.monotonic() + timeout
    codes: List[Optional[int]] = []
    for proc in procs:
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        codes.append(proc.poll())
    return codes


def _spawn_child() -> subprocess.Popen:
    """Start one child of this interpreter, with no marker in its world.

    ``sys.executable`` and not an Apple platform binary: the reaper reads
    a process' environment through the platform's process probe, and that
    probe shows nothing for a binary the system protects.

    ``start_new_session`` puts the child in a session of its own, so a
    signal addressed to the child cannot reach the test runner's group --
    which matters here precisely because this file sends real signals.
    """
    return subprocess.Popen(
        [sys.executable, "-c", CHILD_CODE],
        env=dict(CHILD_ENV),
        start_new_session=True,
    )


@pytest.fixture
def scoped_children():
    """Four real processes: two inside the scope, two canaries outside it."""
    in_scope = [_spawn_child() for _ in range(IN_SCOPE_COUNT)]
    canary = [_spawn_child() for _ in range(CANARY_COUNT)]
    procs = in_scope + canary
    try:
        for proc in procs:
            _wait_until_its_environment_can_be_read(proc.pid)
        yield _Children(in_scope, canary)
    finally:
        _terminate_all(procs)


def _broken_reap(verdicts, config, **kwargs):
    """The action layer of the accident: signal every verdict it is handed.

    This is deliberately the worst version of the action layer there is.
    It consults neither the verdict's decision, nor its reason, nor the
    dry-run flag, nor the scope -- it signals whatever it is given.  It
    exists so that the question "what could a broken action layer reach?"
    has an answer that is measured rather than argued.

    It is only ever called with the verdict list of a **scoped** scan, and
    only after the test has asserted that the list names nothing outside
    that scope.  The scope is what makes the call safe; the broken layer
    is what makes the test worth having.
    """
    for verdict in verdicts:
        os.kill(verdict.proc.pid, signal.SIGTERM)


def test_scoped_scan_cannot_reach_outside_the_scope(tmp_path, scoped_children):
    """A scoped verdict list cannot name a process outside the scope.

    The scope is enforced by the scan, so the property holds for any
    action layer -- including the broken one below, which is the point:
    the broken layer is handed a list it cannot widen.
    """
    config = dataclasses.replace(
        reaper.config_from_env({}),
        plans_root=tmp_path,
        pid_scope=scoped_children.in_scope_pids,
        dry_run=False,
    )

    verdicts = reaper.scan(config=config).verdicts

    # Before anything is signalled.  The list is the evidence, and the
    # list must name the in-scope pids and nothing else.  A failure here
    # ends the test, and the signal below is never sent.
    scanned_pids = {verdict.proc.pid for verdict in verdicts}
    scope = set(scoped_children.in_scope_pids)
    outside = sorted(scanned_pids - scope)
    if outside:
        sample = ", ".join(str(pid) for pid in outside[:10])
        pytest.fail(
            "the scoped scan named %d pids outside the scope %r (e.g. %s). "
            "The signal below would have reached a process that was never "
            "in scope." % (len(outside), sorted(scope), sample)
        )
    missing = scope - scanned_pids
    assert not missing, (
        "the scoped scan did not name the in-scope pids %r at all, so the "
        "scope held over an empty list and the test proved nothing" % (
            sorted(missing),
        )
    )

    # The broken action layer, fed the verdict list the scope produced.
    _broken_reap(verdicts, config)

    codes = _wait_for_exit_codes(scoped_children.in_scope)
    assert codes == [SIGTERM_EXIT_CODE] * IN_SCOPE_COUNT, (
        "the in-scope children did not die of SIGTERM: %r" % (codes,)
    )

    # Give a signal that was sent a moment ago the time to land, so
    # "still running" means "not signalled" and not "has not died yet".
    _settle()
    canary_codes = [proc.poll() for proc in scoped_children.canary]
    assert canary_codes == [None] * CANARY_COUNT, (
        "the canaries were signalled: %r" % (canary_codes,)
    )


def test_unscoped_scan_would_have_reached_the_canaries(tmp_path, scoped_children):
    """Without the scope, the very same scan does name the canaries.

    This is the negative control, and it is what makes the first test a
    measurement.  The two tests share every fixture and differ in one
    line: the scope.  Here there is none, so the scan walks the whole
    machine and the canaries come back in its verdict list -- which is
    what the broken action layer would have signalled.

    No signal is sent anywhere in this test.  It scans and asserts, and
    that is the whole point of it: the verdict list is the input the
    action layer would have consumed.
    """
    config = dataclasses.replace(
        reaper.config_from_env({}),
        plans_root=tmp_path,
        pid_scope=None,
        dry_run=False,
    )

    verdicts = reaper.scan(config=config).verdicts

    scanned_pids = {verdict.proc.pid for verdict in verdicts}
    for pid in scoped_children.canary_pids:
        assert pid in scanned_pids, (
            "the unscoped scan did not name the canary %d, so the scoped "
            "test would pass for the wrong reason: it would pass even if "
            "the scope were dropped" % pid
        )
