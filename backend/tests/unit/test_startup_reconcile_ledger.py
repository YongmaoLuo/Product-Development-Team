"""The startup reconcile: the recorded pid, checked against the kernel.

What these tests pin
--------------------
``plan_execution.exec_pid`` is a pid, and pids are recycled.  The boot
recovery used to ask ``os.kill(pid, 0)`` — "is anything running at this
number?" — and took the answer for a yes, so a pid the kernel had
handed to somebody else was adopted as the plan's executor and later
signalled along with it.

The reconcile asks the kernel what the pid *is* instead.  The ledger
holds the start time and the command line of every process this backend
started, and the identity check compares them against the live reading.
Three answers, three consequences:

* **match** → the executor really is ours → it is reaped and the plan
  is parked as ``stopped``;
* **gone** → the pid names no process any more → the exit is recorded,
  the pid is cleared and the plan is parked as ``stopped``;
* **mismatch** → the pid is alive but is not ours → nothing is
  signalled, the pid is cleared, the plan is parked as ``stopped`` and
  the ledger records an ``identity_mismatch`` event.

The last one matters most: a wrong kill is not recoverable, and a pid
nobody can vouch for is not a pid that may be killed.

Hermeticity
-----------
Same contract as ``tests/test_server_recovery.py``: the ``plan_execution``
rows are seeded through the per-test ``PDT_STATE_DB_PATH`` database that
``server._state_db_path()`` resolves, never the operator's live
``state.db``.  The children the tests signal are real processes, started
in their own session exactly like the detached layers of the real tree.
"""

from __future__ import annotations

import os
import subprocess
import time

import process_identity
import process_ledger as ledger
import process_reaper as reaper

from server import _execution_state, _recover_execution_states


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn(argv, cwd=None):
    """Start a real child process and return the ``Popen`` handle.

    ``start_new_session=True`` gives the child a process group of its
    own, which is what a detached PDT layer looks like.  The reaper
    refuses to signal a group that is the caller's own, so a test that
    wants the child signalled at all has to detach it.
    """
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
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


def _dead_pid():
    """Return a pid that is guaranteed not to be running.

    A pid that was just used and released, rather than a magic
    constant: the constant can name a live process on a machine whose
    ``pid_max`` is large enough, and this test is about what happens
    when the pid names *nothing*.
    """
    proc = _spawn(["sleep", "30"])
    proc.kill()
    proc.wait(timeout=10)
    return proc.pid


def _wait_for(path, timeout=10.0):
    """Wait until ``path`` exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _stable_identity(pid, tries=40, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    On macOS the framework launcher rewrites ``argv[0]`` shortly after
    the exec, so a reading taken the instant a process is spawned can
    spell the command line differently from a reading taken a moment
    later.  The identity check compares command lines, so the
    *recorded* one has to be the settled one.
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


def _record(plan_dir, role, proc, *, plan_id, create_time_delta=0.0):
    """Write a spawn record for ``proc`` into the ledger at ``plan_dir``."""
    ident = _stable_identity(proc.pid)
    assert ident is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=ident.pgid if ident.pgid else proc.pid,
        create_time=ident.create_time + create_time_delta,
        cmdline=list(ident.cmdline),
        cwd=ident.cwd or "",
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _seed_execution_row(plan_id, **fields):
    """Insert/update a ``plan_execution`` row in the hermetic test DB.

    Writes through the production repository so the row shape matches
    what ``_recover_execution_states`` reads back via
    ``ExecutionRepository.summary``.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


def _execution_row(plan_id):
    """Read the ``plan_execution`` row back, as the API would see it."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open

    conn = db_open(_state_db_path())
    try:
        row = conn.execute(
            "SELECT exec_status, exec_pid, stop_reason FROM plan_execution "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "exec_status": row[0],
        "exec_pid": row[1],
        "stop_reason": row[2],
    }


def _events(plan_dir, name):
    """Every ledger event called ``name``."""
    return [e for e in ledger.read_events(plan_dir) if e.get("event") == name]


def _seed_task_row(plan_id, task_id, status):
    """Insert one ``plan_tasks`` row in the hermetic test DB."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
            (plan_id, task_id, status),
        )
    finally:
        conn.close()


