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
    """
    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid if pid > 0 else None


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

    Two things it will not do, both of which it used to do silently:

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
