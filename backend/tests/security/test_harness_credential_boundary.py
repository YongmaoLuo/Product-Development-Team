"""SEC-08..15 — the credential boundary around a harness run's MCP
configuration.

What is pinned here
-------------------

A harness run is handed an MCP configuration, and that configuration may
carry a credential. The credential is written as a *reference* —
``${VAR}`` — and the CLI that reads it resolves the reference itself,
in the child's own environment. The adapter does not resolve it: a
``${VAR}`` in the configuration is a ``${VAR}`` in the file.

That is a small decision with a large blast radius, because the file the
adapter writes lives in somebody else's working tree, and the framework
commits that tree. If the adapter expanded the reference, the plaintext
would land in the tree, in the commit, in the history, and in every log
that recorded the run. If the adapter's restore ran *after* the commit,
the file would reach the history even without an expansion. Both halves
have to hold, and neither is observable from the code alone — they are
observable from the disk.

The cases
---------

===  ==========================  =========================================
SEC  Case                        Boundary
===  ==========================  =========================================
08   the sentinel on disk        the token a ``${VAR}`` stands for is in
                                 no worktree, no plan artifact, no log,
                                 no state database and no git object
09   a configuration too large   refused outright, never truncated
10   the file in git             neither the tree nor the history carries
                                 the adapter's file
11   every way a run ends        the workspace comes back byte for byte
                                 and mode for mode
12   a private file              a 0600 file is still 0600 afterwards
13   two tasks at once           neither task can see the other's servers
14   a backend that cannot run   the process stops before a task starts
15   the profile's own config    the account's ``config.yaml`` is
                                 untouched, and no code path writes one
===  ==========================  =========================================

The observation point
--------------------

The disk is the observation point. Every case reads the files that are
actually there — the workspace, the plan artifact, the log, the SQLite
database, the git object database — and compares hashes, modes and
contents. Nothing is mocked: a case that cannot see the disk has
nothing to say about it.

The child is a real process. A small probe stands in for the coding
CLI: it records what the workspace looked like from the inside, while
the run was happening, and writes the record where the case can read
it. The probe replaces the CLI's *command line* only — the prepare
hook, the environment and the working directory are the ones the
adapter's own dispatch chain produced.

Three decisions worth stating
-----------------------------

**The sweep is over the framework's output, not over the operator's
input.** A configuration that arrives as a file is the operator's file
and it is not swept: it is the input, and it is allowed to contain what
it contains. What is swept is what the framework *writes* — the
workspace, the plan artifact, the log, the state database, the git
objects. The workspace is the interesting one: the adapter's file
really is there, with the reference in it, for the length of the run.
The sweep runs after the run, so the file has to be gone.

**Two strings, two searches.** The configuration names a variable —
``${PDT_TEST_SENTINEL_…}`` — and the environment holds that variable's
*value*. The name is a reference: it says where a secret is, not what
it is. The value is the secret. They are swept separately, because a
single search would only answer one of the two questions, and the
answers are not the same: a configuration that was expanded would hold
the value and not the name, and a framework that recorded the
configuration verbatim would hold the name and not the value.

**A sweep that cannot fail proves nothing.** The sentinel case ends
with a negative control: the sweep is pointed at a file that does hold
the string, and it has to find it — for the name and for the value
alike. A sweep that finds nothing because it looks in the wrong place
is indistinguishable from a clean run.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import logging
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pytest

from config_paths import resolve_state_db_path
from execution_logger import ExecutionLogger
from harness.contract import (
    HarnessConfigError,
    HarnessInvocation,
    TaskInvocation,
)
from harness.mcode import McodeHarness, _SNAPSHOT_MAX_BYTES
from harness.registry import (
    HARNESS_NAMES,
    get_runtime_config,
    reset_runtime_config_for_tests,
    resolve_harness,
)
from harness.runner import HarnessInterrupted, Runner
from mcode_profiles import ProfileBinding
from state_machine.db.connection import open as open_state_db
from state_machine.db.schema import migrate
from state_machine.repositories.plan_task_repository import PlanTaskRepository

#: The repository root — the parent of ``backend``. Derived from this
#: file rather than written out, so the suite says the same thing on
#: every machine and no path becomes a machine-specific constant.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The backend package root — the parent of ``tests``.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: The file the mcode adapter writes for the length of a run.
MCP_FILENAME = ".mcp.json"

#: The name the credential's environment variable carries. This is
#: the *reference*: it is what the configuration names, and a name is
#: not a secret.
SENTINEL_PREFIX = "PDT_TEST_SENTINEL_"

#: The prefix the credential's *value* carries. The value is a random
#: string of its own, so that a sweep for the name and a sweep for the
#: value are two different searches: if the value were built out of the
#: name, the two sweeps would say the same thing, and a file holding
#: the value would be found by the name sweep for the wrong reason.
CREDENTIAL_PREFIX = "PDT_TEST_CREDENTIAL_"

#: The configuration the cases that do not care about the credential's
#: shape are exercised with.
CONFIG: Dict[str, Any] = {
    "mcpServers": {"boundary-probe": {"command": "true", "args": ["--flag"]}}
}

#: How long a run is allowed to hang before the deadline is enforced.
#: Long enough that the probe is certainly running, short enough that
#: the lane does not slow down.
DEADLINE_SECONDS = 2.0

#: How long a case waits for a file the child is supposed to write.
FILE_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# The sentinel
# ---------------------------------------------------------------------------


def _sentinel() -> str:
    """The name of the variable the credential is held in.

    The name is random, so it cannot collide with anything that is
    already there. It is a *reference* — the configuration says
    ``${NAME}`` — and the boundary says a reference may be recorded
    while the value it stands for may not.

    Returns:
        The sentinel.
    """
    return SENTINEL_PREFIX + secrets.token_hex(8)


def _credential() -> str:
    """The value the sentinel's environment variable holds.

    A random string of its own, so that a sweep for the name and a
    sweep for the value are two different searches. This is the string
    that must never reach the disk: it is the secret, and a secret on
    disk is a secret in every commit that follows it.

    Returns:
        The credential.
    """
    return CREDENTIAL_PREFIX + secrets.token_hex(8)


def _config(sentinel: str, server: str = "s") -> Dict[str, Any]:
    """An MCP configuration that carries a credential by reference.

    Args:
        sentinel: The name of the variable the credential is held in.
        server: The name of the MCP server the configuration names.

    Returns:
        The configuration, as a mapping.
    """
    return {
        "mcpServers": {
            server: {"headers": {"Authorization": "${%s}" % sentinel}}
        }
    }


# ---------------------------------------------------------------------------
# The disk
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    """The hash of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mode(path: Path) -> int:
    """A file's permission bits."""
    return stat.S_IMODE(path.stat().st_mode)


