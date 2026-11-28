#!/usr/bin/env python3
"""A fake CLI that reports how it was run.

Why this file exists
=====================

The harness layer has exactly one spawn path, and that path has to be
observable from the outside — otherwise "the child ran under the
sandbox" is a claim nobody can check. This script is that observation
point: it stands in for a real coding CLI, records what it was handed,
and writes the record where the test can read it.

It is deliberately a *file*, not a mock: a mock would have to be
installed inside the runner, and the thing under test here is the
process the runner actually spawns — its argv, its environment, its
working directory, its stdin, and the sandbox it ended up inside.

The contract
------------

Three environment variables drive it:

``PROBE_OUT``
    Where to write the report, as one JSON document.
``PROBE_EXIT``
    The exit code to leave with. Defaults to ``0``.
``PROBE_DENY_PATH``
    Optional. A file the sandbox profile denies reading. The probe
    tries to read it and records whether the kernel let it — that is
    how a run proves it was *confined* rather than merely *marked*.

The report
----------

``argv``
    The command line, as the process received it.
``env``
    The environment, with secret-looking values redacted.
``cwd``
    The working directory.
``stdin``
    Everything that arrived on standard input, so a test can see that
    the runner wrote the prompt *and* closed the pipe.
``ppid`` / ``ppid_cmdline`` / ``ancestor_cmdlines``
    The process the probe was spawned under, and the chain above it.
    ``ppid_cmdline`` degrades to the parent's *executable path* on
    macOS, where the full command line of another process cannot be
    read without ``ps`` — and ``ps`` may not be executed from inside a
    sandbox.
``sandbox_marker``
    The value of the environment variable the sandbox wrapper sets for
    a confined run.
``sandbox_enforced``
    Whether the kernel actually denied an operation the profile
    denies. ``None`` when the probe was not told what to try.

A note on the ancestor chain
----------------------------

The chain is recorded on a best-effort basis, because the platforms
disagree about what a sandboxed child can see:

* ``ps`` is setuid root, and macOS refuses to exec a setuid binary
  from inside a sandbox — so a confined probe cannot list the process
  table at all.
* ``sandbox-exec`` replaces itself with the command it runs, so the
  wrapper's own command line is not left anywhere in the process tree
  to be found.

What survives both is the kernel's own enforcement, which is why
``sandbox_enforced`` exists and why it — not the process table — is
what a sandboxed run is proved by.
"""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

#: Where the report is written.
OUT_ENV = "PROBE_OUT"

#: The exit code the probe leaves with.
EXIT_ENV = "PROBE_EXIT"

#: The file the sandbox profile denies reading.
DENY_ENV = "PROBE_DENY_PATH"

#: The environment variable the sandbox wrapper sets for a confined run.
SANDBOX_MARKER_ENV = "PDT_SANDBOX_ACTIVE"

#: Values of variables whose name looks like a secret are replaced.
SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE
)

#: What a redacted value is replaced with.
REDACTED = "<redacted>"

#: How many ancestors are recorded at most.
MAX_ANCESTORS = 8


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _redacted_env() -> Dict[str, str]:
    """The environment, with secret-looking values replaced.

    The report is written to a file that outlives the run, so the
    values that must not outlive it do not go into it. The *names* are
    kept: which variables a run was given is part of what the test is
    checking.
    """
    out: Dict[str, str] = {}
    for key, value in os.environ.items():
        out[key] = REDACTED if SECRET_NAME.search(key) else value
    return out


def _read_stdin() -> str:
    """Everything that arrived on standard input.

    The probe reads to EOF, which is only possible because the runner
    closes the pipe after writing the prompt. A runner that forgot to
    would hang here instead of failing quietly.
    """
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def _cwd_entries() -> List[str]:
    """The names in the working directory.

    This is what lets a test see that the child ran while a resource
    was held: a file the runner's context manager prepared is in the
    directory while the child is running, and gone afterwards.
    """
    try:
        return sorted(os.listdir("."))[:50]
    except OSError:
        return []


def _denied_read(path: str) -> Optional[bool]:
    """Try to read ``path``; report whether the kernel refused.

    ``True`` means the read was denied, ``False`` means it succeeded,
    and ``None`` means the question could not be asked (no path was
    given, or the file is simply not there — a missing file is not a
    denial).
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            handle.read()
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return True
        return False
    return False


# ---------------------------------------------------------------------------
# The process tree above the probe
# ---------------------------------------------------------------------------


def _ps_table() -> Dict[int, Tuple[int, str]]:
    """The process table, as ``pid -> (ppid, command line)``.

    Empty when the table cannot be read — which is exactly what
    happens inside a macOS sandbox, where ``ps`` may not be executed.
    """
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}

    table: Dict[int, Tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2].strip())
    return table


def _proc_cmdline(pid: int) -> str:
    """One process's command line, without ``ps``.

    Linux has ``/proc``; macOS has ``proc_pidpath``, which answers with
    the executable's path rather than its arguments. Both are asked
    only as a fallback, and neither is allowed to raise.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
        if raw:
            return " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
    except (OSError, ValueError):
        pass

    if sys.platform == "darwin":
        try:
            import ctypes

            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            buffer = ctypes.create_string_buffer(4096)
            written = libproc.proc_pidpath(pid, buffer, 4096)
            if written > 0:
                return buffer.value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - platform detail
            pass
    return ""


def _ancestor_cmdlines() -> List[str]:
    """The command lines of the processes above the probe, nearest first."""
    table = _ps_table()
    if not table:
        return []

    own = table.get(os.getpid())
    if own is None:
        return []

    chain: List[str] = []
    pid = own[0]
    seen = {os.getpid()}
    for _ in range(MAX_ANCESTORS):
        if pid in seen:
            break
        seen.add(pid)
        row = table.get(pid)
        if row is None:
            break
        ppid, cmdline = row
        chain.append(cmdline)
        if ppid <= 1:
            break
        pid = ppid
    return chain


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Write the report, say one thing on stdout, and leave."""
    out_path = os.environ.get(OUT_ENV, "")
    exit_code = int(os.environ.get(EXIT_ENV, "0") or 0)

    report = {
        "argv": list(sys.argv),
        "env": _redacted_env(),
        "cwd": os.getcwd(),
        "cwd_entries": _cwd_entries(),
        "stdin": _read_stdin(),
        "ppid": os.getppid(),
        "ppid_cmdline": _proc_cmdline(os.getppid()),
        "ancestor_cmdlines": _ancestor_cmdlines(),
        "sandbox_marker": os.environ.get(SANDBOX_MARKER_ENV, ""),
        "sandbox_enforced": _denied_read(os.environ.get(DENY_ENV, "")),
    }

    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True)

    # One line on stdout, so the runner has a stream to read and the
    # test can see that the line arrived as a line.
    print(json.dumps({"probe": "ok", "argv": report["argv"]}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
