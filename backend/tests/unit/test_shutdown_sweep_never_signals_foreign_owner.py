"""A shutdown sweep must never signal a record whose owner is alive.

Why these tests exist
---------------------

The production incident: a plan's executor was recorded with an
``owner_pid`` naming a backend that was still alive and still serving
it, and the ledger carried the kill under the reason ``server_shutdown``.

The rule these tests pin is the one the tree needs: **a sweep collects
a record only when no living process is serving it.**  The owner on
record answers that when it is there; when it is not, the kernel's
current parent answers it.

The entry point under test is the *production* one —
``service_manager.reap_all_plans``, the function the backend's
lifespan shutdown calls.  The unit-level guard in
``process_reaper.reap_plan`` is covered by
``tests/unit/test_reaper_owner_guard.py``; what is pinned here is that
the guard still speaks when the sweep comes in through the door the
server actually uses.

The three cases
---------------

``test_a_live_owner_keeps_the_record``
    The ordinary shape: a record owned by a **different, living**
    process.  The sweep must leave it alone and say why.

``test_a_record_that_is_its_own_owner_is_not_collected``
    The degenerate shape: the record's ``owner_pid`` names the
    record's **own** pid.  The owner is alive, so the headline rule
    says the record is not an orphan — and the sweep used to collect
    it anyway, because the cascade rule ("an owner this sweep is
    going to kill cannot protect its children") was answered with the
    record's own pid: the record justified itself.

``test_a_dead_owner_leaves_the_record_to_be_reaped``
    The negative control: a **dead** owner makes the record an
    orphan, and the sweep must collect it.  It is what says the two
    cases above are measuring the guard and not a sweep that never
    signals anything.

Hermeticity
-----------

Real child processes and a real kernel: the gate asks the kernel
whether the owner is alive, and a mock would answer the question
instead of the kernel.
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

import process_identity  # noqa: E402
import process_ledger as ledger  # noqa: E402
import service_manager  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn_sleeper():
    """Start a real, detached child and return its ``Popen`` handle.

    ``start_new_session`` gives the child its own process group, which
    is what a detached PDT layer looks like — and the reaper refuses
    to signal the caller's own group.
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

    On macOS the launcher rewrites ``argv[0]`` shortly after the
    exec, so a reading taken the instant a process is spawned can
    spell the command line differently from one taken a moment later.
    The identity check compares command lines, so the *recorded* one
    has to be the settled one.
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
    *owner* check, and it must not be reached through a record the
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


def _skipped_reasons(reports):
    """Every skip reason the sweep reported, flattened."""
    reasons = []
    for report in reports:
        for entry in report.skipped:
            reasons.append(str(entry.get("reason")))
    return reasons


def _dead_owner():
    """A process that is gone: killed *and* waited for.

    A zombie still answers ``kill(pid, 0)``, and the gate would read it
    as a live owner, so the corpse has to be reaped.
    """
    proc = _spawn_sleeper()
    proc.kill()
    proc.wait(timeout=10)
    assert not _alive(proc.pid), "the dead owner must really be gone"
    return proc.pid


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

def test_a_live_owner_keeps_the_record(tmp_path):
    """A live owner means the record is not an orphan: leave it alone.

    ``plan-a`` holds a record whose owner is a living process; the
    sweep must not signal it, and must say why it did nothing.
    ``plan-b`` holds a record whose owner is gone — an orphan, which is
    what a sweep is for — so the sweep must collect that one.  The
    second plan is what proves the sweep was not simply idle.
    """
    plans_dir = tmp_path / "plans"
    plan_a = plans_dir / "plan-a"
    plan_b = plans_dir / "plan-b"
    plan_a.mkdir(parents=True)
    plan_b.mkdir(parents=True)

    owner = _spawn_sleeper()          # plan-a's owner: alive
    target_a = _spawn_sleeper()       # plan-a's record
    target_b = _spawn_sleeper()       # plan-b's record
    try:
        dead_owner_pid = _dead_owner()   # plan-b's owner: gone

        _write_spawn(
            plan_a, "executor", target_a,
            owner_pid=owner.pid, plan_id="plan-a",
        )
        _write_spawn(
            plan_b, "executor", target_b,
            owner_pid=dead_owner_pid, plan_id="plan-b",
        )

        reports = _sweep(plans_dir)

        # --- plan-a: the owner is alive, so the record is left alone ---
        assert target_a.poll() is None, (
            f"the sweep signalled pid {target_a.pid}, whose owner "
            f"({owner.pid}) is still alive; a record a living process is "
            "serving is not an orphan"
        )
        assert _alive(target_a.pid), (
            f"pid {target_a.pid} is gone although its owner "
            f"({owner.pid}) is still alive"
        )
        assert "owner_alive" in _skipped_reasons(reports), (
            "the sweep must report the record it left alone, with the "
            f"reason 'owner_alive'; it reported {reports!r}"
        )

        # --- plan-b: the owner is gone, so the record is an orphan ---
        target_b.wait(timeout=10)
        assert target_b.poll() is not None, (
            f"the sweep did not collect pid {target_b.pid}, whose owner "
            f"({dead_owner_pid}) is gone; a sweep that collects nothing "
            "proves nothing"
        )
    finally:
        _cleanup(owner, target_a, target_b)