def _fingerprint(path: Path) -> Dict[str, Any]:
    """What is at ``path``: nothing, or something with these bytes and mode.

    Args:
        path: The file to look at.

    Returns:
        The fingerprint: whether the file is there, the hash of its
        bytes, and its permission bits.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return {"exists": False, "sha256": None, "mode": None}
    try:
        mode = _mode(path)
    except OSError:
        mode = None
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": mode,
    }


def _tree_digest(root: Path) -> Dict[str, Tuple[str, int]]:
    """A digest of every file under ``root``: its hash and its mode."""
    digest: Dict[str, Tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        digest[str(path.relative_to(root))] = (_sha256(path), _mode(path))
    return digest


def _grep_path(path: Path, needle: bytes) -> List[str]:
    """Every file under ``path`` whose bytes hold ``needle``.

    Args:
        path: A file or a directory to sweep.
        needle: The bytes to look for.

    Returns:
        The paths that hold it. Empty when nothing does.
    """
    if not path.exists():
        return []
    if path.is_file():
        try:
            data = path.read_bytes()
        except OSError:
            return []
        return [str(path)] if needle in data else []

    hits: List[str] = []
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        if needle in data:
            hits.append(str(candidate))
    return hits


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo``, and return what it said.

    Args:
        repo: The repository to run it in.
        args: The command and its arguments.

    Returns:
        The command's standard output.
    """
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_git_repo(workspace: Path) -> None:
    """Turn a directory into a git repository with one commit in it."""
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "boundary@example.invalid")
    _git(workspace, "config", "user.name", "credential boundary")
    _git(workspace, "config", "commit.gpgsign", "false")
    (workspace / "README.md").write_text(
        "the task's working tree\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "the task's starting point")


def _auto_commit(workspace: Path) -> List[str]:
    """The framework's auto-commit hook, and what it staged.

    Args:
        workspace: The task's working tree.

    Returns:
        The paths the commit staged.
    """
    _git(workspace, "add", "-A")
    staged = [
        line
        for line in _git(
            workspace, "diff", "--cached", "--name-only"
        ).splitlines()
        if line.strip()
    ]
    _git(workspace, "commit", "-m", "the framework's auto-commit")
    return staged


def walk_git_objects_for(repo: Path, needle: str) -> List[str]:
    """Every object in ``repo``'s object database that mentions ``needle``.

    Every object is looked at, not only the blobs: a tree carries the
    *names* of the files in it, so a file that reached a commit would
    be found here even if its content never had to be read.

    Args:
        repo: The repository to walk.
        needle: The text to look for.

    Returns:
        The object ids that mention it. Empty when none does.
    """
    listing = _git(
        repo,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype)",
    )
    wanted = needle.encode("utf-8")
    hits: List[str] = []
    for line in listing.splitlines():
        sha, _, kind = line.partition(" ")
        if not sha:
            continue
        completed = subprocess.run(
            ["git", "cat-file", kind, sha],
            cwd=str(repo),
            capture_output=True,
        )
        if wanted in completed.stdout:
            hits.append(sha)
    return hits


# ---------------------------------------------------------------------------
# The probe — the child that stands in for a coding CLI
# ---------------------------------------------------------------------------

_PROBE_SOURCE = r'''#!/usr/bin/env python3
"""A probe that records what the workspace looked like while it ran.

It writes its own pid (the case needs it to end the child and to prove
that nothing was left behind), reads the ``.mcp.json`` in its working
directory and records what it says — that is the "the file was there,
for the run, with these bytes" observation, taken from inside the run —
writes a file of its own so the auto-commit has something to commit,
prints one line so the runner has a stream to read, holds the run open
until the other run has started too when it is told to, and hangs when
it is told to.

It does not dump the environment. The environment holds the
credential's *value* during a run, and a probe that wrote it down would
put the credential on disk by itself — which is the probe's doing, not
the framework's, and would make the sweep meaningless.
"""

import hashlib
import json
import os
import sys
import time

report_path = os.environ["PROBE_REPORT"]
pid_file = os.environ["PROBE_PID_FILE"]
mode = os.environ.get("PROBE_MODE", "ok")
barrier = os.environ.get("PROBE_BARRIER", "")
name = os.environ.get("PROBE_NAME", "probe")

with open(pid_file, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))

target = os.path.join(os.getcwd(), ".mcp.json")
record = {
    "pid": os.getpid(),
    "mcp_json_exists": os.path.exists(target),
    "mcp_json_sha256": None,
    "mcp_json_text": None,
}
try:
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()
    record["mcp_json_text"] = text
    record["mcp_json_sha256"] = hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
except OSError:
    pass

with open(report_path, "w", encoding="utf-8") as handle:
    json.dump(record, handle, sort_keys=True)

work = os.path.join(os.getcwd(), "work.txt")
with open(work, "w", encoding="utf-8") as handle:
    handle.write("the run did something\n")

print(json.dumps({"probe": "ok"}, sort_keys=True), flush=True)

if barrier:
    ready = os.path.join(barrier, "%s.ready" % name)
    with open(ready, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    deadline = time.time() + 30
    while time.time() < deadline:
        if len([n for n in os.listdir(barrier) if n.endswith(".ready")]) >= 2:
            break
        time.sleep(0.02)

if mode == "hang":
    while True:
        time.sleep(3600)
'''


#: The driver — the runner in a process of its own, for the SIGTERM case.
_DRIVER_SOURCE = r'''#!/usr/bin/env python3
"""Run one harness run in a process of its own, and report the workspace.

A SIGTERM has to be delivered to the *runner's* process group. Under
pytest that cannot be done to the test process itself: signalling the
group would signal pytest. So the run happens in a process of its own,
spawned into its own session, and the signal is delivered to that
process's group.

The harness child is spawned with ``start_new_session=True`` — it is in
a group of its own — so the signal does not reach it. Only the trap
can end it. The trap ends it, the runner reads the child's death as
the end of the run, and the workspace is given back before the report
is written.
"""

import hashlib
import json
import os
import signal
import stat
import sys

sys.path.insert(0, sys.argv[1])

from harness.contract import HarnessInvocation
from harness.mcode import McodeHarness
from harness.runner import Runner

OPTIONS = json.loads(sys.argv[2])

TARGET = os.path.join(OPTIONS["workspace"], ".mcp.json")


def _fingerprint(path):
    """What is at ``path``: nothing, or something with these bytes and mode."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return {"exists": False, "sha256": None, "mode": None}
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = None
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": mode,
    }


def _read_pid(path):
    """The pid the probe wrote, or ``None``."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _trap(signum, frame):
    """End the child: it is in a group of its own, so the signal missed it."""
    pid = _read_pid(OPTIONS["pid_file"])
    if pid is None:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


signal.signal(signal.SIGTERM, _trap)

report = {"before": _fingerprint(TARGET), "after": None, "error": None}

adapter = McodeHarness()
invocation = HarnessInvocation(
    argv=(sys.executable, OPTIONS["probe"]),
    env_delta=dict(OPTIONS["env"]),
    cwd=OPTIONS["workspace"],
    prepare=lambda: adapter.mcp_prepare(
        OPTIONS["workspace"], OPTIONS["config"]
    ),
)

try:
    result = Runner().run(adapter, invocation)
    report["exit_code"] = result.exit_code
except BaseException as exc:
    report["error"] = repr(exc)

report["after"] = _fingerprint(TARGET)

with open(OPTIONS["report"], "w", encoding="utf-8") as handle:
    json.dump(report, handle, sort_keys=True)
'''


