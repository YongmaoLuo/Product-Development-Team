"""Name — and reap — subprocesses that outlive the test that spawned them.

Why this exists
---------------
The `unit` shard wedges on GitHub in a way that defeats every normal
diagnostic: no step timeout, no job timeout, no post-steps, no artifact,
no log. The reason there is *no evidence at all* is structural, and it is
worth stating precisely because the shape recurs:

    pytest ... 2>&1 | tee pytest-shard.log

`tee` exits when it sees EOF on the pipe. A descendant process that
inherited the write end of that pipe and is still alive keeps the pipe
open, so **`tee` never exits, the step never completes**, the post-steps
stay `pending`, and GitHub eventually reaps the job at ~45 minutes. The
hang is not in pytest; pytest may well have exited. Nothing downstream of
the pipe can run, which is why the wedge leaves no record.

So there are two defects and this module addresses the second one:

  1. something leaks a child that outlives its test  (the culprit), and
  2. nothing notices, so the leak is invisible and unbounded.

This plugin makes (2) true no matter what (1) turns out to be. It watches
the process *group* — the same unit ``bounded_subprocess`` kills — diffs
it around every test, and reports by name anything that appeared and
survived, so a leak is attributed to the test that caused it rather than
to the shard as a whole.

Scope and safety
----------------
It only ever looks at, reports on, and kills processes in **its own
process group**, and it only kills when that group is *dedicated* — i.e.
the parent sits in a different group, which is what ``setsid`` in the CI
step guarantees. Under a plain `pytest` run in a terminal the parent
shares our group and the plugin reports without reaping, so it can never
take down an operator's shell.

Enabled explicitly (``-p ci_process_guard``), never auto-loaded.
"""

from __future__ import annotations

import faulthandler
import os
import signal
import time

__all__ = [
    "pytest_configure",
    "pytest_runtest_logstart",
    "pytest_runtest_logreport",
    "pytest_sessionfinish",
]

_STATE: dict = {
    "pgid": -1,
    "before": {},
    "nodeid": "",
    "seen": set(),
    "dedicated": False,
    "stack_dump_path": "",
}

#: Held for the lifetime of the session because ``faulthandler.register``
#: requires its ``file`` argument to stay referenced — dropping it lets
#: CPython close the descriptor and the handler then writes nowhere.
_STACK_DUMP_FILE = None

#: A test slower than this gets its group diffed on the way out. Cheap
#: enough to run on every test on Linux (a /proc walk), but there is no
#: reason to pay it for the thousands of sub-millisecond ones.
DIFF_OVER_SECONDS = float(os.environ.get("CI_PROCESS_GUARD_MIN_SECONDS", "0"))


def _pids_in_group(pgid: int) -> dict:
    """Map pid -> cmdline for every process in ``pgid``, excluding us.

    Linux is the fast path (a ``/proc`` walk, no fork). Anything else
    falls back to ``ps``, which is slower and therefore only used on
    developer machines where the guard is report-only anyway.
    """
    out: dict = {}
    self_pid = os.getpid()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == self_pid:
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    stat = fh.read()
                # Field 5 is pgrp. comm (field 2) is parenthesised and may
                # contain spaces and parens, so split after the LAST ')'.
                fields = stat[stat.rindex(b")") + 2:].split()
                if int(fields[2]) != pgid:
                    continue
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().replace(b"\0", b" ").decode(
                        "utf-8", "replace"
                    ).strip()
            except (OSError, ValueError, IndexError):
                continue
            out[pid] = cmd or f"<pid {pid}>"
        return out
    except FileNotFoundError:
        pass

    import subprocess

    # ``ps -A`` lists itself, and the probe is a child in our own group,
    # so without this the guard reports its own probe as a leak on every
    # single test. The Linux path forks nothing and never sees this.
    probe_marker = "pid=,pgid=,command="
    try:
        res = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    for line in res.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, group = int(parts[0]), int(parts[1])
        if pid == self_pid or group != pgid:
            continue
        cmd = parts[2] if len(parts) > 2 else f"<pid {pid}>"
        if probe_marker in cmd:
            continue
        out[pid] = cmd
    return out


def _report(title: str, message: str, level: str = "warning") -> None:
    print(f"::{level} title={title}::{message}", flush=True)


def _reap(pids) -> list:
    """SIGKILL the given pids — they are all in our own group by
    construction. Returns the ones we could not kill."""
    stubborn = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError:
            stubborn.append(pid)
    return stubborn


