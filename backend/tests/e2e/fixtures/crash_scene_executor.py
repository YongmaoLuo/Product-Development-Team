#!/usr/bin/env python3
"""A stand-in executor that dies the way the case under test needs it to.

Why this exists
---------------
The crash forensics have to be proven on a **real** death: a real signal, a
real exit code, a real pid that the server spawned and the process ledger
backs.  A mocked return code proves nothing about the parent's fallback —
the whole point of that fallback is that it runs when the child is *gone*,
and a mock that returns ``-9`` is still a process that is very much alive.

So this file is installed through the named production seam
(``PDT_EXECUTOR_CMD``): the **server** spawns it, the server waits for it,
and the server sees the end of it exactly as it would see the end of the
real executor.

What it deliberately does not do
--------------------------------
**It writes no crash scene of its own.**  The whole point of the two
crashing modes is that the child is unable to write anything: a SIGKILL
cannot be caught, so no ``finally`` block runs, no handler fires, and the
disk keeps no trace of the process.  If this file wrote a scene, the
parent's fallback would find it, reuse it, and the case would prove
nothing about the fallback at all.  The scene these cases assert on is the
one the **parent** wrote after the child was gone.

The contract
------------
Everything is decided by the environment, so the same file serves every
case without being edited between runs:

===========================  ===============================================
``PDT_E2E_CRASH_MODE``       How the process ends.
                             ``sigkill`` → ``SIGKILL`` on itself,
                             ``exit7`` → exits with code 7,
                             ``clean`` → exits with code 0.
===========================  ===============================================

A mode this file does not know is a configuration error, and it is
reported as one (a message on stderr and a non-zero exit) rather than
quietly turning into "exited cleanly": a stand-in that did the opposite of
what it was asked to do is worse than one that stops.

This file contains no assertions and imports nothing from the application
under test.  A fixture that failed the test would make every test that
used it fail for a reason that has nothing to do with what the test is
about; a fixture that imported the code under test could not tell a broken
executor from a broken stand-in.
"""

from __future__ import annotations

import os
import signal
import sys

#: The environment variable that decides how the process ends.
ENV_CRASH_MODE = "PDT_E2E_CRASH_MODE"

#: The process is killed by a signal it cannot catch or handle.
MODE_SIGKILL = "sigkill"

#: The process exits on its own, with a code.
MODE_EXIT7 = "exit7"

#: The process exits on its own, cleanly.
MODE_CLEAN = "clean"

#: The code ``exit7`` exits with.
EXIT_CODE = 7

#: The code an unknown mode exits with (``EX_USAGE``): the run was asked
#: for something this file cannot do, and saying so is better than dying
#: quietly in a way the case under test would read as a clean exit.
UNKNOWN_MODE_EXIT_CODE = 64


def main() -> int:
    """End this process the way ``PDT_E2E_CRASH_MODE`` says, and return
    the exit code it should exit with.
    """
    mode = (os.environ.get(ENV_CRASH_MODE) or "").strip()

    if mode == MODE_SIGKILL:
        # SIGKILL cannot be caught, blocked or ignored: the kernel takes
        # the process down where it stands.  Nothing after this line
        # runs, which is the whole point of the mode.
        os.kill(os.getpid(), signal.SIGKILL)
        return 0  # unreachable: the process is already gone

    if mode == MODE_EXIT7:
        return EXIT_CODE

    if mode == MODE_CLEAN:
        return 0

    sys.stderr.write(
        f"{ENV_CRASH_MODE}={mode!r} is not a mode this executor knows; "
        f"expected one of {MODE_SIGKILL!r}, {MODE_EXIT7!r}, {MODE_CLEAN!r}\n"
    )
    return UNKNOWN_MODE_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())