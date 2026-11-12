"""The sweep belongs to the backend that owns the plans root.

What went wrong
---------------
On 2026-11-12 22:26 a shutdown sweep came out of a **pytest run in a
worktree**.  That process was not a backend: it had imported the server
module, entered the lifespan, and — because the executor sets the whole
plan context into the environment — it carried the complete set of PDT
markers (``PDT_PLANS_DIR`` / ``PDT_PLAN_ID``).  So the markers could
not tell it apart from a legitimate sweeper: it looked exactly like the
backend's own descendant.

What *can* tell them apart is the lock.  The production root's instance
lock is held by the real backend, and the pytest process never held it.
So the question "is the caller the backend that owns this root?" is
answered by the lock, and this module pins that answer.

The rule
--------
``service_manager.reap_all_plans`` refuses to sweep a plans root that
this process does not hold the instance lock on.  It logs one warning
line and returns no reports, and it does not touch a single process.

Why this does not cost the orphan sweep anything
-------------------------------------------------
Orphans are swept by the process that **holds** the lock.  When the
backend dies, the kernel releases the lock together with the file
descriptor it died holding — that is what ``flock`` is for — and the
next backend takes the lock first and runs its boot sweeps afterwards.
The orphans are therefore still collected; they are just collected by
their owner instead of by a passer-by.

So the judgement here is **"who is the sweeper"**, not "is the target my
descendant".  The latter is the reaper's own identity check, and it
still runs unchanged for every record a permitted sweep collects.

Hermeticity
-----------
Real child processes and a real kernel.  The gate under test is a
decision about *this* process, and the negative control is a real
process that really has to die — a mocked ``os.kill`` would answer the
question instead of the kernel.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import instance_lock  # noqa: E402
import process_identity  # noqa: E402
import process_ledger as ledger  # noqa: E402
import service_manager  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn_sleeper():
    """Start a real, detached child and return its ``Popen`` handle.

    ``start_new_session`` gives the child its own process group, which
    is what a detached PDT layer looks like — and the reaper refuses to
    signal the caller's own group.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
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
    command line differently from one taken a moment later.  The
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


def _write_spawn(plan_dir, role, proc, *, owner_pid, plan_id):
    """Write a spawn record for ``proc`` into the ledger at ``plan_dir``.

    The identity fields come from the kernel, so the reaper's own
    identity check can vouch for the record: the gate under test is the
    *lock* gate, and it must not be reached through a record the
    identity check would refuse.
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


def _alive(pid):
    """True when ``pid`` still names a process (a zombie still does)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep(plans_dir):
    """Run the production shutdown sweep over ``plans_dir``."""
    return service_manager.reap_all_plans(
        plans_dir, reason="server_shutdown", caller="server.shutdown_sweep",
    )


def _plans_dir(tmp_path):
    """A plans root with one plan in it."""
    plans_dir = tmp_path / "plans"
    (plans_dir / "plan-a").mkdir(parents=True)
    return plans_dir


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

def test_a_sweep_without_the_lock_is_refused(tmp_path):
    """No lock, no sweep: the process it names keeps running.

    The record's owner is **this** process, so nothing but the lock
    gate stands between the record and the signal — the reaper's own
    rules would collect it.  The child therefore has to survive, and the
    sweep has to come back with nothing.
    """
    plans_dir = _plans_dir(tmp_path)
    plan_a = plans_dir / "plan-a"

    child = _spawn_sleeper()
    try:
        _write_spawn(
            plan_a, "executor", child,
            owner_pid=os.getpid(), plan_id="plan-a",
        )

        reports = _sweep(plans_dir)

        assert reports == [], (
            "a process that does not hold the plans root's instance lock "
            "must not sweep that root; it reported "
            f"{reports!r}"
        )
        assert child.poll() is None, (
            f"the sweep signalled pid {child.pid} although this process "
            "does not hold the lock on that plans root"
        )
        assert _alive(child.pid), (
            f"pid {child.pid} is gone although the sweep was refused"
        )
    finally:
        _cleanup(child)


def test_a_sweep_with_the_lock_reaps(tmp_path):
    """The negative control: the lock is what opens the gate, not a
    closed sweep.

    The same fixture, the same record, the same owner — and this time
    the lock is held.  The sweep must go through and the child must
    really die.  Without this case the case above would prove nothing:
    a sweep that never signals anything passes it too.
    """
    plans_dir = _plans_dir(tmp_path)
    plan_a = plans_dir / "plan-a"

    child = _spawn_sleeper()
    locks = None
    try:
        _write_spawn(
            plan_a, "executor", child,
            owner_pid=os.getpid(), plan_id="plan-a",
        )

        locks = instance_lock.acquire_instance_locks(
            plans_dir, tmp_path / "state.db"
        )

        reports = _sweep(plans_dir)

        assert reports != [], (
            "the holder of the plans root's lock must be able to sweep it; "
            f"the sweep reported {reports!r}"
        )
        child.wait(timeout=15)
        assert child.poll() is not None, (
            f"the sweep left pid {child.pid} running although this process "
            "holds the lock on that plans root — the gate must refuse the "
            "sweep, not the reaping"
        )
        assert not _alive(child.pid), (
            f"pid {child.pid} is still alive after a permitted sweep"
        )
    finally:
        if locks is not None:
            locks.release()
        _cleanup(child)


def test_a_sweep_after_the_release_is_refused(tmp_path):
    """The gate closes again when the lock goes away.

    The lock is not a one-way door: the backend drops it at the end of
    its shutdown, and a process that has dropped it is no longer the
    backend of that root.  So the sweep after the release must be
    refused — and a *second* child, recorded after the release, must
    survive it.
    """
    plans_dir = _plans_dir(tmp_path)
    plan_a = plans_dir / "plan-a"

    first = _spawn_sleeper()
    second = _spawn_sleeper()
    locks = None
    try:
        _write_spawn(
            plan_a, "executor", first,
            owner_pid=os.getpid(), plan_id="plan-a",
        )

        locks = instance_lock.acquire_instance_locks(
            plans_dir, tmp_path / "state.db"
        )

        held_reports = _sweep(plans_dir)
        assert held_reports != [], (
            "the holder of the lock must be able to sweep; the sweep "
            f"reported {held_reports!r}"
        )
        first.wait(timeout=15)
        assert first.poll() is not None, (
            f"the permitted sweep left pid {first.pid} running"
        )

        # --- the lock goes away ---
        locks.release()

        # A second child, recorded after the release: it is the one the
        # refused sweep must not touch.
        _write_spawn(
            plan_a, "executor", second,
            owner_pid=os.getpid(), plan_id="plan-a",
        )

        reports = _sweep(plans_dir)

        assert reports == [], (
            "after the lock is released this process is no longer the "
            "backend of that plans root, so the sweep must be refused; it "
            f"reported {reports!r}"
        )
        assert second.poll() is None, (
            f"the sweep signalled pid {second.pid} after the lock was "
            "released"
        )
        assert _alive(second.pid), (
            f"pid {second.pid} is gone although the sweep was refused"
        )
    finally:
        if locks is not None:
            locks.release()
        _cleanup(first, second)
