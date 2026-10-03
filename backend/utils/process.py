"""Shared process-management utilities.

The ``kill_process_group`` helper centralises the ``getpgid`` +
``killpg`` + ``wait`` pattern that was previously copy-pasted in
``coding_tool.py``'s Claude / Vendor C / OpenCode adapters (5 copies in
the timeout and retry paths).  Centralising it here means:

  * the SIGKILL-by-default contract is documented in one place;
  * future coding-tool backends only need to call this one function;
  * ``ProcessLookupError`` / ``PermissionError`` / ``OSError`` are
    uniformly swallowed (best-effort: the caller wants the
    subprocess gone, not an exception).
"""

from __future__ import annotations

import os
import signal
from typing import Optional

__all__ = ["kill_process_group"]


def _child_pid(proc: object) -> Optional[int]:
    """``proc``'s pid if it is a real one, else ``None``.

    The ``isinstance`` is the whole point. ``os.getpgid`` does not want an
    ``int``; it wants *anything* with ``__index__``, and CPython will
    happily call it. ``MagicMock`` configures ``__index__`` to return
    ``1``, so the old ``getattr(proc, "pid", None)`` guard let a mocked
    process through and ``os.getpgid`` answered with the group of PID 1.
    Every fallback path in ``coding_tool`` — six call sites, all reached
    with a ``MagicMock`` in the unit suite — therefore sent ``SIGKILL``
    to the init process group on its way out of a test.

    ``bool`` is excluded explicitly because ``bool`` is an ``int`` and
    ``True`` would resolve to PID 1 all over again.

    A real ``subprocess.Popen`` is unaffected: its ``pid`` is an ``int``
    from the OS, and the fastest way to prove that is the fact that
    :func:`kill_process_group` still works, which
    ``test_kill_process_group_terminates_proc`` pins against a real
    child.

    ``pid > 0`` is kept for the same reason: ``pid`` arriving here as a
    real ``int`` does not make ``1`` a plausible child pid.
    """
    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid if pid > 0 else None


def _signallable_pgid(pgid: object) -> Optional[int]:
    """``pgid`` if it names a process group we may signal, else ``None``.

    The second line of defence, behind :func:`_child_pid`. That one stops
    a *mock* from ever becoming a pid; this one stops a *value* from
    ever becoming a group, no matter how it got there. The two are not
    redundant: a ``Popen``-shaped stub with ``pid = 1`` is a real
    ``int`` and sails straight past an ``isinstance`` check.

    Everything here is about POSIX sentinels, not about tidiness:

    ``1``
        ``kill(-1, sig)`` — broadcast to every process the caller may
        signal. This is the value that reached ``os.killpg`` during the
        CI wedge.
    ``0``
        ``kill(0, sig)`` — the caller's own group.
    ``<= -1``
        glibc rejects it with ``EINVAL``; Darwin treats ``-p`` as a
        plain pid and signals that one process. Neither is a group.

    ``bool`` is excluded for the same reason :func:`_child_pid` excludes
    it: ``True`` and ``False`` are ``int`` instances, and they are ``1``
    and ``0`` — the two sentinels above.

    ``os.getpgid`` only ever returns a positive int or raises, so on the
    current call path this is defence in depth rather than a live bug
    fix. It is here so that the next refactor of the pid path cannot
    quietly reopen the hole, and so the other three ``os.killpg`` call
    sites in this repo have one documented predicate to copy.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int):
        return None
    return pgid if pgid > 1 else None


def kill_process_group(
    proc: Optional[object],
    *,
    sig: int = signal.SIGKILL,
    wait_timeout: float = 5.0,
) -> Optional[int]:
    """Best-effort terminate a subprocess's entire process group.

    Sends ``sig`` to the process group that ``proc`` belongs to and
    waits up to ``wait_timeout`` seconds for the process to exit.
    Returns the exit code if the process terminated within the
    timeout, else ``None``.

    This is a no-op when ``proc`` is ``None`` or already dead —
    every error class returned by the underlying POSIX calls is
    swallowed so a stuck subprocess cannot crash a long-running
    scheduler.  The caller already has a fallback path (timeout
    error, retry, etc.) so propagating these exceptions would only
    mask the real cause.

    Three things it will not do, all of which it used to do silently:

    * **Signal a group it was never given.** ``os.getpgid`` accepts any
      object implementing ``__index__``, and ``MagicMock`` implements it
      — returning ``1``. A ``MagicMock`` standing in for a subprocess
      therefore resolved "the child's group" to the group of PID 1, and
      this function ``SIGKILL``ed it. Every provider-fallback path in
      ``coding_tool`` reaches here with a mocked process, so the suite
      aimed a kill at init on the way past. :func:`_child_pid` requires
      a genuine ``int``, which turns each of those into a no-op.
    * **Signal its own group.** A child spawned *without*
      ``start_new_session=True`` sits in the caller's group, and killing
      that group kills the caller — under pytest, the whole session.
      ``bounded_subprocess`` already refuses this; this helper did not.
    * **Signal a sentinel.**  See :func:`_signallable_pgid`.

    Why the sentinel guard is not redundant with the two above — this
    is the part worth knowing before "simplifying" it away:

        os.killpg(p, sig)   ==   kill(-p, sig)

    so ``p`` is not a group id in the ordinary sense. Two values are
    reserved by POSIX and neither names a group you may signal:

    ==================  ==================================  ==============
    ``p``               ``kill(-p, sig)`` means            safe to call?
    ==================  ==================================  ==============
    ``1``               every process the caller may       NO — lethal
                        signal, same uid, except itself
    ``0``               the caller's *own* process group   NO
    ``< 0``             (glibc: EINVAL; Darwin: a plain    NO
                        signal to the process ``-p``)
    ==================  ==================================  ==============

    ``p == 0`` is *not* covered by the own-group comparison above: a
    real ``own_pgid`` is whatever the kernel handed us (70961 on a dev
    laptop), so it can never equal the sentinel ``0``. It has to be
    rejected as a value, not as an equality.

    ``p == 1`` is the one that actually shipped the incident this guard
    now prevents, and **the consequence is platform-dependent in a way
    that hides the bug from developers**:

    =============  ==================================  ==================
    Platform       ``killpg(1, sig)``                 Result
    =============  ==================================  ==================
    Linux / glibc  no special case; becomes           broadcasts.
                   ``kill(-1, sig)``                   Runner.Worker,
                                                        step shell, and
                                                        the whole CI job
                                                        die.
    macOS / Darwin ``libc`` returns ``EPERM`` from     harmless no-op.
                   ``pgid == 1`` on its first line;    The bug cannot
                   the kernel is never called.         reproduce on a
                                                        developer machine.
    =============  ==================================  ==================

    So the same line is a live grenade in CI and a dud on the laptop
    that owns the code — which is why this sat in the tree for a whole
    release without a single local failure. Do not "fix" a local repro
    that cannot fail: the absence of a macOS symptom is the trap, not
    the all-clear.
    """
    if proc is None:
        return None

    pid = _child_pid(proc)
    if pid is None:
        return None

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    else:
        pgid = _signallable_pgid(pgid)

    # Never our own group: that is the caller, and everything it owns.
    # Signalling it would take down the backend — or, under pytest, the
    # whole session, which is a failure with no trace of its cause.
    try:
        own_pgid: Optional[int] = os.getpgrp()
    except OSError:  # pragma: no cover - exotic platform
        own_pgid = None
    if own_pgid is not None and pgid == own_pgid:
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        return proc.wait(timeout=wait_timeout)
    except Exception:
        return None
