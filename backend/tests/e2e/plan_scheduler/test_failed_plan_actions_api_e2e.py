"""End-to-end: the operator's two actions on a **failed** plan, over HTTP.

What this file is for
---------------------
Tasks 1-3 of this plan changed three layers on their own: the lifecycle
facade (``requeue_plan`` now accepts the ``failed`` phase), the action
table the panel renders, and the request layer that posts the action.
Each of those layers can be green while the **product** is broken — the
panel can offer a button the command executor refuses, and the facade
can accept a phase the route never reaches.

So this file asks the question the product asks, against something that
is really running: *a plan whose execution failed, clicked through the
real HTTP route, ends up **queued** instead of answered with a 409.*

What is real here
-----------------
Everything but the executor, and the executor is never reached:

* the server is a **process** (a child ``uvicorn``, in its own process
  group, killed by that group);
* the state is a **SQLite file** on disk;
* the plans are **directories**;
* the scheduler reads **its own settings file** through the named
  production seam (``server._scheduler_settings_path``) — the seam the
  repository's own integration cases use.  Nothing is replaced by a
  double; the seam is *named*.

Why the scheduler never starts a plan
-------------------------------------
Two numbers in the settings file say so, and they say it for different
reasons:

* ``tick_seconds: 3600`` — the worker's tick is an hour away, so the
  scheduler does not decide anything while this case is looking;
* ``dry_run: true`` — even a tick that did run would write its
  decisions down instead of carrying them out.

The case is about the **operator's** action, not about the tick.  A
plan that the tick pulled off the line would prove something else.

The seed data, and why its order is a dependency
-------------------------------------------------
The rows below are written **before** the server starts, and each one
exists because a later step reads it:

1. ``open_db`` + ``migrate`` — without the tables, ``PlanState``'s
   legacy-file migration is swallowed by its own ``except Exception``,
   the routing row is never created, and every assertion after it is
   luck rather than evidence.
2. ``RoutingRepository.insert(plan_id, "failed")`` — this row is the
   phase.  ``/api/plans`` reads it for ``current_phase`` (without it
   the panel cannot draw the button at all) and the verification route's
   CAS reads it to decide whether the plan may start.
3. ``plans/<id>/plan_state.json`` — the same phase in the legacy file.
   ``PlanState`` reads SQLite first and only falls back to the file, so
   this is the reconciliation copy, not the authority.
4. ``ExecutionRepository.insert(plan_id, project_dir=...)`` — the
   project directory.  Without it the verification start answers 400
   "Missing project directory" and the re-run case measures the wrong
   thing.
5. ``plans/<id>/tasks.json`` — one trivial task, so the re-run
   verification has something to do.

Two plans, two jobs
-------------------
Plan **A** is the one that is started: it goes from ``failed`` to
``queued``.  Plan **B** is the one that is re-verified, and it stays in
``failed`` for the whole run — a re-run verification must not quietly
turn the plan into a queued plan.

What this file produces for the later cases
-------------------------------------------
:class:`FailedPlanWorld` — the world, with ``base_url`` / ``start()`` /
``stop()`` / ``http()`` — and the path templates :data:`PLAN_START` and
:data:`PLAN_VERIFY`.  They are defined here so the later cases import
one world rather than each building their own, and the module has **no
side effect at import**: importing it creates nothing and starts
nothing.
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

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The paths
# ---------------------------------------------------------------------------

#: ``<repo>/backend`` — this file lives at ``backend/tests/e2e/plan_scheduler/``.
BACKEND_DIR: Path = Path(__file__).resolve().parents[3]

#: ``<repo>`` — the checkout the server is booted from.
REPO_ROOT: Path = BACKEND_DIR.parent

#: The interpreter the server runs on.  The project's own virtualenv when
#: there is one, the interpreter running pytest otherwise — a checkout
#: without a virtualenv must still boot a server rather than fail before
#: it starts.
_VENV_PYTHON: Path = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON: Path = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)


# ---------------------------------------------------------------------------
# The paths the operator's actions are posted to
# ---------------------------------------------------------------------------

#: The operator's "start" action — make this plan the next one to run.
PLAN_START: str = "/api/plan/{plan_id}/scheduler/start"

#: The operator's "re-run verification" action.
PLAN_VERIFY: str = "/api/verification/{plan_id}/start"

#: The listing, terminal plans included — the panel's own source of truth.
PLAN_LIST: str = "/api/plans?include_terminal=true"


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: The plan that is started.
PLAN_A_ID: str = "pdt-e2e-failed-a"

#: The plan that is re-verified; it stays in ``failed`` for the whole run.
PLAN_B_ID: str = "pdt-e2e-failed-b"

#: How many plans may run at the same time.  One is enough: the case is
#: about an action, and a capacity of one leaves room for exactly one
#: plan to be started.
CAPACITY: int = 1

#: How long a young run is protected from being interrupted.
MIN_RUNTIME_SECONDS: int = 600

#: How long one tick waits.  An hour: the tick must not decide anything
#: while this case is looking — the case is about the operator's action,
#: not about the tick.
TICK_SECONDS: int = 3600

#: How long a single command may take.
COMMAND_TIMEOUT_SECONDS: int = 30

#: The phase a failed plan is in when the case starts.
FAILED_PHASE: str = "failed"

#: The phase a started plan reaches.
QUEUED_PHASE: str = "queued"

#: The header every ``/api/*`` request must carry.
REQUEST_HEADER: str = "X-PDT-Request"
REQUEST_HEADER_VALUE: str = "1"

#: How long the case waits for the phase to change, in total.  The
#: listing's own cache is bounded to a couple of seconds, so a phase
#: that has changed is at most that far behind.
PHASE_WAIT_SECONDS: float = 10.0


# ---------------------------------------------------------------------------
# The launcher — the server, pointed at a settings file of its own
# ---------------------------------------------------------------------------

_LAUNCHER_SOURCE = r'''
"""Boot the backend with the scheduler's settings file pointed elsewhere.

