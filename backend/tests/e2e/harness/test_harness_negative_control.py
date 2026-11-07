"""The harness refuses to run a backend it was told to use — and says so.

Why these controls exist
========================

The registry resolves ``PDT_HARNESS`` to a backend, and a backend that
is *named* is not a backend that is *installed*. Two refusals have to
hold, and neither of them is enforced anywhere else:

  1. **A backend that is not installed fails, loudly.** The operator
     asked for ``mcode``; ``mcode`` is not on this machine. The run
     must fail, and it must name the backend that is missing. The one
     thing it must not do is answer through a *different* backend: a
     run that silently continued on ``claude`` would spend another
     provider's money under another provider's terms and report a
     success that belongs to a backend nobody asked for.

  2. **A backend that does not exist at all is refused before the
     service starts.** ``PDT_HARNESS=mcode-x`` is a typo, and a typo
     that resolves to some default backend is a typo that costs a plan
     its account.

Both are negative controls, and a negative control is only a control if
it can fail: each case below builds a world in which the wrong thing
would be *visible* if it happened, and then asserts that it did not
happen.

The instrument
==============

The world each control builds is the same: a ``PATH`` that carries a
recording ``claude`` and no ``mcode``. The recording ``claude`` appends
a line to a file every time it is run, so "the run fell back to
claude" is a fact on disk rather than an opinion about a log. A
fallback that nobody can see is a fallback that cannot be disproved.

A counter that has never counted anything is not evidence either, so
each control runs the recording ``claude`` once by hand before it runs
the scenario: the mark it leaves is checked, and the file is emptied.
After that, "the recording was never run" is a statement about the
scenario rather than about a launcher that was never executable.

The processes
=============

The controls run real processes and never mock ``subprocess.Popen``:
what is under test is what the service does when the backend it
resolved cannot be started, and a mocked spawn proves nothing about
that. The first case runs the service's own dispatch, in a process of
its own, with the doctored ``PATH``; the second runs the service's own
start-up.

Neither case needs a model, a network, or the MiniMax CLI, so neither
is gated: they carry no ``e2e`` marker and no opt-in variable, and they
run in every lane that collects them, on every commit. The file lives
under ``tests/e2e/`` because it is the end-to-end counterpart of the
real chain, not because it needs the chain.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

# --------------------------------------------------------------------------
# Where everything lives
# --------------------------------------------------------------------------

#: The backend's own source tree.  ``backend/tests/e2e/harness`` sits
#: three levels below it.
BACKEND_DIR = Path(__file__).resolve().parents[3]

#: The repository's root: the backend is a package inside it, so a
#: process that imports ``server`` has to be able to import
#: ``backend.*`` as well.
PROJECT_ROOT = Path(__file__).resolve().parents[4]

#: The interpreter that owns the backend's dependencies.  The controls
#: run the real service, so they run the service's own interpreter.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The backend the controls configure.
HARNESS_NAME = "mcode"

#: A value no backend answers to.
ILLEGAL_HARNESS = "mcode-x"

#: The system directories every control's ``PATH`` ends with.  The
#: MiniMax CLI is not among them -- that is the point of the control.
SYSTEM_PATH = os.pathsep.join(("/usr/bin", "/bin"))

#: How long a control waits for the process it started.  It is a
#: ceiling on a hung child, not a measurement of anything.
CONTROL_TIMEOUT = 120.0

# --------------------------------------------------------------------------
# The programs the controls run
# --------------------------------------------------------------------------

#: The ``claude`` a fallback would find.  It records that it was called
#: and exits cleanly: a control that counts fallbacks has to be able to
#: count one.
_FAKE_CLAUDE = """#!/bin/sh
# A ``claude`` that records that it was called.  Nothing else ever runs
# it -- a fallback to the claude backend would, and that is the whole
# point of the control.
printf 'claude fallback: %s\\n' "$*" >> "@@MARKER@@"
exit 0
"""

#: The first task of a service whose selected backend is not installed.
#:
#: The program is the service's own: it resolves the backend the way the
#: service resolves it -- from the environment, once, at import time --
#: and then runs one task through the production dispatch.  Nothing in
#: it stands in for the service; it *is* the service, in a process of
#: its own, so the failure it reports is the failure the service would
#: report.
_FIRST_TASK = '''\
"""The first task of a service whose selected backend is not installed."""

import os
import sys

from harness.registry import get_runtime_config

config = get_runtime_config()
print(
    "HARNESS_SELECTED:",
    config.name,
    type(config.harness).__name__,
    flush=True,
)

import coding_tool

workspace = os.environ["PDT_TASK_CWD"]
tool = coding_tool.ClaudeCodingTool(cwd=workspace)
try:
    tool._run_claude_interactive("say OK", harness=config.harness)
except BaseException as error:
    print(
        "FIRST_TASK_FAILED:",
        type(error).__name__,
        str(error),
        flush=True,
    )
    raise SystemExit(3)
print("FIRST_TASK_SUCCEEDED", flush=True)
raise SystemExit(0)
'''


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _python_path() -> str:
    """The import path the real service needs.

    The backend is a package inside the repository, so a process that
    serves it has to be able to import ``backend.*`` as well as the
    backend's own top-level modules.
    """
    entries = [BACKEND_DIR, PROJECT_ROOT, PROJECT_ROOT / "tools"]
    return os.pathsep.join(
        str(entry) for entry in entries if Path(entry).exists()
    )


def _real_home() -> str:
    """The home the process was launched with.

    The hermetic test environment replaces ``HOME`` with a scratch
    directory, which is right for a test that must not touch the
    operator's files and wrong for a process that is the service.  The
    real home is published by the hermetic environment itself, so it is
    read from there rather than written down here.
    """
    return (
        os.environ.get("PDT_TEST_REAL_HOME")
        or os.environ.get("HOME")
        or str(Path.home())
    )


def _free_port() -> int:
    """A loopback port that is free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _control_environment(tmp_path: Path, **overrides: str) -> Dict[str, str]:
    """The environment a control's process runs in.

    **Not** the ambient environment.  The environment a developer runs
    a shell in carries things that only mean something inside the
    process that set them -- most of all ``PDT_SECRET_FD_*``, which
    names a *file descriptor number* a launcher published a secret on.
    Those numbers are the launcher's, not the server's, and a server
    that inherited them would read whatever happens to sit on descriptor
    3 in its own process.

    So the process is given what it needs and nothing else: the paths
    of its own world, the backend it drives, and the path it has to
    import from.  The home is the real one, because a process that is
    the service has to reach the operator's configuration.

    Args:
        tmp_path: The control's own tree.
        **overrides: Variables to set or replace.

    Returns:
        The environment to start the process with.
    """
    for directory in ("plans", "config"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": SYSTEM_PATH,
        "HOME": _real_home(),
        "PYTHONPATH": _python_path(),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PDT_HARNESS": HARNESS_NAME,
        "PDT_PROVIDER_MODE": "inherit",
        "PDT_PLANS_DIR": str(tmp_path / "plans"),
        "PDT_CONFIG_DIR": str(tmp_path / "config"),
        "PDT_STATE_DB_PATH": str(tmp_path / "state.db"),
        "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
        "PDT_DISABLE_TEMP_SWEEP": "1",
    }
    env.update(overrides)
    return env


