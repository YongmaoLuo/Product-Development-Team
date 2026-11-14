"""The load rig asks the host whether it is idle before it makes it busy.

Why this file exists
--------------------
``backend/tests/_load_rig.py`` makes the host busy on purpose: a case
that asserts on a wall-clock bound needs a machine that is already
under load, and the rig is the packaged way to get one.

But a rig that loads a machine that is *already* loaded is a rig that
makes the incident worse.  The suite has exactly one answer to "is this
host too busy to measure on?" — ``backend/tests/_host_load.py`` — and the
rig has to ask it before it starts a single worker, for the same reason
the wall-clock lanes ask it before they assert a bound: a busy host
turns a measurement into an accident.

So the rig asks that one gate, and stands down where the gate stands
down.  A CI runner's load is not the runner's own doing, and a rig that
refused to load a CI runner would leave the lane asserting nothing.

What these cases pin
--------------------
* a busy host is refused, and the refusal happens *before* any worker
  is started — the machine is not made busier by the attempt to notice
  it is already busy;
* an idle host gets its workers, as before;
* CI is not refused, however loaded the runner is;
* the skip message carries the load and the limit, so the log says why
  the case did not run without anybody having to reproduce the load;
* a limit that is not a number falls back to the default instead of
  silently switching the gate off.

Why the load and the limit are injected
---------------------------------------
A gate that can only be exercised on a host that happens to be busy is
not a test.  The load, the limit and the environment are all
injectable, and ``None`` means "read the real thing" — the convention
``tests._host_load`` already uses.

Why the markers are what they are
---------------------------------
``integration`` — the cases start real subprocesses, which is what that
marker is for.

``time_sensitive`` — this file is about the machine's load, so it
belongs to the lane that runs with the machine to itself.
"""

from __future__ import annotations

import subprocess

import pytest

from tests import _load_rig
from tests._load_rig import cpu_load


pytestmark = [pytest.mark.integration, pytest.mark.time_sensitive]


#: The environment variable that overrides the load limit.  It is spelled
#: out here rather than imported, because the name *is* the contract: a
#: test that read the constant would keep passing if the constant moved.
_LIMIT_ENV = "PDT_HOST_LOAD_LIMIT"


class _ForbiddenPopen:
    """A ``subprocess`` stand-in that refuses to spawn anything.

    The rig calls ``subprocess.Popen(...)`` through the module attribute
    on purpose, so that a test can replace the whole module and watch
    exactly which calls the rig makes.  Here the point is the opposite
    of the lifecycle file's: a call is a failure, because on a host the
    gate has already refused there must not be a single worker.

    ``TimeoutExpired`` is carried over so that the stand-in is a
    complete enough replacement for the module that a path which reaches
    the reclamation code would still find the exception it catches.
    """

    def __init__(self, real: object) -> None:
        self._real = real
        #: Every spawn the rig attempted, in order.
        self.calls: list = []
        #: The exception the module raises when a worker will not stop.
        self.TimeoutExpired = real.TimeoutExpired  # type: ignore[attr-defined]

    def Popen(self, *args, **kwargs):  # noqa: N802 - it stands in for the module
        """Record the attempt and refuse it: a busy host gets no worker."""
        self.calls.append((args, kwargs))
        raise AssertionError(
            "the rig spawned a worker on a host the gate had already refused"
        )


def test_busy_host_skips_before_spawning(monkeypatch) -> None:
    """A host over the limit is refused, and not one worker is started."""
    forbidden = _ForbiddenPopen(subprocess)
    monkeypatch.setattr(_load_rig, "subprocess", forbidden)

    with pytest.raises(pytest.skip.Exception, match="host load 55.0"):
        with cpu_load(workers=2, load=55.0, limit=20.0, env={}):
            pass

    assert forbidden.calls == [], (
        "the gate must be asked before any worker is started; the rig "
        "spawned %d" % (len(forbidden.calls),)
    )


def test_idle_host_starts_workers() -> None:
    """A host under the limit is loaded as asked."""
    with cpu_load(workers=2, load=1.0, limit=20.0, env={}) as rig:
        assert len(rig.pids) == 2
        assert all(pid > 0 for pid in rig.pids)


def test_ci_stands_the_gate_down() -> None:
    """CI is never refused: a runner's load is not the runner's own doing."""
    with cpu_load(workers=2, load=55.0, limit=20.0, env={"CI": "true"}) as rig:
        assert len(rig.pids) == 2


def test_skip_message_carries_both_numbers() -> None:
    """The refusal says what the load was and what the limit was."""
    with pytest.raises(pytest.skip.Exception) as refusal:
        with cpu_load(workers=1, load=55.0, limit=20.0, env={}):
            pass

    message = str(refusal.value)
    assert "55.0" in message, message
    assert "20" in message, message


def test_invalid_limit_in_env_falls_back_to_default() -> None:
    """A limit that is not a number falls back to the default, not to zero.

    A broken override must not switch the gate off: the default limit
    still refuses a host at load 25, which is over it.
    """
    with pytest.raises(pytest.skip.Exception, match="host load 25.0 >= 20"):
        with cpu_load(
            workers=1,
            load=25.0,
            limit=None,
            env={_LIMIT_ENV: "abc"},
        ):
            pass
