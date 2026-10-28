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
* The child is told which directory it is in.  ``$PWD`` is an ordinary
  environment variable and is inherited like one, so a child started
  with ``cwd=X`` still carries the *parent's* ``$PWD`` unless somebody
  rewrites it.  See :func:`_child_environment` for why that matters and
  what is done about it.
* The child is also told who spawned it.  Every environment built here
  carries the ``PDT_SPAWN_*`` marker, which is what lets the orphan
  reaper attribute a stray process to an owner instead of guessing.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Any, Dict, Optional, Set, Union

from utils.process import _signallable_pgid

__all__ = ["run_bounded", "terminate_process_tree"]

logger = logging.getLogger(__name__)


#: Seconds to let a signalled group exit on SIGTERM before escalating.
DEFAULT_TERM_GRACE_SEC = 2.0

#: Seconds to wait for captured pipes to drain *after* the group is gone.
DEFAULT_DRAIN_TIMEOUT_SEC = 5.0

#: Topics already said out loud.
#:
#: The marker is best-effort, so a broken one must not turn into a log
#: line for every child a round starts.
_SPAWN_MARKER_WARNED: Set[str] = set()


def _warn_once(topic: str, message: str, *args: Any) -> None:
    """Say *message* the first time *topic* comes up -- and never again."""
    if topic in _SPAWN_MARKER_WARNED:
        return
    _SPAWN_MARKER_WARNED.add(topic)
    logger.warning(message, *args)


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

    can_signal_group = (
        _signallable_pgid(pgid) is not None and pgid != own_pgid
    )

    if can_signal_group:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        if _wait_bounded(proc, grace):
            # Leader exited; descendants may not have been.  SIGKILL the
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


def _child_environment(
    cwd: Optional[str],
    env: Optional[Dict[str, str]],
) -> Dict[str, str]:
    """The environment the child starts with: ``$PWD`` made true, spawn
    marker applied.

    ``$PWD`` is an ordinary environment variable, and it is inherited
    like one: a child started with ``cwd=X`` still carries the
    *parent's* ``$PWD`` unless somebody rewrites it.  The child's
    working directory is the kernel's answer to ``getcwd()``; ``$PWD``
    is a claim about it, and the claim is inherited along with
    everything else.

    Some shells repair the claim themselves — bash checks ``$PWD``
    against the working directory and replaces it when it does not
    match — but not all of them do, and the tools that believe the
    claim instead of asking the kernel are exactly the ones a task's
    ``test_command`` runs: a git hook, npm, poetry.  A child that was
    told to run in the project directory and was handed somebody else's
    ``$PWD`` reports a directory that is not the one it is in, and a
    hook that resolves its repository root from ``$PWD`` writes into
    the wrong tree.

    So the claim is set here, where the directory is known: ``$PWD``
    names the directory the child is in, and ``$OLDPWD`` — which would
    name the directory the child came *from*, a directory it has no
    relationship to — is dropped.

    The spawn marker goes on the same environment, for the same
    reason: this is the one place where a child's environment is
    *explicitly constructed*, so it is the one place where the marker
    can be guaranteed.  A child that reaches the orphan reaper without
    ``PDT_SPAWN_*`` cannot be attributed to an owner, and an
    unattributable process is one the reaper must not touch.

    An environment is therefore **always** returned.  ``env=None``
    means "inherit the parent's environment", which is exactly
    ``dict(os.environ)`` — the base the marker is written onto — and a
    missing ``cwd`` only means ``$PWD`` names the current directory.
    Returning ``None`` here (which this function used to do when
    neither ``cwd`` nor ``env`` was given) would mean *no* marker and
    no ``$PWD`` repair for the "no arguments at all" call site, which
    is precisely the child the reaper is supposed to be able to see.

    The caller's mapping is never modified: the child environment is a
    copy, so a caller may reuse the same mapping for a second child.
    """
    child = dict(os.environ if env is None else env)
    child["PWD"] = os.path.abspath(cwd) if cwd else os.getcwd()
    child.pop("OLDPWD", None)
    _stamp_spawn_tags(child)
    return child


def _stamp_spawn_tags(env: Dict[str, str]) -> None:
    """Stamp the PDT spawn marker onto ``env`` in place.

    The marker names the owner (pid + start time, so a reused pid
    cannot masquerade as the original) and the kind of child
    (``subprocess`` here).  Any marker already present is
    **overwritten**: a child inherits its parent's marker, and a marker
    that survived a new owner would misattribute the child to an
    ancestor that may long since be gone.

    The stamp is best effort.  If :mod:`orphan_reaper` cannot be
    imported the child is still spawned — an unmarked child is a
    blind spot for the reaper, but a command that refuses to run
    because a *diagnostic* could not be attached is a worse failure.
    The blind spot is *said* once, though: a child the reaper cannot
    see is a hole in its coverage, and a hole nobody hears about is a
    hole nobody fixes.  ``service_manager`` — the other spawn point —
    loses its marker the same way and says so the same way.
    """
    try:
        import orphan_reaper
    except Exception:  # noqa: BLE001 - a marker never blocks a spawn
        _warn_once(
            "missing-module",
            "[bounded_subprocess] orphan_reaper is not importable; "
            "children are started without a spawn marker",
        )
        return

    try:
        env.update(orphan_reaper.spawn_tags(orphan_reaper.KIND_SUBPROCESS))
    except Exception as exc:  # noqa: BLE001 - a marker never blocks a spawn
        _warn_once(
            "spawn-tags",
            "[bounded_subprocess] could not build the spawn marker (%s); "
            "children are started without one",
            exc,
        )


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

    The child is told which directory it is in: ``$PWD`` is rewritten to
    match *cwd* and ``$OLDPWD`` is dropped, so a command that believes
    the variable rather than asking the kernel is not misled.  The same
    environment carries the ``PDT_SPAWN_*`` marker, so the orphan
    reaper can attribute the child to this process.  See
    :func:`_child_environment`.
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
        env=_child_environment(cwd, env),
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