The scheduler reads the ``scheduler:`` section of the backend's own
``config.yaml``.  The path it reads is a seam the server exposes
(``server._scheduler_settings_path``), so this run points it at a file
of its own: the case can then say what the scheduler is told without
touching the repository's configuration.

Nothing is replaced.  The seam is *named*, exactly the way the
repository's own integration cases name it.
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


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    """Whether the port can be bound again.

    The check binds the way a *server* binds — with ``SO_REUSEADDR`` —
    because that is the question being asked.  A plain bind would answer
    "no" for a port whose connections are still in ``TIME_WAIT``, which
    says nothing about whether anything is still listening on it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------
# The world: a server, a database, a plans tree, and two failed plans
# ---------------------------------------------------------------------------


class FailedPlanWorld:
    """One end-to-end world, holding two **failed** plans.

    Everything in here is real: the server is a child process, the state
    is a SQLite file, and the plans are directories whose phase is a row
    the state machine wrote.

    The world is built in ``__init__`` — the seed rows are written
    *before* the server is started, because a row written after the
    server came up would be a fact the case made up rather than one the
    product read.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.plans_dir = self.root / "plans"
        self.db_path = self.root / "state.db"
        self.logs_dir = self.root / "logs"
        self.settings_path = self.root / "scheduler-settings.yaml"
        self.launcher_path = self.root / "launch_server.py"
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: Optional[subprocess.Popen] = None
        self._log_handle: Optional[Any] = None

        for directory in (self.plans_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self._write_settings()
        self._write_launcher()
        self._seed_plans()

    # -- the pieces ---------------------------------------------------------

    def _write_settings(self) -> None:
        """Write the settings file the scheduler is told to read.

        The scheduler is on (the operator's actions need it), it changes
        nothing it decides (``dry_run``), one plan may run at a time, a
        young run is protected for ten minutes, a command may take half
        a minute, and the tick is an hour away.
        """
        self.settings_path.write_text(
            "scheduler:\n"
            "  enabled: true\n"
            "  dry_run: true\n"
            "  capacity: {0}\n"
            "  min_runtime_seconds: {1}\n"
            "  tick_seconds: {2}\n"
            "  command_timeout_seconds: {3}\n".format(
                CAPACITY, MIN_RUNTIME_SECONDS, TICK_SECONDS, COMMAND_TIMEOUT_SECONDS
            ),
            encoding="utf-8",
        )

    def _write_launcher(self) -> None:
        """Write the launcher that boots the server on this port."""
        self.launcher_path.write_text(_LAUNCHER_SOURCE, encoding="utf-8")

    def _seed_plans(self) -> None:
        """Write the two failed plans: their artifacts, and their phase.

        The order is a dependency, and each step exists because the next
        one reads it — see the module docstring.
        """
        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))

        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
        from state_machine.repositories.routing_repository import RoutingRepository

        # 1. The tables.  Without them the legacy migration is swallowed
        #    by its own ``except Exception`` and the routing row below is
        #    never created.
        connection = open_db(self.db_path)
        try:
            migrate(connection)
            routing = RoutingRepository(connection)
            execution = ExecutionRepository(connection)

            for plan_id in (PLAN_A_ID, PLAN_B_ID):
                plan_dir = self.plans_dir / plan_id
                project_dir = plan_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)

                # 2. The phase.  ``/api/plans`` reads this row for
                #    ``current_phase``, and the verification route's CAS
                #    reads it to decide whether the plan may start.
                routing.insert(plan_id, FAILED_PHASE)

                # 3. The same phase in the legacy file.  ``PlanState``
                #    reads SQLite first; this is the reconciliation copy.
                (plan_dir / "plan_state.json").write_text(
                    json.dumps({"current_phase": FAILED_PHASE}, ensure_ascii=False),
                    encoding="utf-8",
                )

                # 4. The project directory.  Without it the verification
                #    start answers 400 "Missing project directory".
                execution.insert(plan_id, project_dir=str(project_dir))

                # 5. One trivial task, so the re-run verification has
                #    something to do.
                (plan_dir / "tasks.json").write_text(
                    json.dumps(
                        {
                            "requirement": "A plan that failed, waiting to be re-run.",
                            "tasks": [
                                {
                                    "id": f"{plan_id}-1",
                                    "title": "The one task this plan has",
                                    "description": "The e2e plan waits for the operator.",
                                    "test_command": "true",
                                    "project_dir": str(project_dir),
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
        finally:
            connection.close()

    # -- the server ---------------------------------------------------------

    def _server_env(self) -> Dict[str, str]:
        """The environment the server is booted with."""
        env = dict(os.environ)
        env.update(
            {
                "PDT_STATE_DB_PATH": str(self.db_path),
                "PDT_PLANS_DIR": str(self.plans_dir),
                # The provider chain is CC Switch's to serve, and CC Switch
                # is another application: nothing in this repository starts
                # it.  Without this the server's lifespan asks
                # ``127.0.0.1:4000`` for a chain nobody serves, the request
                # is refused, and the child exits before it ever listens.
                # No plan is executed in this case, so "inherit" is the
                # honest setting: no provider is ever asked for anything.
                "PDT_PROVIDER_MODE": "inherit",
                # A capacity file that does not exist = "no rules configured",
                # so the operator's own capacity file is never read.
                "PDT_PROVIDER_CAPACITY_FILE": str(
                    self.root / "absent-provider-capacity.yaml"
                ),
                # The scripted double answers every model call from a
                # fixture file, so no real provider is ever reached.
                "PDT_LLM_MODE": "scripted",
                "PYTHONPATH": os.pathsep.join(
                    [str(REPO_ROOT), str(BACKEND_DIR), env.get("PYTHONPATH", "")]
                ),
            }
        )
        # A stale secret descriptor must never be closed by a process
        # that never owned it.
        for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
            env.pop(key, None)
        return env

    def start(self) -> None:
        """Boot the server and wait until it answers."""
        log_handle = (self.logs_dir / "server.log").open("wb")
        self._log_handle = log_handle
        self.proc = subprocess.Popen(
            [
                str(VENV_PYTHON),
                str(self.launcher_path),
                str(BACKEND_DIR),
                str(self.settings_path),
                str(self.port),
            ],
            cwd=str(BACKEND_DIR),
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
        the machine rather than on this case.  Waiting longer can only
        cost time; giving up early costs the whole run.
        """
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                status, _ = self.http("/health", timeout=2.0)
                if 200 <= status < 300:
                    return
                last = f"status {status}"
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
        goes down with it; the wait is what makes "gone" true rather than
        "asked to go".
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
        frontend's own API client carries, and the one the server refuses
        to answer without.
        """
        request = urllib.request.Request(
            f"{self.base_url}{path}",
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

    # -- what the product says ---------------------------------------------

    def listing(self) -> List[Dict[str, Any]]:
        """The plans the API lists, terminal ones included."""
        status, payload = self.http(PLAN_LIST)
        assert status == 200, f"the listing was answered with {status}: {payload!r}"
        rows = payload if isinstance(payload, list) else payload.get("plans", [])
        return list(rows)

    def phase_of(self, plan_id: str) -> Optional[str]:
        """The phase the API reports for *plan_id*, or ``None``."""
        for row in self.listing():
            if str(row.get("id") or row.get("plan_id")) == plan_id:
                return row.get("current_phase")
        return None

    def wait_for_phase(
        self, plan_id: str, expected: str, timeout: float = PHASE_WAIT_SECONDS
    ) -> Optional[str]:
        """Wait until the API reports *expected* for *plan_id*.

        The listing is cached for a couple of seconds, so the case polls
        the API rather than reading the database once: what the panel
        sees is what the case asserts, and a phase the panel cannot see
        yet is a phase that is not there yet.
        """
        deadline = time.monotonic() + timeout
        phase: Optional[str] = None
        while time.monotonic() < deadline:
            phase = self.phase_of(plan_id)
            if phase == expected:
                return phase
            time.sleep(0.25)
        return phase


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def failed_world(tmp_path):
    """A running server, its own database, its own plans, and two failed plans."""
    the_world = FailedPlanWorld(tmp_path)
    try:
        the_world.start()
        yield the_world
    finally:
        the_world.stop()


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_start_from_failed_reaches_queued(failed_world):
    """Starting a failed plan puts it back in the line — it is not a conflict.

    Before the fix the facade accepted only ``stopped``, so the command
    executor's start turned a failed plan into a **409**.  The whole
    chain is asserted here, on the wire: the route's answer, and the
    phase the plan is in afterwards.
    """
    world = failed_world

    # The precondition the whole case rests on: the plan really is in
    # the failed phase, in the routing row the product reads.
    assert world.phase_of(PLAN_A_ID) == FAILED_PHASE, (
        "the seeded plan is not in the failed phase, so a conflict would "
        "not be about the fix this case is about"
    )

    status, payload = world.http(
        PLAN_START.format(plan_id=PLAN_A_ID), method="POST"
    )

    assert status == 200, (
        f"starting a failed plan was answered with {status}: {payload!r}; "
        f"before the fix it was a 409 conflict"
    )
    assert isinstance(payload, dict), f"the answer is not a document: {payload!r}"
    assert payload.get("outcome") != "conflict", (
        f"the start was refused as a conflict: {payload!r}"
    )

    phase = world.wait_for_phase(PLAN_A_ID, QUEUED_PHASE)
    assert phase == QUEUED_PHASE, (
        f"the plan is in the phase {phase!r} after the start, not "
        f"{QUEUED_PHASE!r}"
    )
