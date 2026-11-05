"""End-to-end: a plan that is let in — the whole chain, on the wire and on the page.

What this file is for
---------------------
Three defects sit on the same chain — *a plan that is let into the
execution line* — and a chain that is green in three places can still be
broken in the fourth:

1. a **failed** plan the operator resumed did not come back: the resume
   answered ``applied`` and the plan vanished from the listing;
2. an **import** that was refused left a directory on disk with no row
   behind it, and the resume answered "no such plan" about a plan that
   was right there;
3. the **description** of a task said ``{1-4}`` and the generator read it
   as one literal id called ``1-4``.

Each of the three was fixed on its own, and each fix was proved on its
own.  This file asks the question the product asks: **does the whole
chain hold, end to end, on the wire and on the page?**

What is real here
-----------------
* the app is the **real FastAPI app** — the production routers, the
  production request guard, the production state machine;
* the state is a **SQLite file** on disk, written through the production
  repositories;
* the plans are **directories**;
* the scheduler is the **production engine**, with its worker thread and
  its own settings file;
* the page is a **real Chromium** driven by Playwright — the button is
  found the way an operator's eye finds it, and it is *clicked*.

The two halves
--------------
The API half runs **in process**: the app is mounted on a real
``TestClient``, the engine is the production one (built by
``server._build_scheduler_engine``, hung on ``app.state`` exactly as the
lifespan hangs it), and the request carries the guard's header — the
same header the frontend's own client carries.

The UI half runs the server as a **child process** — a child
``uvicorn``, on its own port, in its own process group — because a
browser cannot be pointed at an in-process app.  The child is booted
the way the brief's start command boots it::

    PDT_PLANS_DIR=<the plans tree> PDT_STATE_DB_PATH=<the state db> \
      python3 -m uvicorn server:app --host 127.0.0.1 --port 8791

with one addition: the child's scheduler settings are pointed at a file
of the case's own, through the seam the server exposes
(``server._scheduler_settings_path``).  The seam is *named*; nothing is
replaced by a double.

What is deliberately **not** here
--------------------------------
The import's **success** path is not what the three defects are about,
so the plans the cases act on are **built on disk and in the state
database** — the shape a real failed run leaves behind — rather than
produced by an import.  The import is used for the two things only the
import can show: the **refusal** (which must leave a registered plan
behind) and the **description rule** (which the generator applies to
what the model said).

What the environment cannot give the test
-----------------------------------------
A browser.  When Playwright is not installed, or its browser binaries
are not, the **UI case alone** is skipped and says so: a panel that was
not driven proves nothing about the panel.  The API cases run either
way — they are not about the browser.

Isolation
---------
Every case has a plans tree and a state database of its own (the
autouse ``isolated_plans_dir`` fixture points ``server.PLANS_DIR``,
``server._state_db_path`` and the two environment knobs at the case's
``tmp_path``).  The child process inherits the same two paths, so the
browser sees the very world the case seeded — and not the operator's.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

#: ``<repo>/backend`` — this file lives at ``backend/tests/e2e/plan_scheduler/``.
_BACKEND_DIR: Path = Path(__file__).resolve().parents[3]

#: ``<repo>`` — the checkout the child server is booted from.
REPO_ROOT: Path = _BACKEND_DIR.parent

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE  # noqa: E402

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The plans the cases act on
# ---------------------------------------------------------------------------

#: The plan whose first task failed and whose second task never ran.
#:
#: This is the shape the first defect was reported against: task 1
#: failed, the plan is over, and the operator wants it back in line.
FAILED_PLAN_ID: str = "pdt-e2e-admission-failed"

#: The plan whose **directory** is on disk with no row behind it.
#:
#: An orphan: the material an operator needs in order to find out what
#: happened, and a plan the tables cannot see.
ORPHAN_PLAN_ID: str = "pdt-e2e-admission-orphan"


# ---------------------------------------------------------------------------
# The numbers the scheduler is told
# ---------------------------------------------------------------------------

#: How many plans may run at the same time.
CAPACITY: int = 1

#: How long a young run is protected from being interrupted.
MIN_RUNTIME_SECONDS: int = 600

#: How long one tick waits.  An hour: the tick must not decide anything
#: while a case is looking — the cases are about the operator's action,
#: not about the tick.
TICK_SECONDS: int = 3600

#: How long a single command may take.
COMMAND_TIMEOUT_SECONDS: int = 30


# ---------------------------------------------------------------------------
# The ports and the paths
# ---------------------------------------------------------------------------

#: The port the browser drives the child server on.
E2E_PORT: int = int(os.environ.get("PDT_E2E_PORT", "8791"))

#: Where the screenshot lands.  The path is the one the brief names; it
#: is evidence, not an input, and nothing in the repository depends on it.
SCREENSHOT_PATH: Path = Path(
    os.environ.get("PDT_E2E_SCREENSHOT", "/tmp/pdt-e2e/screenshots/scheduler.png")
)

#: The interpreter the child server runs on.  The project's own
#: virtualenv when there is one, the interpreter running pytest
#: otherwise — a checkout without a virtualenv must still boot a server
#: rather than fail before it starts.
_VENV_PYTHON: Path = _BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON: Path = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _headers() -> Dict[str, str]:
    """The headers every request carries.

    The request guard is a middleware over the whole API: a request
    without the guard's header is refused with a 403 before any handler
    sees it.  The frontend's own client carries this header, so the
    cases carry it too — they are asking the API the same question the
    page asks.
    """
    return {REQUEST_HEADER: REQUEST_HEADER_VALUE}


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    """Whether the port can be bound again.

    The check binds the way a *server* binds — with ``SO_REUSEADDR`` —
    because that is the question being asked.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _browser_executable() -> Optional[str]:
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache, and
    the cache lives under the home the run was launched with.  A test
    run **redirects** ``$HOME`` — so that nothing in the suite reaches
    the operator's own files — and the redirect would hide the cache
    with it.  So the home the run was *launched* with is asked first
    (the hermetic environment records it), and the redirected one
    second.

    Nothing is downloaded and nothing is hard-coded: the patterns are
    the layouts Playwright itself installs.
    """
    homes: List[Path] = []
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        homes.append(Path(real_home))
    homes.append(Path.home())

    roots: List[Path] = []
    for home in homes:
        roots.append(home / "Library" / "Caches" / "ms-playwright")
        roots.append(home / ".cache" / "ms-playwright")

    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern)):
                if candidate.exists():
                    return str(candidate)
    return None


def _scheduler_settings() -> str:
    """The settings the scheduler is told to read.

    The scheduler is **on** (the operator's actions need it), it changes
    nothing it decides (``dry_run``), one plan may run at a time, a
    young run is protected for ten minutes, a command may take half a
    minute, and the tick is an hour away.
    """
    return (
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: true\n"
        "  capacity: {0}\n"
        "  min_runtime_seconds: {1}\n"
        "  tick_seconds: {2}\n"
        "  command_timeout_seconds: {3}\n".format(
            CAPACITY,
            MIN_RUNTIME_SECONDS,
            TICK_SECONDS,
            COMMAND_TIMEOUT_SECONDS,
        )
    )


# ---------------------------------------------------------------------------
# The seed data
# ---------------------------------------------------------------------------


def _write_plan_files(plan_dir: Path, *, phase: str, statuses: List[str]) -> Path:
    """Write a plan's own files: the phase, the project, the task list.

    The phase is written twice — as the legacy snapshot and, by the
    caller, as the routing row — because the two readers do not agree
    on which one is the authority.  The routing row is; the file is the
    reconciliation copy.
    """
    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_dir.name, "current_phase": phase}),
        encoding="utf-8",
    )
    tasks = [
        {
            "id": "task-{0}".format(index + 1),
            "title": "Task {0}".format(index + 1),
            "description": "## 背景\nthe task this plan carries",
            "status": status,
            "test_command": "true",
            "project_dir": str(project_dir),
        }
        for index, status in enumerate(statuses)
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": "a plan that failed and is waiting to come back",
                "tasks": tasks,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project_dir


def _seed_failed_plan(plans_dir: Path, db_path: Path, plan_id: str) -> Path:
    """Put a plan on disk and in the tables, the way a failed run leaves it.

    Three facts, and each one is read by somebody later:

    * the **directory** — the plan's own files, with task 1 ``failed``
      and task 2 ``pending``;
    * the **routing row** — the phase ``failed``, which is the verdict
      the listing reads and the one the resume moves;
    * the **execution row** — the verdict ``failed``, which is the other
      half of the listing's terminal decision.

    The rows are written through the production repositories, on the
    production database, before anything is asked of the API.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_dir = plans_dir / plan_id
    project_dir = _write_plan_files(
        plan_dir, phase="failed", statuses=["failed", "pending"]
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "failed")
        ExecutionRepository(conn).insert(
            plan_id,
            exec_status="failed",
            stop_reason="failed: task-1 failed",
            project_dir=str(project_dir),
        )
    finally:
        conn.close()
    return plan_dir


def _seed_orphan_directory(plans_dir: Path, plan_id: str) -> Path:
    """Put a plan's directory on disk with **no row behind it**.

    The directory holds one of the plan's own files, so it is a plan's
    directory — and nothing in the tables knows about it.  That is the
    orphan: material an operator needs, and a plan every table-driven
    reader answers "no such plan" about.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "import.json").write_text(
        json.dumps({"material": "the material nobody registered"}),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"requirement": "a plan nobody registered", "tasks": []}),
        encoding="utf-8",
    )
    return plan_dir


def _task_statuses(plan_dir: Path) -> List[str]:
    """The status of every task of the plan, in order."""
    payload = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    return [str(task.get("status")) for task in payload.get("tasks", [])]


# ---------------------------------------------------------------------------
# The API half — the real app, in process
# ---------------------------------------------------------------------------


class ApiWorld:
    """The real app, the real engine, the real state database.

    The engine is the production one: ``server._build_scheduler_engine``
    builds it and starts its worker, and it is hung on
    ``app.state.scheduler_engine`` exactly as the lifespan hangs it.
    The only thing that is *pointed elsewhere* is the settings file (a
    named seam) and the two paths (the case's own).
    """

    def __init__(self, plans_dir: Path, db_path: Path) -> None:
        self.plans_dir = Path(plans_dir)
        self.db_path = Path(db_path)
        self.settings_path = self.plans_dir.parent / "scheduler-settings.yaml"
        self.client = TestClient(_server_app())
        self._runtime: Any = None

    # -- the engine ---------------------------------------------------------

    def start(self, monkeypatch) -> None:
        """Build the engine, start its worker, and hang it on the app."""
        import server

        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(_scheduler_settings(), encoding="utf-8")

        # The named seam: the scheduler reads its settings from here.
        monkeypatch.setattr(
            server, "_scheduler_settings_path", lambda: self.settings_path
        )

        self._runtime = server._build_scheduler_engine()
        engine = getattr(self._runtime, "engine", None)
        assert engine is not None, (
            "the scheduler engine could not be built; the case has nothing "
            "to ask and would prove nothing"
        )
        server.app.state.scheduler_engine = engine

        # The engine binds itself on its first tick.  A command that
        # arrives before the binding is answered "the scheduler is
        # switched off" — so the case waits for the binding rather than
        # racing it.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not engine.bound:
            time.sleep(0.05)
        assert engine.bound, (
            "the scheduler engine did not bind itself within 30s; the "
            "worker never reached its first tick"
        )

    def stop(self) -> None:
        """Stop the worker and give the handle back."""
        import server

        if self._runtime is not None:
            server._stop_scheduler_engine(self._runtime)
            self._runtime = None
        server.app.state.scheduler_engine = None

    # -- the requests -------------------------------------------------------

    def get(self, path: str) -> Any:
        """One GET against the mounted app."""
        response = self.client.get(path, headers=_headers())
        assert response.status_code == 200, (
            f"GET {path} was answered with {response.status_code}: "
            f"{response.text}"
        )
        return response.json()

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        """One POST against the mounted app; the answer is the response."""
        return self.client.post(
            path,
            headers=_headers(),
            json=body if body is not None else {},
        )

    # -- what the product says ---------------------------------------------

    def scheduler_state(self) -> Dict[str, Any]:
        """The scheduler's one read model."""
        payload = self.get("/api/scheduler/state")
        assert isinstance(payload, dict), (
            f"the read model is not a document: {payload!r}"
        )
        return payload

    def listing(self) -> List[Dict[str, Any]]:
        """The plans the API lists (the terminal ones filtered out)."""
        payload = self.get("/api/plans")
        rows = payload if isinstance(payload, list) else payload.get("plans", [])
        return list(rows)

    def wait_for_listing(
        self, plan_id: str, timeout: float = 15.0
    ) -> Optional[Dict[str, Any]]:
        """Wait until the listing holds *plan_id*.

        The listing is cached for a couple of seconds by design, so a
        phase that has changed is at most that far behind.  The case
        polls the API rather than reading the database once: what the
        panel can see is what the case asserts.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for entry in self.listing():
                if str(entry.get("id") or entry.get("plan_id")) == plan_id:
                    return entry
            time.sleep(0.25)
        return None


def _server_app() -> Any:
    """The production FastAPI app."""
    import server

    return server.app


@pytest.fixture
def api_world(isolated_plans_dir, tmp_path, monkeypatch):
    """The real app, its own database, its own plans, and its engine."""
    the_world = ApiWorld(Path(isolated_plans_dir), tmp_path / "state.db")
    try:
        the_world.start(monkeypatch)
        yield the_world
    finally:
        the_world.stop()


# ---------------------------------------------------------------------------
# The UI half — the real server, in a child process
# ---------------------------------------------------------------------------

_LAUNCHER_SOURCE = r'''
"""Boot the backend with the scheduler's settings file pointed elsewhere.

The scheduler reads the ``scheduler:`` section of the backend's own
configuration.  The path it reads is a seam the server exposes
(``server._scheduler_settings_path``), so this run points it at a file
of its own: the case can then say what the scheduler is told without
touching the repository's configuration.

Nothing is replaced.  The seam is *named*.
"""
import sys
from pathlib import Path

backend_dir = Path(sys.argv[1])
settings_path = Path(sys.argv[2])
port = int(sys.argv[3])

sys.path.insert(0, str(backend_dir))
sys.path.insert(0, str(backend_dir.parent))

import server

server._scheduler_settings_path = lambda: settings_path

import uvicorn

uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
'''


class BrowserWorld:
    """A child ``uvicorn``, its own database, its own plans, one failed plan.

    The server is a **process**: the browser cannot be pointed at an
    in-process app, and the page is served by the backend itself (the
    frontend is a static bundle the backend mounts).
    """

    def __init__(self, root: Path, plans_dir: Path, db_path: Path) -> None:
        self.root = Path(root)
        self.plans_dir = Path(plans_dir)
        self.db_path = Path(db_path)
        self.logs_dir = self.root / "logs"
        self.settings_path = self.root / "scheduler-settings.yaml"
        self.launcher_path = self.root / "launch_server.py"
        self.stub_dir = self.root / "no-lark"

        self.port = E2E_PORT if _port_is_free(E2E_PORT) else _free_port()
        self.base_url = "http://127.0.0.1:{0}".format(self.port)
        self.proc: Optional[subprocess.Popen] = None
        self._log_handle: Optional[Any] = None

    # -- the pieces ---------------------------------------------------------

    def _write_settings(self) -> None:
        self.settings_path.write_text(_scheduler_settings(), encoding="utf-8")

    def _write_launcher(self) -> None:
        self.launcher_path.write_text(_LAUNCHER_SOURCE, encoding="utf-8")

    def _write_lark_stub(self) -> None:
        """A directory whose ``lark_oapi`` is a stub, so the boot stays quick.

        The server's lifespan builds the Feishu notifier, and the
        notifier's client imports ``lark_oapi`` — an SDK of more than ten
        thousand modules.  The stub raises :class:`ImportError` on
        import, which is exactly what the client does with a missing
        SDK: the notifier logs that it is disabled and stays inert.  It
        is inert in this test either way — Feishu is not configured on a
        test machine.
        """
        self.stub_dir.mkdir(parents=True, exist_ok=True)
        (self.stub_dir / "lark_oapi.py").write_text(
            'raise ImportError(\n'
            '    "lark-oapi is stubbed out for this E2E run: the real SDK is "\n'
            '    "more than ten thousand modules and importing it costs tens "\n'
            '    "of seconds, which is more than the boot budget allows. The "\n'
            '    "Feishu notifier is inert in this test either way -- Feishu "\n'
            '    "is not configured here."\n'
            ')\n',
            encoding="utf-8",
        )

    def _server_env(self) -> Dict[str, str]:
        """The environment the server is booted with."""
        env = dict(os.environ)
        env.update(
            {
                "PDT_STATE_DB_PATH": str(self.db_path),
                "PDT_PLANS_DIR": str(self.plans_dir),
                # The provider chain is CC Switch's to serve, and CC Switch
                # is another application: nothing in this repository starts
                # it.  No plan is executed in this case, so "inherit" is
                # the honest setting.
                "PDT_PROVIDER_MODE": "inherit",
                # A capacity file that does not exist = "no rules
                # configured", so the operator's own capacity file is
                # never read.
                "PDT_PROVIDER_CAPACITY_FILE": str(
                    self.root / "absent-provider-capacity.yaml"
                ),
                # The scripted double answers every model call from a
                # fixture file, so no real provider is ever reached.
                "PDT_LLM_MODE": "scripted",
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(self.stub_dir),
                        str(REPO_ROOT),
                        str(_BACKEND_DIR),
                        env.get("PYTHONPATH", ""),
                    ]
                ),
            }
        )
        # A stale secret descriptor must never be closed by a process
        # that never owned it.
        for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
            env.pop(key, None)
        return env

    # -- the server ---------------------------------------------------------

    def start(self) -> None:
        """Boot the server and wait until it answers."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._write_settings()
        self._write_launcher()
        self._write_lark_stub()

        log_handle = (self.logs_dir / "server.log").open("wb")
        self._log_handle = log_handle
        self.proc = subprocess.Popen(
            [
                str(VENV_PYTHON),
                str(self.launcher_path),
                str(_BACKEND_DIR),
                str(self.settings_path),
                str(self.port),
            ],
            cwd=str(_BACKEND_DIR),
            env=self._server_env(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._wait_for_server()

    def _wait_for_server(self, timeout: float = 120.0) -> None:
        """Block until the server answers on ``/health``.

        The bound is generous because the server's startup walks the
        operator's credential store, and how long that takes depends on
        the machine rather than on this case.
        """
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                status, _ = self.http("/health", timeout=2.0)
                if 200 <= status < 300:
                    return
                last = "status {0}".format(status)
            except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
                last = repr(exc)
            time.sleep(0.2)
        raise AssertionError(
            f"the server on {self.base_url} did not come up within {timeout}s; "
            f"the last answer was {last}"
        )

    def stop(self) -> None:
        """Kill the server's process group and wait for it to be gone.

        The server was given a session of its own, so the whole group
        goes down with it; the wait is what makes "gone" true rather
        than "asked to go".
        """
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None

        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:  # noqa: BLE001 — closing a log must not fail a test
                pass
            self._log_handle = None

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if _port_is_free(self.port):
                return
            time.sleep(0.1)

    # -- the requests -------------------------------------------------------

    def http(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[bytes] = None,
        timeout: float = 30.0,
    ) -> Tuple[int, Any]:
        """One HTTP request against the running server; return ``(status, body)``.

        Every request carries the guard's header — the same header the
        frontend's own client carries, and the one the server refuses to
        answer without.
        """
        request = urllib.request.Request(
            "{0}{1}".format(self.base_url, path),
            data=body,
            method=method,
            headers={
                REQUEST_HEADER: REQUEST_HEADER_VALUE,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                payload = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = exc.read() if exc.fp else b""
        try:
            return status, json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, payload


@pytest.fixture
def browser_world(isolated_plans_dir, tmp_path):
    """A child server, its own database, its own plans, and one failed plan."""
    plans_dir = Path(isolated_plans_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "state.db"

    _seed_failed_plan(plans_dir, db_path, FAILED_PLAN_ID)

    the_world = BrowserWorld(tmp_path, plans_dir, db_path)
    try:
        the_world.start()
        yield the_world
    finally:
        the_world.stop()


# ---------------------------------------------------------------------------
# The fake coding tool — the model, scripted
# ---------------------------------------------------------------------------


class FakeCodingTool:
    """Stands in for ``CodingTool``; scripts the two calls the generator makes.

    ``query_json`` is the synthesis and ``query`` is the mandatory
    self-review.  The self-review reply echoes the synthesis payload
    *by reference*, which is what the generator mutates in place while
    it adds ``depends_on`` and resolves ``files_to_modify`` — so the
    fake always reviews the list as it stands at that moment.
    """

    def __init__(self, reply: Optional[Dict[str, Any]] = None) -> None:
        self._reply = reply
        self.json_calls: List[Dict[str, Any]] = []
        self.query_calls: List[str] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.json_calls.append(
            {"prompt": prompt, "system_instruction": system_instruction}
        )
        return self._reply if self._reply is not None else {}

    def query(self, prompt: str) -> str:
        self.query_calls.append(prompt)
        return json.dumps(
            {"findings": [], "fixed_content": self._reply},
            ensure_ascii=False,
        )


@pytest.fixture
def scripted_model(monkeypatch):
    """Point ``server.create_coding_tool`` at a fake; hand back the script.

    The factory takes the reply the model gives and installs it.  The
    reply is the model's own output, so a case that wants a refused
    list installs one and a case that wants a runnable list installs
    another — the fake is the same in both.
    """
    import server

    tool = FakeCodingTool()

    def _install(reply: Dict[str, Any]) -> FakeCodingTool:
        tool._reply = reply
        return tool

    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return _install


@pytest.fixture
def checkout(tmp_path):
    """A real directory for the tasks to target.

    ``harden_for_execution`` refuses a plan whose ``project_dir`` does
    not exist, so a fixture that pointed at a made-up path would be
    testing the 422 branch on the happy path.
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py", "a.py", "b.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    """The RED probe would run the generated test commands for real."""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


# ---------------------------------------------------------------------------
# 1 — a failed plan that is resumed really comes back
# ---------------------------------------------------------------------------


def test_resume_failed_plan_is_visible(api_world, isolated_plans_dir):
    """A resumed failed plan is in the queue **and** in the listing.

    The defect this case was written for: the resume answered
    ``applied``, and the plan vanished from the page — the phase said
    *waiting* while the persisted run verdict said *over*, and the
    listing read the verdict.

    So the case asserts **both** places the plan has to appear, and it
    asserts them on the wire: the scheduler's own read model (the line)
    and the listing the panel draws from.
    """
    world = api_world
    plan_dir = Path(isolated_plans_dir) / FAILED_PLAN_ID

    # The precondition: the plan really is over.  Task 1 failed, task 2
    # never ran, and the plan is hidden from the listing by the two
    # verdicts that say so.
    _seed_failed_plan(Path(isolated_plans_dir), world.db_path, FAILED_PLAN_ID)
    assert _task_statuses(plan_dir) == ["failed", "pending"]
    assert all(
        str(row.get("id")) != FAILED_PLAN_ID for row in world.listing()
    ), "the failed plan is in the listing before the resume; the case would prove nothing"

    response = world.post(
        "/api/plan/{0}/scheduler/resume".format(FAILED_PLAN_ID)
    )

    assert response.status_code == 200, (
        f"resuming a failed plan was answered with {response.status_code}: "
        f"{response.text}; before the fix the plan came back nowhere"
    )
    assert response.json().get("outcome") == "applied", (
        f"the resume was not applied: {response.text}"
    )

    # The line: the scheduler's own read model holds the plan.
    state = world.scheduler_state()
    assert FAILED_PLAN_ID in [
        str(row.get("plan_id")) for row in state.get("queue", [])
    ], f"the resumed plan is not in the queue: {state.get('queue')!r}"

    # The listing: the panel's own source of truth holds the plan.
    row = world.wait_for_listing(FAILED_PLAN_ID)
    assert row is not None, (
        "the resumed plan is not in the listing; the operator pressed "
        "resume and the plan disappeared from the page"
    )

    # And the tasks are not touched: a resume puts the *plan* back in
    # line, it does not undo the failure of the task that failed.
    assert _task_statuses(plan_dir) == ["failed", "pending"], (
        "the resume rewrote the task list"
    )


# ---------------------------------------------------------------------------
# 2 — the button on the panel, clicked in a real browser
# ---------------------------------------------------------------------------


def test_scheduler_panel_requeues_failed_plan(browser_world):
    """The 「重新排队执行」 button puts the failed plan back in the line.

    The panel is what an operator touches, so the panel is what this
    case drives: a real Chromium, the real page, the button found by
    the label and the ``data-action`` the panel gives it, and the
    button **clicked** — not called.

    The assertion is made against the **page**, because the page is
    the thing under test: after the click, the plan's id is drawn in
    the queue list.  The screenshot is the evidence that the panel was
    really driven.
    """
    playwright = pytest.importorskip(
        "playwright.sync_api",
        reason=(
            "Playwright is not installed in this environment; the panel "
            "cannot be driven, and a panel that was not driven proves "
            "nothing about the panel"
        ),
    )

    world = browser_world
    plan_id = FAILED_PLAN_ID

    with playwright.sync_playwright() as driver:
        executable = _browser_executable()
        launch_kwargs: Dict[str, Any] = {"headless": True}
        if executable:
            launch_kwargs["executable_path"] = executable
        try:
            browser = driver.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001 — the browser may be absent
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not ({0}); the panel cannot be driven".format(exc)
            )

        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto("{0}/".format(world.base_url), wait_until="load")
            page.wait_for_selector("#app", timeout=30000)

            # The operator opens the scheduling view.
            page.click("#btn-open-scheduler", timeout=30000)
            page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)

            # The failed plan is drawn in the terminal block, with the
            # button that says what it does.
            button = page.locator(
                '#scheduler-terminal-list button.scheduler-action'
                '[data-action="start"][data-plan="{0}"]'.format(plan_id)
            )
            button.wait_for(state="visible", timeout=30000)
            assert button.inner_text().strip() == "重新排队执行", (
                "the button on a failed plan does not say what it does: "
                "{0!r}".format(button.inner_text())
            )

            # The operator presses it.
            button.click()

            # The plan is in the line, and the line says so.
            page.wait_for_function(
                "planId => (document.querySelector('#scheduler-queue-list')"
                " || {}).innerText.includes(planId)",
                arg=plan_id,
                timeout=10000,
            )

            SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
        finally:
            browser.close()

    assert SCREENSHOT_PATH.exists(), (
        f"the panel was driven but left no screenshot behind: {SCREENSHOT_PATH}"
    )
    assert SCREENSHOT_PATH.stat().st_size > 0, (
        f"the screenshot is empty: {SCREENSHOT_PATH}"
    )


# ---------------------------------------------------------------------------
# 3 — a refused import is registered, and the resume says why
# ---------------------------------------------------------------------------


def test_import_failure_registered_and_resume_explains(
    api_world, isolated_plans_dir, scripted_model, checkout, state_db_reader
):
    """A refused import leaves a **registered** plan, and the resume names the reason.

    Before the fix the refusal left the directory behind and nothing
    else: no routing row, no execution row.  Every reader that goes
    through the tables answered "no such plan" — the resume included —
    about a plan that was right there on the disk.

    So the case asserts three things: the material is still on disk, the
    plan is registered in the ``import_failed`` phase, and the resume
    answers with the reason **in the import's own words**.
    """
    world = api_world

    # The audit refuses a task with no test command: nothing says the
    # change can be verified.
    scripted_model(
        {
            "requirement": "add a daily grouped bar chart to the reporting view",
            "tasks": [
                {
                    "id": "T1",
                    "title": "add the chart renderer",
                    "description": "## 背景\nthe reporting view has no chart",
                    "files_to_modify": ["backend/chart.py"],
                    # no test_command: the audit refuses this list
                },
            ],
        }
    )

    response = world.post(
        "/api/plan/import",
        {
            "project_dir": str(checkout),
            "content": "把 CSV 报表加上按天分组的柱状图",
            "title": "Fast lane refused import",
        },
    )

    assert response.status_code == 422, response.text
    body = response.json()
    plan_id = body["plan_id"]
    problems = list(body.get("problems") or [])
    assert problems, f"the refusal named no problem: {response.text}"

    # The directory is kept: the imported material is still there.
    plan_dir = Path(isolated_plans_dir) / plan_id
    assert plan_dir.exists(), "the refused import threw its directory away"
    assert (plan_dir / "import.json").exists(), (
        "the refused import threw the imported material away"
    )

    # And the plan is registered in the phase that says "this exists and
    # it is not runnable".
    routing = state_db_reader.routing(plan_id)
    assert routing is not None, (
        "a refused import must leave a routing row behind; without one "
        "every table-driven reader answers 'no such plan'"
    )
    assert routing["current_phase"] == "import_failed", (
        f"the refused import was registered at {routing['current_phase']!r}, "
        "not 'import_failed'"
    )

    # And the resume says why — in the import's own words.
    resume = world.post("/api/plan/{0}/scheduler/resume".format(plan_id))
    assert resume.status_code == 409, (
        f"resuming an import_failed plan was answered with "
        f"{resume.status_code}: {resume.text}"
    )
    detail = str(resume.json().get("detail") or "")
    assert "import_failed" in detail, (
        f"the refusal does not name the phase: {detail!r}"
    )
    for problem in problems:
        assert problem in detail, (
            f"the refusal does not carry the import's own words: "
            f"{problem!r} is not in {detail!r}"
        )


# ---------------------------------------------------------------------------
# 4 — the orphan: a directory with no row behind it
# ---------------------------------------------------------------------------


def test_orphan_directory_resume_detail(api_world, isolated_plans_dir):
    """An orphan's resume says the directory is there.

    Before the fix every plan the scheduler did not know was answered
    ``no_such_plan``, and the answer never said **why**.  A directory
    that is on disk with a plan's own files in it is not "no such plan":
    it is material the operator needs, and the answer has to point at
    it.
    """
    world = api_world

    _seed_orphan_directory(Path(isolated_plans_dir), ORPHAN_PLAN_ID)

    response = world.post("/api/plan/{0}/scheduler/resume".format(ORPHAN_PLAN_ID))

    assert response.status_code == 404, (
        f"resuming an orphan was answered with {response.status_code}: "
        f"{response.text}"
    )
    detail = str(response.json().get("detail") or "")
    assert detail == "plan directory exists but is not registered", (
        f"the orphan's refusal does not name the directory: {detail!r}"
    )


# ---------------------------------------------------------------------------
# 5 — the description rule, on the way in
# ---------------------------------------------------------------------------


def test_brace_range_import(api_world, isolated_plans_dir, scripted_model, checkout):
    """``{1-4}`` in a task's description names tasks 1, 2, 3 and 4.

    The rule: only a **brace group** declares a prerequisite.  A bare
    ``1-4`` is prose; ``{1-4}`` names four tasks.  The generator and
    the validator must agree on it word for word, or the import writes
    a dependency the validator then refuses.

    The case drives the **import** — the only place the generator's
    post-processing runs — and reads the dependency list the import
    wrote onto the plan.
    """
    world = api_world

    tasks = []
    files = [
        "backend/chart.py",
        "backend/reporting_view.py",
        "backend/loader.py",
        "backend/a.py",
        "backend/b.py",
    ]
    for index, file_name in enumerate(files, start=1):
        task: Dict[str, Any] = {
            "id": str(index),
            "title": "task {0}".format(index),
            "description": "## 背景\ntask {0} does its own thing".format(index),
            "files_to_modify": [file_name],
            "test_command": "pytest -q",
        }
        if index == 5:
            task["description"] = (
                "## 背景\ntask 5 leans on the first four\n"
                "前置条件：任务 {1-4} 已完成。"
            )
        tasks.append(task)

    scripted_model(
        {
            "requirement": "a plan whose fifth task leans on the first four",
            "tasks": tasks,
        }
    )

    response = world.post(
        "/api/plan/import",
        {
            "project_dir": str(checkout),
            "content": "a plan with a braced range in the fifth task",
            "title": "Fast lane braced range",
        },
    )

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    written = json.loads(
        (Path(isolated_plans_dir) / plan_id / "tasks.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {str(task.get("id")): task for task in written.get("tasks", [])}

    assert "5" in by_id, f"the fifth task is not in the written list: {sorted(by_id)!r}"
    assert by_id["5"].get("depends_on") == ["1", "2", "3", "4"], (
        "the braced range was not expanded into the four tasks it names: "
        "{0!r}".format(by_id["5"].get("depends_on"))
    )
