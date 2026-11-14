"""Is the round instance that owns a token still alive? (2026-11-13).

The criterion this file pins
----------------------------
A round *instance* is one run of the verification loop over one plan.
While it runs, the loop holds the plan's workflow lease
(``.verification_round.lock``) and refreshes that lease's heartbeat.

The question "is the instance that owns THIS token still alive?" has
exactly one reliable answer, and it is the lease:

  * the service-owner registry cannot answer it — the owner it records
    is the long-lived backend, so a token whose round was already
    released keeps looking alive for ever, and the next round would
    inherit it;
  * the lease can answer it — a lease still held by this token, whose
    pid is alive and whose heartbeat is fresh, is the evidence that the
    instance is running.

What this file pins:

  1. a fresh lease held by the token → alive;
  2. no lease at all → not alive (the round is over, so the caller may
     take the plan over);
  3. a lease held by a DIFFERENT token → not alive (that is somebody
     else's round);
  4. a lease whose heartbeat went stale → not alive (the holder stopped
     saying it was alive);
  5. an empty token, a token that is not a string, or no plan
     directory → not alive, and never an exception.

The function never TAKES a lease — it only reports on one.  A plan with
no lease has no live instance, so a missing lease means "the round is
over", which is what the caller needs in order to decide whether the
next round may take the plan over.

Hermeticity
------------
Everything happens inside ``tmp_path``: the lease file is the only thing
written, and it is written into the test's own plan directory.  The
only process spawned is the one-shot that produces a pid the operating
system has already taken back (the "dead pid" the stale-holder test
needs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server  # noqa: E402,F401
import verification_loop as vl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(tmp_path: Path, plan_id: str = "p1") -> Path:
    """The plan's directory — the workflow lease lives inside it."""
    path = tmp_path / "plans" / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_lease(
    plan_dir: Path,
    round_token: str,
    *,
    pid: Optional[int] = None,
    heartbeat_at: Optional[str] = None,
    record: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write the plan's workflow lease, held by ``round_token``."""
    path = vl._verification_workflow_lease_path(plan_dir)
    if record is None:
        record = {
            "round_token": round_token,
            "run_id": "run-1",
            "pid": os.getpid() if pid is None else pid,
            "started_at": datetime.now().isoformat(),
            "heartbeat_at": (
                datetime.now().isoformat()
                if heartbeat_at is None else heartbeat_at
            ),
        }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _dead_pid() -> int:
    """A pid the operating system has already handed back.

    A real process is started and waited for, so the number returned is
    one the OS has already taken back.  The assertion at the end is what
    makes that a fact rather than a hope: if the pid were somehow alive
    again, the test says so instead of probing a stranger's process.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pid = proc.pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    raise AssertionError(
        f"pid {pid} came back to life — it is not a dead pid, so this "
        f"test would be probing a live process"
    )


# ---------------------------------------------------------------------------
# The five named cases
# ---------------------------------------------------------------------------


def test_true_for_fresh_lease(tmp_path: Path) -> None:
    """A lease this token holds, freshly heartbeated → the instance lives."""
    plan_dir = _plan_dir(tmp_path)
    _write_lease(plan_dir, "verification_loop-1-abc")

    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is True


def test_false_without_lease(tmp_path: Path) -> None:
    """No lease at all → the round is over, so the instance is not alive."""
    plan_dir = _plan_dir(tmp_path)

    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is False


def test_false_for_another_token(tmp_path: Path) -> None:
    """A lease held by somebody else's token says nothing about ours."""
    plan_dir = _plan_dir(tmp_path)
    _write_lease(plan_dir, "verification_loop-2-def")

    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is False


def test_false_for_stale_heartbeat(tmp_path: Path) -> None:
    """A heartbeat older than the lease's staleness bound → not alive."""
    plan_dir = _plan_dir(tmp_path)
    stale = (datetime.now() - timedelta(seconds=600)).isoformat()
    _write_lease(plan_dir, "verification_loop-1-abc", heartbeat_at=stale)

    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is False


def test_false_for_empty_token(tmp_path: Path) -> None:
    """An empty token, or no plan directory → not alive, no exception."""
    plan_dir = _plan_dir(tmp_path)
    _write_lease(plan_dir, "verification_loop-1-abc")

    assert vl._round_instance_is_live(plan_dir, "") is False
    assert vl._round_instance_is_live(None, "verification_loop-1-abc") is False


# ---------------------------------------------------------------------------
# The boundaries around it
# ---------------------------------------------------------------------------


def test_false_for_missing_plan_dir(tmp_path: Path) -> None:
    """A plan directory that does not exist holds no live instance."""
    missing = tmp_path / "plans" / "never-created"

    assert vl._round_instance_is_live(
        missing, "verification_loop-1-abc",
    ) is False


def test_false_for_non_string_token(tmp_path: Path) -> None:
    """A token that is not a string names no instance."""
    plan_dir = _plan_dir(tmp_path)
    _write_lease(plan_dir, "verification_loop-1-abc")

    assert vl._round_instance_is_live(plan_dir, None) is False
    assert vl._round_instance_is_live(plan_dir, 123) is False


def test_false_for_dead_holder_pid(tmp_path: Path) -> None:
    """A lease whose holder died is not a live instance."""
    plan_dir = _plan_dir(tmp_path)
    _write_lease(plan_dir, "verification_loop-1-abc", pid=_dead_pid())

    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is False


def test_heartbeat_falls_back_to_the_file_mtime(tmp_path: Path) -> None:
    """A lease with no recorded heartbeat is judged by the file's age.

    The heartbeat is the answer; the file's mtime is the fallback for a
    lease whose record is unreadable — a holder killed between the
    ``O_EXCL`` create and the write leaves exactly that.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _write_lease(
        plan_dir, "verification_loop-1-abc",
        record={
            "round_token": "verification_loop-1-abc",
            "pid": os.getpid(),
        },
    )
    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is True

    old = time.time() - 600
    os.utime(path, (old, old))

    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is False


def test_false_for_unreadable_lease(tmp_path: Path) -> None:
    """A lease file that carries no record names no holder."""
    plan_dir = _plan_dir(tmp_path)
    path = vl._verification_workflow_lease_path(plan_dir)
    path.write_text("not json at all", encoding="utf-8")

    assert vl._round_instance_is_live(
        plan_dir, "verification_loop-1-abc",
    ) is False
