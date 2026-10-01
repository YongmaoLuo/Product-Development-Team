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
    """
    if proc is None:
        return None

    pid = getattr(proc, "pid", None)
    if pid is None:
        return None

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None

    # Never our own group: killing it kills the caller.
    try:
        own_pgid = os.getpgrp()
    except OSError:
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
