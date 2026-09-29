"""Run a shell command with a timeout that actually terminates it.

Why this module exists
----------------------
``subprocess.run(cmd, shell=True, timeout=N)`` does **not** bound the
work the command does.  On timeout CPython 3.11 only calls
``Popen.kill()`` — a ``SIGKILL`` to the *direct* child.  With
``shell=True`` the direct child is ``/bin/sh``, so the real work runs
in a **grandchild that is never signalled**.  The reproduction is short::

    subprocess.run("sleep 987 & sleep 987", shell=True,
                   capture_output=True, timeout=3)
    -> TimeoutExpired raised at t=3.0s
    -> run() returned at t=3.0s
    -> surviving grandchildren: 2   (PPID=1, still running)

The caller believes the command was killed.  It was not: it was
merely abandoned, and it keeps running — keeping its CPU, its memory,
its file descriptors and any SQLite locks it holds.

This is not a theoretical hazard.  ``check_falsifiable`` executes a
task's own ``test_command``; the backend's own fixtures carry
``test_command="pytest tests/ -v"``.  Run with a ``cwd`` inside the backend's checkout
checkout, that is a **nested pytest over the whole suite** which
re-enters the codepath that spawned it.  Every level that times out
leaves its child alive, so the abandoned tree grows instead of
unwinding — and on a 4-vCPU CI runner it starves the runner agent
itself, which is why the job's own ``timeout-minutes`` never fires and
no log is ever uploaded.

The contract here is therefore: **the timeout terminates the process
group**, so a timed-out command costs one timeout's worth of wall time
and leaves nothing behind.

Usage
-----
Drop-in for the ``subprocess.run(...)`` subset these call sites need::

    completed = run_bounded(cmd, cwd=str(project_dir), timeout=60)
    if completed.returncode != 0: ...

or, when the caller distinguishes a timeout::

    try:
        completed = run_bounded(cmd, cwd=cwd, timeout=60)
    except subprocess.TimeoutExpired as exc:
        ...  # exc.output / exc.stderr carry whatever was captured

Notes
-----
* ``start_new_session=True`` is what makes ``killpg`` safe: the child
  becomes its own session and process-group leader, so signalling the
  group can never reach this interpreter.
* The group is signalled ``SIGTERM`` first and ``SIGKILL`` after a
  grace period, mirroring the three-phase shutdown the supervisor
  documents in ``the external process supervisor``.
* Pipes are drained after the kill with a bounded second wait.  A
  descendant that called ``setsid`` itself can escape the group; the
  bound is what stops its inherited pipe from blocking us forever.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any, Dict, Optional, Union

__all__ = ["run_bounded", "terminate_process_tree"]


#: Seconds to let a signalled group exit on SIGTERM before escalating.
DEFAULT_TERM_GRACE_SEC = 2.0

#: Seconds to wait for captured pipes to drain *after* the group is gone.
DEFAULT_DRAIN_TIMEOUT_SEC = 5.0


def terminate_process_tree(
    proc: subprocess.Popen,
    grace: float = DEFAULT_TERM_GRACE_SEC,
) -> None:
    """Terminate ``proc`` **and every process it started**.

    ``proc`` must have been created with ``start_new_session=True`` so
    that its process group is its own.  When it was not (or the group
    cannot be determined), this degrades to killing the direct child —
    which is still strictly better than leaving it running.

    Never raises: this runs on error paths where the original failure
    is the interesting one.
    """
    if proc.poll() is not None:
        # Leader already gone.  Descendants may still be alive and
        # holding the group; signalling the group is still correct.
        pass

    pgid: Optional[int] = None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    # Never signal our own group: that would kill the backend process running
    # this code (and, under pytest, the whole test session).
    try:
        own_pgid: Optional[int] = os.getpgrp()
    except OSError:  # pragma: no cover - exotic platform
        own_pgid = None

    can_signal_group = pgid is not None and pgid != own_pgid

    if can_signal_group:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        if _wait_bounded(proc, grace):
            # Leader exited; descendants may not have.  SIGKILL the
            # group so nothing survives the escalation.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            return

        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        _wait_bounded(proc, grace)
        return

    # No usable group (child escaped, or start_new_session was not set).
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    _wait_bounded(proc, grace)


def _wait_bounded(proc: subprocess.Popen, timeout: float) -> bool:
    """``proc.wait(timeout)`` that returns False instead of raising."""
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except (ProcessLookupError, OSError):  # pragma: no cover - defensive
        return True


def run_bounded(
    command: Union[str, list],
    *,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
    text: bool = True,
    shell: bool = True,
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SEC,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` whose ``timeout`` terminates the process tree.

    Raises :class:`subprocess.TimeoutExpired` — with whatever output was
    captured before the kill attached as ``.output`` / ``.stderr`` — so
    callers keep the ``subprocess.run`` error contract they already
    handle.

    ``shell=True`` is the default because every call site this replaces
    executes a shell pipeline authored by a task's ``test_command``.
    The shell is the thing being bounded, and it is the reason the
    timeout needs a process group to work.
    """
    # ``shell=True`` is deliberate and load-bearing, not an oversight:
    # the callers execute a task's ``test_command``, which is a shell
    # pipeline (``cd X && source Y && pytest ...``) and cannot be run as
    # an argv list.  Bandit's B602 is suppressed with that reason rather
    # than worked around by hiding the flag behind a variable — the
    # scanner should keep reporting this, and a future reader should see
    # why it was accepted.  The command string is authored by the LLM
    # task generator, i.e. the same trust level as the ``subprocess.run``
    # calls this module replaces (``agent.py``, ``executor.py``,
    # ``verification_ci_runner.py``), all of which are documented as
    # executing task-authored commands rather than provider ids.
    proc = subprocess.Popen(  # nosec B602 - task-authored pipeline, see above
        command,
        shell=shell,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        start_new_session=True,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc)
        stdout, stderr = _drain(proc, drain_timeout)
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout,
            output=stdout,
            stderr=stderr,
        )

    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _drain(proc: subprocess.Popen, timeout: float):
    """Collect whatever the pipes still hold after the group was killed.

    Bounded on purpose: a descendant that escaped the group (its own
    ``setsid``) can keep the write end open, and an unbounded
    ``communicate()`` here would re-introduce exactly the hang this
    module exists to remove.
    """
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _close_quietly(proc.stdout)
        _close_quietly(proc.stderr)
        return None, None
    except (ValueError, OSError):  # pragma: no cover - closed pipes
        return None, None


def _close_quietly(stream: Any) -> None:
    try:
        if stream is not None:
            stream.close()
    except Exception:  # pragma: no cover - defensive
        pass