class _ExplodingAdapter(McodeHarness):
    """An adapter whose own parser fails the moment it reads.

    The failure is inside the adapter, which is where an adapter's
    failures are: the runner has to notice it, end the child, and let
    the error out rather than swallow it.
    """

    def parse_stream(self, lines):
        """Read one line, then fail."""
        for _ in lines:
            break
        raise RuntimeError("the adapter blew up while reading the stream")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _no_sandbox(monkeypatch, tmp_path: Path) -> None:
    """Make sure no sandbox is configured for the run.

    A developer's own seatbelt profile would be applied to the probe,
    and the case would then be testing the profile rather than the
    file.
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


def _write_probe(tmp_path: Path) -> Path:
    """Write the probe script, and return where it landed."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")
    return probe


def _write_driver(tmp_path: Path) -> Path:
    """Write the driver script, and return where it landed."""
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER_SOURCE, encoding="utf-8")
    return driver


def _report_path(tmp_path: Path, name: str = "probe.json") -> Path:
    """Where a probe's report goes. The directory is made first.

    The reports live outside the swept root on purpose: the probe's
    record holds the configuration *as it was written*, and a sweep
    that included it would find the reference in the observer's own
    notebook.

    Args:
        tmp_path: The case's temporary directory.
        name: The report's file name.

    Returns:
        The report's path.
    """
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    return reports / name


def _probe_env(report: Path, pid_file: Path, **extra: str) -> Dict[str, str]:
    """The environment the probe is driven by."""
    env = {
        "PROBE_REPORT": str(report),
        "PROBE_PID_FILE": str(pid_file),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra)
    return env


def _dispatch(
    adapter: McodeHarness,
    request: TaskInvocation,
    probe: Path,
    env: Dict[str, str],
) -> HarnessInvocation:
    """The adapter's own dispatch chain, with the probe as the child.

    The command line is the only thing that is replaced: the prepare
    hook, the environment and the working directory are the ones
    :meth:`McodeHarness.build_invocation` produced, so the chain under
    test is the real one.

    Args:
        adapter: The adapter the request is dispatched through.
        request: The request.
        probe: The probe that stands in for the coding CLI.
        env: The environment the probe is driven by.

    Returns:
        The invocation the runner is handed.
    """
    built = adapter.build_invocation(request)
    assert built.prepare is not None, (
        "a request that carries an MCP configuration must get a prepare "
        "hook — without one the configuration never reaches the workspace"
    )
    return HarnessInvocation(
        argv=(sys.executable, str(probe)),
        env_delta={**dict(built.env_delta), **env},
        cwd=built.cwd,
        prepare=built.prepare,
    )


def _wait_for_file(path: Path, timeout: float = FILE_TIMEOUT_SECONDS) -> None:
    """Wait until ``path`` exists."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


def _read_pid(pid_file: Path) -> int:
    """The pid the probe wrote."""
    return int(pid_file.read_text(encoding="utf-8").strip())


def _deadline_killer(pid_file: Path, delay: float) -> threading.Thread:
    """Kill the probe's process group once the deadline has passed.

    The runner has no timeout of its own: the deadline is the caller's
    to enforce, and this is the caller's half of it. What the runner
    owes once the child has been killed is the same thing it owes after
    any other ending — the workspace goes back.
    """

    def _watch() -> None:
        _wait_for_file(pid_file)
        time.sleep(delay)
        try:
            pid = _read_pid(pid_file)
        except (OSError, ValueError):
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    thread = threading.Thread(
        target=_watch, name="probe-deadline", daemon=True
    )
    thread.start()
    return thread


def _sigterm_run(
    tmp_path: Path,
    workspace: Path,
    probe: Path,
    pid_file: Path,
    name: str,
) -> Tuple[Dict[str, Any], int, str]:
    """Run one harness run in a driver of its own, and stop it with a SIGTERM.

    A SIGTERM has to reach the *runner's* process group. Under pytest
    that cannot be delivered to the test process itself: signalling the
    group would signal pytest. So the run happens in a process of its
    own, spawned into its own session, and the signal is delivered to
    that process's group.

    The driver runs the real runner, with the real adapter, and writes
    down what the run left behind — the workspace's fingerprint and the
    error the run ended with, if any. The report is written on every way
    out of the run, the interrupted one included: a driver that died
    without writing anything would leave the case with no evidence at
    all.

    Args:
        tmp_path: The case's temporary directory.
        workspace: The workspace the run happens in.
        probe: The probe that stands in for the coding CLI.
        pid_file: Where the probe writes its pid.
        name: The case's name, used to keep the two reports apart.

    Returns:
        What the driver reported, the driver's own exit code, and what
        it wrote to standard error.
    """
    driver = _write_driver(tmp_path)
    driver_report = tmp_path / f"{name}-driver.json"
    probe_report = _report_path(tmp_path, f"{name}.json")

    options = {
        "workspace": str(workspace),
        "probe": str(probe),
        "report": str(driver_report),
        "pid_file": str(pid_file),
        "config": CONFIG,
        "env": _probe_env(probe_report, pid_file, PROBE_MODE="hang"),
    }

    process = subprocess.Popen(
        [sys.executable, str(driver), str(_BACKEND_ROOT), json.dumps(options)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for_file(pid_file)
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=FILE_TIMEOUT_SECONDS)
    finally:
        if process.poll() is None:  # pragma: no cover - the driver hung
            process.kill()
            process.communicate()

    assert driver_report.exists(), (
        f"the driver wrote no report at all: {stderr}"
    )
    reported = json.loads(driver_report.read_text(encoding="utf-8"))
    return reported, process.returncode, stderr


@contextlib.contextmanager
def _capture_harness_log(directory: Path) -> Iterator[Path]:
    """Write the harness's own records into ``directory``.

    The logger's level and handlers are put back afterwards: a case
    that leaves the ``harness`` logger at DEBUG changes every case that
    runs after it.

    Args:
        directory: Where the log file is written.

    Yields:
        The log file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "harness.log"

    logger = logging.getLogger("harness.runner")
    saved_level = logger.level
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate

    handler = logging.FileHandler(log_file, encoding="utf-8")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield log_file
    finally:
        handler.close()
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logger.handlers = saved_handlers
        logger.propagate = saved_propagate


