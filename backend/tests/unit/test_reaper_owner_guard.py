"""The sweep only collects what nobody else is still serving.

Why these tests exist
---------------------

The reaper used to ask a narrower question: "is this record *mine*?"
Only a ``server_shutdown`` sweep asked it at all, and only a record
whose ``owner_pid`` named a *different, live* process was spared.
Every other sweep - a startup sweep, a stop, a forced start - had no
owner check whatsoever, and a record with no owner at all (an old
record, or one the ledger could not attribute) was collected by
everyone.

That is how a sweep run by a process that owns nothing took down an
executor another backend was still serving: the record said "owned by
pid 1234", pid 1234 was alive and serving, and the sweep signalled the
executor anyway because the sweep's reason was not
``server_shutdown``.

The rule these tests pin is the one the tree actually needs: **a sweep
collects a record only when no living process is serving it.**  The
owner on record answers that when it is there; when it is not, the
kernel's current parent answers it.

The rules, in full:

* an owner that is alive - leave the record alone (``owner_alive``);
* an owner that is dead - an orphan, collect it;
* an owner that is this process - our own tree, collect it;
* no owner, and the process is gone - nothing to collect;
* no owner, and the process's parent is init or gone - an orphan,
  collect it;
* no owner, and the process's parent is alive - leave the record alone
  (``parent_alive``);
* no owner, and the parent could not be read at all - leave the record
  alone (``owner_unknown``); guessing is how a sweep kills a stranger.

The one sweep these rules do not speak to is the **forced start**
(``start_force``): that one is the operator naming one plan and asking
for its tree back, and skipping on an owner that the host has recycled
would leave the old executor running and start a second one beside it.
That exception is pinned here too - by the last case in this file, and
by ``test_shutdown_sweep_is_scoped_to_the_server.py``.

Hermeticity
-----------

The cases use **real** child processes and a **real** kernel: the
whole point of the gate is that it asks the kernel whether the owner is
alive, and a mock would answer the question instead of the kernel.  The
one thing that is stubbed is the reaper's signalling helper, so that
"not one signal" can be asserted rather than inferred.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import process_identity  # noqa: E402
import process_ledger as ledger  # noqa: E402
import process_reaper as reaper  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn(argv, cwd=None, new_session=True):
    """Start a real child process and return the ``Popen`` handle.

    ``new_session`` gives the child its own process group, which is what
    a detached PDT layer looks like.  The reaper refuses to signal a
    group that is the caller's own, so a test that wants the child
    signalled at all has to detach it.
    """
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )


def _cleanup(*procs):
    """Kill and reap every child the test started."""
    for proc in procs:
        if proc is None:
            continue
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001 - cleanup must not fail the test
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _stable_identity(pid, tries=40, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    On macOS the launcher rewrites ``argv[0]`` shortly after the exec,
    so a reading taken the instant a process is spawned can spell the
    command line differently from a reading taken a moment later.  The
    identity check compares command lines, so the *recorded* one has to
    be the settled one.
    """
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def _record_for(plan_dir, role, proc, *, owner_pid, plan_id="p1"):
    """Write a spawn record for ``proc`` into the ledger at ``plan_dir``.

    ``owner_pid`` says who started it - 0 for a record the ledger could
    not attribute.  The identity fields come from the kernel, so the
    reaper's own identity check can vouch for the record: the gate
    under test is the *owner* check, and it must not be reached
    through a record the identity check would refuse.
    """
    ident = _stable_identity(proc.pid)
    assert ident is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=ident.pgid if ident.pgid else proc.pid,
        create_time=ident.create_time,
        cmdline=list(ident.cmdline),
        cwd=ident.cwd or "",
        owner_pid=owner_pid,
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _watch_signals(monkeypatch):
    """Replace the reaper's signalling helper with a recorder.

    Returns the list the recorder appends to.  The reaper has exactly
    one place where a signal goes out, so "not one signal" can be
    asserted on it - the guard must not even try.
    """
    signals = []
    monkeypatch.setattr(reaper, "_signal", lambda *args: signals.append(args))
    return signals


# ---------------------------------------------------------------------------
# the owner gate
# ---------------------------------------------------------------------------

