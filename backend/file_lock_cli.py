"""CLI the Edit/Write hook shells out to.

The hook runs under whatever ``python3`` the sub-agent has on ``PATH``,
so this file — and everything it imports — must stay on the standard
library. It is invoked by absolute path, which puts its own directory
first on ``sys.path``; that is how ``file_lock_protocol`` resolves
without any environment setup.

Usage::

    file_lock_cli.py acquire --task <id> --path <path> [--timeout <sec>]
    file_lock_cli.py release --task <id> [--path <path>]...
    file_lock_cli.py ping

Exit codes are the entire contract, because an exit code is all a hook
can act on:

===== ==========================================================
0     granted / released / broker answered ``ping``
3     timed out waiting for another holder (the file is *busy*)
4     broker unreachable or answered something unusable
2     bad invocation (a programming error in the hook, not a
      condition the sub-agent can react to)
===== ==========================================================

The distinction between 3 and 4 is the whole point: 3 means "someone
else is editing this file, come back", 4 means "the lock layer is down".
They demand opposite handling (block the edit vs. let it through with a
warning), so collapsing them into one non-zero code would either brick
every sub-agent when the executor dies or silently drop the guarantee.

Policy lives on the hook side; this stays a pure transport. That is also
why it never prints anything on the success path — a hook's stdout is
forwarded back into the SDK's tool pipeline, and stray output there is a
real hazard.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from file_lock_protocol import (  # noqa: E402
    ACQUIRED,
    ALREADY_HELD,
    TIMED_OUT,
    LockBrokerUnavailable,
    acquire,
    release,
    request,
)

EXIT_OK = 0
EXIT_BAD_USAGE = 2
EXIT_BUSY = 3
EXIT_UNAVAILABLE = 4


def _socket_path() -> str:
    """The broker socket, from ``PDT_LOCK_BROKER``.

    An empty value is a *configuration* answer, not an error: it means
    "this workspace has no broker" (tests, CI, a standalone
    ``coding_tool`` run). Callers treat it the same as an unreachable
    broker but without the alarm.
    """
    return os.environ.get("PDT_LOCK_BROKER", "").strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="file_lock_cli")
    sub = parser.add_subparsers(dest="op", required=True)

    p_acquire = sub.add_parser("acquire")
    p_acquire.add_argument("--task", required=True)
    p_acquire.add_argument("--path", required=True)
    p_acquire.add_argument("--timeout", type=float, default=300.0)
    p_acquire.add_argument("--cwd", default=None)

    p_release = sub.add_parser("release")
    p_release.add_argument("--task", required=True)
    p_release.add_argument("--path", action="append", default=None)
    p_release.add_argument("--cwd", default=None)

    sub.add_parser("ping")

    args = parser.parse_args(argv)
    sock = _socket_path()
    if not sock:
        return EXIT_UNAVAILABLE

    try:
        if args.op == "ping":
            request(sock, {"op": "ping"}, timeout=2.0)
            return EXIT_OK

        if args.op == "acquire":
            # Canonicalise *before* the broker sees it so a relative
            # target and an absolute one land on the same lock file.
            cwd = args.cwd or os.getcwd()
            state = acquire(sock, args.task, _resolve(cwd, args.path),
                            args.timeout)
            if state == TIMED_OUT:
                return EXIT_BUSY
            if state in (ACQUIRED, ALREADY_HELD):
                return EXIT_OK
            return EXIT_UNAVAILABLE

        if args.op == "release":
            cwd = args.cwd or os.getcwd()
            targets = None
            if args.path:
                targets = [_resolve(cwd, p) for p in args.path]
            release(sock, args.task, targets)
            return EXIT_OK
    except LockBrokerUnavailable:
        return EXIT_UNAVAILABLE
    except OSError:
        # A malformed socket path (too long, not a socket) surfaces as
        # OSError rather than LockBrokerUnavailable. Same policy: the
        # lock layer is unusable, which is not the file being busy.
        return EXIT_UNAVAILABLE
    return EXIT_BAD_USAGE


def _resolve(cwd: str, path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else Path(cwd) / p)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main())
