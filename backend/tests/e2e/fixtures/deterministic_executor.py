#!/usr/bin/env python3
"""A deterministic stand-in for the executor, driven by the environment.

Why this exists
---------------
The scheduler's end-to-end tests need something to stand in for the
real executor, and the properties they need are properties of a
*process*, not of a mock:

* a **real pid**, so the scheduler records a pid that a reader can look
  up in the process table;
* a **real exit code**, so "finished" and "failed" are distinguishable
  events rather than one event with two names;
* a **real process group**, so a teardown can signal the group and
  show that everything the run started went down with it;
* an exit at a **time the test chose**, so an observation is not a
  race against a process that may or may not still be running.

None of those can be mocked into existence: a mocked pid is not a pid,
and a mocked process group is not something a signal can reach.

The contract
------------
Everything is decided by the environment, so the same file serves every
case without being edited between runs:

===========================  =========================================
``PDT_E2E_EXIT_AFTER``       Seconds to stay alive before exiting.
                             Unset (or empty) means *stay alive until
                             signalled* — the scheduler's tests need an
                             executor that is still running when they
                             look at it.
``PDT_E2E_EXIT_CODE``        The exit code to exit with.  Defaults to 0.
``PDT_E2E_SPAWN_GRANDCHILD`` ``"1"`` forks one extra, sleeping
                             grandchild into the executor's own
                             process group.
``PDT_E2E_PID_FILE``         Where to write the executor's ``pid`` and
                             ``pgid``, one line, two columns.
===========================  =========================================

The heartbeat
-------------
A line is written to the real stdout every 100 ms for as long as the
process lives.  It goes to stdout — not to a file this script keeps to
itself — because the production spawn sends the executor's stdout to
its log file, and that log is what proves the process was alive and
producing output.

A slot that comes due while the process was not scheduled is emitted
late rather than dropped.  The rate stays one line per 100 ms slot, so
the number of lines says how long the process lived without depending
on how busy the machine was.

The grandchild
--------------
``PDT_E2E_SPAWN_GRANDCHILD=1`` forks one child that does nothing but
sleep.  It stays in the executor's process group — deliberately: a
grandchild that escaped into a session of its own would survive a
group teardown, and surviving a teardown is exactly the residue the
teardown exists to remove.  It also outlives the executor, which is
what makes it residue in the first place.

What this file is not
---------------------
It contains no assertions and imports nothing from the application
under test.  A fixture that failed the test would make every test that
used it fail for a reason that has nothing to do with what the test is
about; a fixture that imported the code under test could not tell a
broken executor from a broken stand-in.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

#: The environment variable that decides how long the process lives.
#: Unset means it lives until it is signalled.
ENV_EXIT_AFTER = "PDT_E2E_EXIT_AFTER"

#: The environment variable that decides the exit code.
ENV_EXIT_CODE = "PDT_E2E_EXIT_CODE"

#: The environment variable that asks for a grandchild.
ENV_SPAWN_GRANDCHILD = "PDT_E2E_SPAWN_GRANDCHILD"

#: The environment variable that names the pid file.
ENV_PID_FILE = "PDT_E2E_PID_FILE"

#: The heartbeat period, in seconds.
HEARTBEAT_INTERVAL = 0.1

#: How long the grandchild sleeps.  Long enough to outlive any test
#: that starts it; it is ended by the teardown, not by the clock.
GRANDCHILD_SLEEP_SECONDS = 3600.0


def _read_seconds(name: str) -> Optional[float]:
    """Read *name* as a number of seconds, or ``None`` when it is unset.

    An empty value means the same thing as no value: there is no
    deadline.  A value that is neither empty nor a number is a
    misconfiguration, and it is reported as one rather than quietly
    turning into "no deadline" — a stand-in that silently does the
    opposite of what it was asked to do is worse than one that stops.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a number of seconds, got {raw!r}"
        ) from exc