def _record_plan_artifact(plans: Path) -> Path:
    """Write the run's plan artifact, and return where it landed.

    The artifact records what the run did — the same thing the
    framework's execution log records. It does not record the MCP
    configuration: nothing in the framework does, and the sweep in
    SEC-08 is what says so.

    Args:
        plans: The plan artifact root.

    Returns:
        The log file.
    """
    logger = ExecutionLogger("boundary-plan")
    logger.log(
        "INFO",
        "harness_run",
        "the run finished and the workspace was given back",
    )
    return plans / "boundary-plan" / "execution.log"


def _record_state_db() -> Path:
    """Write the run's task row, and return the database it went into.

    The row records the task's own bookkeeping — the same columns the
    framework's task repository writes. It does not record the MCP
    configuration: there is no column for it, and the sweep in SEC-08
    is what says so.

    Returns:
        The database file.
    """
    path = resolve_state_db_path()
    connection = open_state_db(path)
    try:
        migrate(connection)
        connection.execute(
            "INSERT OR REPLACE INTO plan_execution (plan_id, updated_at) "
            "VALUES (?, ?)",
            ("boundary-plan", "2026-01-01T00:00:00"),
        )
        repository = PlanTaskRepository(connection)
        repository.add_task(
            "boundary-plan",
            {
                "id": "boundary-task",
                "title": "the task",
                "description": "the task's brief",
            },
        )
    finally:
        connection.close()
    return path


# ---------------------------------------------------------------------------
# SEC-08 — the sentinel is nowhere on disk
# ---------------------------------------------------------------------------


def test_sentinel_never_lands_on_disk(tmp_path, monkeypatch):
    """The token a ``${VAR}`` stands for reaches no surface the run writes.

    The sweep covers the five surfaces the boundary names: the
    worktree, the plan artifact directory, the log, the state database
    and the git object database.
    """
    _no_sandbox(monkeypatch, tmp_path)

    root = tmp_path / "root"
    workspace = root / "workspace"
    plans = root / "plans"
    logs = root / "logs"
    for directory in (workspace, plans, logs):
        directory.mkdir(parents=True)
    _init_git_repo(workspace)

    monkeypatch.setenv("PDT_PLANS_DIR", str(plans))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(root / "state.db"))

    sentinel = _sentinel()
    credential = _credential()
    monkeypatch.setenv(sentinel, credential)
    config = _config(sentinel)

    probe = _write_probe(tmp_path)
    report = _report_path(tmp_path)
    pid_file = tmp_path / "probe.pid"

    request = TaskInvocation(
        prompt="p",
        mcp_config=json.dumps(config),
        cwd=str(workspace),
    )

    with _capture_harness_log(logs):
        invocation = _dispatch(
            McodeHarness(), request, probe, _probe_env(report, pid_file)
        )
        result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 0

    # The file really was there, for the run, with the reference in it:
    # the sweep below is not looking for something that never existed.
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen["mcp_json_exists"] is True
    assert "${%s}" % sentinel in seen["mcp_json_text"]
    assert credential not in seen["mcp_json_text"]

    # ... and it is gone again.
    assert not (workspace / MCP_FILENAME).exists()

    # The surfaces the framework writes to, all of them fed by this run.
    _record_plan_artifact(plans)
    _record_state_db()
    _auto_commit(workspace)

    # The sweep. Two strings, two searches: the *name* the
    # configuration names, and the *value* it stands for. The value is
    # the one that must not be anywhere — it is the secret. The name
    # must not be anywhere either, because the framework does not
    # record the configuration: it is handed to the adapter and
    # forgotten.
    hits: List[str] = []
    for needle in (sentinel, credential):
        hits += _grep_path(root, needle.encode("utf-8"))
        hits += walk_git_objects_for(workspace, needle)

    assert hits == [], (
        "the sentinel reached the disk: {}".format("\n".join(hits))
    )

    # The surfaces are not empty: the sweep looked at real content.
    assert _grep_path(plans, b"harness_run")
    assert _grep_path(logs, b"HARNESS RUN")
    assert _grep_path(root / "state.db", b"the task's brief")
    assert _git(workspace, "ls-tree", "-r", "--name-only", "HEAD").strip()

    # The negative control: the sweep finds each string when that
    # string is really there. A sweep that cannot fail proves nothing
    # about the run.
    planted = root / "planted.txt"
    for needle in (sentinel, credential):
        planted.write_text(needle, encoding="utf-8")
        assert _grep_path(root, needle.encode("utf-8")) == [str(planted)]
    planted.unlink()


# ---------------------------------------------------------------------------
# SEC-08 / arch decision point 3 — ``${VAR}`` stays a ``${VAR}``
# ---------------------------------------------------------------------------


def test_vars_are_literal_on_disk(tmp_path, monkeypatch):
    """The configuration is written as it was given, whatever the env says."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    target = workspace / MCP_FILENAME

    sentinel = _sentinel()
    credential = _credential()
    monkeypatch.setenv(sentinel, credential)
    config = _config(sentinel)

    request = TaskInvocation(
        prompt="p",
        mcp_config=json.dumps(config),
        cwd=str(workspace),
    )
    invocation = McodeHarness().build_invocation(request)

    with invocation.prepare():
        written = target.read_text(encoding="utf-8")

    # The reference is on disk, verbatim.
    assert "${%s}" % sentinel in written
    # The value it stands for is not.
    assert credential not in written
    # And what is on disk is the configuration, unchanged.
    assert json.loads(written) == config
    assert written == json.dumps(config, ensure_ascii=False)

    # The environment still holds the value: nothing consumed it, and
    # nothing took it out of the reader's hands.
    assert os.environ[sentinel] == credential


# ---------------------------------------------------------------------------
# SEC-09 — a configuration too large is refused, never truncated
# ---------------------------------------------------------------------------


def test_oversize_config_is_refused_not_truncated(tmp_path, monkeypatch):
    """A configuration over the limit is refused, and nothing is written."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    target = workspace / MCP_FILENAME
    original = b'{"mcpServers": {"before": {}}}\n'
    target.write_bytes(original)
    os.chmod(target, 0o600)
    before = _fingerprint(target)

    oversized = {
        "mcpServers": {
            "s": {
                "headers": {"Authorization": "x" * (_SNAPSHOT_MAX_BYTES + 1)}
            }
        }
    }
    assert len(json.dumps(oversized).encode("utf-8")) > _SNAPSHOT_MAX_BYTES

    adapter = McodeHarness()
    with pytest.raises(HarnessConfigError) as excinfo:
        with adapter.mcp_prepare(workspace, oversized):
            pass

    message = str(excinfo.value)
    assert "refused" in message
    assert "truncated" in message

    # The file that was there is untouched: a refusal happens before a
    # byte is written, and a truncated configuration would have meant a
    # configuration that says something else.
    assert _fingerprint(target) == before


