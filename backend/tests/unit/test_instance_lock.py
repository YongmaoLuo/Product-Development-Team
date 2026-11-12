"""Unit tests for the data-root instance lock (``backend/instance_lock.py``).

What is pinned here
-------------------
The guard has three jobs, and each one has a way to fail silently:

1. **One holder per root.**  A second acquire of the same root must be
   refused — and the refusal must name the holder, so the operator can
   find the process to stop.  The in-process case is pinned here
   explicitly (the kernel's own rules about two file descriptions in
   one process are not the same on every platform, so the registry is
   what makes the rule portable).
2. **A stale file must not block a new instance.**  The lock file is
   only diagnostics; the decision is made by ``flock`` alone.  A file
   left behind by a process that was ``SIGKILL``ed must therefore not
   stop the next server from starting.  This is pinned with a real
   child process that is killed, not with a hand-written file.
3. **No false positives.**  Two different roots must both come up, and
   the release/acquire cycle must not leave a root locked.

The holder JSON's field set is pinned too: a refusal that cannot say
*who* holds the root is a refusal the operator cannot act on.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from instance_lock import (
    InstanceLockHeld,
    acquire_instance_locks,
    instance_lock_paths,
)

pytestmark = [pytest.mark.unit]

BACKEND_DIR = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root(tmp_path: Path, name: str = "root") -> tuple[Path, Path]:
    """A fresh (plans_dir, state_db_path) pair under ``tmp_path``."""
    plans_dir = tmp_path / name / "plans"
    state_db = tmp_path / name / "state" / "state.db"
    plans_dir.mkdir(parents=True, exist_ok=True)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    return plans_dir, state_db


_HOLDER_SCRIPT = """
import sys, time
sys.path.insert(0, {backend_dir!r})
from instance_lock import acquire_instance_locks
locks = acquire_instance_locks(sys.argv[1], sys.argv[2])
print("READY", flush=True)
time.sleep(300)
"""


def _start_holder(plans_dir: Path, state_db: Path) -> subprocess.Popen:
    """Start a real second process that holds the lock for this root.

    A child process — not a thread — because the rule under test is
    about *another process* holding the root, and because a child's
    death is what the SIGKILL case needs.
    """
    script = _HOLDER_SCRIPT.format(backend_dir=str(BACKEND_DIR))
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(plans_dir), str(state_db)],
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line.strip() == "READY", (
        f"the holder process did not take the lock; stdout={line!r} "
        f"stderr={proc.stderr.read() if proc.stderr else ''!r}"
    )
    return proc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# The lock files' locations
# ---------------------------------------------------------------------------


def test_the_two_lock_files_sit_where_the_contract_says(tmp_path):
    """One lock in the plans tree, one next to the state database."""
    plans_dir, state_db = _root(tmp_path)

    plans_lock, state_lock = instance_lock_paths(plans_dir, state_db)

    assert plans_lock == plans_dir / ".pdt-instance.lock"
    assert state_lock == state_db.with_name("state.db.instance.lock")
    assert state_lock.parent == state_db.parent


# ---------------------------------------------------------------------------
# 1. One holder per root
# ---------------------------------------------------------------------------


def test_a_second_acquire_of_the_same_root_is_refused(tmp_path):
    """The same process cannot take the same root twice.

    The refusal names *this* process as the holder — that is the whole
    point of the in-process registry: the rule does not depend on how
    the kernel treats two file descriptions in one process.
    """
    plans_dir, state_db = _root(tmp_path)

    locks = acquire_instance_locks(plans_dir, state_db)
    try:
        with pytest.raises(InstanceLockHeld) as excinfo:
            acquire_instance_locks(plans_dir, state_db)
    finally:
        locks.release()

    message = str(excinfo.value)
    assert f"holder_pid={os.getpid()}" in message, (
        f"the refusal must name this process as the holder; got {message!r}"
    )
    assert "plans_dir=" in message
    assert "state_db=" in message
    assert "holder_started_at=" in message
    assert "holder_cwd=" in message
    assert "PDT_PLANS_DIR" in message, (
        f"the refusal must tell the operator what to do; got {message!r}"
    )


def test_a_second_process_is_refused_and_the_holder_is_named(tmp_path):
    """A second *process* is refused, and the refusal names its pid."""
    plans_dir, state_db = _root(tmp_path)

    holder = _start_holder(plans_dir, state_db)
    try:
        with pytest.raises(InstanceLockHeld) as excinfo:
            acquire_instance_locks(plans_dir, state_db, wait_sec=0)
    finally:
        _stop(holder)

    message = str(excinfo.value)
    assert f"holder_pid={holder.pid}" in message, (
        f"the refusal must name the holder's pid; got {message!r}"
    )


# ---------------------------------------------------------------------------
# 2. A stale lock file must not block a new instance
# ---------------------------------------------------------------------------


def test_a_stale_holder_file_does_not_block_a_new_instance(tmp_path):
    """A lock file nobody holds is diagnostics, not a claim.

    The decision is made by ``flock`` alone; the file's content is
    never consulted to decide anything.  A file left behind by a
    ``SIGKILL``ed server must therefore not stop the next server.
    """
    plans_dir, state_db = _root(tmp_path)
    plans_lock, state_lock = instance_lock_paths(plans_dir, state_db)

    # A holder file that no process holds — the residue of a server
    # that was killed, or of an operator's hand-made file.
    stale = json.dumps({
        "pid": 999999,
        "started_at": "2020-01-01T00:00:00+00:00",
        "cwd": "/nowhere",
        "plans_dir": str(plans_dir),
        "state_db": str(state_db),
        "project_dir": "/nowhere",
        "argv": ["stale"],
    })
    for path in (plans_lock, state_lock):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stale, encoding="utf-8")

    locks = acquire_instance_locks(plans_dir, state_db, wait_sec=0)
    try:
        # The new holder overwrites the stale diagnostics with its own.
        written = json.loads(plans_lock.read_text(encoding="utf-8"))
        assert written["pid"] == os.getpid()
    finally:
        locks.release()


def test_a_sigkilled_holder_does_not_block_the_next_instance(tmp_path):
    """A server killed with ``SIGKILL`` leaves a file and no lock.

    The kernel releases the ``flock`` when the process dies, so the
    next instance starts — the residue on disk is only a record.
    """
    plans_dir, state_db = _root(tmp_path)

    holder = _start_holder(plans_dir, state_db)
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)
    assert holder.poll() is not None

    plans_lock, _ = instance_lock_paths(plans_dir, state_db)
    assert plans_lock.exists(), (
        "the killed holder must have left its file behind — otherwise "
        "this case proves nothing about stale files"
    )

    locks = acquire_instance_locks(plans_dir, state_db, wait_sec=0)
    locks.release()


# ---------------------------------------------------------------------------
# 3. No false positives
# ---------------------------------------------------------------------------


def test_acquire_release_acquire_works(tmp_path):
    """A released root is free again — the ordered lifecycle is not hurt."""
    plans_dir, state_db = _root(tmp_path)

    first = acquire_instance_locks(plans_dir, state_db)
    first.release()
    first.release()  # idempotent

    second = acquire_instance_locks(plans_dir, state_db)
    second.release()


def test_two_different_roots_both_come_up(tmp_path):
    """Two roots are two locks — the guard does not serialise the suite."""
    plans_a, db_a = _root(tmp_path, "a")
    plans_b, db_b = _root(tmp_path, "b")

    locks_a = acquire_instance_locks(plans_a, db_a)
    locks_b = acquire_instance_locks(plans_b, db_b)
    try:
        assert not locks_a.released
        assert not locks_b.released
    finally:
        locks_a.release()
        locks_b.release()


def test_wait_sec_zero_refuses_without_waiting(tmp_path):
    """``wait_sec=0`` is a gate, not a queue: the refusal is immediate."""
    plans_dir, state_db = _root(tmp_path)

    holder = _start_holder(plans_dir, state_db)
    try:
        started = time.monotonic()
        with pytest.raises(InstanceLockHeld):
            acquire_instance_locks(plans_dir, state_db, wait_sec=0)
        elapsed = time.monotonic() - started
    finally:
        _stop(holder)

    assert elapsed < 2.0, (
        f"wait_sec=0 must not wait; the refusal took {elapsed:.2f}s"
    )


def test_a_contended_acquire_gives_up_after_the_wait(tmp_path):
    """The wait is honoured: the refusal comes after it, not before."""
    plans_dir, state_db = _root(tmp_path)

    holder = _start_holder(plans_dir, state_db)
    try:
        started = time.monotonic()
        with pytest.raises(InstanceLockHeld):
            acquire_instance_locks(plans_dir, state_db, wait_sec=1.0)
        elapsed = time.monotonic() - started
    finally:
        _stop(holder)

    assert elapsed >= 1.0, (
        f"the acquire must have waited the whole second; took {elapsed:.2f}s"
    )


# ---------------------------------------------------------------------------
# 4. The holder's diagnostics
# ---------------------------------------------------------------------------


def test_the_holder_file_carries_the_full_diagnostics(tmp_path):
    """A refusal that cannot say *who* holds the root is not actionable."""
    plans_dir, state_db = _root(tmp_path)

    locks = acquire_instance_locks(plans_dir, state_db)
    try:
        plans_lock, state_lock = instance_lock_paths(plans_dir, state_db)
        holder = json.loads(plans_lock.read_text(encoding="utf-8"))
    finally:
        locks.release()

    assert holder["pid"] == os.getpid()
    assert holder["cwd"] == os.getcwd()
    assert holder["plans_dir"] == str(plans_dir.resolve())
    assert holder["state_db"] == str(state_db.resolve())
    assert holder["argv"], "the holder's argv must be recorded"
    assert isinstance(holder["argv"], list)
    assert holder["project_dir"], "the holder's project directory must be recorded"
    # ISO8601, and parseable as such.
    from datetime import datetime
    datetime.fromisoformat(holder["started_at"])
    assert state_lock.exists()