def _read_exit_code() -> int:
    """Read the exit code to exit with.  Defaults to 0."""
    raw = os.environ.get(ENV_EXIT_CODE, "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_EXIT_CODE} must be an integer, got {raw!r}"
        ) from exc


def _write_pid_file(path: str) -> None:
    """Write this process's ``pid`` and ``pgid`` to *path*.

    One line, two columns, separated by whitespace.  The pid file is
    written to a temporary name and moved into place, so a reader that
    sees the file sees a whole line — never half of one.
    """
    pid = os.getpid()
    try:
        pgid = os.getpgid(0)
    except OSError:
        # The group is the process's own unless something moved it.
        pgid = pid

    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(f"{pid} {pgid}\n", encoding="utf-8")
    os.replace(temporary, target)


def _write_heartbeat(slot: float) -> None:
    """Write one heartbeat line to stdout and flush it.

    The flush is what makes the line visible to the reader while the
    process is still running; without it the line could sit in a
    buffer until the process ends, which is the opposite of what a
    heartbeat is for.
    """
    sys.stdout.write(f"heartbeat pid={os.getpid()} t={slot:.3f}\n")
    sys.stdout.flush()


def _spawn_grandchild() -> int:
    """Fork one sleeping grandchild and return its pid.

    The grandchild stays in this process's group — no ``setsid``, no
    new group — because a process that left the group would survive a
    group teardown.  Its stdout goes to ``/dev/null`` so it neither
    adds lines to the heartbeat stream nor holds a pipe open after the
    executor is gone.
    """
    if not hasattr(os, "fork"):
        raise RuntimeError(
            f"{ENV_SPAWN_GRANDCHILD} needs fork(), which this platform "
            f"does not provide"
        )

    pid = os.fork()
    if pid != 0:
        return pid

    # The grandchild.  It must not return into the executor's code
    # path, and it must not run the executor's cleanup — hence
    # ``os._exit`` rather than ``sys.exit``, which would flush the
    # parent's buffers a second time.
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.dup2(devnull, sys.stderr.fileno())
        if devnull > 2:
            os.close(devnull)
        time.sleep(GRANDCHILD_SLEEP_SECONDS)
    except BaseException:  # noqa: BLE001 — a sleeping child has no recovery
        pass
    finally:
        os._exit(0)


def _heartbeat_until(exit_after: Optional[float]) -> None:
    """Write a heartbeat every 100 ms until *exit_after* seconds pass.

    *exit_after* of ``None`` means there is no deadline: the heartbeat
    continues until the process is signalled.
    """
    start = time.monotonic()
    emitted = 0

    while True:
        elapsed = time.monotonic() - start

        # Emit every slot that has come due.  A slot that came due
        # while the process was not scheduled is emitted late rather
        # than dropped, so the number of lines does not depend on how
        # busy the machine was.
        while emitted * HEARTBEAT_INTERVAL <= elapsed:
            _write_heartbeat(emitted * HEARTBEAT_INTERVAL)
            emitted += 1

        if exit_after is not None and elapsed >= exit_after:
            return

        # Sleep until the next slot, or until the deadline — whichever
        # comes first.  Waking at the deadline is what lets the
        # deadline be noticed promptly.
        next_slot = emitted * HEARTBEAT_INTERVAL
        if exit_after is not None:
            next_slot = min(next_slot, exit_after)
        delay = next_slot - elapsed
        if delay > 0:
            time.sleep(delay)


def main() -> int:
    """Run the executor and return the exit code it should exit with."""
    exit_after = _read_seconds(ENV_EXIT_AFTER)
    exit_code = _read_exit_code()
    pid_file = os.environ.get(ENV_PID_FILE, "").strip()
    spawn_grandchild = (
        os.environ.get(ENV_SPAWN_GRANDCHILD, "").strip() == "1"
    )

    # The pid file is written first: it names the process the test is
    # looking for, and the test cannot look for the grandchild until
    # it knows which group to look in.
    if pid_file:
        _write_pid_file(pid_file)

    if spawn_grandchild:
        _spawn_grandchild()

    _heartbeat_until(exit_after)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())