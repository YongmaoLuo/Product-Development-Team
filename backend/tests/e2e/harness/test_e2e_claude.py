"""E2E-CLAUDE — the claude backend, end to end, and the baseline.

Why this scenario exists
========================

The mcode scenario proves that the MiniMax Code backend can be driven
for real.  It cannot, on its own, say whether that backend *behaves
like the one it replaces*: a run that only ever happens against one
backend has no baseline to be measured against, so "equivalent" has
nothing to be equivalent to.

So this file runs **one task** through a backend and hands back what
that run produced, in a shape two backends can be compared on.  The
comparison itself lives in :mod:`test_e2e_parity`; this file owns the
machinery and the claude baseline's own nine assertions.

What is compared
================

The two backends are compared on the things that are supposed to be
the same, and only on those:

  * the files the run changed -- path and content hash;
  * the exit code and the normalised stdout of the task's target
    command;
  * the *categories* of the event stream;
  * the keys of the usage block;
  * the structure of the report the run answered with.

What is **not** compared is the prose.  Two models answer in different
words, and comparing words would measure the models rather than the
backends that carry them.  Latency is not compared either: the two
backends are different programs and are not expected to take the same
time.  The counts of the events are not compared either -- one CLI
reports a thinking token per line and the other reports one message
per event, so the *counts* measure the two CLIs' reporting styles,
not the shape of the run.

The nine assertions
===================

They are the claude-shaped counterparts of the nine the mcode scenario
fixes, and each one is answered by evidence produced by the run itself
-- never by a recorded fixture:

  1. the execution directory is outside ``backend/`` (the guard that
     stops a plan from executing inside the service's own source tree);
  2. the run exited 0, produced a ``result`` event, and reported no
     error;
  3. ``hello.txt`` exists and its content hash is the expected one;
  4. the usage block carries all four canonical keys and
     ``input_tokens > 0``;
  5. the session id the CLI reports is the pinned one, and it has the
     shape of a uuid;
  6. the pinned session id is a uuid5 derived from the plan and the
     task, and the audit sidecar agrees with it;
  7. the MCP configuration travelled on the command line and never
     landed in the workspace, and git never saw a ``.mcp.json``;
  8. the run was confined: the runner reports ``sandbox_state ==
     "applied"``, the process table shows the very process the runner
     spawned still running while the run was in flight, and a write
     outside the workspace was denied with a permission error;
  9. nothing was left running when the run ended.

Cost and blast radius
=====================

The scenario costs one real model call and spawns a real server, so it
is off unless ``PDT_E2E_HARNESS=1`` is set: the local and CI
deterministic gates only *collect* it.  It is marked ``real_model`` (it
really calls a model) and ``e2e`` (it drives a real process), so the
cloud lanes skip it twice over.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple

import pytest

# --------------------------------------------------------------------------
# Where everything lives
# --------------------------------------------------------------------------

#: The backend's own source tree.  ``backend/tests/e2e/harness`` sits
#: four levels below it, so this is the tree the scenario must not
#: execute in.
BACKEND_DIR = Path(__file__).resolve().parents[3]

#: The repository's root.  The backend is a package inside it, so a
#: process that imports ``server`` has to be able to import
#: ``backend.*`` as well -- which means the root has to be on the
#: path, exactly as the backend's own pytest configuration has it.
PROJECT_ROOT = Path(__file__).resolve().parents[4]

#: The interpreter that owns the backend's dependencies.  The scenario
#: runs the real service, so it runs the service's own interpreter.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The variable that turns the scenario on.  Without it the scenario
#: is collected and skipped: the deterministic gates never pay for a
#: real model call.
E2E_ENV_VAR = "PDT_E2E_HARNESS"

#: The header every guarded request carries.  The same header, and the
#: same value, the real client sends.
REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.real_model,
    pytest.mark.skipif(
        os.environ.get(E2E_ENV_VAR) != "1",
        reason=(
            "the real harness chains need PDT_E2E_HARNESS=1: they spawn "
            "the real CLIs under the real macOS seatbelt and cost a model "
            "call, so the deterministic gates only collect them"
        ),
    ),
]

# --------------------------------------------------------------------------
# The scenario
# --------------------------------------------------------------------------

#: The two backends the scenario drives.
CLAUDE = "claude"
MCODE = "mcode"

#: The model each backend is pinned to.  The names differ on purpose:
#: the two backends name their models differently, and a comparison
#: that insisted on one name would be comparing the naming, not the
#: behaviour.
CLAUDE_MODEL = "claude-sonnet-5-5"
MCODE_MODEL = "minimax/MiniMax-M3.1-Flash"

#: The plan and the task the run belongs to.  The task's key is the
#: one the executor uses for a task's session (``task:<id>``), and the
#: pinned id is derived from the plan and that key.
PLAN_ID = "e2e-claude-baseline"
MCODE_PLAN_ID = "e2e-parity-mcode"
STEP_KEY = "task:1"

#: The fixed task.  Fixed on purpose: a moving task would make the run
#: incomparable and the artefact assertions meaningless.  The report is
#: part of it because the report's *structure* is one of the things the
#: two backends are compared on -- and the heading lines are pinned
#: character for character so that the comparison measures the
#: backends rather than the two models' taste in markdown.
FIXED_TASK = (
    "Create a file named hello.txt in the current working directory.\n"
    "Its content must be exactly the single line: hello e2e\n"
    "followed by one newline character (10 bytes in total).\n"
    "Create no other file.\n"
    "Then run `cat hello.txt` and report its output verbatim.\n"
    "\n"
    "Then answer with a markdown report that has exactly three headings.\n"
    "Copy each of these three heading lines character for character,\n"
    "one per line, in this order:\n"
    "\n"
    "# Summary\n"
    "## Changes\n"
    "## Verification\n"
    "\n"
    "Write one short line of prose under each heading, and use no other\n"
    "heading anywhere in your answer.\n"
)

#: The command the task names, and what it has to answer.  It is run
#: against the artefact the run produced, in the run's own workspace,
#: so both backends are measured the same way.
TARGET_COMMAND = ("cat", "hello.txt")
TARGET_STDOUT = "hello e2e"

#: The hashes the artefact is allowed to have.  Two are allowed because
#: "one trailing newline is fine" leaves the newline optional; the
#: *content* is the same either way.
HELLO_HASHES = frozenset(
    {
        hashlib.sha256(f"{TARGET_STDOUT}\n".encode("utf-8")).hexdigest(),
        hashlib.sha256(TARGET_STDOUT.encode("utf-8")).hexdigest(),
    }
)

#: The report the task asks for: the heading level and the heading
#: text, in the order they have to appear.
REQUIRED_REPORT_SECTIONS: Tuple[Tuple[int, str], ...] = (
    (1, "Summary"),
    (2, "Changes"),
    (2, "Verification"),
)

#: The canonical usage block: four keys, no more.  The fourth is
#: ``None`` when the CLI never reported cache creation -- that is a
#: missing measurement, not a zero.
USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)

#: The categories an event can fall into.  ``diagnostic`` is the one
#: that is *not* part of the run's shape: a line the CLI wrote to its
#: standard error is a remark about the run, not a step of it, and the
#: two CLIs do not remark the same number of times.
EVENT_CATEGORIES = ("assistant", "result", "system", "tool")
DIAGNOSTIC_CATEGORY = "diagnostic"

#: The MCP server the workspace configuration names.  It is written to
#: disk rather than inlined, because the configuration has to survive
#: the trip through the workspace's ``.mcp.json`` as a real file.
MARKER_SERVER_SOURCE = '''\
"""An MCP server that answers two questions at once.