def _task_statuses(plan_id):
    """Return ``{task_id: status}`` for the plan, read back from disk."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open

    conn = db_open(_state_db_path())
    try:
        rows = conn.execute(
            "SELECT task_id, status FROM plan_tasks WHERE plan_id = ? "
            "ORDER BY task_id",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# the match: our executor, reaped
# ---------------------------------------------------------------------------

def test_recovery_kills_matching_executor_and_marks_stopped(sample_plan_factory):
    """An executor whose identity matches is reaped, and the plan stops.

    The executor outlived the backend that started it — it has a
    session of its own — and after the restart nothing owns it.  So it
    is killed rather than adopted: a half-adopted executor is worse
    than no executor at all.
    """
    plan_dir = sample_plan_factory(plan_id="reconcile-killed", phase="executing")
    plan_id = plan_dir.name
    child = _spawn(["sleep", "30"], cwd=plan_dir)
    try:
        _record(plan_dir, "executor", child, plan_id=plan_id)
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=child.pid,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        _execution_state.clear()
        actions = _recover_execution_states(plan_dir.parent)

        assert (plan_id, "killed") in actions
        # The executor is gone — it was ours, and it was reaped.
        assert child.wait(timeout=10) is not None
        assert _events(plan_dir, "killed"), "the kill was not recorded"
        # The row no longer claims a live executor.
        row = _execution_row(plan_id)
        assert row["exec_status"] == "stopped"
        assert row["exec_pid"] is None
        # The in-memory state agrees with the row.
        assert _execution_state[plan_id]["status"] == "stopped"
        assert _execution_state[plan_id]["pid"] is None
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# the process is gone
# ---------------------------------------------------------------------------

def test_recovery_clears_pid_when_process_gone(sample_plan_factory):
    """A pid that names no process is cleared and the plan stops.

    The run was interrupted by the restart, not killed by a fault, so
    the plan is parked as ``stopped`` — resumable — rather than marked
    failed.  The pid is cleared and the exit is recorded, so nothing
    reads the row as a live execution afterwards.
    """
    plan_dir = sample_plan_factory(plan_id="reconcile-gone", phase="executing")
    plan_id = plan_dir.name
    dead_pid = _dead_pid()
    _seed_execution_row(
        plan_id,
        exec_status="running",
        exec_pid=dead_pid,
        started_at="2024-01-01T00:00:00",
        project_dir=str(plan_dir / "project"),
    )
    # A task the dead executor had claimed: the owner is gone, so the
    # row is residue and goes back to the scheduler.
    _seed_task_row(plan_id, "repair-r1-11", "in_progress")

    _execution_state.clear()
    actions = _recover_execution_states(plan_dir.parent)

    assert (plan_id, "cleared") in actions
    row = _execution_row(plan_id)
    assert row["exec_status"] == "stopped"
    assert row["exec_pid"] is None
    assert _execution_state[plan_id]["status"] == "stopped"
    assert _execution_state[plan_id]["pid"] is None
    # Not the legacy "died unexpectedly" marking.
    assert _execution_state[plan_id]["stop_reason"] != "process_died_unexpectedly"
    # The exit is recorded, so the ledger stops claiming the process.
    assert _events(plan_dir, "exit")
    # The orphaned task row is handed back to the scheduler: the
    # process that owned it is gone.
    assert _task_statuses(plan_id) == {"repair-r1-11": "pending"}


# ---------------------------------------------------------------------------
# the mismatch: somebody else's pid
# ---------------------------------------------------------------------------

def test_recovery_writes_identity_mismatch_and_does_not_kill(sample_plan_factory):
    """A pid that is not the recorded process is reported, never killed.

    The recorded start time is forged, which is what a recycled pid
    looks like from the ledger's side.  The process keeps running: the
    framework does not know that it is ours, and "not known" is not a
    licence to kill.
    """
    plan_dir = sample_plan_factory(plan_id="reconcile-mismatch", phase="executing")
    plan_id = plan_dir.name
    child = _spawn(["sleep", "30"], cwd=plan_dir)
    try:
        _record(plan_dir, "executor", child, plan_id=plan_id, create_time_delta=3600.0)
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=child.pid,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        _execution_state.clear()
        actions = _recover_execution_states(plan_dir.parent)

        assert (plan_id, "mismatch") in actions
        # Still running: no signal was sent.
        assert child.poll() is None
        # The refusal is on the record, with the pid it refused.
        mismatches = _events(plan_dir, "identity_mismatch")
        assert mismatches, "the mismatch was not recorded"
        assert mismatches[0]["pid"] == child.pid
        # The pid is let go and the plan stops.
        row = _execution_row(plan_id)
        assert row["exec_status"] == "stopped"
        assert row["exec_pid"] is None
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# idempotence: the sweep got there first
# ---------------------------------------------------------------------------

def test_recovery_is_idempotent_after_sweep(sample_plan_factory, monkeypatch):
    """A sweep that already reaped the process leaves nothing to kill.

    The startup sweep and the recovery can both reach the same
    executor.  The second one must not signal a pid that has already
    been collected — and a pid that has been collected is exactly the
    pid somebody else may be holding by then.  All that is left to do
    is record the exit.
    """
    plan_dir = sample_plan_factory(plan_id="reconcile-swept", phase="executing")
    plan_id = plan_dir.name
    child = _spawn(["sleep", "30"], cwd=plan_dir)
    try:
        _record(plan_dir, "executor", child, plan_id=plan_id)
        # The sweep collected the executor before the recovery ran.
        outcome = reaper.reap_plan(
            plan_dir, plan_id, reason="startup_sweep", grace_seconds=2.0
        )
        assert outcome.reaped == [child.pid]
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=child.pid,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        signals = []
        monkeypatch.setattr(
            reaper,
            "_signal",
            lambda pid, group, sig: signals.append((pid, group, sig)),
        )

        _execution_state.clear()
        actions = _recover_execution_states(plan_dir.parent)

        assert (plan_id, "cleared") in actions
        # No second signal: the process was already collected.
        assert signals == []
        # The kill was not repeated; only the exit was added.
        assert len(_events(plan_dir, "killed")) == 1
        assert _events(plan_dir, "exit")
        row = _execution_row(plan_id)
        assert row["exec_status"] == "stopped"
        assert row["exec_pid"] is None
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# nothing to reconcile
# ---------------------------------------------------------------------------

def test_recovery_skips_when_exec_pid_is_empty(sample_plan_factory):
    """A row with no pid has nothing to reconcile.

    The reconcile is skipped, and the legacy "running without a
    process" marking still answers the row — it is a claim about a
    plan, and it is still a claim.
    """
    plan_dir = sample_plan_factory(plan_id="reconcile-empty", phase="executing")
    plan_id = plan_dir.name
    _seed_execution_row(
        plan_id,
        exec_status="running",
        started_at="2024-01-01T00:00:00",
        project_dir=str(plan_dir / "project"),
    )

    _execution_state.clear()
    actions = _recover_execution_states(plan_dir.parent)

    assert (plan_id, "skipped") in actions
    row = _execution_row(plan_id)
    assert row["exec_pid"] is None
    assert _execution_state[plan_id]["status"] == "failed"


def test_recovery_never_signals_the_backends_own_pid(sample_plan_factory, monkeypatch):
    """The backend's own pid is never a plan's executor.

    ``process_identity.matches`` refuses to match our own pid, and the
    framework never signals itself.  A row pointing at the backend is
    left alone rather than being declared a mismatch on a pid we know
    is not the plan's.
    """
    plan_dir = sample_plan_factory(plan_id="reconcile-own-pid", phase="executing")
    plan_id = plan_dir.name
    _seed_execution_row(
        plan_id,
        exec_status="running",
        exec_pid=os.getpid(),
        started_at="2024-01-01T00:00:00",
        project_dir=str(plan_dir / "project"),
    )

    signals = []
    monkeypatch.setattr(
        reaper,
        "_signal",
        lambda pid, group, sig: signals.append((pid, group, sig)),
    )

    _execution_state.clear()
    actions = _recover_execution_states(plan_dir.parent)

    assert (plan_id, "skipped") in actions
    assert signals == []
    assert _execution_state[plan_id]["status"] == "running"
