"""Integration spec for the ``.mcp.json`` restore on every way a run ends.

What is pinned here
-------------------

``mcode exec`` reads its MCP servers from one place only: the
``.mcp.json`` in the workspace root. The adapter writes that file for
the length of the run and takes it away again afterwards, because a
file the adapter writes into somebody else's working tree is a file
that can end up in a commit, in a diff, or in a diff of a diff.

The restore is the half of that contract that fails quietly. It has to
happen on *every* way a run can end, and it has to happen before the
framework commits the task's working tree. The cases pinned here are:

1. **IT-RUN-04 — the file is there for the run, and gone after it.**
   What the child saw is checked from the inside: the probe hashes
   the file while the run lasts.
2. **IT-RUN-05 — a non-zero exit still restores.** The run failed; the
   workspace is not the run's to leave dirty. The verdict on the exit
   is the adapter's to make, and the runner does not second-guess it.
3. **IT-RUN-06 — a run that is cut short still restores.** The probe
   hangs and the deadline is enforced from the outside; the restore
   runs, no process is left behind, and no descriptor is leaked.
4. **IT-RUN-07 — a SIGTERM to the runner's process group still
   restores.** The child is in a group of its own, so the signal does
   not reach it: the runner's own guard does. The run is cut short,
   the workspace comes back byte for byte with the mode it had, and
   the child is gone.
5. **IT-RUN-08 — an exception inside the adapter still restores, and
   the exception is not swallowed.**
6. **IT-RUN-09 — the file never reaches a commit.** The framework's
   auto-commit runs after the run; the tree it produces has no
   ``.mcp.json`` in it, and neither does the object database.

The observation point
--------------------

The child is a real process: a small probe script stands in for a
coding CLI, records what the workspace looked like while it ran, and
writes the record where this spec can read it. Nothing is stubbed —
the thing under test is the spawn, the way the run ends, and the file
that is left behind.

Two notes on the shape of the cases
-----------------------------------

**The runner has no timeout of its own.** The deadline is the
caller's to enforce, so IT-RUN-06 enforces it here — the way the
framework's own supervisor does — and then asserts what the runner
owes once the child has been killed.

**A SIGTERM cannot be delivered to this process.** Signalling the
group the test process is in would signal pytest. So IT-RUN-07 runs
the runner in a process of its own, spawned into its own session, and
signals that process's group. The harness child is spawned with
``start_new_session=True`` — it is in a group of its own — so the
signal does not reach it. The two group ids are compared, so the case
proves that the restore came from the runner's own guard and not from
the group signal reaching the child by accident.

**The driver installs no signal handler.** That is the point of the
case. A restore that only happens because the caller remembered to
install a trap is not one the framework can rely on: the production
chain stops a run by signalling a process group, and the process it
signals has no handler of its own — so with nothing installed, the
default disposition of ``SIGTERM`` would end the process where it
stands and leave the file the adapter wrote behind in the workspace.
The driver reports the disposition it started with, so the case can
prove that what put the workspace back was the runner and not the
case's own discipline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from harness.claude import ClaudeHarness
from harness.contract import (
    HarnessInvocation,
    OUTCOME_MODEL_ERROR,
    OUTCOME_OK,
    OUTCOME_SIGNALED,
    OUTCOME_UNKNOWN,
)
from harness.mcode import McodeHarness
from harness.runner import Runner

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# What the workspace looks like
# ---------------------------------------------------------------------------

#: The file the mcode adapter writes for the length of a run.
MCP_FILENAME = ".mcp.json"

#: The MCP configuration the run is given.
CONFIG: Dict[str, Any] = {
    "mcpServers": {"recovery-probe": {"command": "true", "args": ["--flag"]}}
}

#: The bytes that configuration is written as. The adapter serialises
#: it as one line of JSON; the expectation is computed here rather
#: than read back from the adapter, so the check is independent of
#: the code that does the writing.
EXPECTED_BYTES = json.dumps(CONFIG, ensure_ascii=False).encode("utf-8")

#: The hash of those bytes — what the probe must see while it runs.
EXPECTED_SHA256 = hashlib.sha256(EXPECTED_BYTES).hexdigest()

#: What the workspace's ``.mcp.json`` said before the run.
ORIGINAL_BYTES = b'{"mcpServers": {"before": {}}}\n'

#: The mode it carried before the run.
ORIGINAL_MODE = 0o600

#: The name a replayed restore is recorded under.
#:
#: It is spelled out here rather than imported, because the name *is*
#: the contract: the same string is written to the log and carried by
#: the event, so that one grep finds the evidence of either.
RESTORE_EVENT_NAME = "mcp_restore_replayed"

#: The credential the injected configuration carries — a **marker**, not
#: a secret: a made-up string that stands in for one.
#:
#: It is what must not survive in the workspace after the crash, and
#: what must never appear in the recovery journal — the journal is
#: read by whoever starts the next process.
#:
#: The name says ``MARKER`` and not ``CREDENTIAL`` on purpose.  A
#: credential-looking *name* next to a credential-looking *value* is
#: what the committed-history secret scanner reads as a leaked key, and
#: the repository publishes its commits: this marker is in every clone.
#: The name describes what the value is — a marker — and the value stays
#: a made-up string that no credential-shaped rule has any business
#: matching.
SENTINEL_MARKER = "SENTINEL-VP031-XYZ"

#: The configuration the crashed run injects. It carries the sentinel,
#: so the file the adapter writes into the workspace holds it.
CREDENTIAL_CONFIG: Dict[str, Any] = {
    "mcpServers": {
        "recovery-probe": {
            "command": "true",
            "args": ["--flag"],
            "env": {"MCP_TOKEN": SENTINEL_MARKER},
        }
    }
}

#: How long the run is allowed to hang before the deadline is
#: enforced. Long enough that the probe is certainly running, short
#: enough that the case does not slow the lane down.
DEADLINE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# The probe — the child that stands in for a coding CLI
# ---------------------------------------------------------------------------

PROBE_SOURCE = r'''#!/usr/bin/env python3
"""A probe that records what the workspace looked like while it ran.