It records that the CLI started it -- which can only happen if the CLI
read the MCP configuration the scenario handed it -- and it tries to
write *outside* the workspace.  Under the scenario's seatbelt profile
that write must be refused: if it succeeds, the confinement is only
ever claimed.
"""

import json
import os
import pathlib
import sys


def main() -> int:
    marker = pathlib.Path(os.environ["MCP_MARKER_FILE"])
    escape = pathlib.Path(os.environ["MCP_ESCAPE_FILE"])
    report = pathlib.Path(os.environ["MCP_REPORT_FILE"])
    outcome = {"marker_written": False, "escape_error": None}
    try:
        marker.write_text("mcp server started\\n", encoding="utf-8")
        outcome["marker_written"] = True
    except OSError:
        pass
    try:
        escape.write_text("escaped\\n", encoding="utf-8")
    except OSError as exc:
        outcome["escape_error"] = exc.errno
    try:
        report.write_text(json.dumps(outcome), encoding="utf-8")
    except OSError:
        pass
    sys.stdin.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: The files the MCP probe writes into the workspace.  They are not the
#: task's artefacts, so git is told to ignore them: what git reports is
#: then exactly what the fixed task produced.
PROBE_FILES = ("mcp-marker.txt", "mcp-report.json")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _environment(**overrides: Optional[str]) -> Iterator[None]:
    """Set environment variables for the duration of a block.

    ``monkeypatch`` cannot be used here: the fixtures are
    session-scoped and ``monkeypatch`` is function-scoped, while these
    variables have to outlive a single test.

    Args:
        **overrides: The variables to set.  A value of ``None`` removes
            the variable for the duration of the block.

    Yields:
        Nothing; the block is the point.
    """
    saved = {name: os.environ.get(name) for name in overrides}
    try:
        for name, value in overrides.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _real_home() -> str:
    """The home the process was launched with.

    The hermetic test environment replaces ``HOME`` with a scratch
    directory, which is right for a test that must not touch the
    operator's files -- and wrong for a run that has to authenticate
    against a real account.  The real home is published by the
    hermetic environment itself, so it is read from there rather than
    written down here.
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


def _python_path() -> str:
    """The import path the real service needs.

    The backend is a package inside the repository, so a process that
    serves it has to be able to import ``backend.*`` as well as the
    backend's own top-level modules.  The backend's pytest
    configuration already says so; this is the same three entries,
    handed to a subprocess that does not read that file.
    """
    entries = [BACKEND_DIR, PROJECT_ROOT, PROJECT_ROOT / "tools"]
    return os.pathsep.join(
        str(entry) for entry in entries if Path(entry).exists()
    )


