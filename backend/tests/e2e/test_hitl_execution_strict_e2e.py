"""End-to-end: an open HITL decision holds the execution side still, and
the decision outlives the process that asked it.

Background
----------
A plan can ask a human a question while it runs.  While that question
is unanswered, the task it is about — and everything downstream of it —
must not be scheduled: the work would run on a premise the human has
not confirmed yet.  Everything else about the plan keeps going.

The gate that enforces this is **computed, never stored**
(:func:`hitl.service.blocked_task_ids`): no task status changes, so
answering the question opens the gate again by itself.  Two properties
of that design are easy to get wrong and impossible to see from a unit
test:

* **The question must reach the executor.**  The decision is created
  over HTTP by one process and read by another — the executor — out of
  the same SQLite file.  A decision that only the asking process can
  see is a decision that gates nothing.
* **The question must outlive the process that asked it.**  The
  executor and the API are separate processes with separate lifetimes.
  If the decision lived in the asker's memory, a restart would silently
  release the gate and the plan would run the very work the human was
  asked about.

Why this file is not a ``TestClient`` test
------------------------------------------
``TestClient`` shares the test process's memory with the application:
there is no process boundary, no second SQLite connection and no
restart.  The three facts above are all facts about *processes*, so
they cannot be observed through one.  This file therefore builds the
real thing: a real ``backend.server`` subprocess (its own SQLite file,
its own plans tree, its own port) that spawns the real executor
subprocess, driven over real loopback HTTP.

The boundary is asserted, not assumed: the executor's pid is checked
against the API server's own, and the state is checked to be a file on
disk.  A server that ran the executor in-process would satisfy every
other assertion in this file while proving none of the facts above.

What is pinned here
-------------------
Three cases, one per fact the execution side owes its caller:

1. ``test_strict_suspends_task_and_downstream`` — a ``strict`` question
   on ``B`` holds ``B`` and its downstream ``C`` out of the schedule
   while ``A`` keeps running, and the run stops with the reason that
   says what it is waiting for.  The decision stays ``pending``: a
   strict question has no substitute for a human, so nothing resolves
   it behind the operator's back, and the execution side writes no
   ``pause_state.json`` — that document belongs to the verification
   side, and an execution that wrote one would be claiming a pause the
   plan never took.
2. ``test_answer_releases`` — answering the question opens the gate by
   itself: the next run picks ``B`` and ``C`` up and finishes the
   plan.  Nothing had to be un-blocked, because nothing was ever
   blocked — only held.
3. ``test_decision_survives_restart`` — the server is stopped and
   started again on the same port; the decision is still there, still
   ``pending``, and the execution side is still holding ``B`` and ``C``.

Three layers, asserted at once
------------------------------
The HTTP response, the SQLite row and the files in the plan directory
have to say the same thing.  A response that says "asked" proves
nothing; a row that says "pending" proves nothing; a log line that
says "paused" proves nothing.  All three are read, and the SQLite row
is read with ``sqlite3`` directly rather than through the service, so
the assertion is about the database and not about the code that wrote
it.

How the box is built
--------------------
A real ``backend.server`` subprocess, started the way the product
starts it (``server.py`` with ``PDT_PORT`` / ``PDT_STATE_DB_PATH`` /
``PDT_PLANS_DIR``), which in turn spawns the real executor
subprocess.  The tasks carry ``test_command: "true"``, so the
executor's pre-flight satisfies them without a model call: what runs
and what does not is decided by the scheduling gate alone, and the
suite never spends a token.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from state_machine.db.schema import migrate

#: The e2e layer, and only the e2e layer — the same marker the rest of
#: ``tests/e2e/`` carries, so the CI e2e lane picks this file up.
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + bootstrap
# ---------------------------------------------------------------------------

#: ``backend/`` — this file lives in ``backend/tests/e2e/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The repository root, one level above ``backend/``.
PROJECT_ROOT = BACKEND_DIR.parent

# Prefer the checkout's own virtualenv; fall back to the interpreter
# already running pytest when there is no ``backend/.venv`` (a fresh
# worktree, a container, CI before the venv bootstrap step).  The
# fallback is deliberately not a skip: a suite that skips is a suite
# that asserts nothing.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The request guard's header.  Every ``/api/*`` call carries it, and
#: every call names a loopback host — the guard is part of the
#: production posture this suite drives the real process through.
REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"
LOOPBACK_HOST = "127.0.0.1"

#: The host a ``TestClient`` would have used.  It is *not* this
#: machine, and the guard refuses it — which is what makes the
#: ``Host`` header this suite sends load-bearing rather than
#: decorative.
TESTCLIENT_HOST = "testserver"

#: The graph under test: ``A → B → C``, with the question on ``B``.
TASK_GRAPH: Tuple[Tuple[str, List[str]], ...] = (
    ("A", []),
    ("B", ["A"]),
    ("C", ["B"]),
)

#: The plan's own configuration.  ``strict`` is the mode that matters
#: here: a missed deadline ends an ``auto`` question ``defaulted`` and
#: an ``strict`` one is never resolved by anybody but a human, so the
#: question stays ``pending`` for as long as the operator takes.
#:
#: The outlet names a destination, so the question is asked of
#: somebody.  A plan that names nobody has its questions settled as
#: ``unroutable`` straight away — nobody was asked, so there is no
#: question to wait for, and the gate this case is about would never
#: close.  The ``cmd`` destination always succeeds: it runs ``true``
#: and hands it the payload on stdin, on a thread of its own.
HITL_CONFIG: Dict[str, Any] = {
    "version": 1,
    "mode": "strict",
    "timeout_sec": 1800,
    "hooks": [{"kind": "cmd", "command": "true"}],
}

#: The executor's own stop reason when it is waiting for a human.
STOP_REASON_PENDING = "hitl_decision_pending"

#: The executor's own stop reason when it finished the plan.
STOP_REASON_SUCCESS = "success"

#: How long the executor may take to settle the plan.  It is generous
#: because the executor's own pre-flight runs the tasks' test commands
#: and a loaded CI runner is slow; it is bounded because a plan that
#: never settles must fail the case rather than hang it.
SETTLE_TIMEOUT_SEC = 120.0

#: The executor's structured log, one JSON record per line.
EXECUTION_LOG_FILENAME = "execution.log"

#: The pause checkpoint the *verification* side writes.  The execution
#: side must not write it: an execution that wrote one would be
#: claiming a pause the plan never took.
PAUSE_CHECKPOINT_FILENAME = "pause_state.json"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind an ephemeral loopback port and return its number.

    The kernel hands out a free port and releasing the socket leaves a
    small race window; the readiness polling below tolerates it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return sock.getsockname()[1]


def _assert_port_rebindable(port: int) -> None:
    """Assert nothing is still listening on ``port``.

    ``SO_REUSEADDR`` is set because the question here is "is a live
    process still holding the port" — a socket in ``TIME_WAIT`` from
    the requests this suite just made is not a server that failed to
    exit, and refusing to bind over it would be a false alarm.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_HOST, port))


