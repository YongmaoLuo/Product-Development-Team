"""Fixtures for the real end-to-end harness scenarios.

What this file provides
=======================

``e2e_server``
    A **real** backend process, in a world of its own: uvicorn serving
    ``server:app`` on a free loopback port, with its own plans
    directory, its own state database, its own configuration directory
    and its own seatbelt profile.  Nothing it touches is the operator's
    live tree, and nothing it leaves behind survives the scenario.

    It is spoken to the way a real client speaks to it: every
    ``/api/*`` request carries the ``X-PDT-Request`` header, because a
    scenario that talks to an unguarded server is testing a different
    server.

``mcode_chain``
    The facts **one real mcode dispatch** produced: the harness result,
    the workspace it touched, the audit sidecar, what the process table
    looked like while it ran, and what was still running afterwards.

Why the run is driven through the dispatch and not through the API
=================================================================

The HTTP API cannot answer what this scenario asks.  The session id the
CLI minted, the usage block and the sandbox state live on the
dispatch's result object; the server's own artefacts either do not
carry them or carry them in a shape that cannot be asserted against.
For mcode the *resolved* session id stays the pinned uuid5 — the CLI
mints its own ``mvs_`` id and emits no ``init`` event — so reading an
id back out of a log would answer the wrong question.

So the run goes through the one function every spawn goes through,
``coding_tool._run_claude_interactive``, and the server is booted and
exercised alongside it.  Both halves are real.
"""

from __future__ import annotations

import contextlib
import json
import os
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
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

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

#: The variable that turns the real scenarios on.  Without it the
#: scenarios are collected and skipped: the deterministic gates never
#: pay for a real model call.
E2E_ENV_VAR = "PDT_E2E_HARNESS"

#: The header every guarded request carries.  The same header, and the
#: same value, the real client sends.
REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"

#: The backend this scenario drives.
HARNESS_NAME = "mcode"

#: The plan and the task the run belongs to.  The task's key is the
#: one the executor uses for a task's session (``task:<id>``), and the
#: pinned id is derived from the plan and that key.
PLAN_ID = "e2e-mcode-harness"
TASK_ID = "1"
STEP_KEY = f"task:{TASK_ID}"

#: The fixed task.  Fixed on purpose: a moving task would make the run
#: incomparable and the artefact assertions meaningless.
FIXED_TASK = (
    "Create a file named hello.txt in the current working directory.\n"
    "Its content must be exactly the single line: hello e2e\n"
    "(a single trailing newline is fine). Create no other file.\n"
    "Then run `cat hello.txt` and report its output verbatim."
)