# ---------------------------------------------------------------------------
# SEC-10 — the file reaches neither the tree nor the history
# ---------------------------------------------------------------------------


def test_mcp_json_not_in_git(tmp_path, monkeypatch):
    """The adapter's file is in neither the commit nor the object database."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    _init_git_repo(workspace)

    before = sorted(path.name for path in workspace.iterdir())

    sentinel = _sentinel()
    credential = _credential()
    monkeypatch.setenv(sentinel, credential)
    config = _config(sentinel)

    probe = _write_probe(tmp_path)
    report = _report_path(tmp_path)
    pid_file = tmp_path / "probe.pid"

    request = TaskInvocation(
        prompt="p",
        mcp_config=json.dumps(config),
        cwd=str(workspace),
    )
    invocation = _dispatch(
        McodeHarness(), request, probe, _probe_env(report, pid_file)
    )

    result = Runner().run(McodeHarness(), invocation)
    assert result.exit_code == 0

    # The restore came first: the file is gone before the commit runs.
    assert not (workspace / MCP_FILENAME).exists()

    # And the workspace holds what it held before, plus the work the
    # run itself did — the adapter's file is not among it.
    after = sorted(path.name for path in workspace.iterdir())
    assert [name for name in after if name not in before] == ["work.txt"]
    assert [name for name in before if name not in after] == []

    staged = _auto_commit(workspace)
    assert MCP_FILENAME not in staged

    # Not in the tree the commit produced ...
    tree = _git(workspace, "ls-tree", "-r", "--name-only", "HEAD")
    assert MCP_FILENAME not in tree

    # ... not in the history ...
    patch = _git(workspace, "log", "-p")
    assert MCP_FILENAME not in patch
    assert sentinel not in patch
    assert credential not in patch

    # ... and not in the object database at all, by name or by content.
    assert walk_git_objects_for(workspace, MCP_FILENAME) == []
    assert walk_git_objects_for(workspace, sentinel) == []
    assert walk_git_objects_for(workspace, credential) == []

    # The commit is a real one: the run's own work is in it.
    assert "work.txt" in tree


# ---------------------------------------------------------------------------
# SEC-11 — every way a run ends gives the workspace back
# ---------------------------------------------------------------------------


def test_restore_is_byte_and_mode_identical_on_every_ending(
    tmp_path, monkeypatch
):
    """A run cut short three ways still gives the workspace back exactly."""
    _no_sandbox(monkeypatch, tmp_path)

    original = b'{"mcpServers": {"before": {}}}\n'
    mode = 0o600
    expected = {
        "exists": True,
        "sha256": hashlib.sha256(original).hexdigest(),
        "mode": mode,
    }

    probe = _write_probe(tmp_path)

    def _prepare(name: str) -> Tuple[Path, Path, Path]:
        """A workspace with a private ``.mcp.json`` in it, and its paths."""
        workspace = _workspace(tmp_path, name)
        target = workspace / MCP_FILENAME
        target.write_bytes(original)
        os.chmod(target, mode)
        return workspace, target, tmp_path / f"{name}.pid"

    # --- an exception inside the adapter ----------------------------
    workspace, target, pid_file = _prepare("exception-workspace")
    report = _report_path(tmp_path, "exception.json")
    invocation = HarnessInvocation(
        argv=(sys.executable, str(probe)),
        env_delta=_probe_env(report, pid_file),
        cwd=str(workspace),
        prepare=lambda: McodeHarness().mcp_prepare(workspace, CONFIG),
    )
    with pytest.raises(RuntimeError, match="the adapter blew up"):
        Runner().run(_ExplodingAdapter(), invocation)

    assert _fingerprint(target) == expected

    # --- the deadline, enforced from the outside ---------------------
    workspace, target, pid_file = _prepare("timeout-workspace")
    report = _report_path(tmp_path, "timeout.json")
    invocation = HarnessInvocation(
        argv=(sys.executable, str(probe)),
        env_delta=_probe_env(report, pid_file, PROBE_MODE="hang"),
        cwd=str(workspace),
        prepare=lambda: McodeHarness().mcp_prepare(workspace, CONFIG),
    )
    killer = _deadline_killer(pid_file, DEADLINE_SECONDS)
    try:
        result = Runner().run(McodeHarness(), invocation)
    finally:
        killer.join(timeout=10.0)

    assert result.exit_code < 0
    assert _fingerprint(target) == expected

    # --- a SIGTERM to the runner's own group -------------------------
    workspace, target, pid_file = _prepare("sigterm-workspace")

    reported, returncode, stderr = _sigterm_run(
        tmp_path, workspace, probe, pid_file, "sigterm"
    )

    assert returncode == 0, stderr

    # The interruption is *reported*, not swallowed. The runner turns
    # the signal into an interruption of its own so that the run can
    # unwind and give the workspace back; the driver caught it and wrote
    # down what happened. A run that ended without saying anything would
    # be a run that claims to have finished — the silent success this
    # case must not allow.
    assert reported["error"] is not None, (
        "the SIGTERM was swallowed: the run reported no interruption"
    )
    assert "HarnessInterrupted" in reported["error"]
    assert str(int(signal.SIGTERM)) in reported["error"]

    # And the workspace came back all the same.
    assert reported["after"] == expected


def test_sigterm_is_reported_as_interruption_not_success(
    tmp_path, monkeypatch
):
    """A run that was stopped says so — it does not report itself finished.

    The runner ends a run that is cut short by a signal with an
    interruption of its own, and the interruption is what lets the run
    unwind: the child is ended, the prepared context is closed, and the
    workspace is given back. That is the whole point of the class — a
    signal must not be able to skip the give-back.

    But the same mechanism is the one that could hide a failure. If the
    signal were swallowed and the run returned as if it had finished, the
    workspace would still be handed back, the credential would still not
    reach the tree, and *nothing would say that the run was cut short*.
    A caller that reads the exit code would read a finished run. A
    supervisor that reads the log would read a finished run. The
    interruption has to be visible, or the give-back is the only thing
    that happened and nobody was told.

    So this case is the reverse of the one above: it asks what the run
    *said*, not what it left behind. Anyone who changes the interruption
    back into a silent success turns this case red.
    """
    _no_sandbox(monkeypatch, tmp_path)

    original = b'{"mcpServers": {"before": {}}}\n'
    mode = 0o600
    expected = {
        "exists": True,
        "sha256": hashlib.sha256(original).hexdigest(),
        "mode": mode,
    }

    workspace = _workspace(tmp_path, "visible-sigterm-workspace")
    target = workspace / MCP_FILENAME
    target.write_bytes(original)
    os.chmod(target, mode)

    probe = _write_probe(tmp_path)
    pid_file = tmp_path / "visible-sigterm.pid"

    reported, returncode, stderr = _sigterm_run(
        tmp_path, workspace, probe, pid_file, "visible-sigterm"
    )

    # The driver did its own bookkeeping and left normally: it caught
    # the interruption and wrote down what happened.
    assert returncode == 0, stderr

    # The interruption is visible. A run that reported no error at all
    # would be a run that claims to have finished.
    assert reported["error"] is not None, (
        "the SIGTERM was swallowed: the run reported no interruption"
    )
    assert "HarnessInterrupted" in reported["error"]

    # The signal is named, so the reader can tell which one it was.
    assert str(int(signal.SIGTERM)) in reported["error"]

    # The run produced no result: the driver only records an exit code
    # when the runner *returns*, and a run that was cut short does not
    # return. An exit code here would be the run claiming a completion
    # it never reached.
    assert "exit_code" not in reported, (
        "the run reported an exit code: it claims to have finished"
    )

    # And the interruption cannot be swallowed by accident: it is a
    # BaseException on purpose, so an ``except Exception`` somewhere up
    # the stack cannot turn a stopped run into a normal one.
    assert issubclass(HarnessInterrupted, BaseException)
    assert not issubclass(HarnessInterrupted, Exception)

    # Being visible did not cost the workspace: the file is back, byte
    # for byte and mode for mode, exactly as it was found.
    assert reported["after"] == expected
    assert _fingerprint(target) == expected


# ---------------------------------------------------------------------------
# SEC-12 — a private file is still private afterwards
# ---------------------------------------------------------------------------


def test_private_mode_survives_the_run(tmp_path, monkeypatch):
    """A file's mode comes back as it was, and the adapter's own is private."""
    _no_sandbox(monkeypatch, tmp_path)

    workspace = _workspace(tmp_path)
    target = workspace / MCP_FILENAME

    original = b'{"mcpServers": {"before": {}}}\n'
    target.write_bytes(original)
    os.chmod(target, 0o600)
    before = _fingerprint(target)

    adapter = McodeHarness()
    with adapter.mcp_prepare(workspace, CONFIG):
        # The file the adapter writes carries a configuration, so it is
        # private while it is there.
        assert _mode(target) == 0o600
        assert target.read_bytes() != original

    # The file that was there is back, with its bytes and its mode.
    assert _fingerprint(target) == before
    assert _mode(target) == 0o600

    # The mode is *restored*, not forced: a file that was readable by
    # its owner and its group comes back that way too.
    other = _workspace(tmp_path, "other-workspace")
    other_target = other / MCP_FILENAME
    other_target.write_bytes(original)
    os.chmod(other_target, 0o640)
    other_before = _fingerprint(other_target)

    with adapter.mcp_prepare(other, CONFIG):
        assert _mode(other_target) == 0o600

    assert _fingerprint(other_target) == other_before
    assert _mode(other_target) == 0o640


# ---------------------------------------------------------------------------
# SEC-13 — two tasks at once do not see each other
# ---------------------------------------------------------------------------


def test_concurrent_tasks_do_not_see_each_other(tmp_path, monkeypatch):
    """Two concurrent runs keep their own MCP servers to themselves."""
    _no_sandbox(monkeypatch, tmp_path)

    data_dir = tmp_path / "account-data"
    data_dir.mkdir()
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(data_dir))

    barrier = tmp_path / "barrier"
    barrier.mkdir()

    sentinels = {"first": _sentinel(), "second": _sentinel()}
    credentials = {name: _credential() for name in sentinels}
    for name, sentinel in sentinels.items():
        monkeypatch.setenv(sentinel, credentials[name])

    probe = _write_probe(tmp_path)
    reports = {
        name: _report_path(tmp_path, f"{name}.json") for name in sentinels
    }
    pid_files = {name: tmp_path / f"{name}.pid" for name in sentinels}

    def _run(name: str) -> None:
        """One task, with its own workspace and its own servers."""
        workspace = _workspace(tmp_path, f"{name}-workspace")
        request = TaskInvocation(
            prompt="p",
            mcp_config=json.dumps(
                _config(sentinels[name], server=f"{name}-server")
            ),
            cwd=str(workspace),
        )
        invocation = _dispatch(
            McodeHarness(),
            request,
            probe,
            _probe_env(
                reports[name],
                pid_files[name],
                PROBE_MODE="barrier",
                PROBE_BARRIER=str(barrier),
                PROBE_NAME=name,
            ),
        )
        Runner().run(McodeHarness(), invocation)

    threads = [
        threading.Thread(target=_run, args=(name,), name=name)
        for name in sentinels
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=FILE_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "a concurrent run did not finish"

    # The two runs really overlapped: both children were holding their
    # own configuration at the same time.
    ready = sorted(p.name for p in barrier.iterdir())
    assert len(ready) == 2

    for name, other in (("first", "second"), ("second", "first")):
        seen = json.loads(reports[name].read_text(encoding="utf-8"))
        assert seen["mcp_json_exists"] is True

        # Its own server, and only its own.
        assert '"%s-server"' % name in seen["mcp_json_text"]
        assert '"%s-server"' % other not in seen["mcp_json_text"]

        # Its own reference, and not the other task's — neither the
        # other task's name nor the value its name stands for.
        assert "${%s}" % sentinels[name] in seen["mcp_json_text"]
        assert sentinels[other] not in seen["mcp_json_text"]
        assert credentials[other] not in seen["mcp_json_text"]

        # And not its own value either: the adapter passes the
        # reference through, it does not resolve it.
        assert credentials[name] not in seen["mcp_json_text"]

    # Neither run left its file behind.
    for name in sentinels:
        assert not (tmp_path / f"{name}-workspace" / MCP_FILENAME).exists()

    # The account's own registry was not touched: the run's servers did
    # not go into the user's persistent MCP registry.
    assert not (data_dir / "mcp.json").exists()
    assert not (data_dir / MCP_FILENAME).exists()


# ---------------------------------------------------------------------------
# SEC-14 — a backend this process cannot run stops it
# ---------------------------------------------------------------------------


def test_invalid_harness_blocks_startup(tmp_path, monkeypatch):
    """An unusable ``PDT_HARNESS`` stops the process before a task runs."""
    _no_sandbox(monkeypatch, tmp_path)

    # The registry refuses the value, and builds nothing: the check
    # happens before any factory is reached for.
    built: List[str] = []
    factories = {
        name: (lambda name=name: built.append(name)) for name in HARNESS_NAMES
    }
    with pytest.raises(HarnessConfigError) as excinfo:
        resolve_harness({"PDT_HARNESS": "not-a-backend"}, factories=factories)

    assert "not-a-backend" in str(excinfo.value)
    assert built == []

    # The same answer from the process-wide configuration.
    monkeypatch.setenv("PDT_HARNESS", "not-a-backend")
    reset_runtime_config_for_tests()
    try:
        with pytest.raises(HarnessConfigError):
            get_runtime_config()
    finally:
        reset_runtime_config_for_tests()

    # And the module that runs tasks cannot even be imported with such
    # a value — so no task runs, and nothing is written.
    marker = tmp_path / "the-task-ran.txt"
    script = (
        "import sys\n"
        "sys.path.insert(0, {root!r})\n"
        "import coding_tool\n"
        "open({marker!r}, 'w').write('the task ran')\n"
    ).format(root=str(_BACKEND_ROOT), marker=str(marker))

    def _import_with(value: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PDT_HARNESS"] = value
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    refused = _import_with("not-a-backend")
    assert refused.returncode != 0
    assert "HarnessConfigError" in refused.stderr
    assert not marker.exists()

    # The positive control: a backend the process knows how to run gets
    # past the check, so the refusal above is about the value and not
    # about the import.
    accepted = _import_with("claude")
    assert accepted.returncode == 0, accepted.stderr
    assert marker.read_text(encoding="utf-8") == "the task ran"


# ---------------------------------------------------------------------------
# SEC-15 — the profile's own configuration is not PDT's to write
# ---------------------------------------------------------------------------


def test_config_yaml_unchanged(tmp_path, monkeypatch):
    """A run leaves the account's own ``config.yaml`` exactly as it was."""
    _no_sandbox(monkeypatch, tmp_path)

    profile_home = tmp_path / "profile-home"
    data_dir = profile_home / ".minimax"
    data_dir.mkdir(parents=True)
    config_yaml = data_dir / "config.yaml"
    config_yaml.write_text(
        "model: the-account's-own-model\n", encoding="utf-8"
    )
    os.chmod(config_yaml, 0o600)

    before = _sha256(config_yaml)
    before_tree = _tree_digest(profile_home)

    binding = ProfileBinding(name="boundary-profile", home=str(profile_home))
    adapter = McodeHarness(binding)

    workspace = _workspace(tmp_path)
    sentinel = _sentinel()
    monkeypatch.setenv(sentinel, _credential())
    config = _config(sentinel)

    probe = _write_probe(tmp_path)
    report = _report_path(tmp_path)
    pid_file = tmp_path / "probe.pid"

    request = TaskInvocation(
        prompt="p",
        mcp_config=json.dumps(config),
        cwd=str(workspace),
    )
    invocation = _dispatch(
        adapter, request, probe, _probe_env(report, pid_file)
    )

    result = Runner().run(adapter, invocation)
    assert result.exit_code == 0

    # The profile's configuration file is byte for byte what it was.
    assert _sha256(config_yaml) == before
    assert config_yaml.read_text(encoding="utf-8") == (
        "model: the-account's-own-model\n"
    )

    # And nothing else in the profile's home changed either: PDT does
    # not take the account's directory over.
    assert _tree_digest(profile_home) == before_tree