def _recording_claude(bin_dir: Path, marker: Path) -> Path:
    """Put a recording ``claude`` into ``bin_dir``.

    Args:
        bin_dir: The directory that becomes the front of the ``PATH``.
        marker: The file the recording appends to.

    Returns:
        The launcher that was written.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "claude"
    launcher.write_text(
        _FAKE_CLAUDE.replace("@@MARKER@@", str(marker)), encoding="utf-8"
    )
    launcher.chmod(0o755)
    return launcher


def _claude_fallbacks(marker: Path) -> int:
    """How many times the recording ``claude`` was run."""
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    return len([line for line in lines if line.strip()])


def _prove_the_instrument(bin_dir: Path, marker: Path) -> None:
    """Prove that the recording ``claude`` can record, and then reset it.

    A counter that has never counted anything is not evidence.  The
    controls below claim "the run never fell back to claude", and that
    claim is only worth something if the recording would have caught a
    fallback -- so the recording is run once, by hand, and the mark it
    leaves is checked.  Without this, a ``PATH`` that was doctored
    wrongly (a launcher that was never executable, a directory that was
    never on the ``PATH`` at all) would leave the marker empty and the
    controls would pass while watching nothing.

    The marker is emptied afterwards, so the count the controls read is
    the count of the *scenario's* fallbacks and nothing else.

    Args:
        bin_dir: The directory that holds the recording ``claude``.
        marker: The file the recording appends to.
    """
    launcher = bin_dir / "claude"
    assert launcher.is_file(), (
        f"the recording claude is not at {launcher}, so nothing could have "
        f"recorded a fallback"
    )
    assert os.access(launcher, os.X_OK), (
        f"the recording claude at {launcher} is not executable, so the "
        f"``PATH`` could not have found it"
    )
    probe = subprocess.run(
        [str(launcher), "probe"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, (
        f"the recording claude did not run: {probe.stderr!r}"
    )
    assert _claude_fallbacks(marker) == 1, (
        f"the recording claude ran but recorded nothing in {marker}, so a "
        f"fallback would have gone unnoticed"
    )
    marker.write_text("", encoding="utf-8")


def _failure_text(output: str) -> Optional[str]:
    """The text the child reported as the failure, or ``None``.

    Only the line that carries the failure counts.  The child's output
    also names the backend it *selected*, and a check that looked at the
    whole output would find the backend's name there and call the
    failure explicit when it never said a word about it.
    """
    for line in output.splitlines():
        if line.startswith("FIRST_TASK_FAILED:"):
            return line[len("FIRST_TASK_FAILED:") :].strip()
    return None


# --------------------------------------------------------------------------
# 1. A backend that is not installed
# --------------------------------------------------------------------------


def test_missing_binary_fails_without_fallback(tmp_path):
    """A backend that is not installed fails the task, and never falls back.

    The world is the one the operator would be in: ``PDT_HARNESS``
    names a backend that is legal, and that backend is not on the
    machine.  The service starts -- the *name* is a name the registry
    knows -- so the failure belongs to the first task, and that is where
    this control looks for it.

    What has to be true afterwards:

      * the task failed;
      * the failure named the backend that is missing;
      * the recording ``claude`` was never run.

    The third one is the whole point.  A fallback would not fail, and
    a run that answered through another backend would look exactly like
    a run that worked.

    Args:
        tmp_path: The control's own tree.
    """
    bin_dir = tmp_path / "bin"
    marker = tmp_path / "claude-calls.log"
    _recording_claude(bin_dir, marker)
    # The instrument has to be able to record before "nothing was
    # recorded" means anything.
    _prove_the_instrument(bin_dir, marker)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    script = tmp_path / "first_task.py"
    script.write_text(_FIRST_TASK, encoding="utf-8")

    env = _control_environment(
        tmp_path,
        PATH=os.pathsep.join((str(bin_dir), SYSTEM_PATH)),
        PDT_TASK_CWD=str(workspace),
    )

    completed = subprocess.run(
        [str(VENV_PYTHON), str(script)],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=CONTROL_TIMEOUT,
    )
    output = f"{completed.stdout}\n{completed.stderr}"

    assert completed.returncode != 0, (
        "the task succeeded with the MiniMax CLI missing from PATH. The "
        "backend that was asked for cannot run, so the run has to fail -- "
        f"answering through some other backend is the failure this "
        f"control exists to catch.\n{output}"
    )
    assert "HARNESS_SELECTED: mcode" in output, (
        "the process did not select the mcode backend, so the control did "
        f"not test what it claims to test.\n{output}"
    )

    failure = _failure_text(output)
    assert failure is not None, (
        "the task failed without saying so. A failure that names nothing "
        f"is indistinguishable from a crash.\n{output}"
    )
    assert "mcode" in failure, (
        f"the failure does not name the backend that is missing: "
        f"{failure!r}"
    )
    assert _claude_fallbacks(marker) == 0, (
        "the run fell back to the claude backend: "
        f"{_claude_fallbacks(marker)} call(s) were recorded in {marker}."
    )


# --------------------------------------------------------------------------
# 2. A backend that does not exist
# --------------------------------------------------------------------------


def test_invalid_harness_refuses_start(tmp_path):
    """An illegal ``PDT_HARNESS`` stops the service before it serves anything.

    The service is started for real -- a real uvicorn, a real
    ``server:app`` -- with a value the registry does not know.  The
    refusal has to happen during start-up: a service that accepted the
    value and went on to serve requests would be a service running a
    backend nobody chose.

    What has to be true afterwards:

      * the process exited, and it exited non-zero;
      * the refusal named the illegal value and the values that are
        legal, so the operator can see what to type instead;
      * the recording ``claude`` was never run.

    Args:
        tmp_path: The control's own tree.
    """
    bin_dir = tmp_path / "bin"
    marker = tmp_path / "claude-calls.log"
    _recording_claude(bin_dir, marker)
    # The instrument has to be able to record before "nothing was
    # recorded" means anything.
    _prove_the_instrument(bin_dir, marker)

    env = _control_environment(
        tmp_path,
        PATH=os.pathsep.join((str(bin_dir), SYSTEM_PATH)),
        PDT_HARNESS=ILLEGAL_HARNESS,
    )

    port = _free_port()
    process = subprocess.Popen(
        [
            str(VENV_PYTHON),
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate(timeout=CONTROL_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        raise AssertionError(
            f"the service kept running with PDT_HARNESS={ILLEGAL_HARNESS!r}. "
            "An illegal backend name has to be refused before the service "
            f"serves anything.\n{output}"
        )

    assert process.returncode != 0, (
        f"the service started with PDT_HARNESS={ILLEGAL_HARNESS!r} and "
        f"exited zero. An illegal backend name has to stop it.\n{output}"
    )
    assert ILLEGAL_HARNESS in output, (
        f"the refusal does not name the illegal value:\n{output}"
    )
    assert "HarnessConfigError" in output, (
        "the refusal is not the registry's own error, so it is not the "
        f"refusal this control is about:\n{output}"
    )
    assert "'claude'" in output and "'mcode'" in output, (
        "the refusal does not name the values that are legal, so the "
        f"operator is told no and shown nothing:\n{output}"
    )
    assert _claude_fallbacks(marker) == 0, (
        "the service fell back to the claude backend: "
        f"{_claude_fallbacks(marker)} call(s) were recorded in {marker}."
    )