What it does, in order:

1. writes its own pid to ``PROBE_PID_FILE`` — the case needs the pid
   to find the child, to end it, and to prove that nothing was left
   behind;
2. hashes the ``.mcp.json`` in its working directory and writes the
   hash to ``PROBE_REPORT`` — that is the "the file was there, for the
   run, with these bytes" observation, taken from inside the run;
3. writes ``work.txt`` — a file the run produced, so the auto-commit
   has something of the run's own to commit;
4. prints one line, so the runner has a stream to read;
5. hangs forever when ``PROBE_MODE`` says ``hang``.
"""

import hashlib
import json
import os
import sys
import time


def main():
    report_path = os.environ["PROBE_REPORT"]
    pid_file = os.environ["PROBE_PID_FILE"]
    mode = os.environ.get("PROBE_MODE", "ok")
    exit_code = int(os.environ.get("PROBE_EXIT", "0") or "0")

    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

    target = os.path.join(os.getcwd(), ".mcp.json")
    record = {
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "mcp_json_exists": os.path.exists(target),
        "mcp_json_sha256": None,
    }
    try:
        with open(target, "rb") as handle:
            record["mcp_json_sha256"] = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        pass

    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)

    with open(os.path.join(os.getcwd(), "work.txt"), "w", encoding="utf-8") as handle:
        handle.write("the run did something\n")

    print(json.dumps({"probe": "ok"}, sort_keys=True), flush=True)

    if mode == "hang":
        while True:
            time.sleep(3600)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
'''


# ---------------------------------------------------------------------------
# The driver — the runner in a process of its own, for the SIGTERM case
# ---------------------------------------------------------------------------

DRIVER_SOURCE = r'''#!/usr/bin/env python3
"""Run one harness run in a process of its own, and report what happened.

Why this file exists
====================

A SIGTERM has to be delivered to the *runner's* process group. Under
pytest that cannot be done to the test process itself: the runner
would be the test process, and signalling its group would signal
pytest.

So the run happens in a process of its own, spawned into its own
session. The signal is delivered to that process's group, and the
process reacts to it through the runner's own guard.

The missing handler is the point of the exercise. This driver
installs none: it starts with the platform's default disposition for
SIGTERM, and it reports that disposition so the case can prove it. A
restore that only happens because the *caller* remembered to install a
trap is not a restore the framework can rely on — the production chain
stops a run by signalling a process group, and the process it signals
has no handler of its own.

What it reports
---------------

``sigterm_disposition_at_start``
    What this process had for SIGTERM before it did anything.
``outcome_kind`` / ``exit_code``
    The verdict the runner produced, and the code it came from.
``driver_pgid`` / ``child_pgid``
    The two process groups. They differ — that is the proof that the
    group signal did not reach the child by accident.
``before`` / ``after``
    What the workspace's ``.mcp.json`` looked like before the run and
    after it.
``error`` / ``interrupted_signum``
    The exception, if the run raised one, and the signal it carries.
"""

import hashlib
import json
import os
import signal
import sys

sys.path.insert(0, sys.argv[1])

from harness.contract import HarnessInvocation
from harness.mcode import McodeHarness
from harness.runner import HarnessInterrupted, Runner

OPTIONS = json.loads(sys.argv[2])

TARGET = os.path.join(OPTIONS["workspace"], ".mcp.json")


def _fingerprint(path):
    """What is at ``path``: nothing, or something with these bytes."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return {"exists": False, "sha256": None}
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _disposition_at_start():
    """What this process had for SIGTERM before it did anything.

    ``SIG_DFL`` means no Python handler was installed here — which is
    the whole point of the driver.
    """
    handler = signal.getsignal(signal.SIGTERM)
    if handler == signal.SIG_DFL:
        return "SIG_DFL"
    if handler == signal.SIG_IGN:
        return "SIG_IGN"
    return repr(handler)


report = {
    "driver_pgid": os.getpgid(0),
    "child_pgid": None,
    "before": _fingerprint(TARGET),
    "error": None,
    "interrupted_signum": None,
    "outcome_kind": None,
    "exit_code": None,
    "sigterm_disposition_at_start": _disposition_at_start(),
}