# ---------------------------------------------------------------------------
# SEC-15 — and no code path writes such a file
# ---------------------------------------------------------------------------


#: The calls that put bytes on disk. A call site that writes is one of
#: these; a call site that reads is not.
_WRITE_CALLS = frozenset(
    {
        "write_text",
        "write_bytes",
        "open",
        "replace",
        "rename",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "rmtree",
        "remove",
        "unlink",
        "touch",
        "atomic_write_json",
        "atomic_write_text",
        "symlink_to",
        "hardlink_to",
    }
)


#: Where the path that is written to sits, for the functions a module
#: offers: ``args:N`` is the Nth argument of the call. ``os.replace``
#: and ``shutil.copy`` name the path they write at second, because the
#: first is where the bytes come from.
_MODULE_WRITES: Dict[str, int] = {
    "open": 0,
    "replace": 1,
    "rename": 1,
    "copy": 1,
    "copy2": 1,
    "copyfile": 1,
    "copytree": 1,
    "move": 1,
    "rmtree": 0,
    "remove": 0,
    "unlink": 0,
    "atomic_write_json": 0,
    "atomic_write_text": 0,
}


#: Where the path that is written to sits, for the methods a path
#: offers. ``receiver`` is the path the method is called on; a method
#: that names the other end — ``path.rename(new)`` — writes at the
#: argument instead.
_METHOD_WRITES: Dict[str, Any] = {
    "write_text": "receiver",
    "write_bytes": "receiver",
    "touch": "receiver",
    "unlink": "receiver",
    "symlink_to": "receiver",
    "hardlink_to": "receiver",
    "open": "receiver",
    "rename": 0,
}