def test_a_live_owner_keeps_the_record_and_nothing_is_signalled(
    tmp_path, monkeypatch
):
    """A live owner means the record is not an orphan - it is left alone.

    The reason is ``startup_sweep`` on purpose: the reason-scoped gate
    this replaces spared the record only for ``server_shutdown``, and a
    startup sweep is the reason that took other people's executors
    down.  The owner is a real, live process; the target is a real
    child.  Nothing may be signalled, and the sweep has to say why it
    did nothing.
    """
    owner = _spawn(["sleep", "30"], cwd=tmp_path)
    target = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        assert owner.pid != os.getpid(), (
            "the owner must be a different, live process than the reaper"
        )
        _record_for(tmp_path, "executor", target, owner_pid=owner.pid)
        signals = _watch_signals(monkeypatch)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0
        )

        assert signals == [], (
            f"the sweep signalled {signals!r}; a live owner means the "
            "record belongs to a backend that is still running"
        )
        assert target.poll() is None, (
            f"the sweep signalled pid {target.pid}, whose owner "
            f"({owner.pid}) is still alive"
        )
        assert outcome.reaped == [], (
            f"the sweep reaped {outcome.reaped!r}; a live owner means the "
            "record is not an orphan"
        )
        assert outcome.skipped == [{
            "pid": target.pid,
            "role": "executor",
            "reason": "owner_alive",
        }], (
            "the sweep must report the record it left alone, with the "
            f"reason 'owner_alive'; it reported {outcome.skipped!r}"
        )
    finally:
        _cleanup(owner, target)


def test_a_dead_owner_leaves_the_record_to_be_reaped(tmp_path):
    """A dead owner means an orphan - the sweep collects it.

    The owner is killed *and waited for*, so its pid is gone rather
    than a zombie: a zombie still answers ``kill(pid, 0)``, and the
    gate would read it as a live owner.
    """
    owner = _spawn(["sleep", "30"], cwd=tmp_path)
    target = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        owner.kill()
        owner.wait(timeout=10)
        _record_for(tmp_path, "executor", target, owner_pid=owner.pid)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0
        )

        assert outcome.skipped == [], (
            f"a dead owner is an orphan, not a protected record; the "
            f"sweep skipped {outcome.skipped!r}"
        )
        assert target.pid in outcome.reaped, (
            f"the sweep must reap the orphan; it reaped {outcome.reaped!r}"
        )
        target.wait(timeout=10)
        assert target.poll() is not None, (
            "the orphan was still running after the sweep"
        )
    finally:
        _cleanup(owner, target)


def test_no_owner_and_an_orphaned_process_is_reaped(tmp_path, monkeypatch):
    """No owner and a parent of init - the process is an orphan.

    The kernel's answer is stubbed with the one it gives for an
    orphaned process: a parent of 1.  Nobody is serving this record,
    so the sweep collects it.
    """
    target = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_for(tmp_path, "executor", target, owner_pid=0)
        monkeypatch.setattr(
            reaper.process_identity, "parent_pid", lambda pid: 1
        )

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0
        )

        assert outcome.skipped == [], (
            f"a process whose parent is init is an orphan; the sweep "
            f"skipped {outcome.skipped!r}"
        )
        assert target.pid in outcome.reaped, (
            f"the sweep must reap the orphan; it reaped {outcome.reaped!r}"
        )
        target.wait(timeout=10)
        assert target.poll() is not None
    finally:
        _cleanup(target)


def test_no_owner_and_a_living_parent_keeps_the_record(tmp_path):
    """No owner, but the kernel says the parent is alive - leave it.

    The parent here is the real one: the target is a child of this
    test process, and this process is alive.  Nothing is stubbed, so
    the test also proves that :func:`process_identity.parent_pid` asks
    the kernel and gets a usable answer.
    """
    target = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_for(tmp_path, "executor", target, owner_pid=0)
        parent = process_identity.parent_pid(target.pid)
        assert parent == os.getpid(), (
            f"the test's child must be this process's child; the kernel "
            f"says its parent is {parent!r}"
        )

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0
        )

        assert target.poll() is None, (
            f"the sweep signalled pid {target.pid}, whose parent "
            f"({parent}) is still alive"
        )
        assert outcome.reaped == [], (
            f"the sweep reaped {outcome.reaped!r}; a living parent owns "
            "the process"
        )
        assert outcome.skipped == [{
            "pid": target.pid,
            "role": "executor",
            "reason": "parent_alive",
        }], (
            "the sweep must report the record it left alone, with the "
            f"reason 'parent_alive'; it reported {outcome.skipped!r}"
        )
    finally:
        _cleanup(target)


