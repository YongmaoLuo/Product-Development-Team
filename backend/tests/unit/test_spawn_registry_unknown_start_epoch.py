"""A pid whose start time is unknown is not a pid this run may signal.

Why this file exists
--------------------
The spawn registry records a process' *start time* next to its pid,
because a pid on its own is not an identity: pids are recycled, and a
recycled pid signalled by mistake kills a process this run never
started.

But the recording is not guaranteed.  ``process_start_epoch()`` reads
the start time from the operating system, and there are platforms --
and moments -- where it cannot: no ``/proc``, no usable ``ps``, a
``ps`` that times out.  It says so by returning an empty string, and
its own documentation says what that means:

    An empty string means "could not be determined" ... Callers must
    treat that as *unknown*, never as "different": an unknown start
    time means the process is skipped, not that it is killed.

The reaping path did not do that.  It only compared start times when
it had *both* of them; when either was missing it fell straight
through to ``os.killpg`` / ``os.kill``.  So on exactly the platform
where the run could not tell a live owner from a reused pid, it killed
the pid anyway -- and "the run could not tell" is exactly the
situation in which the pid may already belong to somebody else.

What has to be true now
------------------------
* a start time that is unknown on either side means the entry is
  **skipped**, not signalled;
* the skip is a *skip*, not a *give back*: the entry stays in the
  registry, because the run has not established that the process is
  gone, and the next cleanup pass may know more;
* a start time that is known and *different* is still ``foreign``:
  the fix must not weaken the recycled-pid guard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402


# --------------------------------------------------------------------------
# What a signal would look like
# --------------------------------------------------------------------------


def _record_signals(monkeypatch) -> list:
    """Record every signal the reaper sends, and send none of them.

    ``orphan_reaper`` reaches the kernel through the ``os`` module it
    imported, so patching ``os.kill`` / ``os.killpg`` here is what
    "did the reaper signal anything?" is answered by.  The stubs
    succeed, so a test that *did* signal would carry on and the
    assertion would still see the call.
    """
    calls: list = []

    def _kill(pid, sig):
        calls.append(("kill", pid, sig))

    def _killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))

    monkeypatch.setattr(reaper.os, "kill", _kill)
    monkeypatch.setattr(reaper.os, "killpg", _killpg)
    return calls


#: An entry that names a pid and says nothing about when it started.
_UNKNOWN_ENTRY = {
    "pid": 4242,
    "pgid": None,
    "kind": "service",
    "name": "backend.server",
    "owner_pid": 123,
    "start_epoch": "",
}


# --------------------------------------------------------------------------
# The two ways a start time can be unknown
# --------------------------------------------------------------------------


def test_an_entry_without_a_recorded_start_time_is_not_signalled(monkeypatch):
    """The run never recorded when the process started: leave it alone.

    The registry records the start time next to the pid, and the
    recording can fail -- the platform told the run nothing.  The
    entry then says *unknown*, and unknown is not "different": the pid
    may already have come back to somebody else.
    """
    calls = _record_signals(monkeypatch)

    status = reaper._reap_registry_entry(
        dict(_UNKNOWN_ENTRY), grace_seconds=1.0, reason="gate over",
        probe=lambda pid: "100", alive=lambda pid: True,
    )

    assert status == "unknown", (
        f"an entry with no recorded start time was not skipped: {status!r}"
    )
    assert calls == [], (
        f"the run signalled a process it could not identify: {calls}"
    )


def test_an_entry_whose_start_time_cannot_be_observed_is_not_signalled(
    monkeypatch,
):
    """The start time cannot be read now: leave the process alone.

    The entry was written down with a start time, but the platform
    cannot tell us the process' start time any more -- so the run
    cannot tell a live owner from a recycled pid.  Guessing "same"
    would kill a process this run never started.
    """
    calls = _record_signals(monkeypatch)
    entry = dict(_UNKNOWN_ENTRY, start_epoch="100")

    status = reaper._reap_registry_entry(
        entry, grace_seconds=1.0, reason="gate over",
        probe=lambda pid: "", alive=lambda pid: True,
    )

    assert status == "unknown", (
        f"an entry whose start time cannot be observed was not skipped: "
        f"{status!r}"
    )
    assert calls == [], (
        f"the run signalled a process whose identity it could not confirm: "
        f"{calls}"
    )


# --------------------------------------------------------------------------
# A skip is a skip, not a give back
# --------------------------------------------------------------------------


def test_an_unknown_entry_survives_the_rewrite(tmp_path, monkeypatch):
    """The entry stays in the registry, because nothing was given back.

    The registry is what the next cleanup pass reads.  An entry whose
    process was skipped has not been dealt with -- the run never
    established that the process is gone -- so dropping it would lose
    the record of a process this run started.
    """
    calls = _record_signals(monkeypatch)
    registry = tmp_path / "spawn_registry.jsonl"
    registry.write_text(
        json.dumps(_UNKNOWN_ENTRY, ensure_ascii=False) + "\n", encoding="utf-8",
    )

    reaped = reaper.reap_spawned_processes(
        registry, reason="gate over", grace_seconds=1.0,
        probe=lambda pid: "", alive=lambda pid: True,
    )

    assert reaped == [], (
        f"a process that was skipped was reported as given back: {reaped}"
    )
    survivors = reaper.read_spawn_registry(registry)
    assert [int(entry["pid"]) for entry in survivors] == [4242], (
        f"the skipped entry was dropped from the registry: {survivors}"
    )
    assert calls == [], (
        f"the run signalled a process it could not identify: {calls}"
    )


# --------------------------------------------------------------------------
# The guard that must not be weakened
# --------------------------------------------------------------------------


def test_a_recycled_pid_is_still_foreign(monkeypatch):
    """A pid whose start time is known to be different is still foreign.

    The fix is about the *unknown* case only.  When both start times
    are known and they differ, the number came back -- and the process
    holding it now is somebody else's.
    """
    calls = _record_signals(monkeypatch)
    entry = dict(_UNKNOWN_ENTRY, start_epoch="100")

    status = reaper._reap_registry_entry(
        entry, grace_seconds=1.0, reason="gate over",
        probe=lambda pid: "200", alive=lambda pid: True,
    )

    assert status == "foreign", (
        f"a recycled pid was not reported as foreign: {status!r}"
    )
    assert calls == [], (
        f"the run signalled a process that belongs to somebody else: {calls}"
    )


# --------------------------------------------------------------------------
# The order of the two questions
# --------------------------------------------------------------------------


def test_a_process_that_is_already_gone_is_gone_even_when_its_start_time_is_unknown(
    monkeypatch,
):
    """A process that is not there any more is gone -- that comes first.

    There is nothing to skip and nothing to signal: the process is
    gone, so the entry is dropped.  The start time never gets asked
    about, because there is no process left to identify.
    """
    calls = _record_signals(monkeypatch)

    status = reaper._reap_registry_entry(
        dict(_UNKNOWN_ENTRY), grace_seconds=1.0, reason="gate over",
        probe=lambda pid: "", alive=lambda pid: False,
    )

    assert status == "gone", (
        f"a process that is not there any more was not reported as gone: "
        f"{status!r}"
    )
    assert calls == [], (
        f"the run signalled a process it could not identify: {calls}"
    )