def _server_environment(
    *,
    harness: str,
    plans_dir: Path,
    config_dir: Path,
    state_db: Path,
    profile_path: Path,
) -> Dict[str, str]:
    """The environment the real service is started with.

    **Not** the ambient environment.  The environment a developer runs
    a shell in carries things that only mean something inside the
    process that set them -- most of all ``PDT_SECRET_FD_*``, which
    names a *file descriptor number* a launcher published a secret on.
    Those numbers are the launcher's, not the server's: a server that
    inherited them would read whatever happens to sit on descriptor 3
    in its own process and then close it, which on macOS takes the
    event loop's own socket with it.

    So the server is given what it needs and nothing else: the paths of
    its own world, the harness it drives, and the path it has to import
    from.  The home is the real one, because a service that cannot
    reach the operator's configuration is not the service.

    Args:
        harness: The backend the server drives.
        plans_dir: The server's plans directory.
        config_dir: The server's configuration directory.
        state_db: The server's state database.
        profile_path: The seatbelt profile the server applies.

    Returns:
        The environment to start the server with.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": _real_home(),
        "PYTHONPATH": _python_path(),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PDT_HARNESS": harness,
        E2E_ENV_VAR: "1",
        "PDT_PLANS_DIR": str(plans_dir),
        "PDT_CONFIG_DIR": str(config_dir),
        "PDT_STATE_DB_PATH": str(state_db),
        "PDT_SANDBOX_PROFILE": str(profile_path),
        "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
        "PDT_DEBUG_ROUTES": "1",
        "PDT_DISABLE_TEMP_SWEEP": "1",
        "PDT_PROVIDER_MODE": "inherit",
    }


def _sandbox_profile(root: Path, workspace: Path) -> str:
    """The seatbelt profile this scenario runs under.

    Two rules, in this order, because the later one wins:

      * writes anywhere in the scenario's own temporary tree are
        refused -- that tree belongs to the scenario, and a run that
        wrote outside its workspace would be writing into the state
        the assertions read; and
      * writes inside the workspace are allowed again, because that is
        where the fixed task is supposed to write.

    Everything else stays allowed: the CLIs keep their account files
    under the real home, and a profile that refused those would not be
    testing the confinement but the CLI's ability to start.

    Args:
        root: The scenario's temporary tree.
        workspace: The directory the run is supposed to write in.

    Returns:
        The profile, as seatbelt source.
    """
    return (
        "(version 1)\n"
        "(allow default)\n"
        f'(deny file-write* (subpath "{root}"))\n'
        f'(allow file-write* (subpath "{workspace}"))\n'
    )


def _git_status(workspace: Path) -> str:
    """The workspace's ``git status --porcelain``, or ``""`` on failure."""
    completed = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _stop_process_group(process: subprocess.Popen) -> None:
    """Stop a child and everything it started.

    The child was started in its own session, so the process *group* is
    the unit that has to be signalled: signalling only the leader would
    leave the helpers it spawned running.
    """
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), 15)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - the child hung
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        process.wait(timeout=10)