def _now_iso() -> str:
    """UTC now, formatted the way the state machine writes timestamps."""
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_plan(plans_dir: Path, plan_id: str) -> Path:
    """Materialise a plan directory the executor can run.

    The document is written the way a plan's own task generator would
    leave it: a requirement and a task list, each task naming what it
    depends on.  ``test_command: "true"`` is deliberate — the
    executor's pre-flight runs it, it succeeds in a millisecond, and
    the sub-agent is skipped, so no model is called and the *only*
    thing that decides which task runs is the scheduling gate.

    The directory is a git repository because the executor holds a
    ``GitManager``, which opens the directory as a repository at
    construction time; a directory that is not one is a construction
    error, not a scheduling detail.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    tasks = [
        {
            "id": task_id,
            "title": f"task {task_id}",
            "description": f"task {task_id}",
            "test_command": "true",
            "depends_on": list(depends_on),
            "files_to_modify": [],
        }
        for task_id, depends_on in TASK_GRAPH
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {"requirement": "hitl execution gate e2e", "tasks": tasks},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "hitl_config.json").write_text(
        json.dumps(HITL_CONFIG, ensure_ascii=False), encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", "-q", "."],
        cwd=str(plan_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return plan_dir


def _seed_ready_plan(state_db: Path, plan_id: str) -> None:
    """Put the plan in the ``ready`` phase the execution start wants.

    A plan that has never been through the interview has no routing
    row, and the start endpoint's compare-and-set refuses to move a
    plan it cannot find.  The row is written straight into the
    database the server owns: the server reads routing state from the
    database on every request, so it sees the row without being told.
    """
    # ``isolation_level=None`` -- the connection is in autocommit mode,
    # which is what the production migrator requires for its DDL to
    # apply (see ``state_machine.db.schema.migrate``).
    connection = sqlite3.connect(str(state_db), isolation_level=None)
    try:
        # The database the server owns carries the production schema, and
        # in production it is the writer that creates it.  This test
        # writes the routing row itself, so it has to create the schema
        # too: ``sqlite3`` makes a bare file otherwise, and the INSERT
        # below dies with "no such table: plan_routing".
        migrate(connection)
        connection.execute(
            "INSERT OR IGNORE INTO plan_routing "
            "(plan_id, current_phase, substage, version, updated_at) "
            "VALUES (?, ?, NULL, 0, ?)",
            (plan_id, "ready", _now_iso()),
        )
        connection.commit()
    finally:
        connection.close()


def _decision_row(state_db: Path, decision_id: str) -> sqlite3.Row:
    """The decision's row, read straight out of SQLite.

    The row is read with ``sqlite3`` rather than through the service,
    so that the assertion is about the database and not about the code
    that wrote it.
    """
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM plan_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, f"no decision row for {decision_id!r}"
    return row


def _scheduled_task_ids(state_db: Path, plan_id: str) -> List[str]:
    """The task ids the execution side actually picked up.

    ``plan_tasks`` is the runtime mirror the executor writes a row into
    when it schedules a task.  A task the gate held back never gets a
    row, so this list is the schedule the execution side really ran —
    not a claim about what it would have run.
    """
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT task_id FROM plan_tasks WHERE plan_id = ? ORDER BY rowid",
            (plan_id,),
        ).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def _executor_events(plan_dir: Path, event: str) -> List[Dict[str, Any]]:
    """The executor's own structured log, filtered to one event name."""
    log_path = plan_dir / EXECUTION_LOG_FILENAME
    if not log_path.exists():
        return []
    found: List[Dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == event:
            found.append(record)
    return found


def _held_task_ids(plan_dir: Path) -> List[str]:
    """The task ids the executor said it was waiting for.

    Read from the executor's own ``execution_paused`` record, which
    names the tasks it is holding.  The executor's stop reason alone
    would say *that* it stopped; this says *which* tasks it stopped
    for.
    """
    paused = _executor_events(plan_dir, "execution_paused")
    if not paused:
        return []
    data = paused[-1].get("data") or {}
    return list(data.get("held_task_ids") or [])


def _plan_verdict(plan_dir: Path) -> Dict[str, Any]:
    """The executor's own verdict, as it left it in the plan's task list.

    The executor writes down why it stopped next to the tasks it was
    working on, so the plan's own document says what happened without
    anybody having to interpret an exit code.
    """
    document = json.loads(
        (plan_dir / "tasks.json").read_text(encoding="utf-8")
    )
    return {
        "stop_reason": document.get("stop_reason"),
        "reason_detail": document.get("reason_detail"),
    }


# ---------------------------------------------------------------------------
# The server under test
# ---------------------------------------------------------------------------


class _Server:
    """A real ``backend.server`` process and everything it owns.

    The subprocess gets its own SQLite file, its own plans tree and
    its own port, so everything this suite asserts about is the state a
    real deployment would have written.  The handle can be stopped and
    started again on the same port — that is what the restart case
    needs.
    """

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.plans_dir = workdir / "plans"
        self.state_db = workdir / "state.db"
        self.port = _free_port()
        self.proc: Optional[subprocess.Popen] = None
        self._log_handle = None

    @property
    def base_url(self) -> str:
        """The URL the suite talks to."""
        return f"http://{LOOPBACK_HOST}:{self.port}"

    def _env(self) -> Dict[str, str]:
        """The environment the server runs in.

        Everything the server owns is pointed at this suite's own
        directory: its own SQLite file, its own plans tree, its own
        port.  ``PDT_PROVIDER_MODE=inherit`` and a coding tool that is
        not installed keep the suite away from a live provider — the
        tasks' pre-flight test commands satisfy them, so no model is
        ever asked for anything.
        """
        env = dict(os.environ)
        env.update(
            {
                "PDT_STATE_DB_PATH": str(self.state_db),
                "PDT_PLANS_DIR": str(self.plans_dir),
                "PDT_HOST": LOOPBACK_HOST,
                "PDT_PORT": str(self.port),
                # The loopback hostnames are accepted by the guard on
                # their own; ``testserver`` deliberately is not on the
                # list, so a request that carried it would be refused.
                "PDT_ALLOWED_HOSTS": f"{LOOPBACK_HOST},localhost",
                "PDT_PROVIDER_MODE": "inherit",
                "PDT_DEFAULT_CODING_TOOL": "vendor-c",
                "PYTHONPATH": (
                    f"{BACKEND_DIR}{os.pathsep}{PROJECT_ROOT}"
                    f"{os.pathsep}{env.get('PYTHONPATH', '')}"
                ),
            }
        )
        # The secret handles are inherited file descriptors, and the
        # backend closes the descriptor it is handed.  In a test
        # process that descriptor is frequently an unrelated one, and
        # the server dies on it during startup.
        for name in [key for key in env if key.startswith("PDT_SECRET_FD_")]:
            env.pop(name, None)
        return env

    def boot(self) -> None:
        """Start the server and wait until it answers.

        The port is the one this handle probed; the server prints a
        line when it had to fall back to another one, and the handle
        follows that line so the suite always talks to the process it
        started.
        """
        assert self.proc is None, "the server is already running"
        for directory in (self.plans_dir, self.workdir / "backups", self.workdir / "logs"):
            directory.mkdir(parents=True, exist_ok=True)

        self._log_handle = (self.workdir / "logs" / "server.log").open("ab")
        self.proc = subprocess.Popen(
            [str(VENV_PYTHON), "server.py"],
            cwd=str(BACKEND_DIR),
            env=self._env(),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            self._wait_until_healthy()
        except Exception:
            self._kill()
            raise
        self._adopt_fallback_port()

    def _adopt_fallback_port(self) -> None:
        """Follow the server's own report of the port it bound.

        The server increments the port when the one it was given is
        taken.  Reading the line it printed keeps the suite's URL and
        the running process in agreement.
        """
        log_path = self.workdir / "logs" / "server.log"
        if not log_path.exists():
            return
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "using fallback port" in line:
                self.port = int(line.rsplit(" ", 1)[1])

    def _wait_until_healthy(self, timeout: float = 60.0) -> None:
        """Block until ``GET /health`` answers 2xx."""
        deadline = time.time() + timeout
        last: Optional[Exception] = None
        while time.time() < deadline:
            try:
                with httpx.Client(timeout=2.0, trust_env=False) as probe:
                    response = probe.get(f"{self.base_url}/health")
                if 200 <= response.status_code < 300:
                    return
            except Exception as exc:  # noqa: BLE001 - any failure is "not yet"
                last = exc
            time.sleep(0.2)
        raise RuntimeError(
            f"the server at {self.base_url} did not start in {timeout}s; "
            f"last error: {last!r}"
        )

    def _kill(self) -> None:
        """Terminate the server, escalate to a kill, and close its log."""
        proc = self.proc
        self.proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        """Stop the server and assert it let go of the port.

        A server that exits but leaves a child listening would keep
        the port, and the next start on it would land on the wrong
        process.  The rebind is the check that the port really came
        back.
        """
        self._kill()
        _assert_port_rebindable(self.port)


# ---------------------------------------------------------------------------
# The HTTP client
# ---------------------------------------------------------------------------


class _Client:
    """An HTTP client that carries the guard's header and a real host.

    Every ``/api/*`` call needs the ``X-PDT-Request`` header and a
    ``Host`` the guard accepts.  The host is this machine's loopback
    address — not the ``testserver`` a ``TestClient`` would send, which
    the guard refuses, and which is exactly the difference between a
    test that exercises the request path and one that bypasses it.
    """

    def __init__(self, base_url: str, port: int) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=30.0,
            # ``trust_env=False`` keeps the request on the loopback
            # interface.  A developer machine may have an HTTP proxy
            # configured in the environment, and httpx would otherwise
            # route *these* calls through it — which is not the thing
            # under test, and which answers a request naming a
            # different host with the proxy's own 502 instead of the
            # server's 403.
            trust_env=False,
            headers={
                REQUEST_HEADER: REQUEST_HEADER_VALUE,
                "Host": f"{LOOPBACK_HOST}:{port}",
            },
        )

    def get(
        self, path: str, headers: Optional[Dict[str, str]] = None
    ) -> httpx.Response:
        return self._client.get(path, headers=headers)

    def post(
        self,
        path: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        return self._client.post(path, json=payload, headers=headers)

    def close(self) -> None:
        self._client.close()


@pytest.fixture
def server(tmp_path: Path):
    """A running backend, its own state directory, and a client for it.

    The handle is function-scoped: every case gets a server of its
    own, so one case's plan, decisions and executor run cannot be seen
    by the next one.
    """
    handle = _Server(tmp_path)
    handle.boot()
    client = _Client(handle.base_url, handle.port)
    try:
        yield handle, client
    finally:
        client.close()
        handle.stop()


# ---------------------------------------------------------------------------
# The flow under test
# ---------------------------------------------------------------------------


def _ask_question(
    client: _Client, plan_id: str, task_id: str
) -> Dict[str, Any]:
    """Ask the human a question about ``task_id``, over real HTTP.

    The question is asked through the product's own route, against the
    real server, with the real request guard in front of it.  It is
    ``strict``: a question a human has to answer, with no fallback for
    the backend to take on the operator's behalf.
    """
    response = client.post(
        f"/api/hitl/{plan_id}/decisions",
        {
            "question": f"May task {task_id} run?",
            "options": ["accept", "reject"],
            "task_id": task_id,
            "mode": "strict",
            "source": "execution",
        },
    )
    assert response.status_code == 200, response.text
    decision = response.json()
    assert decision["status"] == "pending", decision
    assert decision["mode"] == "strict", decision
    assert decision["task_id"] == task_id, decision
    return decision


def _start_execution(client: _Client, plan_id: str, plan_dir: Path) -> None:
    """Start the plan's execution the way an operator would."""
    response = client.post(
        f"/api/execution/{plan_id}/start",
        {"project_dir": str(plan_dir)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "started", response.text


def _wait_for_stop_reason(
    client: _Client, plan_id: str, expected: str
) -> Dict[str, Any]:
    """Block until the executor has settled with ``expected``.

    Two things have to be true before the plan's own state can be
    read: the executor wrote its stop reason into the plan's task
    list, and the server saw the executor process exit.  Waiting for
    both keeps the case from reading a half-written plan.
    """
    deadline = time.time() + SETTLE_TIMEOUT_SEC
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"/api/execution/{plan_id}/progress")
        if response.status_code == 200:
            last = response.json()
            if (
                last.get("stop_reason") == expected
                and last.get("execution_status") in ("completed", "failed")
            ):
                return last
        time.sleep(0.2)
    raise AssertionError(
        f"the executor never settled with stop_reason={expected!r}; "
        f"last progress: {json.dumps(last, ensure_ascii=False)}"
    )


def _suspended_plan(
    client: _Client, plan_id: str, plan_dir: Path
) -> Dict[str, Any]:
    """Ask the question, start the plan, and wait for it to suspend."""
    decision = _ask_question(client, plan_id, "B")
    _start_execution(client, plan_id, plan_dir)
    progress = _wait_for_stop_reason(client, plan_id, STOP_REASON_PENDING)
    return {"decision": decision, "progress": progress}


# ---------------------------------------------------------------------------
# 1 — the execution side holds the question's task and its downstream
# ---------------------------------------------------------------------------


def test_strict_suspends_task_and_downstream(server):
    """A strict question holds its task and the downstream; the rest runs.

    The question is on ``B``, so ``B`` and ``C`` — which depends on it
    — are held out of the schedule while ``A`` runs to completion.  The
    executor says so itself: it stops with ``hitl_decision_pending``
    and names the tasks it is waiting for.

    The decision stays ``pending``.  A strict question has no
    substitute for a human, so nothing resolves it on the operator's
    behalf, and the execution side writes no ``pause_state.json``: that
    document belongs to the verification side, and an execution that
    wrote one would be claiming a pause the plan never took.
    """
    handle, client = server
    plan_id = "hitl-exec-strict-suspend"
    plan_dir = _write_plan(handle.plans_dir, plan_id)
    _seed_ready_plan(handle.state_db, plan_id)

    decision = _ask_question(client, plan_id, "B")
    _start_execution(client, plan_id, plan_dir)
    progress = _wait_for_stop_reason(client, plan_id, STOP_REASON_PENDING)

    # --- the HTTP layer: the plan is waiting, and it says why ---------
    assert progress["pending_decisions"]["count"] == 1, progress
    assert progress["pending_decisions"]["items"][0]["decision_id"] == (
        decision["decision_id"]
    ), progress
    assert progress["stop_reason"] == STOP_REASON_PENDING, progress

    # --- the execution layer: A ran, B and C never did ----------------
    assert _scheduled_task_ids(handle.state_db, plan_id) == ["A"], (
        "the execution side must pick up A and nothing else: B is the "
        "question's task and C is downstream of it"
    )
    assert _held_task_ids(plan_dir) == ["B", "C"], (
        "the executor must name B and C as the tasks it is waiting for"
    )
    assert progress["counts"]["completed"] == 1, progress
    assert progress["counts"]["pending"] == 2, progress

    # --- the process boundary is real, not an in-process fake -------
    # The premise of this file is that a ``TestClient`` cannot show any
    # of the facts above, because every one of them is a fact about
    # *processes*.  The executor's pid is the server's child, not the
    # server's own: an executor that shared the API server's process
    # would have answered the question out of that process's memory,
    # and the decision would never have had to cross a process
    # boundary at all — which is the thing this file exists to rule
    # out.
    assert progress["pid"] is not None, progress
    assert handle.proc is not None, "the API server process is gone"
    assert progress["pid"] != handle.proc.pid, (
        f"the executor must run in its own process: the progress "
        f"endpoint reported pid {progress['pid']!r}, which is the API "
        f"server's own pid ({handle.proc.pid!r}).  An executor that "
        f"shared the server's process would read the decision out of "
        f"that process's memory instead of out of the database, and "
        f"this file would be proving nothing"
    )

    # --- the SQLite layer: the question is still open -----------------
    # The state lives in a file on disk, not in the server process's
    # memory — the property the executor's separate process depends
    # on, and the reason a restart does not lose the question.
    assert handle.state_db.is_file(), (
        f"the state must live in a file-backed SQLite database, not in "
        f"the server process's memory; expected a file at "
        f"{handle.state_db!r}"
    )
    row = _decision_row(handle.state_db, decision["decision_id"])
    assert row["status"] == "pending", dict(row)
    assert row["mode"] == "strict", dict(row)
    assert row["task_id"] == "B", dict(row)

    # --- the file layer: the executor's own verdict, and no pause ----
    verdict = _plan_verdict(plan_dir)
    assert verdict["stop_reason"] == STOP_REASON_PENDING, verdict
    assert "B" in (verdict["reason_detail"] or ""), verdict
    assert "C" in (verdict["reason_detail"] or ""), verdict

    assert not (plan_dir / PAUSE_CHECKPOINT_FILENAME).exists(), (
        "the execution side must not write a pause checkpoint; that "
        "document belongs to the verification side"
    )

    # --- the request guard is in force, not bypassed ------------------
    refused = client.post(
        f"/api/hitl/{plan_id}/decisions",
        {
            "question": "May task B run?",
            "options": ["accept", "reject"],
            "task_id": "B",
        },
        headers={"Host": TESTCLIENT_HOST},
    )
    assert refused.status_code == 403, refused.text


# ---------------------------------------------------------------------------
# 2 — answering releases what the question held
# ---------------------------------------------------------------------------


def test_answer_releases(server):
    """Answering the question opens the gate by itself.

    Nothing has to be un-blocked: the gate is recomputed from the
    decision's status, so the answered decision no longer holds
    anything back and the next run picks ``B`` and ``C`` up and
    finishes the plan.
    """
    handle, client = server
    plan_id = "hitl-exec-strict-release"
    plan_dir = _write_plan(handle.plans_dir, plan_id)
    _seed_ready_plan(handle.state_db, plan_id)

    suspended = _suspended_plan(client, plan_id, plan_dir)
    decision = suspended["decision"]
    assert _scheduled_task_ids(handle.state_db, plan_id) == ["A"], suspended

    answer = client.post(
        f"/api/hitl/{plan_id}/decisions/{decision['decision_id']}/answer",
        {"option": "accept", "answered_by": "e2e"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "answered", answer.text

    # The answer alone changes nothing on disk: the release is the gate
    # recomputing, not a task status being rewritten.
    row = _decision_row(handle.state_db, decision["decision_id"])
    assert row["status"] == "answered", dict(row)

    _start_execution(client, plan_id, plan_dir)
    progress = _wait_for_stop_reason(client, plan_id, STOP_REASON_SUCCESS)

    assert progress["pending_decisions"]["count"] == 0, progress
    assert _scheduled_task_ids(handle.state_db, plan_id) == ["A", "B", "C"], (
        "the released tasks must be picked up by the next run"
    )
    assert progress["counts"]["completed"] == 3, progress
    assert progress["counts"]["pending"] == 0, progress


# ---------------------------------------------------------------------------
# 3 — the decision outlives the process that asked it
# ---------------------------------------------------------------------------


def test_decision_survives_restart(server):
    """A restart does not release the gate, and does not lose the question.

    The server is stopped and started again on the same port.  The
    decision is still there, still ``pending``, and the execution side
    is still holding the same two tasks: a question that lived in the
    asking process's memory would have vanished, and the plan would
    have run the very work the human was asked about.
    """
    handle, client = server
    plan_id = "hitl-exec-strict-restart"
    plan_dir = _write_plan(handle.plans_dir, plan_id)
    _seed_ready_plan(handle.state_db, plan_id)

    suspended = _suspended_plan(client, plan_id, plan_dir)
    decision = suspended["decision"]
    assert _held_task_ids(plan_dir) == ["B", "C"], suspended

    # --- the process goes away ---------------------------------------
    handle.stop()
    client.close()

    # --- and comes back on the same port ------------------------------
    handle.boot()
    client = _Client(handle.base_url, handle.port)
    try:
        listed = client.get(f"/api/hitl/{plan_id}/decisions?status=pending")
        assert listed.status_code == 200, listed.text
        pending = listed.json()
        assert len(pending) == 1, pending
        assert pending[0]["decision_id"] == decision["decision_id"], pending
        assert pending[0]["status"] == "pending", pending
        assert pending[0]["mode"] == "strict", pending
        assert pending[0]["task_id"] == "B", pending

        # The execution side still sees it, and still holds the same
        # two tasks.
        progress = client.get(f"/api/execution/{plan_id}/progress")
        assert progress.status_code == 200, progress.text
        body = progress.json()
        assert body["pending_decisions"]["count"] == 1, body
        assert body["stop_reason"] == STOP_REASON_PENDING, body
        assert _scheduled_task_ids(handle.state_db, plan_id) == ["A"], body

        row = _decision_row(handle.state_db, decision["decision_id"])
        assert row["status"] == "pending", dict(row)
        assert row["mode"] == "strict", dict(row)

        assert not (plan_dir / PAUSE_CHECKPOINT_FILENAME).exists(), (
            "the execution side must not write a pause checkpoint; that "
            "document belongs to the verification side"
        )

        # The plan's own document still says the executor was waiting.
        verdict = _plan_verdict(plan_dir)
        assert verdict["stop_reason"] == STOP_REASON_PENDING, verdict
    finally:
        client.close()
