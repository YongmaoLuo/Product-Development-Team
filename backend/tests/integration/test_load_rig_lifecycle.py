"""The lifecycle of the CPU load rig: it starts, it spins, and it is given back.

Why this file exists
--------------------
A case that needs the host to be busy used to build its own busy loop —
a shell pipeline with ``while :; do :; done`` behind it, started from
the test and never reaped.  That shape has no owner.  When the case
failed, the loop kept running; when several of them ran at once, the
host was so busy that the rest of the suite measured the load instead of
the code under test.  The load had become the incident.

``backend/tests/_load_rig.py`` is the packaged answer to that: a context
manager that starts a known number of workers, counts what they did, and
hands every one of them back when the block ends — including when the
block ends by raising.

What these cases pin
--------------------
The rig's contract is a *reclamation* contract, so most of these cases
are about reclamation rather than about speed:

* the requested number of workers is the number that gets started;
* the workers really spin (a rig that started and then idled would
  satisfy every other case in this file);
* nothing is left running once the block is over — the pids are gone,
  not merely stopped;
* an exception raised inside the block does not leave the workers
  behind either;
* a worker that fails to start takes the already-started ones with it,
  rather than leaving half a rig running;
* a worker that will not answer ``SIGTERM`` is killed rather than left
  running;
* a request for zero workers is refused before anything is started.

Why the markers are what they are
---------------------------------
``integration`` — the cases start real subprocesses, which is what that
marker is for.

``time_sensitive`` — one of the cases asserts on a sleep, so it belongs
to the lane that runs with the machine to itself.  The unit lane
collects ``tests/unit`` only, and the parallel lanes drop
``time_sensitive``, so this file never runs beside the busy shards: a
wall-clock assertion measured next to eight busy workers measures the
workers.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from tests import _load_rig
from tests._load_rig import cpu_load


pytestmark = [pytest.mark.integration, pytest.mark.time_sensitive]


class _StubSubprocess:
    """A stand-in for the ``subprocess`` module.

    The rig calls ``subprocess.Popen(...)`` through the module attribute
    on purpose, so that a test can replace the whole module and watch
    exactly which calls the rig makes.  A name bound at import time
    could not be replaced that way, and the two paths that need it — the
    spawn that fails, and the worker that will not stop — would then be
    untestable.
    """

    def __init__(self, real: object, fail_on_call=None, popen=None) -> None:
        self._real = real
        self._fail_on_call = fail_on_call
        self._popen = popen
        #: The workers the rig was handed, in order.
        self.started: list = []
        #: The rig asks for this one too, when a worker refuses to stop.
        self.TimeoutExpired = real.TimeoutExpired  # type: ignore[attr-defined]

    def Popen(self, *args, **kwargs):  # noqa: N802 - it stands in for the module
        """Hand the rig its next worker, or fail the way a failed spawn does."""
        if (
            self._fail_on_call is not None
            and len(self.started) + 1 == self._fail_on_call
        ):
            raise OSError("simulated spawn failure")
        if self._popen is not None:
            worker = self._popen(*args, **kwargs)
        else:
            worker = self._real.Popen(*args, **kwargs)  # type: ignore[attr-defined]
        self.started.append(worker)
        return worker


class _StubbornWorker:
    """A worker that does not answer ``SIGTERM``.

    It stands in for a process that is wedged: the rig has to escalate to
    a kill for it, and the escalation is the part worth pinning.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        """Refuse to be waited for until the worker has been killed."""
        if not self.killed:
            raise subprocess.TimeoutExpired(cmd="stubborn-worker", timeout=timeout)
        return self.pid


def _assert_no_such_process(pid: int) -> None:
    """Assert that *pid* names no process any more.

    ``os.kill(pid, 0)`` asks "does this process exist?" — it delivers no
    signal, but the kernel still looks the pid up, and a pid that no
    longer exists raises ``ProcessLookupError``.  A worker that was
    stopped but not reaped would still answer, so this asks about the
    pid being gone rather than about the process being idle.
    """
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cpu_load_spawns_the_requested_number_of_workers() -> None:
    """The rig starts exactly the number of workers it was asked for."""
    with cpu_load(workers=2) as rig:
        assert len(rig.pids) == 2
        assert all(pid > 0 for pid in rig.pids)


def test_workers_actually_spin() -> None:
    """The workers burn CPU — a rig that started and then idled would pass the rest."""
    with cpu_load(workers=2) as rig:
        before = rig.iterations()
        time.sleep(0.2)
        after = rig.iterations()

    assert after > before
    assert after > 0


def test_every_worker_is_gone_after_the_block() -> None:
    """Leaving the block leaves no worker behind."""
    with cpu_load(workers=2) as rig:
        pids = list(rig.pids)

    assert len(pids) == 2
    for pid in pids:
        _assert_no_such_process(pid)


def test_workers_are_reclaimed_when_the_body_raises() -> None:
    """An exception inside the block does not strand the workers."""
    pids: list[int] = []
    with pytest.raises(RuntimeError):
        with cpu_load(workers=2) as rig:
            pids = list(rig.pids)
            raise RuntimeError("the body failed")

    assert len(pids) == 2
    for pid in pids:
        _assert_no_such_process(pid)


def test_zero_workers_is_rejected() -> None:
    """A rig with no workers is refused, and nothing is started for it."""
    with pytest.raises(ValueError):
        with cpu_load(workers=0):
            pass


def test_a_worker_that_fails_to_start_takes_the_others_with_it(monkeypatch) -> None:
    """A spawn that fails reclaims the workers that did start, then re-raises."""
    fake = _StubSubprocess(subprocess, fail_on_call=2)
    monkeypatch.setattr(_load_rig, "subprocess", fake)

    with pytest.raises(OSError):
        with cpu_load(workers=3):
            pass

    assert len(fake.started) == 1
    for proc in fake.started:
        _assert_no_such_process(proc.pid)


def test_a_worker_that_ignores_sigterm_is_killed(monkeypatch) -> None:
    """A worker that will not stop is escalated to a kill, not left running."""
    stubborn = _StubbornWorker(pid=999999)
    fake = _StubSubprocess(subprocess, popen=lambda *args, **kwargs: stubborn)
    monkeypatch.setattr(_load_rig, "subprocess", fake)

    with cpu_load(workers=1) as rig:
        assert rig.pids == [stubborn.pid]

    assert stubborn.terminated
    assert stubborn.killed