#: The MCP server the workspace configuration names.  It is written to
#: disk rather than inlined, because the configuration has to survive
#: the trip through the workspace's ``.mcp.json`` as a real file.
MARKER_SERVER_SOURCE = '''\
"""An MCP server that answers two questions at once.

It records that mcode started it -- which can only happen if mcode read
the ``.mcp.json`` the adapter injected into the workspace -- and it
tries to write *outside* the workspace.  Under the scenario's seatbelt
profile that write must be refused: if it succeeds, the confinement is
only ever claimed.
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
    names a *file descriptor number* a launcher published a secret
    on.  Those numbers are the launcher's, not the server's: a server
    that inherited them would read whatever happens to sit on
    descriptor 3 in its own process and then close it, which on macOS
    takes the event loop's own socket with it -- the service dies
    during start-up with ``Bad file descriptor`` in ``kevent``.

    So the server is given what it needs and nothing else: the paths
    of its own world, the harness it drives, and the path it has to
    import from.  The home is the real one, because a service that
    cannot reach the operator's configuration is not the service.

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

    Everything else stays allowed: the MiniMax CLI keeps its account
    files under the real home, and a profile that refused those would
    not be testing the confinement but the CLI's ability to start.

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


def _read_sidecar(plan_dir: Path) -> Dict[str, Any]:
    """The sidecar's ``sessions`` mapping, or ``{}`` when there is none."""
    from plan_sessions import sessions_path

    try:
        data = json.loads(sessions_path(plan_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = data.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


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


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------


@dataclass
class E2eServer:
    """A real backend process, and everything it was given.

    Attributes:
        base_url: The root URL the server answers on.
        port: The loopback port it was given.
        headers: The headers a real client sends -- the request guard
            needs its own header, so every caller gets it here rather
            than repeating it.
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
    process: subprocess.Popen

    def url(self, path: str) -> str:
        """The absolute URL of ``path`` on this server."""
        return f"{self.base_url}{path}"

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 20.0,
    ) -> Tuple[int, bytes]:
        """Make one request and return its status and body.

        Args:
            method: The HTTP method.
            path: The path, relative to the server's root.
            payload: A JSON-serialisable body, or ``None``.
            headers: Extra headers for this request only.
            timeout: Seconds to wait for the answer.

        Returns:
            The status code and the raw body.
        """
        body = None
        request_headers = {**self.headers, **(headers or {})}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.url(path),
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def get_json(self, path: str) -> Any:
        """GET ``path`` and decode the answer.

        Args:
            path: The path, relative to the server's root.

        Returns:
            The decoded body.

        Raises:
            AssertionError: The server did not answer 200.
        """
        status, body = self.request("GET", path)
        if status != 200:
            raise AssertionError(f"GET {path} answered {status}: {body[:400]!r}")
        return json.loads(body.decode("utf-8"))

    def post_json(self, path: str, payload: Any) -> Any:
        """POST ``payload`` to ``path`` and decode the answer.

        Args:
            path: The path, relative to the server's root.
            payload: A JSON-serialisable body.

        Returns:
            The decoded body.

        Raises:
            AssertionError: The server refused the request.
        """
        status, body = self.request("POST", path, payload=payload)
        if status >= 400:
            raise AssertionError(f"POST {path} answered {status}: {body[:400]!r}")
        return json.loads(body.decode("utf-8")) if body else None


def _wait_for_health(
    server: E2eServer,
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


@pytest.fixture(scope="session")
def e2e_server() -> Iterator[E2eServer]:
    """A real backend process, in a world of its own.

    The server is started for real -- a real uvicorn, a real
    ``server:app`` -- on a free loopback port, with its own plans
    directory, its own state database, its own configuration directory
    and its own seatbelt profile.  The tree it lives in is removed when
    the scenario ends, so nothing survives it.

    Yields:
        The running server.
    """
    root = Path(tempfile.mkdtemp(prefix="pdt-e2e-")).resolve()
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
    server = E2eServer(
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
        process=None,  # type: ignore[arg-type]
    )

    env = _server_environment(
        harness=HARNESS_NAME,
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
    ):
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
            # command line as it goes -- the MiniMax CLI is a Node
            # program, and Node replaces the argv it was started with
            # -- so the tree it was given is no longer on it.
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
        return [
            f"{pid}: {command}"
            for pid, _ppid, command in self._rows()
        ]

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


@dataclass
class ChainFacts:
    """What one real mcode dispatch produced.

    Attributes:
        server: The server the scenario booted.
        workspace: The directory the run executed in.
        hello_path: The file the fixed task was supposed to write.
        marker_path: The file the MCP server writes when it starts.
        escape_path: The file the MCP server tries to write outside the
            workspace -- it must not exist afterwards.
        mcp_config_path: The MCP configuration the adapter injects.
        result: The harness result of the run.
        text: What the run answered.
        resolved_session_id: The session id the dispatch ran under.
        meta: The dispatch's own metadata.
        pinned_session_id: The session id that was pinned before the
            run.
        plan_id: The plan the run belongs to.
        step_key: The task the run belongs to.
        sidecar: The audit sidecar's recorded sessions.
        git_status: The workspace's ``git status --porcelain``.
        runner_pid: The process the runner spawned.
        sampled_pids: Every process seen while the run was in flight.
        sampled_rows: The process table rows the run produced.
        mcp_json_seen: Whether the injected ``.mcp.json`` was there
            while the run was in flight.
        escape_errno: The errno the refused write reported, when it was
            refused.
        orphans: What was still running when the run ended.
        invocation_cwd: The directory the invocation ran in.
        dispatch_error: What the dispatch raised, when it raised.
    """

    server: E2eServer
    workspace: Path
    hello_path: Path
    marker_path: Path
    escape_path: Path
    mcp_config_path: Path
    result: Any
    text: str
    resolved_session_id: Optional[str]
    meta: Dict[str, Any]
    pinned_session_id: str
    plan_id: str
    step_key: str
    sidecar: Dict[str, Any]
    git_status: str
    runner_pid: Optional[int]
    sampled_pids: List[int]
    sampled_rows: List[Tuple[int, int, str]]
    mcp_json_seen: bool
    escape_errno: Optional[int]
    orphans: List[str]
    invocation_cwd: Optional[str]
    dispatch_error: Optional[BaseException] = None

    def stderr_excerpt(self) -> str:
        """The last lines the run wrote to stderr, for a failure message."""
        lines = [
            event.text
            for event in getattr(self.result, "events", ())
            if getattr(event, "type", "") == "stderr" and getattr(event, "text", "")
        ]
        excerpt = "\n".join(lines[-20:])
        if self.dispatch_error is not None:
            excerpt = (
                f"{self.dispatch_error!r}\n{excerpt}"
                if excerpt
                else repr(self.dispatch_error)
            )
        return excerpt or "<the run wrote nothing to stderr>"


@pytest.fixture(scope="session")
def mcode_chain(e2e_server: E2eServer) -> Iterator[ChainFacts]:
    """The facts one real mcode dispatch produced.

    The scenario is opt-in: without ``PDT_E2E_HARNESS=1`` it is
    skipped, because it spawns the real MiniMax Code CLI under the
    real macOS seatbelt and costs a model call.

    Yields:
        What the run produced.
    """
    if os.environ.get(E2E_ENV_VAR) != "1":
        pytest.skip(
            f"{E2E_ENV_VAR}=1 is not set: the real mcode chain is opt-in, "
            f"because it costs a model call and spawns the real CLI"
        )

    from harness.contract import HarnessResult

    server = e2e_server
    workspace = server.workspace

    # The workspace is a git repository, so the assertions about what
    # git did and did not see are real ones.
    subprocess.run(
        ["git", "init", "-q", str(workspace)],
        check=True,
        capture_output=True,
    )

    # The MCP server that records the injection and probes the
    # confinement.  It lives outside the workspace, so the only way it
    # can be reached is through the configuration mcode read.
    mcp_dir = server.root / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    marker_path = workspace / "mcp-marker.txt"
    escape_path = server.escape_dir / "escaped.txt"
    report_path = workspace / "mcp-report.json"
    # The scenario's own probe files are not part of the task's
    # artefact, so git is told to ignore them: what git reports is
    # then exactly what the fixed task produced.
    exclude_path = workspace / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text(
        "mcp-marker.txt\nmcp-report.json\n", encoding="utf-8"
    )
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

    # The pinned session id, derived and recorded the way the executor
    # derives and records it.
    from plan_sessions import save_session_id, stable_session_id

    plan_dir = server.plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    pinned_session_id = stable_session_id(PLAN_ID, STEP_KEY)
    save_session_id(plan_dir, STEP_KEY, pinned_session_id)

    # -- the run itself --------------------------------------------------
    import coding_tool
    from execution_logger import ExecutionLogger
    from harness.registry import resolve_harness

    watch = _RunWatch(
        marker=str(server.root),
        workspace=workspace,
        exclude_pids=[server.process.pid],
    )
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

    logger = ExecutionLogger(PLAN_ID, plans_dir=server.plans_dir)
    tool = coding_tool.ClaudeCodingTool(
        cwd=str(workspace),
        mcp_config=str(mcp_config_path),
        logger=logger,
    )
    harness = resolve_harness({"PDT_HARNESS": HARNESS_NAME})

    # The environment the child runs in.  The home has to be the real
    # one -- the MiniMax CLI keeps its account under it -- and the
    # secrets published on descriptors by whatever launcher started
    # *this* process have to go: their numbers name descriptors that
    # mean something else here.
    scrubbed = {
        name: None
        for name in os.environ
        if name.startswith("PDT_SECRET_FD_")
    }
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
                    harness=harness,
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
            harness=HARNESS_NAME,
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

    facts = ChainFacts(
        server=server,
        workspace=workspace,
        hello_path=workspace / "hello.txt",
        marker_path=marker_path,
        escape_path=escape_path,
        mcp_config_path=mcp_config_path,
        result=result,
        text=text,
        resolved_session_id=resolved_session_id,
        meta=meta,
        pinned_session_id=pinned_session_id,
        plan_id=PLAN_ID,
        step_key=STEP_KEY,
        sidecar=_read_sidecar(plan_dir),
        git_status=_git_status(workspace),
        runner_pid=runner_pid,
        sampled_pids=sorted(set(watch.pids)),
        sampled_rows=list(watch.rows),
        mcp_json_seen=watch.mcp_json_seen,
        escape_errno=escape_errno,
        orphans=watch.orphans(),
        invocation_cwd=(
            Path(str(invocation.cwd)) if invocation is not None else None
        ),
        dispatch_error=dispatch_error,
    )
    yield facts