def _arm_stack_dump() -> bool:
    """Dump every thread's stack when the CI watchdog sends SIGUSR1.

    A shard that overruns its budget is usually parked in a *blocking*
    call, and ``pytest-timeout``'s thread method cannot interrupt those:
    it raises into the main thread through ``PyThreadState_SetAsyncExc``,
    which is only delivered at a bytecode boundary, so a test sitting in
    ``waitpid``/``select``/``recv`` never sees it. That is why a wedged
    shard's log ends at the last test that *started* and says nothing
    about where inside it the process stopped.

    ``faulthandler`` does not need the interpreter to reach a boundary —
    it walks the C stacks from the signal handler — so a stack dump is
    the one artefact that survives a genuinely stuck process.

    **It has to be an explicit file, not stderr.** pytest's default
    ``fd``-level capture replaces fd 2 with a per-test temp file while a
    test runs, so a dump written to stderr lands in a buffer that is
    discarded when the process is killed — the exact case this exists
    for. Measured: with stderr as the sink, a wedged run produced no dump
    at all; with the file below, the stack came through intact. (The
    file object must stay referenced for as long as the handler is
    registered, which is why it is held in a module global.)

    Returns whether the handler is armed, so ``pytest_configure`` can say
    so in the log rather than leaving the reader to guess.
    """
    global _STACK_DUMP_FILE
    if not hasattr(signal, "SIGUSR1"):  # Windows
        return False
    # Idempotent: arming twice must not leak the first file object, which
    # would leave a descriptor open for the life of the session.
    _disarm_stack_dump()
    path = os.environ.get("CI_STACK_DUMP_PATH", "pytest-stack-dump.txt")
    handle = None
    try:
        handle = open(path, "a", encoding="utf-8")
        # ``chain=False``: override any existing handler rather than
        # refusing to install. ``faulthandler.register`` raises when a
        # handler is already present and chaining was requested, and a
        # bolt-on guard must never be the reason a session fails to
        # start — so a failure here is "not armed", never an exception.
        faulthandler.register(
            signal.SIGUSR1,
            file=handle,
            all_threads=True,
            chain=False,
        )
    except (ValueError, RuntimeError, OSError):
        # Close before giving up: a handle opened and then abandoned would
        # leak for the life of the session on a path that already failed.
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return False
    _STACK_DUMP_FILE = handle
    _STATE["stack_dump_path"] = path
    return True


def _disarm_stack_dump() -> None:
    """Undo :func:`_arm_stack_dump` — unregister, then close the file.

    A signal handler and its open descriptor are process-global state, not
    a per-test resource, so a test that arms the dump has to hand the
    process back the way it found it. Without this the handler survives
    into every later test in the session.
    """
    global _STACK_DUMP_FILE
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.unregister(signal.SIGUSR1)
        except (ValueError, RuntimeError, OSError):
            pass
    if _STACK_DUMP_FILE is not None:
        try:
            _STACK_DUMP_FILE.close()
        except OSError:
            pass
    _STACK_DUMP_FILE = None
    _STATE["stack_dump_path"] = ""


def pytest_configure(config):
    try:
        pgid = os.getpgrp()
        dedicated = pgid != os.getpgid(os.getppid())
    except OSError:
        pgid, dedicated = -1, False

    _STATE["pgid"] = pgid
    _STATE["dedicated"] = dedicated
    _STATE["before"] = _pids_in_group(pgid)
    _arm_stack_dump()

    _report(
        "ci-process-guard",
        f"watching process group {pgid} "
        f"(dedicated={dedicated}, "
        f"{len(_STATE['before'])} process(es) already present, "
        f"reap={'yes' if dedicated else 'no — report only'}, "
        f"stack-dump-on-SIGUSR1="
        f"{_STATE['stack_dump_path'] or 'unavailable'})",
        level="notice",
    )


def pytest_runtest_logstart(nodeid, location):
    _STATE["nodeid"] = nodeid
    _STATE["before"] = _pids_in_group(_STATE["pgid"])
    _STATE["started"] = time.monotonic()


def pytest_runtest_logreport(report):
    """Attribute any survivor to the test that just ran.

    Diffing per test is what turns "the shard leaks" into "this test
    leaks"; without it the session-end summary can only name the process,
    which is often not enough to find the test.
    """
    if report.when != "teardown":
        return

    elapsed = time.monotonic() - _STATE.get("started", time.monotonic())
    if elapsed < DIFF_OVER_SECONDS:
        return

    after = _pids_in_group(_STATE["pgid"])
    leaked = {p: c for p, c in after.items() if p not in _STATE["before"]}
    if not leaked:
        return

    for pid, cmd in sorted(leaked.items()):
        _STATE["seen"].add((pid, cmd))
        _report(
            "ci-leaked-process",
            f"{report.nodeid} left pid {pid} alive "
            f"after {elapsed:.1f}s: {cmd[:300]}",
        )


def pytest_sessionfinish(session, exitstatus):
    survivors = _pids_in_group(_STATE["pgid"])
    _STATE["seen"].update((p, c) for p, c in survivors.items())

    if not survivors:
        _report(
            "ci-process-guard",
            f"clean exit — no process outlived the session "
            f"({len(_STATE['seen'])} leak(s) seen during the run)",
            level="notice",
        )
        return

    for pid, cmd in sorted(survivors.items()):
        _report(
            "ci-leaked-process",
            f"still alive at session end: pid {pid}: {cmd[:300]}",
        )

    if _STATE["dedicated"]:
        stubborn = _reap(survivors)
        _report(
            "ci-process-guard",
            f"reaped {len(survivors) - len(stubborn)} of "
            f"{len(survivors)} survivor(s); stubborn={stubborn}",
            level="notice",
        )
    else:
        _report(
            "ci-process-guard",
            "not reaping: this group is shared with the parent (run under "
            "`setsid` for a dedicated group)",
            level="notice",
        )