#: The directories that hold code this repository did not write. A
#: virtualenv inside ``backend/`` is somebody else's code: a path that
#: appears in it is not a path PDT writes.
_FOREIGN_DIRS = frozenset(
    {
        "venv",
        "site-packages",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
    }
)


def _backend_sources() -> List[Path]:
    """Every first-party source file under ``backend/``.

    The tests are not first-party code — they are the cases, not the
    code the cases are about — and neither is anything inside a
    virtualenv, a ``node_modules`` or a build directory.
    """
    sources: List[Path] = []
    for path in sorted(_BACKEND_ROOT.rglob("*.py")):
        relative = path.relative_to(_BACKEND_ROOT)
        if "tests" in relative.parts:
            continue
        if path.name.startswith("test_"):
            continue
        if any(
            part in _FOREIGN_DIRS or part.startswith(".")
            for part in relative.parts
        ):
            continue
        sources.append(path)
    return sources


def _module_aliases(tree: ast.AST) -> Set[str]:
    """The module names a file imports.

    This is what tells ``os.rename(src, dst)`` — which writes at the
    second argument — from ``path.rename(dst)`` — which writes at the
    first one. The two have the same name and opposite arguments, and
    only the receiver says which is which.

    Args:
        tree: The parsed file.

    Returns:
        The names the file imports.
    """
    aliases: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases.add(alias.asname or alias.name)
    return aliases