def test_no_owner_and_an_unreadable_parent_keeps_the_record(
    tmp_path, monkeypatch
):
    """No owner, and the kernel's answer could not be read - leave it.

    ``parent_pid`` returning ``None`` means "could not be determined",
    not "nobody owns it".  Treating it as an orphan is how a sweep
    kills a stranger, so the record is left alone and the sweep says
    that it does not know.
    """
    target = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_for(tmp_path, "executor", target, owner_pid=0)
        monkeypatch.setattr(
            reaper.process_identity, "parent_pid", lambda pid: None
        )
        signals = _watch_signals(monkeypatch)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0
        )

        assert signals == [], (
            f"the sweep signalled {signals!r}; an owner that cannot be "
            "determined must never be guessed at"
        )
        assert target.poll() is None, (
            f"the sweep signalled pid {target.pid}, whose owner could "
            "not be determined"
        )
        assert outcome.reaped == [], (
            f"the sweep reaped {outcome.reaped!r}; an unknown owner is "
            "not an orphan"
        )
        assert outcome.skipped == [{
            "pid": target.pid,
            "role": "executor",
            "reason": "owner_unknown",
        }], (
            "the sweep must report the record it left alone, with the "
            f"reason 'owner_unknown'; it reported {outcome.skipped!r}"
        )
    finally:
        _cleanup(target)


# ---------------------------------------------------------------------------
# the gate holds for every reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "reason",
    ["server_shutdown", "startup_sweep", "stop", ""],
    ids=["server_shutdown", "startup_sweep", "stop", "no_reason"],
)
def test_a_live_owner_is_never_signalled_whatever_the_reason(
    tmp_path, monkeypatch, reason
):
    """The gate is not scoped to a reason - no sweep may signal it.

    This is the regression pin for the whole fix: the old gate fired
    only for ``server_shutdown``, so a startup sweep - the reason that
    actually took other people's executors down - signalled whatever
    it found.  Every tree-wide sweep has to leave a record with a live
    owner alone, and not one signal may go out.

    The forced start is not one of these reasons, and the next case
    says why.
    """
    owner = _spawn(["sleep", "30"], cwd=tmp_path)
    target = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_for(tmp_path, "executor", target, owner_pid=owner.pid)
        signals = _watch_signals(monkeypatch)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason=reason, grace_seconds=2.0
        )

        assert signals == [], (
            f"a {reason!r} sweep signalled {signals!r}; a record whose "
            "owner is alive belongs to a backend that is still running"
        )
        assert target.poll() is None, (
            f"the {reason!r} sweep signalled pid {target.pid}, whose "
            f"owner ({owner.pid}) is still alive"
        )
        assert outcome.reaped == [], (
            f"the {reason!r} sweep reaped {outcome.reaped!r}"
        )
        assert outcome.skipped == [{
            "pid": target.pid,
            "role": "executor",
            "reason": "owner_alive",
        }], (
            f"the {reason!r} sweep must report the record it left "
            f"alone; it reported {outcome.skipped!r}"
        )
    finally:
        _cleanup(owner, target)


# ---------------------------------------------------------------------------
# the one sweep the gate does not speak to
# ---------------------------------------------------------------------------

def test_a_forced_start_still_collects_a_record_whose_owner_is_alive(
    tmp_path
):
    """A forced start names one plan and asks for its tree back.

    The forced start is the path that clears a tree the previous
    backend left behind, and the owner-alive probe cannot tell that
    story apart from a live backend: on a host that recycles pids
    quickly it answers "alive" for an owner that died long ago.  A
    forced start that skipped on that answer would not clear the tree
    - it would report a clean sweep, start, and leave the old executor
    running next to the new one.

    So the forced start collects the record whatever the ledger says
    about its owner.  Every other reason leaves it alone; see
    ``test_a_live_owner_is_never_signalled_whatever_the_reason``.

    The signalling helper is **not** stubbed here: this case has to see
    the process actually go down, which is the whole difference between
    "the tree was cleared" and "the plan ends up with two executors".
    """
    owner = _spawn(["sleep", "30"], cwd=tmp_path)
    target = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_for(tmp_path, "executor", target, owner_pid=owner.pid)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="start_force", grace_seconds=2.0
        )

        assert outcome.skipped == [], (
            f"the forced start must not skip the record; it skipped "
            f"{outcome.skipped!r}"
        )
        assert target.pid in outcome.reaped, (
            f"a forced start must still collect the tree it was asked "
            f"for; it reaped {outcome.reaped!r} and failed "
            f"{outcome.failed!r}"
        )
        target.wait(timeout=10)
        assert target.poll() is not None, (
            "the target process was still running after the forced reap"
        )
    finally:
        _cleanup(owner, target)