def test_a_record_that_is_its_own_owner_is_not_collected(tmp_path):
    """A record whose owner is itself is not an orphan either.

    The record says: "this process is owned by this process".  The
    owner is alive, so the headline rule says the sweep must not
    signal it — and the sweep used to signal it anyway: the cascade
    rule ("an owner this sweep is going to kill cannot protect its
    children") was answered with the record's **own** pid, so the
    record justified itself.  A record may not be its own
    justification.

    ``plan-b`` carries the dead-owner record again, so this case says
    the sweep is still collecting the orphans.
    """
    plans_dir = tmp_path / "plans"
    plan_a = plans_dir / "plan-a"
    plan_b = plans_dir / "plan-b"
    plan_a.mkdir(parents=True)
    plan_b.mkdir(parents=True)

    target_a = _spawn_sleeper()       # plan-a's record — and its owner
    target_b = _spawn_sleeper()       # plan-b's record
    try:
        dead_owner_pid = _dead_owner()

        _write_spawn(
            plan_a, "executor", target_a,
            owner_pid=target_a.pid, plan_id="plan-a",
        )
        _write_spawn(
            plan_b, "executor", target_b,
            owner_pid=dead_owner_pid, plan_id="plan-b",
        )

        reports = _sweep(plans_dir)

        assert target_a.poll() is None, (
            f"the sweep signalled pid {target_a.pid}, whose owner "
            f"({target_a.pid}) is still alive; a record whose owner is "
            "alive is not an orphan"
        )
        assert _alive(target_a.pid), (
            f"pid {target_a.pid} is gone although its owner "
            f"({target_a.pid}) is still alive"
        )
        assert "owner_alive" in _skipped_reasons(reports), (
            "the sweep must report the record it left alone, with the "
            f"reason 'owner_alive'; it reported {reports!r}"
        )

        target_b.wait(timeout=10)
        assert target_b.poll() is not None, (
            f"the sweep did not collect pid {target_b.pid}, whose owner "
            f"({dead_owner_pid}) is gone; a sweep that collects nothing "
            "proves nothing"
        )
    finally:
        _cleanup(target_a, target_b)


def test_a_dead_owner_leaves_the_record_to_be_reaped(tmp_path):
    """The negative control: a dead owner turns the record into an orphan.

    This is the same fixture as the cases above, with the owner
    **dead**.  The record is then an orphan, the sweep collects it,
    and the assertion "the process is still alive" must fail — which is
    what says the cases above are measuring the guard and not a sweep
    that never signals anything.
    """
    plans_dir = tmp_path / "plans"
    plan_a = plans_dir / "plan-a"
    plan_a.mkdir(parents=True)

    target_a = _spawn_sleeper()
    try:
        dead_owner_pid = _dead_owner()
        _write_spawn(
            plan_a, "executor", target_a,
            owner_pid=dead_owner_pid, plan_id="plan-a",
        )

        _sweep(plans_dir)

        target_a.wait(timeout=10)
        assert target_a.poll() is not None, (
            f"the sweep left pid {target_a.pid} alone, but its owner "
            f"({dead_owner_pid}) is dead — the record is an orphan and "
            "the sweep must collect it; if it did not, the 'owner "
            "alive' cases above are proving nothing"
        )
    finally:
        _cleanup(target_a)