def _is_write_call(node: ast.Call) -> bool:
    """Whether a call writes to the disk.

    ``open`` only counts when it asks for a writing mode: the same call
    with ``"r"`` is a read, and a scan that counted it would report
    reads as writes.

    Args:
        node: The call to judge.

    Returns:
        Whether the call writes.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return False

    if name not in _WRITE_CALLS:
        return False

    if name == "open":
        mode: Optional[str] = None
        for argument in node.args[1:2]:
            if isinstance(argument, ast.Constant):
                mode = argument.value
        for keyword in node.keywords:
            if keyword.arg == "mode" and isinstance(
                keyword.value, ast.Constant
            ):
                mode = keyword.value.value
        if not (isinstance(mode, str) and any(ch in mode for ch in "wax+")):
            return False

    return True


def _write_targets(node: ast.Call, modules: Set[str]) -> List[ast.AST]:
    """The expressions a write call puts its bytes at.

    The path a write lands on is not always an argument: ``path.write_bytes``
    writes at the path the method is called on, and the argument is
    the data. A scan that only looked at the arguments would read that
    call backwards.

    Args:
        node: The call to judge.
        modules: The module names the file imports.

    Returns:
        The expressions that name the path.
    """
    func = node.func
    targets: List[ast.AST] = []

    if isinstance(func, ast.Name):
        position = _MODULE_WRITES.get(func.id)
        if position is not None and len(node.args) > position:
            targets.append(node.args[position])
    elif isinstance(func, ast.Attribute):
        receiver = func.value
        is_module = isinstance(receiver, ast.Name) and receiver.id in modules
        if is_module:
            position = _MODULE_WRITES.get(func.attr)
            if position is not None and len(node.args) > position:
                targets.append(node.args[position])
        else:
            position = _METHOD_WRITES.get(func.attr)
            if position == "receiver":
                targets.append(receiver)
            elif (
                isinstance(position, int)
                and len(node.args) > position
            ):
                targets.append(node.args[position])

    for keyword in node.keywords:
        if keyword.arg in ("path", "dst", "destination", "target"):
            targets.append(keyword.value)

    return targets


def _bindings(tree: ast.AST) -> Dict[str, List[ast.AST]]:
    """Every expression a name is bound to, in one file.

    A name that is bound in several places is bound to all of them.
    The scan has to catch a write whose path *is* a ``config.yaml``,
    and a name that is bound to such a path anywhere in the file is
    worth looking at wherever it is written.

    Returns:
        The names, and the expressions they are bound to.
    """
    bindings: Dict[str, List[ast.AST]] = {}
    for node in ast.walk(tree):
        value: Optional[ast.AST] = None
        names: List[ast.Name] = []
        if isinstance(node, ast.Assign):
            value = node.value
            names = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            names = [node.target] if isinstance(node.target, ast.Name) else []
        elif isinstance(node, ast.NamedExpr):
            value = node.value
            names = [node.target] if isinstance(node.target, ast.Name) else []
        if value is None:
            continue
        for name in names:
            bindings.setdefault(name.id, []).append(value)
    return bindings


def _names_config_yaml(
    node: ast.AST,
    bindings: Dict[str, List[ast.AST]],
    cache: Dict[str, bool],
    stack: Set[str],
    depth: int = 0,
) -> bool:
    """Whether an expression can be a path to a ``config.yaml``.

    The expression is followed through the names it uses: a path is
    very often built in one statement and written in the next, and a
    scan that only looked at the write call's own arguments would miss
    exactly that.

    A path is recognised by the string it is built from — ``"config.yaml"``
    — and a name is followed into the expressions it is bound to. What
    each name is bound to is worked out once and remembered: a name is
    mentioned from every expression that uses it, and without that the
    scan would walk the same expressions again and again until it ran
    out of patience.

    Args:
        node: The expression to judge.
        bindings: The names and what they are bound to.
        cache: What is already known about a name.
        stack: The names currently being followed, so a cycle ends.
        depth: How far the follow has gone.

    Returns:
        Whether the expression names a ``config.yaml``.
    """
    if depth > 8:
        return False

    for child in ast.walk(node):
        if isinstance(child, ast.Constant):
            if isinstance(child.value, str) and "config.yaml" in child.value:
                return True
            continue
        if not isinstance(child, ast.Name):
            continue
        if child.id in cache:
            if cache[child.id]:
                return True
            continue
        if child.id in stack:
            continue
        stack.add(child.id)
        found = False
        for bound in bindings.get(child.id, []):
            if _names_config_yaml(bound, bindings, cache, stack, depth + 1):
                found = True
                break
        stack.discard(child.id)
        cache[child.id] = found
        if found:
            return True
    return False


def _config_yaml_constants() -> Dict[str, List[str]]:
    """The names that stand for a path ending in ``config.yaml``.

    A constant like ``BACKEND_CONFIG_YAML`` is a write target even
    though the string ``config.yaml`` does not appear at the call
    site, so the scan has to know about them.

    Returns:
        The constant names, and the files that define them.
    """
    constants: Dict[str, List[str]] = {}
    for path in _backend_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                name, value = node.target, node.value
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name, value = node.targets[0], node.value
            else:
                continue
            if not isinstance(name, ast.Name) or value is None:
                continue
            if (
                isinstance(value, ast.BinOp)
                and isinstance(value.op, ast.Div)
                and isinstance(value.right, ast.Constant)
                and value.right.value == "config.yaml"
            ):
                constants.setdefault(name.id, []).append(str(path))
    return constants


def test_no_code_path_writes_a_config_yaml():
    """No source file writes a ``config.yaml`` — the account's or ours.

    The dynamic half of SEC-15 says a run does not change the profile's
    configuration. This is the half that says *no code path could*:
    the scan walks every first-party source file, finds every call
    that writes, follows the path it writes at back through the names
    that path is built from, and checks that none of them ends at a
    ``config.yaml`` — whether the name is written out at the call site
    or reached through a variable that stands for such a path.
    """
    constants = _config_yaml_constants()
    offenders: List[str] = []

    for path in _backend_sources():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        bindings = _bindings(tree)
        modules = _module_aliases(tree)
        cache: Dict[str, bool] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_write_call(node):
                continue
            for target in _write_targets(node, modules):
                if _names_config_yaml(target, bindings, cache, set()):
                    offenders.append(
                        "{}:{}: {}".format(
                            path,
                            node.lineno,
                            ast.unparse(target),
                        )
                    )

    assert offenders == [], (
        "a code path writes a config.yaml:\n{}".format("\n".join(offenders))
    )

    # The scan is looking at something: the backend's own configuration
    # file is a constant the scan knows about, and it is read, not
    # written.
    assert constants, (
        "the scan found no config.yaml constant — the rule it enforces "
        "would pass vacuously"
    )