def _read_sidecar(plan_dir: Path) -> Dict[str, Any]:
    """The sidecar's ``sessions`` mapping, or ``{}`` when there is none."""
    from plan_sessions import sessions_path

    try:
        data = json.loads(sessions_path(plan_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = data.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


# --------------------------------------------------------------------------
# Reading a run back
# --------------------------------------------------------------------------

#: A markdown heading: the hash run, then the text.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")


def _report_sections(text: str) -> Tuple[Tuple[int, str], ...]:
    """The headings of a report, as ``(level, title)`` pairs.

    A heading inside a fenced code block is not a heading: the block
    is quoted material, and a model that quotes the task's own heading
    lines inside a fence has not produced a section.

    Args:
        text: The report.

    Returns:
        One pair per heading, in the order they appear.
    """
    sections: List[Tuple[int, str]] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _HEADING_RE.match(stripped)
        if match:
            sections.append((len(match.group(1)), match.group(2).strip()))
    return tuple(sections)


def _carries_tool_activity(event: Any) -> bool:
    """Whether an event carries a tool call or a tool result.

    The two CLIs report the same fact in their own shapes: the claude
    CLI puts a ``tool_use`` block in the assistant message and the
    ``tool_result`` block in the user message that follows it, while
    mcode puts the call and its output in one ``tool_call`` item.

    Args:
        event: The event to read.

    Returns:
        Whether the event is about tool activity.
    """
    raw = getattr(event, "raw", None)
    if not isinstance(raw, dict):
        return False

    message = raw.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("tool_use", "tool_result"):
                    return True

    inner = raw.get("raw")
    if isinstance(inner, dict):
        item = inner.get("item")
        if isinstance(item, dict) and item.get("type") == "tool_call":
            return True
        if str(inner.get("type", "")).startswith("tool."):
            return True
    return False


def _event_category(event: Any) -> str:
    """The category one event falls into.

    Args:
        event: The event to categorise.

    Returns:
        One of :data:`EVENT_CATEGORIES`, or
        :data:`DIAGNOSTIC_CATEGORY` for a remark the CLI made about
        the run rather than a step of it.
    """
    kind = getattr(event, "type", "")
    if kind == "result":
        return "result"
    if kind == "system":
        return "system"
    if kind in ("stderr", "error", "parse_error"):
        return DIAGNOSTIC_CATEGORY
    if _carries_tool_activity(event):
        return "tool"
    return "assistant"


def _changed_files(workspace: Path) -> Tuple[Tuple[str, str], ...]:
    """The files the run changed, as ``(path, sha256)`` pairs.

    Git is the witness: the workspace is a repository, so what git
    reports is what the run left behind.  The probe files are excluded
    from the repository (see :data:`PROBE_FILES`), so what is left is
    exactly the task's own artefacts.

    Args:
        workspace: The directory the run wrote in.

    Returns:
        One pair per changed file, sorted by path.
    """
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ()

    fields = completed.stdout.split("\0")
    paths: List[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status = entry[:2]
        path = entry[3:]
        if status[0] in ("R", "C"):
            # A rename or a copy: the entry carries the new path and
            # the next field carries the old one.  The new path is the
            # one the file is at now, so that is the one to hash.
            index += 1
        paths.append(path)

    changed: List[Tuple[str, str]] = []
    for relative in sorted(set(paths)):
        target = workspace / relative
        if not target.is_file():
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        changed.append((relative, digest))
    return tuple(changed)


def _run_target_command(workspace: Path) -> Tuple[int, str]:
    """Run the task's target command against the artefact, and read it.

    The command is run here, in the run's own workspace, against the
    file the run produced -- so both backends are measured the same
    way, and the measurement does not depend on how either CLI chose
    to quote the command's output back.

    Args:
        workspace: The directory the run wrote in.

    Returns:
        The command's exit code and its normalised stdout.
    """
    completed = subprocess.run(
        list(TARGET_COMMAND),
        cwd=str(workspace),
        capture_output=True,
        text=True,
    )
    return completed.returncode, completed.stdout.strip()


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------


@dataclass
class ScenarioServer:
    """A real backend process, and everything it was given.

    Attributes:
        harness: The backend the server drives.
        base_url: The root URL the server answers on.
        port: The loopback port it was given.
        headers: The headers a real client sends.
        root: The scenario's temporary tree.
        plans_dir: Where the server keeps its plan artefacts.
        config_dir: Where the server keeps its configuration.
        profile_path: The seatbelt profile the server was given.
        workspace: The directory the run executes in.
        escape_dir: A directory outside the workspace that the profile
            refuses to let the run write into.
        state_db: The server's own state database.
        backend_dir: The backend's source tree -- the place the run
            must not execute in.
        log_path: Where the server's output goes.
        process: The server process.
    """

    harness: str
    base_url: str
    port: int
    headers: Dict[str, str]
    root: Path
    plans_dir: Path
    config_dir: Path
    profile_path: Path
    workspace: Path
    escape_dir: Path
    state_db: Path
    backend_dir: Path
    log_path: Path
    process: Optional[subprocess.Popen] = None

    def url(self, path: str) -> str:
        """The absolute URL of ``path`` on this server."""
        return f"{self.base_url}{path}"

    def get_json(self, path: str) -> Any:
        """GET ``path`` and decode the answer.

        Args:
            path: The path, relative to the server's root.

        Returns:
            The decoded body.

        Raises:
            AssertionError: The server did not answer 200.
        """
        request = urllib.request.Request(
            self.url(path), headers=dict(self.headers), method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=20.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise AssertionError(
                f"GET {path} answered {error.code}: {error.read()[:400]!r}"
            ) from error


def _wait_for_health(
    server: ScenarioServer,
    process: subprocess.Popen,
    timeout: float = 90.0,
) -> None:
    """Wait until the server answers its health probe.

    Args:
        server: The server being waited for.
        process: The server's process.
        timeout: Seconds to wait.

    Raises:
        RuntimeError: The server died, or never became healthy.
    """
    deadline = time.monotonic() + timeout
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"the e2e server exited with {process.returncode}; its log:\n"
                f"{server.log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        try:
            with urllib.request.urlopen(
                f"{server.base_url}/health", timeout=2.0
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError) as error:
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(
        f"the e2e server never became healthy within {timeout}s "
        f"({last_error!r}); its log:\n"
        f"{server.log_path.read_text(encoding='utf-8', errors='replace')}"
    )


@contextlib.contextmanager
def _server(harness: str) -> Iterator[ScenarioServer]:
    """Boot a real backend process for ``harness``, and reclaim it.

    The server is started for real -- a real uvicorn, a real
    ``server:app`` -- on a free loopback port, with its own plans
    directory, its own state database, its own configuration directory
    and its own seatbelt profile.  The tree it lives in is removed when
    the block ends, so nothing survives it.

    Args:
        harness: The backend the server drives.

    Yields:
        The running server.
    """
    root = Path(tempfile.mkdtemp(prefix=f"pdt-e2e-{harness}-")).resolve()
    plans_dir = root / "plans"
    config_dir = root / "config"
    workspace = root / "workspace"
    escape_dir = root / "escape"
    for directory in (plans_dir, config_dir, workspace, escape_dir):
        directory.mkdir(parents=True, exist_ok=True)
    state_db = root / "state.db"
    profile_path = config_dir / "sandbox_profile.sb"
    profile_path.write_text(
        _sandbox_profile(root, workspace), encoding="utf-8"
    )

    port = _free_port()
    log_path = root / "server.log"
    server = ScenarioServer(
        harness=harness,
        base_url=f"http://127.0.0.1:{port}",
        port=port,
        headers={REQUEST_HEADER: REQUEST_HEADER_VALUE},
        root=root,
        plans_dir=plans_dir,
        config_dir=config_dir,
        profile_path=profile_path,
        workspace=workspace,
        escape_dir=escape_dir,
        state_db=state_db,
        backend_dir=BACKEND_DIR.resolve(),
        log_path=log_path,
    )

    env = _server_environment(
        harness=harness,
        plans_dir=plans_dir,
        config_dir=config_dir,
        state_db=state_db,
        profile_path=profile_path,
    )

    log_handle = log_path.open("w", encoding="utf-8")
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
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    server.process = process
    try:
        _wait_for_health(server, process)
        # A guarded round trip: the server has to answer a request that
        # carries the header a real client sends, or the rest of the
        # scenario would be talking to something that is not the
        # guarded service.
        server.get_json("/api/health")
        yield server
    finally:
        _stop_process_group(process)
        log_handle.close()
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    """What one run of the fixed task produced.

    The first six fields are the **contract** the two backends are
    compared on.  Everything after them is what the run was and what
    it left behind, kept so that a failed comparison can be read.

    Attributes:
        changed_files: The files the run changed, as
            ``(relative path, sha256)`` pairs, sorted by path.
        target_exit: The exit code of the task's target command.
        target_stdout: The normalised stdout of that command.
        event_kinds: The categories the run's event stream carried.
        usage_keys: The canonical usage keys the run reported.
        report_sections: The headings of the report the run answered
            with, as ``(level, title)`` pairs.
        harness: The backend that produced the run.
        model: The model that backend was pinned to.
        argv: The command line the run was spawned with.
        exit_code: The CLI's own exit code.
        is_error: Whether the run reported an error.
        final_text: The run's answer.
        event_types: The raw event types the run produced.
        usage_reported_keys: Every key the usage block carried, the
            canonical ones and whatever else the CLI reported.
        usage: The usage block, as the CLI reported it.
        session_id: The conversation the run happened in.
        sandbox_state: What the sandbox did with the run.
        server: The server the scenario booted.
        workspace: The directory the run wrote in.
        hello_path: The file the fixed task was supposed to write.
        marker_path: The file the MCP server writes when it starts.
        escape_path: The file the MCP server tries to write outside
            the workspace -- it must not exist afterwards.
        mcp_config_path: The MCP configuration the run was given.
        pinned_session_id: The session id that was pinned before the
            run.
        resolved_session_id: The session id the dispatch ran under.
        plan_id: The plan the run belongs to.
        step_key: The task the run belongs to.
        sidecar: The audit sidecar's recorded sessions.
        git_status: The workspace's ``git status --porcelain``.
        runner_pid: The process the runner spawned.
        sampled_pids: Every process seen while the run was in flight.
        mcp_json_seen: Whether the workspace held a ``.mcp.json`` while
            the run was in flight.  It is how the "it is gone now"
            claim is made to mean something.
        escape_errno: The errno the refused write reported.
        orphans: What was still running when the run ended.
        invocation_cwd: The directory the invocation ran in.
        dispatch_error: What the dispatch raised, when it raised.
    """

    changed_files: Tuple[Tuple[str, str], ...]
    target_exit: int
    target_stdout: str
    event_kinds: Tuple[str, ...]
    usage_keys: FrozenSet[str]
    report_sections: Tuple[Tuple[int, str], ...]

    harness: str = ""
    model: str = ""
    argv: Tuple[str, ...] = ()
    exit_code: int = -1
    is_error: bool = True
    final_text: str = ""
    event_types: Tuple[str, ...] = ()
    usage_reported_keys: FrozenSet[str] = frozenset()
    usage: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    sandbox_state: Optional[str] = None

    server: Any = None
    workspace: Optional[Path] = None
    hello_path: Optional[Path] = None
    marker_path: Optional[Path] = None
    escape_path: Optional[Path] = None
    mcp_config_path: Optional[Path] = None
    pinned_session_id: str = ""
    resolved_session_id: Optional[str] = None
    plan_id: str = ""
    step_key: str = ""
    sidecar: Dict[str, Any] = field(default_factory=dict)
    git_status: str = ""
    runner_pid: Optional[int] = None
    sampled_pids: Tuple[int, ...] = ()
    mcp_json_seen: bool = False
    escape_errno: Optional[int] = None
    orphans: Tuple[str, ...] = ()
    invocation_cwd: Optional[Path] = None
    dispatch_error: Optional[str] = None


class _RunWatch:
    """A watch on the process table and the workspace during a run.

    Two things are sampled, because two of the assertions are about
    things that only exist *while* the run is in flight:

      * the process table -- so the process the runner spawned can be
        seen, and so whatever is left afterwards can be found; and
      * the workspace's directory listing -- so the injected
        ``.mcp.json`` can be shown to have been there during the run,
        which is what makes "it is gone now" mean something.
    """

    def __init__(
        self,
        marker: str,
        workspace: Path,
        exclude_pids: Sequence[int] = (),
        interval: float = 0.25,
    ) -> None:
        """Prepare the watch.

        Args:
            marker: A string every process of this scenario carries on
                its command line.
            workspace: The directory to watch.
            exclude_pids: Processes that belong to the scenario but are
                not its children -- the service the scenario booted.
            interval: Seconds between samples.
        """
        self._marker = marker
        self._workspace = workspace
        self._exclude = set(exclude_pids)
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.pids: List[int] = []
        self.rows: List[Tuple[int, int, str]] = []
        self.workspace_names: set = set()
        self.mcp_json_seen = False

    def _rows(self) -> List[Tuple[int, int, str]]:
        """The processes that belong to this scenario, right now."""
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
        )
        rows: List[Tuple[int, int, str]] = []
        for line in completed.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, ppid, command = int(parts[0]), int(parts[1]), parts[2]
            if pid in self._exclude or command.startswith("ps -Ao"):
                continue
            # Two ways a process belongs to this scenario: it carries
            # the scenario's own tree on its command line, or it is a
            # direct child of the process that spawned it.  The second
            # one matters because the runner's child rewrites its
            # command line as it goes -- a Node program replaces the
            # argv it was started with -- so the tree it was given is
            # no longer on it.
            if self._marker in command or ppid == os.getpid():
                rows.append((pid, ppid, command))
        return rows

    def _scan(self) -> None:
        for pid, ppid, command in self._rows():
            self.pids.append(pid)
            self.rows.append((pid, ppid, command))
        try:
            names = set(os.listdir(self._workspace))
        except OSError:
            names = set()
        self.workspace_names |= names
        if ".mcp.json" in names:
            self.mcp_json_seen = True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception:  # pragma: no cover - the watch never fails
                pass
            self._stop.wait(self._interval)

    def start(self) -> None:
        """Start sampling in the background."""
        self._thread = threading.Thread(
            target=self._loop, name="pdt-e2e-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and wait for the thread to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def alive(self) -> List[str]:
        """The scenario's processes that are running right now."""
        return [f"{pid}: {command}" for pid, _ppid, command in self._rows()]

    def orphans(self, grace: float = 15.0) -> List[str]:
        """What is still running, after giving it a moment to go.

        The scan is keyed on the scenario's own temporary tree and on
        this process's own children: a process of somebody else's that
        happens to be running is none of this test's business, and a
        process of this test's that is still there is exactly what this
        test is about.  The service the scenario booted is excluded --
        it belongs to the scenario but it is meant to outlive the run.

        Args:
            grace: Seconds to wait for the stragglers.

        Returns:
            One line per process that is still running.
        """
        deadline = time.monotonic() + grace
        while True:
            alive = self.alive()
            if not alive or time.monotonic() >= deadline:
                return alive
            time.sleep(0.5)


def _prepare_workspace(
    workspace: Path,
    server: Any,
) -> Tuple[Path, Path, Path, Path]:
    """Make the workspace a repository, and write the MCP configuration.

    Args:
        workspace: The directory the run writes in.
        server: The server the scenario booted.

    Returns:
        The MCP configuration path, the marker path, the escape path
        and the report path.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(workspace)],
        check=True,
        capture_output=True,
    )
    # The scenario's own probe files are not part of the task's
    # artefact, so git is told to ignore them: what git reports is
    # then exactly what the fixed task produced.
    exclude_path = workspace / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text(
        "\n".join(PROBE_FILES) + "\n", encoding="utf-8"
    )

    # The MCP server that records the injection and probes the
    # confinement.  It lives outside the workspace, so the only way it
    # can be reached is through the configuration the CLI read.
    mcp_dir = Path(server.root) / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    marker_path = workspace / PROBE_FILES[0]
    escape_path = Path(server.escape_dir) / "escaped.txt"
    report_path = workspace / PROBE_FILES[1]
    server_script = mcp_dir / "marker_server.py"
    server_script.write_text(MARKER_SERVER_SOURCE, encoding="utf-8")
    mcp_config_path = mcp_dir / "mcp.json"
    mcp_config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "probe": {
                        "command": sys.executable,
                        "args": ["-B", str(server_script)],
                        "env": {
                            "MCP_MARKER_FILE": str(marker_path),
                            "MCP_ESCAPE_FILE": str(escape_path),
                            "MCP_REPORT_FILE": str(report_path),
                        },
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return mcp_config_path, marker_path, escape_path, report_path


def run_scenario(
    *,
    harness: str,
    model: str,
    server: Any = None,
    workspace: Optional[Path] = None,
    plan_id: Optional[str] = None,
) -> ScenarioResult:
    """Run the fixed task through one backend, and report what it made.

    The run goes through the one function every spawn goes through,
    ``coding_tool._run_claude_interactive``, so the dispatch, the
    sandbox and the session bookkeeping are the real ones.

    Args:
        harness: The backend to drive -- ``"claude"`` or ``"mcode"``.
        model: The model that backend is pinned to.
        server: The server to run against.  When it is ``None`` a
            server is booted for the run and reclaimed when the run
            ends.
        workspace: The directory the run writes in.  Defaults to the
            server's own workspace.
        plan_id: The plan the run belongs to.  Defaults to a name
            derived from the backend.

    Returns:
        What the run produced.
    """
    if server is None:
        with _server(harness) as owned:
            return run_scenario(
                harness=harness,
                model=model,
                server=owned,
                workspace=workspace,
                plan_id=plan_id,
            )
    return _run_on(
        server,
        harness=harness,
        model=model,
        workspace=workspace or Path(server.workspace),
        plan_id=plan_id or f"e2e-{harness}-scenario",
    )


def _run_on(
    server: Any,
    *,
    harness: str,
    model: str,
    workspace: Path,
    plan_id: str,
) -> ScenarioResult:
    """The run itself, against a server that is already up.

    Args:
        server: The server to run against.
        harness: The backend to drive.
        model: The model that backend is pinned to.
        workspace: The directory the run writes in.
        plan_id: The plan the run belongs to.

    Returns:
        What the run produced.
    """
    from harness.contract import HarnessResult

    import coding_tool
    from execution_logger import ExecutionLogger
    from harness.registry import resolve_harness
    from plan_sessions import save_session_id, stable_session_id

    (
        mcp_config_path,
        marker_path,
        escape_path,
        report_path,
    ) = _prepare_workspace(workspace, server)

    # The pinned session id, derived and recorded the way the executor
    # derives and records it.
    plan_dir = Path(server.plans_dir) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    pinned_session_id = stable_session_id(plan_id, STEP_KEY)
    save_session_id(plan_dir, STEP_KEY, pinned_session_id)

    created: List[Any] = []

    class _RecordingDispatchRunner(coding_tool._DispatchRunner):
        """The runner, with the result kept.

        The dispatch's result carries three things no plan artefact
        keeps in an assertable shape: the exit code, the session id
        the CLI minted, and the usage block.  So the scenario watches
        the one spawn point instead of reading them back out of a log.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.dispatched: Any = None
            self.invocation: Any = None

        def run(
            self, harness: Any, invocation: Any, stdin_text: Any = None
        ) -> Any:
            self.invocation = invocation
            self.dispatched = super().run(
                harness, invocation, stdin_text=stdin_text
            )
            return self.dispatched

    def _runner_factory(*args: Any, **kwargs: Any) -> Any:
        runner = _RecordingDispatchRunner(*args, **kwargs)
        created.append(runner)
        return runner

    original_runner = coding_tool._DispatchRunner
    coding_tool._DispatchRunner = _runner_factory

    logger = ExecutionLogger(plan_id, plans_dir=Path(server.plans_dir))
    tool = coding_tool.ClaudeCodingTool(
        cwd=str(workspace),
        mcp_config=str(mcp_config_path),
        logger=logger,
        model=model,
    )
    resolved_harness = resolve_harness({"PDT_HARNESS": harness})

    # The environment the child runs in.  The home has to be the real
    # one -- the CLIs keep their account under it -- and the secrets
    # published on descriptors by whatever launcher started *this*
    # process have to go: their numbers name descriptors that mean
    # something else here.
    scrubbed = {
        name: None for name in os.environ if name.startswith("PDT_SECRET_FD_")
    }
    watch = _RunWatch(
        marker=str(server.root),
        workspace=workspace,
        exclude_pids=(
            [server.process.pid] if getattr(server, "process", None) else []
        ),
    )
    text = ""
    resolved_session_id: Optional[str] = None
    meta: Dict[str, Any] = {}
    dispatch_error: Optional[BaseException] = None
    try:
        with _environment(
            HOME=_real_home(),
            PDT_SANDBOX_PROFILE=str(server.profile_path),
            PDT_PROVIDER_MODE="inherit",
            PDT_PLANS_DIR=str(server.plans_dir),
            PDT_STATE_DB_PATH=str(server.state_db),
            PDT_PLAN_ID=None,
            **scrubbed,
        ):
            watch.start()
            try:
                text, resolved_session_id, meta = tool._run_claude_interactive(
                    FIXED_TASK,
                    session_id=pinned_session_id,
                    harness=resolved_harness,
                )
            except Exception as error:  # noqa: BLE001 — the failure is the fact
                dispatch_error = error
            finally:
                watch.stop()
    finally:
        coding_tool._DispatchRunner = original_runner

    runner = created[0] if created else None
    result = getattr(runner, "dispatched", None)
    if result is None:
        result = HarnessResult(
            exit_code=1,
            events=(),
            final_text="",
            usage=None,
            is_error=True,
            session_id=None,
            sandbox_state="not_configured",
            harness=harness,
        )
    invocation = getattr(runner, "invocation", None)
    process = getattr(runner, "process", None)
    runner_pid = getattr(process, "pid", None)

    escape_errno: Optional[int] = None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        escape_errno = report.get("escape_error")
    except (OSError, ValueError):
        pass

    events = list(getattr(result, "events", ()) or ())
    categories = [_event_category(event) for event in events]
    usage = getattr(result, "usage", None)

    target_exit, target_stdout = _run_target_command(workspace)

    return ScenarioResult(
        changed_files=_changed_files(workspace),
        target_exit=target_exit,
        target_stdout=target_stdout,
        event_kinds=tuple(
            sorted({c for c in categories if c != DIAGNOSTIC_CATEGORY})
        ),
        usage_keys=frozenset(USAGE_KEYS & set(usage or {})),
        report_sections=_report_sections(
            getattr(result, "final_text", "") or text or ""
        ),
        harness=harness,
        model=model,
        argv=tuple(getattr(invocation, "argv", ()) or ()),
        exit_code=getattr(result, "exit_code", -1),
        is_error=bool(getattr(result, "is_error", True)),
        final_text=getattr(result, "final_text", "") or text or "",
        event_types=tuple(sorted({getattr(e, "type", "") for e in events})),
        usage_reported_keys=frozenset(usage or {}),
        usage=usage,
        session_id=getattr(result, "session_id", None),
        sandbox_state=getattr(result, "sandbox_state", None),
        server=server,
        workspace=workspace,
        hello_path=workspace / "hello.txt",
        marker_path=marker_path,
        escape_path=escape_path,
        mcp_config_path=mcp_config_path,
        pinned_session_id=pinned_session_id,
        resolved_session_id=resolved_session_id,
        plan_id=plan_id,
        step_key=STEP_KEY,
        sidecar=_read_sidecar(plan_dir),
        git_status=_git_status(workspace),
        runner_pid=runner_pid,
        sampled_pids=tuple(sorted(set(watch.pids))),
        mcp_json_seen=watch.mcp_json_seen,
        escape_errno=escape_errno,
        orphans=tuple(watch.orphans()),
        invocation_cwd=(
            Path(str(invocation.cwd)) if invocation is not None else None
        ),
        dispatch_error=dispatch_error,
    )


# --------------------------------------------------------------------------
# The fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def claude_server() -> Iterator[ScenarioServer]:
    """A real backend process, driven by the claude backend.

    Yields:
        The running server.
    """
    with _server(CLAUDE) as server:
        yield server


@pytest.fixture(scope="session")
def claude_scenario(claude_server: ScenarioServer) -> ScenarioResult:
    """The one real claude run this session is built on.

    The scenario is opt-in: without ``PDT_E2E_HARNESS=1`` it is
    skipped, because it spawns the real CLI under the real macOS
    seatbelt and costs a model call.

    Args:
        claude_server: The server the run happens against.

    Returns:
        What the run produced.
    """
    if os.environ.get(E2E_ENV_VAR) != "1":
        pytest.skip(
            f"{E2E_ENV_VAR}=1 is not set: the real claude chain is "
            f"opt-in, because it costs a model call and spawns the real CLI"
        )
    return run_scenario(
        harness=CLAUDE,
        model=CLAUDE_MODEL,
        server=claude_server,
        workspace=claude_server.workspace,
        plan_id=PLAN_ID,
    )


# --------------------------------------------------------------------------
# The baseline
# --------------------------------------------------------------------------


def test_claude_baseline_full_chain(claude_scenario: ScenarioResult) -> None:
    """All nine assertions, answered by one real claude run.

    Args:
        claude_scenario: The facts one real claude dispatch produced.
    """
    facts = claude_scenario

    # -- 1: the execution directory is outside the service's own tree ---
    backend_dir = Path(facts.server.backend_dir).resolve()
    workspace = Path(facts.workspace).resolve()
    assert backend_dir not in workspace.parents, (
        f"the run executed inside the backend source tree: {workspace}"
    )
    assert not str(workspace).startswith(f"{backend_dir}{os.sep}"), (
        f"the run executed inside the backend source tree: {workspace}"
    )

    # -- 2: the run itself succeeded -------------------------------------
    assert facts.exit_code == 0, (
        f"the real claude run exited {facts.exit_code}; "
        f"the run said: {facts.final_text[:400]!r}"
    )
    assert facts.is_error is False, "the real claude run reported an error"
    assert "result" in facts.event_types, (
        "the real claude run produced no result event"
    )

    # -- 3: the fixed task's artefact ------------------------------------
    hello_path = Path(facts.hello_path)
    assert hello_path.is_file(), (
        f"the fixed task left no hello.txt in {facts.workspace}"
    )
    hello_bytes = hello_path.read_bytes()
    assert hashlib.sha256(hello_bytes).hexdigest() in HELLO_HASHES, (
        f"hello.txt does not carry the expected content; got {hello_bytes!r}"
    )
    assert hello_bytes.decode("utf-8").strip() == TARGET_STDOUT
    assert TARGET_STDOUT in facts.final_text, (
        "the run did not report the output of `cat hello.txt`"
    )

    # -- 4: usage ---------------------------------------------------------
    usage = facts.usage
    assert usage is not None, "the real claude run reported no usage"
    assert USAGE_KEYS <= facts.usage_reported_keys, (
        f"the usage block does not carry the four canonical keys: "
        f"{sorted(facts.usage_reported_keys)}"
    )
    assert facts.usage_keys == USAGE_KEYS, (
        f"the canonical usage keys are not all there: "
        f"{sorted(facts.usage_keys)}"
    )
    assert usage["input_tokens"] > 0, (
        f"the run reported no input tokens: {usage}"
    )

    # -- 5: the session the run happened in ------------------------------
    assert facts.session_id, "the real claude run reported no session"
    assert facts.session_id == facts.pinned_session_id, (
        f"the run did not happen under the pinned session id: "
        f"{facts.session_id!r} != {facts.pinned_session_id!r}"
    )

    # -- 6: the pinned id is a uuid5, and the audit agrees ----------------
    import uuid

    assert uuid.UUID(facts.pinned_session_id).version == 5, (
        f"the pinned session id is not a uuid5: {facts.pinned_session_id!r}"
    )
    assert facts.pinned_session_id == str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"pdt:{facts.plan_id}:{facts.step_key}")
    ), (
        f"the pinned session id is not the one derived from the plan and "
        f"the task: {facts.pinned_session_id!r}"
    )
    assert facts.sidecar.get(facts.step_key) == facts.pinned_session_id, (
        f"the audit sidecar does not agree with the pinned id: "
        f"{facts.sidecar!r}"
    )
    assert facts.resolved_session_id == facts.pinned_session_id, (
        "the dispatch did not run under the pinned session id"
    )

    # -- 7: the MCP configuration did not land in the workspace ----------
    assert "--mcp-config" in facts.argv, (
        f"the claude backend did not carry the MCP configuration on its "
        f"command line: {facts.argv!r}"
    )
    assert not (workspace / ".mcp.json").exists(), (
        "a .mcp.json was left behind in the workspace"
    )
    assert ".mcp.json" not in facts.git_status, (
        f"git saw a .mcp.json: {facts.git_status!r}"
    )
    assert "hello.txt" in facts.git_status, (
        f"git saw nothing at all, so the .mcp.json check is vacuous: "
        f"{facts.git_status!r}"
    )

    # -- 8: the run really was confined ----------------------------------
    assert facts.sandbox_state == "applied", (
        f"the run was not confined: sandbox_state={facts.sandbox_state!r}"
    )
    assert facts.runner_pid in facts.sampled_pids, (
        f"the process table never showed the process the runner spawned "
        f"(pid {facts.runner_pid}); sampled {list(facts.sampled_pids)}"
    )
    assert facts.marker_path.is_file(), (
        "the MCP server the configuration named was never started, so the "
        "CLI did not read the configuration it was given"
    )
    assert not facts.escape_path.exists(), (
        "a write outside the workspace succeeded, so the seatbelt did "
        "not confine the run"
    )
    assert facts.escape_errno in (errno.EACCES, errno.EPERM), (
        "the write outside the workspace failed for some reason other "
        f"than a permission refusal: errno={facts.escape_errno!r}"
    )

    # -- 9: nothing was left running -------------------------------------
    assert facts.orphans == (), (
        f"the run left processes behind: {list(facts.orphans)}"
    )