def _finish(code):
    """Write what happened, and leave with ``code``.

    The report is written on every way out of the run, the interrupted
    one included: a driver that died without writing anything would
    leave the case with no evidence at all.
    """
    report["after"] = _fingerprint(TARGET)
    try:
        with open(OPTIONS["probe_report"], "r", encoding="utf-8") as handle:
            report["child_pgid"] = json.load(handle).get("pgid")
    except (OSError, ValueError):
        pass
    with open(OPTIONS["report"], "w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True)
    sys.exit(code)


adapter = McodeHarness()
invocation = HarnessInvocation(
    argv=(sys.executable, OPTIONS["probe"]),
    env_delta=dict(OPTIONS["env"]),
    cwd=OPTIONS["workspace"],
    prepare=lambda: adapter.mcp_prepare(OPTIONS["workspace"], OPTIONS["config"]),
)

try:
    result = Runner().run(adapter, invocation)
    report["outcome_kind"] = result.outcome.kind
    report["exit_code"] = result.exit_code
except HarnessInterrupted as exc:
    # The signal was turned into an interruption by the runner's own
    # guard. The exit code is the one the CLI leaves with.
    report["error"] = repr(exc)
    report["interrupted_signum"] = exc.signum
    _finish(128 + exc.signum)
except BaseException as exc:
    report["error"] = repr(exc)
    _finish(1)

_finish(0)
'''


# ---------------------------------------------------------------------------
# The driver that dies where it stands
# ---------------------------------------------------------------------------

CRASH_DRIVER_SOURCE = r'''#!/usr/bin/env python3
"""A driver that is killed with the adapter's file in place.

Why this driver exists
======================

Every way a run can end *cleanly* ends in the adapter's restore: a
normal return, an exception, a SIGTERM that the runner's own guard
turns into an interruption. The restore is the adapter's promise that
the workspace is handed back.

A SIGKILL is not one of those ways. It cannot be caught at all: the
process stops between the write and the restore, and the workspace
keeps the file the adapter wrote — a credential — where the user's own
file was. The user's own file is gone.

So the driver enters exactly the context the runner enters — the
adapter's ``mcp_prepare`` — and then waits to be killed. It spawns no
child, so nothing is left running when it dies, and the file it leaves
behind is the adapter's doing and not the runner's.

What it reports
---------------

``pid``
    The pid it wrote into the pid file, which is the pid that owns the
    recovery journal: the replay must not touch a workspace whose owner
    is still alive.
"""

import json
import os
import sys
import time

sys.path.insert(0, sys.argv[1])

from harness.mcode import McodeHarness

OPTIONS = json.loads(sys.argv[2])

adapter = McodeHarness()

with adapter.mcp_prepare(OPTIONS["workspace"], OPTIONS["config"]):
    with open(OPTIONS["pid_file"], "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    while True:
        time.sleep(3600)
'''


# ---------------------------------------------------------------------------
# An adapter that fails inside itself
# ---------------------------------------------------------------------------


class _ExplodingAdapter(McodeHarness):
    """An adapter whose own parser fails the moment it reads.

    The failure is inside the adapter, which is where an adapter's
    failures are: it is the adapter's own code that raises, and the
    runner is what has to notice it, end the child, and let the error
    out rather than swallow it.
    """

    def parse_stream(self, lines):
        """Read one line, then fail.

        The first line is read on purpose: the probe writes its pid
        file before it prints, so by the time a line arrives the child
        is known and the case can assert that nothing was left behind.
        """
        for _ in lines:
            break
        raise RuntimeError("the adapter blew up while reading the stream")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_up_after_every_case(tmp_path):
    """No case leaves a directory lying around.

    The children are waited for inside the cases themselves — that is
    what the "no orphan" half of the contract is — so by the time this
    runs, the workspace is free of anything that holds it open and can
    be taken down.
    """
    yield
    for leftover in tmp_path.iterdir():
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)


def _no_sandbox(monkeypatch, tmp_path: Path) -> None:
    """Make sure no sandbox is configured for the run.

    A developer's own seatbelt profile would be applied to the probe,
    and the case would then be testing the profile rather than the
    restore.
    """
    monkeypatch.delenv("PDT_SANDBOX_PROFILE", raising=False)
    empty_config = tmp_path / "empty-config"
    empty_config.mkdir(exist_ok=True)
    monkeypatch.setenv("PDT_CONFIG_DIR", str(empty_config))


def _workspace(tmp_path: Path, name: str = "workspace") -> Path:
    """An empty workspace directory."""
    workspace = tmp_path / name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _with_original_mcp(workspace: Path, mode: int = ORIGINAL_MODE) -> bytes:
    """Put a ``.mcp.json`` in the workspace, and return what it says."""
    target = workspace / MCP_FILENAME
    target.write_bytes(ORIGINAL_BYTES)
    os.chmod(target, mode)
    return ORIGINAL_BYTES


def _write_probe(tmp_path: Path) -> Path:
    """Write the probe script, and return where it landed."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE_SOURCE, encoding="utf-8")
    return probe


def _write_crash_driver(tmp_path: Path) -> Path:
    """Write the driver that dies where it stands, and return its path."""
    driver = tmp_path / "crash-driver.py"
    driver.write_text(CRASH_DRIVER_SOURCE, encoding="utf-8")
    return driver


def _start_crash_driver(
    tmp_path: Path,
    workspace: Path,
    driver: Path,
    prefix: str = "crash",
) -> Tuple[Any, int]:
    """Start the driver and wait until the file is in place.

    Args:
        tmp_path: The case's scratch directory.
        workspace: The workspace the run happens in.
        driver: The driver script.
        prefix: What this run's files are named after.

    Returns:
        The running process and the pid the driver wrote.
    """
    pid_file = tmp_path / f"{prefix}-driver.pid"
    options = {
        "workspace": str(workspace),
        "pid_file": str(pid_file),
        "config": CREDENTIAL_CONFIG,
    }

    process = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(Path(__file__).resolve().parents[3]),
            json.dumps(options),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    # The file is in place only once the driver says so: the pid file
    # is written inside the adapter's context, after the write.
    _wait_for_file(pid_file, timeout=30.0)
    return process, _read_pid(pid_file)


def _kill_crash_driver(process: Any) -> Dict[str, Any]:
    """SIGKILL the driver where it stands, and wait for it.

    The driver is spawned into a session of its own, so the signal goes
    to its own process group and to nothing else — there is no child to
    leave behind. The process is waited for afterwards, so its pid is
    gone from the process table rather than lingering as a zombie: a
    zombie still answers a liveness probe, and the replay must be able
    to tell a dead owner from a live one.

    Args:
        process: The running driver.

    Returns:
        What the kill left behind: the exit code and what the driver
        said on its standard error.
    """
    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    _stdout, stderr = process.communicate(timeout=30)
    return {"exit_code": process.returncode, "stderr": stderr}


def _invocation(
    workspace: Path,
    probe: Path,
    report: Path,
    pid_file: Path,
    exit_code: int = 0,
    mode: str = "ok",
) -> HarnessInvocation:
    """The invocation the runner is handed.

    The prepare hook is the mcode adapter's ``mcp_prepare``: the file
    the run needs, and the file the run has to give back.
    """
    adapter = McodeHarness()
    return HarnessInvocation(
        argv=(sys.executable, str(probe)),
        env_delta={
            "PROBE_REPORT": str(report),
            "PROBE_PID_FILE": str(pid_file),
            "PROBE_MODE": mode,
            "PROBE_EXIT": str(exit_code),
        },
        cwd=str(workspace),
        prepare=lambda: adapter.mcp_prepare(workspace, CONFIG),
    )


def _read_pid(pid_file: Path) -> int:
    """The pid the probe wrote."""
    return int(pid_file.read_text(encoding="utf-8").strip())


def _wait_for_file(path: Path, timeout: float = 30.0) -> None:
    """Wait until ``path`` exists."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


def _assert_gone(pid: int) -> None:
    """Assert that ``pid`` is neither running nor a zombie.

    A reaped child is gone from the process table. A zombie is not:
    the pid still answers, because the kernel keeps the entry until
    somebody waits for it. So this catches both "the child was left
    running" and "the child was killed and never waited for".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:  # pragma: no cover - not our process
        return
    raise AssertionError(f"process {pid} is still around after the run")


def _open_fds() -> int:
    """How many descriptors this process holds."""
    for directory in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(directory):
            return len(os.listdir(directory))
    raise AssertionError("no way to count descriptors on this platform")


def _wait_for_fd_baseline(baseline: int, timeout: float = 5.0) -> int:
    """Wait for the descriptor count to come back to ``baseline``."""
    deadline = time.monotonic() + timeout
    current = _open_fds()
    while current != baseline and time.monotonic() < deadline:
        time.sleep(0.02)
        current = _open_fds()
    return current


def _deadline_killer(pid_file: Path, deadline: float) -> threading.Thread:
    """Kill the probe's process group once the deadline has passed.

    The runner has no timeout of its own: the deadline is the
    caller's to enforce, and this is the caller's half of it. What the
    runner owes after the child has been killed is the same thing it
    owes after any other ending — the workspace goes back.
    """

    def _watch() -> None:
        _wait_for_file(pid_file, timeout=30.0)
        time.sleep(deadline)
        try:
            pid = _read_pid(pid_file)
        except (OSError, ValueError):
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    thread = threading.Thread(target=_watch, name="probe-deadline", daemon=True)
    thread.start()
    return thread


def _sigterm_run(
    tmp_path: Path,
    workspace: Path,
    probe: Path,
    prefix: str,
) -> Tuple[Dict[str, Any], int, int]:
    """Deliver a SIGTERM to a runner that installed no trap of its own.

    The run happens in a driver process of its own, in a session of its
    own, so that the signal can be delivered to the runner's process
    group without signalling pytest. The driver has no signal handler
    of its own: what reacts to the signal is the runner.

    Args:
        tmp_path: The case's scratch directory.
        workspace: The workspace the run happens in.
        probe: The probe script that stands in for a coding CLI.
        prefix: What this run's files are named after.

    Returns:
        What the driver reported, the driver's exit code, and the pid
        of the child the driver was driving.
    """
    driver = tmp_path / f"{prefix}-driver.py"
    driver.write_text(DRIVER_SOURCE, encoding="utf-8")
    driver_report = tmp_path / f"{prefix}-driver.json"
    probe_report = tmp_path / f"{prefix}-probe.json"
    probe_pid = tmp_path / f"{prefix}-probe.pid"

    options = {
        "workspace": str(workspace),
        "probe": str(probe),
        "report": str(driver_report),
        "probe_report": str(probe_report),
        "pid_file": str(probe_pid),
        "config": CONFIG,
        "env": {
            "PROBE_REPORT": str(probe_report),
            "PROBE_PID_FILE": str(probe_pid),
            "PROBE_MODE": "hang",
            "PROBE_EXIT": "0",
        },
    }

    process = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(Path(__file__).resolve().parents[3]),
            json.dumps(options),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stderr = ""
    try:
        # Wait until the child is really running, so the signal is
        # not delivered into a race.
        _wait_for_file(probe_pid, timeout=30.0)
        # The signal goes to the runner's process group. The harness
        # child is in a group of its own and does not receive it.
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"the driver never finished: {exc}") from exc
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stuck
                process.kill()
                process.wait(timeout=5)
        else:
            process.wait(timeout=5)

    assert driver_report.exists(), (
        f"the driver wrote no report; its stderr was: {stderr}"
    )
    reported = json.loads(driver_report.read_text(encoding="utf-8"))
    return reported, process.returncode, _read_pid(probe_pid)


def _git(workspace: Path, *args: str, stdin: Optional[str] = None) -> str:
    """Run a git command in the workspace, and return what it said."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        input=stdin,
        check=True,
    )
    return completed.stdout


def _init_git_repo(workspace: Path) -> None:
    """Turn the workspace into a git repository with one commit in it."""
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "probe@example.invalid")
    _git(workspace, "config", "user.name", "recovery probe")
    _git(workspace, "config", "commit.gpgsign", "false")
    (workspace / "README.md").write_text(
        "the task's working tree\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "the task's starting point")


def _auto_commit(workspace: Path, record: Path) -> List[str]:
    """The framework's auto-commit hook, and what it staged.

    This is the step that turns a task's working tree into a commit.
    It is here because the question IT-RUN-09 asks is about the
    *order* of two things: the adapter's restore, and this commit. The
    restore has to come first, or the file the adapter wrote for the
    run ends up in the commit.

    Args:
        workspace: The task's working tree.
        record: Where the staged paths are written, for the case to
            read. It is outside the workspace on purpose: a file
            inside it would be committed by the very step it records.

    Returns:
        The paths the commit staged.
    """
    _git(workspace, "add", "-A")
    staged = [
        line
        for line in _git(workspace, "diff", "--cached", "--name-only").splitlines()
        if line.strip()
    ]
    record.write_text("\n".join(staged) + "\n", encoding="utf-8")
    _git(workspace, "commit", "-m", "the framework's auto-commit")
    return staged


# ---------------------------------------------------------------------------
# IT-RUN-04 — the file is there for the run, and gone after it
# ---------------------------------------------------------------------------


def test_restore_on_success(tmp_path, monkeypatch):
    """The workspace is exactly as it was found when the run ends."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    original = _with_original_mcp(workspace)
    probe = _write_probe(tmp_path)
    report = tmp_path / "probe.json"
    pid_file = tmp_path / "probe.pid"

    invocation = _invocation(workspace, probe, report, pid_file)

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 0
    assert result.outcome.kind == OUTCOME_OK

    # What the child saw, from the inside: the file was there, for the
    # run, with the bytes the adapter wrote.
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen["mcp_json_exists"] is True
    assert seen["mcp_json_sha256"] == EXPECTED_SHA256

    # What is left: the workspace is as it was found — the bytes and
    # the mode.
    target = workspace / MCP_FILENAME
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == ORIGINAL_MODE

    # The child was waited for, so nothing is left behind.
    _assert_gone(_read_pid(pid_file))


# ---------------------------------------------------------------------------
# IT-RUN-05 — a non-zero exit still restores
# ---------------------------------------------------------------------------


def test_restore_on_nonzero_exit(tmp_path, monkeypatch):
    """A run that failed still gives the workspace back."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    original = _with_original_mcp(workspace)
    probe = _write_probe(tmp_path)
    report = tmp_path / "probe.json"
    pid_file = tmp_path / "probe.pid"

    invocation = _invocation(
        workspace, probe, report, pid_file, exit_code=3
    )

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 3

    # The verdict on the exit is the adapter's to make. The mcode
    # adapter carries no exit table of its own, so the runner's own
    # rule applies and a non-zero exit that nothing classifies is
    # ``unknown`` — with the code in the message, so the operator can
    # see what happened.
    assert result.outcome.kind == OUTCOME_UNKNOWN
    assert "3" in result.outcome.message
    assert result.is_error is True

    # The restore happened all the same: a failed run is not allowed
    # to leave the workspace dirty.
    assert (workspace / MCP_FILENAME).read_bytes() == original
    _assert_gone(_read_pid(pid_file))

    # An adapter that *does* classify answers for itself, and the
    # runner does not second-guess it: the same kind of non-zero exit
    # is a model error under the claude adapter's table.
    claude_workspace = _workspace(tmp_path, "claude-workspace")
    claude_original = _with_original_mcp(claude_workspace)
    claude_invocation = _invocation(
        claude_workspace,
        probe,
        tmp_path / "claude-probe.json",
        tmp_path / "claude-probe.pid",
        exit_code=1,
    )

    claude_result = Runner().run(ClaudeHarness(), claude_invocation)

    assert claude_result.exit_code == 1
    assert claude_result.outcome.kind == OUTCOME_MODEL_ERROR
    assert (claude_workspace / MCP_FILENAME).read_bytes() == claude_original


# ---------------------------------------------------------------------------
# IT-RUN-06 and IT-RUN-07 — a run that is cut short
# ---------------------------------------------------------------------------


def test_restore_on_timeout_and_sigterm(tmp_path, monkeypatch):
    """A run the clock or a signal cuts short still restores."""
    _no_sandbox(monkeypatch, tmp_path)

    # --- IT-RUN-06: the deadline is enforced from the outside -------
    workspace = _workspace(tmp_path, "timeout-workspace")
    original = _with_original_mcp(workspace)
    probe = _write_probe(tmp_path)
    report = tmp_path / "timeout-probe.json"
    pid_file = tmp_path / "timeout-probe.pid"

    invocation = _invocation(
        workspace, probe, report, pid_file, mode="hang"
    )

    baseline = _open_fds()
    killer = _deadline_killer(pid_file, DEADLINE_SECONDS)
    try:
        result = Runner().run(McodeHarness(), invocation)
    finally:
        killer.join(timeout=10.0)

    # The child was killed, and the runner read its death as the end
    # of the run: a child killed by a signal exits with the negative
    # of the signal.
    assert result.exit_code < 0
    assert result.outcome.kind == OUTCOME_SIGNALED

    # The restore happened ...
    assert (workspace / MCP_FILENAME).read_bytes() == original

    # ... nothing is left running (a reaped child is gone from the
    # process table; a zombie would still answer) ...
    _assert_gone(_read_pid(pid_file))

    # ... and no descriptor was leaked by the run.
    assert _wait_for_fd_baseline(baseline) == baseline

    # --- IT-RUN-07: the signal goes to the runner's group -----------
    sig_workspace = _workspace(tmp_path, "sigterm-workspace")
    sig_original = _with_original_mcp(sig_workspace)

    reported, returncode, child_pid = _sigterm_run(
        tmp_path, sig_workspace, probe, "sigterm"
    )

    # The signal reached the runner, and the runner turned it into an
    # interruption of its own: the run was cut short, not finished.
    assert reported["interrupted_signum"] == int(signal.SIGTERM)
    assert returncode == 128 + int(signal.SIGTERM)

    # The two process groups differ. That is the proof that the group
    # signal did not reach the child, and that the child was ended by
    # the runner instead.
    assert reported["child_pgid"] is not None
    assert reported["driver_pgid"] is not None
    assert reported["child_pgid"] != reported["driver_pgid"]

    # The runner's own guard restored the workspace.
    assert reported["after"] == reported["before"]
    assert (sig_workspace / MCP_FILENAME).read_bytes() == sig_original

    # And the child the runner ended was waited for.
    _assert_gone(child_pid)


# ---------------------------------------------------------------------------
# IT-RUN-08 — an exception inside the adapter still restores
# ---------------------------------------------------------------------------


def test_restore_on_exception(tmp_path, monkeypatch):
    """An adapter that blows up restores the file, and the error escapes."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    original = _with_original_mcp(workspace)
    probe = _write_probe(tmp_path)
    report = tmp_path / "probe.json"
    pid_file = tmp_path / "probe.pid"

    invocation = _invocation(workspace, probe, report, pid_file)

    with pytest.raises(RuntimeError, match="the adapter blew up"):
        Runner().run(_ExplodingAdapter(), invocation)

    # The exception was not swallowed: it came out of the runner.
    assert (workspace / MCP_FILENAME).read_bytes() == original

    # The child the runner was driving was ended on the way out, so
    # the failure leaves nothing running.
    _assert_gone(_read_pid(pid_file))


# ---------------------------------------------------------------------------
# IT-RUN-07 — a SIGTERM nobody prepared for
# ---------------------------------------------------------------------------


def test_production_sigterm_without_a_self_installed_trap_restores_the_file(
    tmp_path, monkeypatch
):
    """A SIGTERM the caller did not ask for still gives the workspace back.

    The production chain stops a run by signalling a process group, and
    the process it signals has no handler of its own: the runner is
    handed a command line and a workspace, and nothing else. So the
    restore cannot be the caller's job — it is the runner's, or it does
    not happen at all.

    The file the adapter writes for the run is what shows it. A restore
    that does not happen leaves that file in the workspace, and the
    next ``git add -A`` puts it into a commit: the credential it
    carries ends up in the repository.

    So the case delivers a SIGTERM to a runner that installed no trap
    of its own, and then asks the only question that matters: is the
    workspace exactly as it was found?
    """
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path, "production-sigterm-workspace")
    original = _with_original_mcp(workspace)
    probe = _write_probe(tmp_path)

    reported, returncode, child_pid = _sigterm_run(
        tmp_path, workspace, probe, "production-sigterm"
    )

    # The driver had no handler of its own: it started with the
    # platform's default disposition for SIGTERM. So what put the
    # workspace back was the runner and not the case's discipline.
    assert reported["sigterm_disposition_at_start"] == "SIG_DFL"

    # The signal reached the runner and the runner turned it into an
    # interruption of its own: the run was cut short, not finished.
    assert reported["interrupted_signum"] == int(signal.SIGTERM)
    assert reported["error"] is not None
    assert "HarnessInterrupted" in reported["error"]

    # The exit code says which signal it was — the code the CLI leaves
    # with when a run is stopped this way.
    assert returncode == 128 + int(signal.SIGTERM)

    # The file is back: the same bytes, the same mode.
    assert reported["after"] == reported["before"]
    target = workspace / MCP_FILENAME
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == ORIGINAL_MODE

    # The two process groups differ: the group signal did not reach the
    # child by accident, and the child was ended by the runner.
    assert reported["child_pgid"] is not None
    assert reported["driver_pgid"] is not None
    assert reported["child_pgid"] != reported["driver_pgid"]

    # And the child was waited for: an interrupted run leaves nothing
    # running.
    _assert_gone(child_pid)


# ---------------------------------------------------------------------------
# IT-RUN-09 — the file never reaches a commit
# ---------------------------------------------------------------------------


def test_not_in_commit(tmp_path, monkeypatch):
    """The adapter's file does not end up in the task's commit."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    _init_git_repo(workspace)
    probe = _write_probe(tmp_path)
    report = tmp_path / "probe.json"
    pid_file = tmp_path / "probe.pid"

    invocation = _invocation(workspace, probe, report, pid_file)

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 0

    # The run happened, and the adapter's file is gone again: it was
    # never in the workspace to begin with.
    assert not (workspace / MCP_FILENAME).exists()

    # The framework's auto-commit: it runs after the task's run, and
    # it is the step that would have committed the file if the restore
    # had not happened first.
    staged = _auto_commit(workspace, tmp_path / "staged.txt")

    # The file was not among the staged paths ...
    assert MCP_FILENAME not in staged
    assert MCP_FILENAME not in (tmp_path / "staged.txt").read_text(
        encoding="utf-8"
    )

    # ... it is not in the tree the commit produced ...
    tree = _git(workspace, "ls-tree", "-r", "--name-only", "HEAD")
    assert MCP_FILENAME not in tree

    # ... and its content never entered the object database at all.
    blob = _git(workspace, "hash-object", "--stdin", stdin=EXPECTED_BYTES.decode())
    objects = _git(
        workspace, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"
    )
    assert blob.strip() not in objects.split()

    # The commit is a real one: the run's own work is in it.
    assert "work.txt" in tree


# ---------------------------------------------------------------------------
# IT-RUN-10 — a stopped run leaves with the code of the signal
# ---------------------------------------------------------------------------


def test_the_cli_leaves_with_the_code_of_the_signal_that_ended_the_run(
    tmp_path, monkeypatch
):
    """A run that was stopped is reported as stopped, not as a crash.

    The runner turns the signal into an exception so that the run can
    unwind and give the workspace back. The CLI is what turns that
    exception into an exit code, and the code it picks is the one the
    shell reports for a process killed by that signal: 128 plus the
    number. A caller can therefore tell "the run was stopped" from
    "the run failed", which is exit code 1.
    """
    import cli
    from harness.runner import HarnessInterrupted

    def _stopped(**_kwargs):
        raise HarnessInterrupted(int(signal.SIGTERM))

    monkeypatch.setattr(cli, "autonomous_coding", _stopped)
    monkeypatch.setattr(
        sys, "argv", ["autonomous-coding", "--workspace", str(tmp_path)]
    )
    # Without a plan there is no log to write the note to, so the note
    # goes to standard error instead.
    monkeypatch.delenv("PDT_PLAN_ID", raising=False)

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 128 + int(signal.SIGTERM)


# ---------------------------------------------------------------------------
# IT-RUN-11 — a killed run's workspace is put back by the next start
# ---------------------------------------------------------------------------
#
# Every way a run can end cleanly ends in the adapter's restore. One
# way cannot: a SIGKILL, or a crash, or the machine going away. The
# process is gone between the write and the restore, so the workspace
# keeps the file the adapter wrote — the run's credential — where the
# user's own file was, and the user's own file is gone.
#
# The case below is that one, and it asks two questions:
#
# 1. **Does the crash leave the credential in the workspace?** It must
#    — that is the damage — and the recovery journal must be able to
#    give the user's file back.
# 2. **Does the next start of a product entry point put it back?** It
#    must, and it must not put the file back while the run that wrote
#    it is still alive.


def test_sigkill_backup_is_replayed_on_next_product_start(
    tmp_path, monkeypatch, caplog
):
    """A workspace a killed run left dirty is put back by the next start.

    The recovery journal is what makes that possible: before the
    adapter opens the workspace's ``.mcp.json``, it writes down what
    was there — the path, the bytes, the mode, the process that owns
    the write — into a private copy outside the workspace. A later
    start reads the journals it finds and puts the workspaces back.

    The journal is not a credential store: it names the file and what
    was in it, and the adapter's own payload is written down nowhere.
    The credential lives in the workspace, and that is exactly why the
    workspace has to be put back.
    """
    _no_sandbox(monkeypatch, tmp_path)

    # The recovery journal lives in the framework's own runtime state,
    # outside the workspace: a workspace is somebody's working tree, and
    # a file left in it is a file the next ``git add -A`` picks up.
    state = tmp_path / "restore-state"
    monkeypatch.setenv("PDT_MCP_RESTORE_DIR", str(state))

    workspace = _workspace(tmp_path, "crash-workspace")
    original = _with_original_mcp(workspace, mode=0o640)
    driver = _write_crash_driver(tmp_path)

    process, driver_pid = _start_crash_driver(tmp_path, workspace, driver)

    # The write happened: the workspace holds the adapter's payload, and
    # the journal that can give the user's file back is on disk.
    target = workspace / MCP_FILENAME
    assert SENTINEL_MARKER in target.read_text(encoding="utf-8")
    assert len(sorted(state.glob("*.json"))) == 1

    # A live run is not to be robbed. While the driver is alive the
    # workspace is in use, and nothing may put it back under it.
    McodeHarness()
    assert SENTINEL_MARKER in target.read_text(encoding="utf-8")
    assert len(sorted(state.glob("*.json"))) == 1

    # Now kill it where it stands: SIGKILL, which no handler can turn
    # into an orderly return, so the restore never runs.
    crashed = _kill_crash_driver(process)
    assert crashed["exit_code"] == -int(signal.SIGKILL)

    # The crash left the workspace exactly as the damage describes it:
    # the adapter's payload, with the credential, in place of the
    # user's file. The mode is the adapter's, not the user's.
    assert target.read_bytes() != original
    assert SENTINEL_MARKER in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    # The journal is there, and it is private: it names the file and
    # what was in it.
    journals = sorted(state.glob("*.json"))
    copies = sorted(state.glob("*.bin"))
    assert len(journals) == 1
    assert len(copies) == 1
    assert stat.S_IMODE(journals[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(copies[0].stat().st_mode) == 0o600

    record = json.loads(journals[0].read_text(encoding="utf-8"))
    assert record["target"] == str(target.resolve())
    assert record["existed"] is True
    assert record["mode"] == 0o640
    assert record["pid"] == driver_pid

    # The journal and its copy do not carry the credential.
    assert SENTINEL_MARKER not in journals[0].read_text(encoding="utf-8")
    assert SENTINEL_MARKER not in copies[0].read_text(encoding="utf-8")

    # The copy is the user's file, byte for byte: that is what has to
    # be given back.
    assert copies[0].read_bytes() == original

    # Nothing of the journal's is in the workspace: the working tree
    # the framework commits holds neither the credential nor the copy.
    assert sorted(entry.name for entry in workspace.iterdir()) == [
        MCP_FILENAME
    ]

    # --- the next start of a product entry point ------------------
    caplog.set_level(logging.INFO, logger="harness.mcode")
    adapter = McodeHarness()

    # The workspace is the user's again: the same bytes, the same mode,
    # and no credential.
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert SENTINEL_MARKER not in target.read_text(encoding="utf-8")

    # The journal and its copy are gone: the restore is done.
    assert not list(state.glob("*.json"))
    assert not list(state.glob("*.bin"))

    # And it was recorded, under one name, in the event and in the log.
    assert [event.type for event in adapter.recovered_restores] == [
        RESTORE_EVENT_NAME
    ]
    assert any(
        RESTORE_EVENT_NAME in entry.getMessage() for entry in caplog.records
    )

    # --- the CLI does the same -----------------------------------
    cli_workspace = _workspace(tmp_path, "cli-workspace")
    cli_original = _with_original_mcp(cli_workspace, mode=0o640)
    cli_process, _cli_pid = _start_crash_driver(
        tmp_path, cli_workspace, driver, prefix="cli"
    )
    _kill_crash_driver(cli_process)
    cli_target = cli_workspace / MCP_FILENAME
    assert SENTINEL_MARKER in cli_target.read_text(encoding="utf-8")

    import cli

    monkeypatch.setattr(cli, "autonomous_coding", lambda **_kwargs: None)
    monkeypatch.setattr(
        sys, "argv", ["autonomous-coding", "--workspace", str(cli_workspace)]
    )
    monkeypatch.delenv("PDT_PLAN_ID", raising=False)
    cli.main()

    assert cli_target.read_bytes() == cli_original
    assert stat.S_IMODE(cli_target.stat().st_mode) == 0o640

    # --- and so does the server's start-up ------------------------
    server_workspace = _workspace(tmp_path, "server-workspace")
    server_original = _with_original_mcp(server_workspace, mode=0o640)
    server_process, _server_pid = _start_crash_driver(
        tmp_path, server_workspace, driver, prefix="server"
    )
    _kill_crash_driver(server_process)
    server_target = server_workspace / MCP_FILENAME
    assert SENTINEL_MARKER in server_target.read_text(encoding="utf-8")

    import server

    server._replay_pending_mcp_restores()

    assert server_target.read_bytes() == server_original
    assert stat.S_IMODE(server_target.stat().st_mode) == 0o640


# ---------------------------------------------------------------------------
# IT-RUN-11 — a journal whose workspace is gone
# ---------------------------------------------------------------------------
#
# The journal outlives the workspace it names when the workspace itself
# is removed: a task's directory that is cleaned up, a scratch directory
# that goes away with the run that made it. The replay then has nothing
# to put back into, and the journal must not stay behind either — it
# would be retried on every start, forever, saying nothing.


def test_a_journal_whose_workspace_is_gone_is_taken_away(
    tmp_path, monkeypatch, caplog
):
    """A journal that has nothing left to restore is taken away.

    The run was killed, so the journal was written. The workspace it
    names is then removed — the task's directory was cleaned up. The
    next start has nothing to put back, so the journal and its copy go
    away with it.
    """
    _no_sandbox(monkeypatch, tmp_path)

    state = tmp_path / "restore-state"
    monkeypatch.setenv("PDT_MCP_RESTORE_DIR", str(state))

    workspace = _workspace(tmp_path, "doomed-workspace")
    _with_original_mcp(workspace, mode=0o640)
    driver = _write_crash_driver(tmp_path)

    process, _driver_pid = _start_crash_driver(tmp_path, workspace, driver)
    _kill_crash_driver(process)

    assert len(sorted(state.glob("*.json"))) == 1
    assert len(sorted(state.glob("*.bin"))) == 1

    # The workspace is gone: the directory that held the file is not
    # there any more.
    shutil.rmtree(workspace)

    caplog.set_level(logging.INFO, logger="harness.mcode")
    adapter = McodeHarness()

    # Nothing was put back — there was nowhere to put it — and the
    # journal is not left behind to be retried on every start.
    assert not workspace.exists()
    assert not list(state.glob("*.json"))
    assert not list(state.glob("*.bin"))

    # A start that had nothing to do does not report a restore it did
    # not do.
    assert adapter.recovered_restores == ()

    # And it says what happened, as a note rather than as an error: a
    # workspace that died is not a failure of the framework.
    assert any("is gone" in entry.getMessage() for entry in caplog.records)
    assert not [
        entry
        for entry in caplog.records
        if entry.levelno >= logging.ERROR
    ]


# ---------------------------------------------------------------------------
# IT-RUN-12 — the file never reaches the index
# ---------------------------------------------------------------------------
#
# The restore is the first half of the contract; this is the second
# half, and it does not depend on the restore having happened. The
# file the adapter writes carries the run's credential, and the working
# tree of a task is a tree the framework commits. The repository
# ignores the file by name, so ``git add -A`` cannot put it into the
# index — not even in the run that was killed before it could put the
# workspace back.


def test_mcp_json_never_reaches_the_index(tmp_path, monkeypatch):
    """The adapter's file cannot be staged, whatever the restore did.

    The rule is deployed in this repository: ``git check-ignore``
    answers for the file the adapter writes. The same rule, in the
    working tree of a task, keeps the file out of the index — and the
    index is where a credential becomes a commit.
    """
    _no_sandbox(monkeypatch, tmp_path)

    repo_root = Path(__file__).resolve().parents[4]

    # The rule is deployed here: the repository root ignores the file
    # the adapter writes into a working tree.
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", MCP_FILENAME],
        cwd=str(repo_root),
        capture_output=True,
    )
    assert ignored.returncode == 0, (
        "the repository root does not ignore .mcp.json, so a task's "
        "working tree can stage the run's credential"
    )

    # The same rule, in the working tree of a task. The repository
    # root's own ignore file is what is copied: the case is about the
    # rule that is deployed, not about a rule written out by hand.
    workspace = _workspace(tmp_path, "index-workspace")
    _init_git_repo(workspace)
    (workspace / ".gitignore").write_text(
        (repo_root / ".gitignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (workspace / MCP_FILENAME).write_text(
        json.dumps(CREDENTIAL_CONFIG, ensure_ascii=False), encoding="utf-8"
    )
    (workspace / "work.txt").write_text(
        "the run did something\n", encoding="utf-8"
    )

    # The framework's auto-commit: the step that would have committed
    # the credential if the file could be staged.
    staged = _auto_commit(workspace, tmp_path / "staged.txt")

    assert MCP_FILENAME not in staged
    assert MCP_FILENAME not in (tmp_path / "staged.txt").read_text(
        encoding="utf-8"
    )

    # The index has no such path: there is nothing to show.
    shown = subprocess.run(
        ["git", "show", f":{MCP_FILENAME}"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
    )
    assert shown.returncode != 0
    assert SENTINEL_MARKER not in shown.stdout

    # Nor is it tracked, and it is not in the tree the commit made.
    tracked = _git(workspace, "ls-files").split()
    assert MCP_FILENAME not in tracked
    tree = _git(workspace, "ls-tree", "-r", "--name-only", "HEAD")
    assert MCP_FILENAME not in tree

    # The commit is a real one: the run's own work is in it.
    assert "work.txt" in tree
