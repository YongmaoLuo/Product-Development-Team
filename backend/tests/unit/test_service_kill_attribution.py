"""Who asked the service to die? (2026-10-26, repair-r1-49).

The incident
------------
On 2026-10-26 at 02:21:49 the round's api service finished its readiness
self-check and the ledger recorded it as ready — pid 43904, port 64469.
Two seconds later the service logged ``shutdown complete`` and was gone.

The only trace of the death was on the **service's** side::

    received SIGTERM pid=43904 verifying_plans=[]

Nothing said **who** sent that signal, and nothing said **why**.  The
round's own log held no kill event at all, so the round could not tell a
startup sweep from a shutdown from a rival round's reap — it could only
know that *somebody* had sent a SIGTERM.  The same shape had happened
twice before (2026-10-24, 21:44 and 21:54): three rounds in a row, each
losing its service two or three seconds after it came up, each finding
out from a VP that met a refused connection two minutes later.

The gap was structural.  The signals were sent from scattered places —
the round's own reap, the startup sweep, the shutdown sweep, the
execution-subprocess cleanup — and each of them was right to send a
SIGTERM while none of them left a record.  "The service died" had no
"somebody asked it to" beside it.

What this file pins
-------------------
* **The audit.**  One function — :func:`service_manager.kill_service` —
  is the only place a service is signalled from, and it writes the
  request down **before** the signal goes out.
  ``service_kill_requested`` carries the pid, the port, the caller, the
  reason, the round the service belonged to, and a hint at the call
  site.  The next death of this shape can be read out of the round's own
  event stream instead of reconstructed from timestamps.
* **The criterion.**  A plan parked on capacity stays parked.  Its
  status and its stop reason both keep saying ``waiting_for_quota`` —
  through the death of the service it was running against, and through
  the restart that followed — and neither is ever rewritten into
  ``failed`` or ``stopped``.  A pause is not a crash, and the operator
  who asks "what is still left to do?" must not be told otherwise.

Hermeticity
------------
Everything lives under the test's own ``tmp_path``: the plans tree, the
state database (the autouse ``isolated_plans_dir`` fixture points
``server.PLANS_DIR`` / ``server._state_db_path`` and the
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` env vars at it), and the
service's own script.  The rows are written through the real
repositories, so the endpoint reads back the shapes production writes.
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402


#: The plan the parked run belongs to.
PLAN_ID = "20261026-vp-quota-parked-kill"

#: The round that started the run — the token every event of this round
#: carries, so the streams can be correlated.
ROUND_TOKEN = "round-token-kill-attribution"

#: The three tasks of the parked plan.  ``VP-001`` is the one that ran
#: into the provider's wall; the two after it are downstream of it and
#: must therefore still be ``pending``.
TASK_IDS = ("VP-001", "VP-002", "VP-003")

#: The task that was interrupted by the park.
INTERRUPTED_TASK_ID = "VP-001"

#: How many routing rows the round's fixture holds.
_FIXTURE_ROWS = 4


# ---------------------------------------------------------------------------
# The service under test
# ---------------------------------------------------------------------------
#
# A real process on a real port: the whole subject is a *process* dying,
# and a stubbed process cannot go anywhere.

#: The service, written into the scratch directory at test time rather
#: than committed.  It answers two routes: ``/health`` (the readiness
#: probe) and ``/health/data`` (the data-plane probe, carrying the
#: number of routing rows the round's fixture holds).
_SERVICE_SOURCE = '''\
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROWS = int(sys.argv[1])
PORT = int(sys.argv[2])


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0].endswith("/health/data"):
            body = json.dumps({
                "status": "ok",
                "plan_routing_rows": ROWS,
                "instance": {"boot_id": "kill-attribution-fixture"},
            }).encode("utf-8")
        else:
            body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), _Handler).serve_forever()
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be running.

    A real process is started and waited for, so the number returned is
    one the operating system has already handed out and already taken
    back.  The assertion at the end is what makes that a fact rather
    than a hope.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pid = proc.pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    raise AssertionError(
        f"pid {pid} came back to life — it is not a dead pid, so this "
        f"test would be probing a live process"
    )


def _write_tasks_file(plan_dir: Path) -> None:
    """Write the static task list of the plan.

    Static fields only — ``status`` lives in ``state.db``
    (``plan_tasks``) since the runtime overlay landed there.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": tid, "title": f"task {tid}", "description": f"do {tid}"}
        for tid in TASK_IDS
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )


def _seed_parked_execution(project_dir: Optional[Path]) -> int:
    """Write the row and the task rows of a run that parked on capacity.

    The row says ``waiting_for_quota`` **and** carries that reason: a
    pause and the reason for it are both durable, and a reader that only
    had the status could not tell a parked run from a crashed one.

    The pid the row carries belongs to the process that owned the run —
    and that process has already exited, because the park ended it.
    One task was interrupted by the park and still reads
    ``in_progress``; the two tasks downstream of it are still
    ``pending``.

    Returns the dead owner's pid.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    owner_pid = _dead_pid()
    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        row_fields: Dict[str, Any] = {
            "exec_status": "waiting_for_quota",
            "stop_reason": "waiting_for_quota",
            "exec_pid": owner_pid,
            "started_at": "2026-10-26T02:21:00",
        }
        if project_dir is not None:
            row_fields["project_dir"] = str(project_dir)
        ExecutionRepository(conn).update_fields(
            PLAN_ID, create_if_missing=True, **row_fields,
        )

        task_repo = PlanTaskRepository(conn)
        for tid in TASK_IDS:
            task_repo.add_task(
                PLAN_ID,
                {"id": tid, "title": f"task {tid}", "description": f"do {tid}"},
            )
        task_repo.update_task(
            PLAN_ID, INTERRUPTED_TASK_ID, {"status": "in_progress"},
        )
    finally:
        conn.close()
    return owner_pid


def _read_execution_row() -> Dict[str, Any]:
    """Read the durable execution row back."""
    from state_machine.db.connection import open as db_open
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        return ExecutionRepository(conn).summary(PLAN_ID) or {}
    finally:
        conn.close()


def _leave_the_stale_owner_view(owner_pid: int, project_dir: Path) -> None:
    """Leave this process holding the owner's stale view of the run.

    The boot-time recovery scan restores a row into the registry while
    the row still says ``running``.  The registry is never told about
    the park that happens afterwards, because the park is recorded by
    the process that owns the run — a different process from the one
    serving.  So the serving process keeps saying "running" with a pid
    that has since exited, and that stale view is exactly what the
    progress endpoint must not mistake for the truth.
    """
    server._execution_state[PLAN_ID] = {
        "status": "running",
        "pid": owner_pid,
        "started_at": "2026-10-26T02:21:00",
        "ended_at": None,
        "stop_reason": None,
        "project_dir": str(project_dir),
        "process": None,
        "logs": deque(maxlen=2000),
    }


def _free_port() -> int:
    port = sm.allocate_free_port()
    assert port is not None, "could not allocate a free port"
    return port


def _declaration(port: int, script: Path) -> ServiceDeclaration:
    return ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=(
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
            f"{_FIXTURE_ROWS} {port}"
        ),
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=30,
        log_path="tmp/api.log",
    )


def _get_json(url: str, timeout: float = 5.0) -> Tuple[int, Dict[str, Any]]:
    """``GET *url*`` — returns ``(status, body)``."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return int(response.status), json.loads(raw)


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.1):
    """Wait until *predicate* is true.  Returns its value, or ``None``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def round_dirs(tmp_path: Path) -> Tuple[Path, Path]:
    """The plan directory and the project directory of one round."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir, project_dir


@pytest.fixture
def declaration(round_dirs) -> ServiceDeclaration:
    """The declaration of the round's api service.

    One declaration for the whole test: the service that comes back
    after the kill must take **the same port** — a restart that moved
    the address would be a different incident.
    """
    _, project_dir = round_dirs
    project_dir.mkdir(parents=True, exist_ok=True)
    script = project_dir / "fixture_service.py"
    script.write_text(_SERVICE_SOURCE, encoding="utf-8")
    return _declaration(_free_port(), script)


@pytest.fixture
def started() -> Iterator[List[sm.ServiceRuntime]]:
    """Every service this test started, so the teardown can stop it.

    A service left running keeps its port, and the next test that asks
    for a port then fails for a reason that has nothing to do with what
    it is testing.
    """
    services: List[sm.ServiceRuntime] = []
    yield services
    for runtime in services:
        sm.stop_service(runtime, None, grace_seconds=5.0)


@pytest.fixture
def execution_state_guard() -> Iterator[None]:
    """Give the test the registry, and give it back the way it was."""
    saved = dict(server._execution_state)
    server._execution_state.clear()
    try:
        yield
    finally:
        server._execution_state.clear()
        server._execution_state.update(saved)


def _bring_up(
    plan_dir: Path, project_dir: Path, declaration: ServiceDeclaration,
) -> sm.ServiceRuntime:
    """Bring the round's api service up through ``ensure_services``."""
    runtime_map = sm.ensure_services(
        project_dir, {"api": declaration},
        plan_dir=plan_dir, plan_id=PLAN_ID,
    )
    return runtime_map.runtimes["api"]


# ---------------------------------------------------------------------------
# The criterion
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_a_parked_execution_is_still_answerable_after_the_owning_process_exits(
    round_dirs,
    declaration,
    started,
    execution_state_guard,
) -> None:
    """A parked run is still parked after its service died and came back.

    This is the shape of the 2026-10-26 round.  The run parked on
    capacity; the process that owned it exited; the round's api service
    was killed two seconds after it came up and was gone before the
    first VP asked it anything; a new instance took the port.

    The operator's question does not change with any of that: *what is
    still left to do?*  The answer is in the row — the task that was
    interrupted by the park is still the task that was in flight, the
    two tasks downstream of it are still waiting, and nothing has
    failed.

    And the pause stays a pause in **both** fields.  ``exec_status``
    says ``waiting_for_quota`` and ``stop_reason`` says
    ``waiting_for_quota``: a status without its reason is a status that
    a restart cannot interpret, and a reason without its status is a
    note nobody reads.  Neither of them may become ``failed`` (the
    service died — that is not the run's fault) or ``stopped`` (nobody
    stopped it — the service died).

    The kill itself is on the record: the request that ended the
    service names who asked and why, so the round's own log can answer
    the question the incident could not.
    """
    plan_dir, project_dir = round_dirs
    _write_tasks_file(plan_dir)

    # The run parked on capacity; the process that owned it is gone.
    owner_pid = _seed_parked_execution(project_dir=project_dir)
    _leave_the_stale_owner_view(owner_pid, project_dir)

    # The round's api service, up and answering.
    runtime = _bring_up(plan_dir, project_dir, declaration)
    started.append(runtime)
    assert runtime.ready is True, runtime.detail
    first_pid = runtime.pid

    # It is killed — and the request is written down first.
    outcome = sm.kill_service(
        runtime,
        "test.the_round_exits",
        "the round's own cleanup",
        ROUND_TOKEN,
        plan_dir=plan_dir,
    )
    assert outcome["signalled"], outcome

    # And it comes back: a new instance takes the same port.
    assert _wait_for(
        lambda: not sm.probe_port(declaration.port).listening,
        timeout=20.0,
    ) is not None, "the service must release its port"
    again = _bring_up(plan_dir, project_dir, declaration)
    started.append(again)
    assert again.ready is True, again.detail
    assert again.pid != first_pid, (
        "the service must be started again, not the dead runtime reused"
    )

    # The parked run still answers.
    client = TestClient(server.app)
    resp = client.get(f"/api/execution/{PLAN_ID}/progress")

    assert resp.status_code == 200, (
        f"a parked run must still answer the progress endpoint after its "
        f"service died and came back; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body["execution_status"] == "waiting_for_quota", (
        f"the endpoint reported {body['execution_status']!r}; the run "
        f"parked on capacity — the death of the service it was running "
        f"against says nothing about the work having failed"
    )

    counts = body.get("counts") or {}
    assert counts.get("failed") == 0, (
        f"a quota wait must not fail anything: {counts!r}"
    )
    assert counts.get("in_progress") == 1, (
        f"the task that was interrupted by the park must still read as "
        f"in flight — it was interrupted, never judged: {counts!r}"
    )
    assert counts.get("pending") == 2, (
        f"the two tasks downstream of the interrupted one must still be "
        f"pending: {counts!r}"
    )

    # The durable verdict says the same thing in **both** fields.
    row = _read_execution_row()
    assert row.get("exec_status") == "waiting_for_quota", (
        f"a parked run must keep its status; the row now says "
        f"{row.get('exec_status')!r}"
    )
    assert row.get("stop_reason") == "waiting_for_quota", (
        f"a parked run must keep its reason — it is the only thing that "
        f"survives a restart and tells a reader the run is waiting, not "
        f"broken; the row now says {row.get('stop_reason')!r}"
    )
    assert row.get("exec_status") not in ("failed", "stopped"), (
        f"the run was not failed and not stopped; the row says "
        f"{row.get('exec_status')!r}"
    )

    # And the kill is on the record — with the fields that answer the
    # question the 2026-10-26 round could not.
    events = sm.read_kill_events(plan_dir)
    kills = [e for e in events if e.get("event") == sm.KILL_EVENT_REQUESTED]
    assert len(kills) == 1, (
        f"exactly one kill request must be on the record; got {events!r}"
    )
    kill = kills[0]
    assert kill.get("caller") == "test.the_round_exits", kill
    assert kill.get("reason") == "the round's own cleanup", kill
    assert kill.get("round_token") == ROUND_TOKEN, kill
    assert kill.get("pid") == first_pid, kill
    assert kill.get("port") == declaration.port, kill
    assert kill.get("stack_hint"), (
        f"the request must say where it was asked for; got {kill!r}"
    )


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def test_the_kill_request_names_its_caller_and_reason(round_dirs) -> None:
    """The request says who asked, and why.

    A SIGTERM without a name is a rumour.  The 2026-10-26 service died
    of one, and the round could not say whether a sweep, a shutdown or
    a rival round had sent it.
    """
    plan_dir, _ = round_dirs
    runtime = sm.ServiceRuntime(
        name="api", port=_free_port(), pid=_dead_pid(),
    )

    sm.kill_service(
        runtime, "service_manager.reap_services", "startup_sweep",
        ROUND_TOKEN, plan_dir=plan_dir,
    )

    events = sm.read_kill_events(plan_dir)
    assert len(events) == 1, events
    event = events[0]
    assert event["event"] == sm.KILL_EVENT_REQUESTED
    assert event["caller"] == "service_manager.reap_services"
    assert event["reason"] == "startup_sweep"
    assert event["round_token"] == ROUND_TOKEN
    assert event["pid"] == runtime.pid
    assert event["port"] == runtime.port
    assert event["stack_hint"], event


def test_the_kill_request_is_written_before_the_signal_is_sent(
    round_dirs, monkeypatch,
) -> None:
    """The record is there *before* the signal goes out.

    A signal that lands first can take the round's log with it — and a
    request written afterwards is a request nobody can trust to describe
    what happened.
    """
    plan_dir, _ = round_dirs
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        runtime = sm.ServiceRuntime(
            name="api", port=_free_port(), pid=child.pid,
        )
        seen: List[bool] = []
        real_kill = os.kill

        def _spy(pid: int, sig: int) -> None:
            seen.append(bool(sm.read_kill_events(plan_dir)))
            real_kill(pid, sig)

        monkeypatch.setattr(sm.os, "kill", _spy)

        sm.kill_service(
            runtime, "test.ordering", "the request goes first",
            ROUND_TOKEN, plan_dir=plan_dir,
        )

        assert seen, "the signal was never sent"
        assert all(seen), (
            f"the request must be on the record before the signal is "
            f"sent; the event file held a request at signal time: {seen!r}"
        )
    finally:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            child.kill()
            child.wait(timeout=5)


@pytest.mark.timeout(300)
def test_the_round_s_own_reap_names_itself_as_the_caller(
    round_dirs, declaration, started,
) -> None:
    """The reap says it was the reap.

    This is the path that killed a running round's api server on
    2026-10-24: the sweep read the entry as an orphan and stopped it.
    The sweep was not wrong to stop an orphan — it was silent about
    stopping it, so nobody could tell it apart from a crash.
    """
    plan_dir, project_dir = round_dirs
    runtime = _bring_up(plan_dir, project_dir, declaration)
    started.append(runtime)
    assert runtime.ready is True, runtime.detail

    sm.register_service_owner(
        plan_dir, ROUND_TOKEN, current=True, pid=os.getpid(),
    )
    sm.record_runtimes(
        plan_dir, PLAN_ID,
        sm.ServiceRuntimeMap(runtimes={"api": runtime}),
        project_dir=project_dir, owner_token=ROUND_TOKEN,
    )

    report = sm.reap_services(
        plan_dir, PLAN_ID, reason="verification_stopped",
        owner_token=ROUND_TOKEN,
    )
    assert report.reaped == ["api"], report.to_dict()

    events = sm.read_kill_events(plan_dir)
    kills = [e for e in events if e.get("event") == sm.KILL_EVENT_REQUESTED]
    assert len(kills) == 1, events
    kill = kills[0]
    assert kill["caller"] == "service_manager.reap_services", kill
    assert kill["reason"] == "verification_stopped", kill
    assert kill["round_token"] == ROUND_TOKEN, kill
    assert kill["pid"] == runtime.pid, kill
    assert kill["port"] == declaration.port, kill


# ---------------------------------------------------------------------------
# The rule the audit enforces: one place sends the signal
# ---------------------------------------------------------------------------


def _kill_calls(path: Path) -> List[Tuple[int, str, bool]]:
    """Every ``os.kill`` / ``os.killpg`` call in *path*.

    Returns ``(line, enclosing function, is_a_liveness_probe)`` for each
    one.  A liveness probe is ``os.kill(pid, 0)`` — it asks whether a
    process is there and signals nothing.

    The enclosing function is the **outermost** one: a helper nested
    inside :func:`service_manager.kill_service` is still inside it, and
    the rule the check enforces is "the signal is sent from the one
    function that owns it" — not "the signal is written on the
    function's own line".
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: List[Tuple[int, str, bool]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: List[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("kill", "killpg")
                and isinstance(func.value, ast.Name)
                and func.value.id in ("os", "_os")
            ):
                owner = self.stack[0] if self.stack else "<module>"
                probe = (
                    len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == 0
                )
                found.append((node.lineno, owner, probe))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def test_no_service_kill_is_sent_outside_kill_service() -> None:
    """One place signals a service — and one place says so.

    The audit only works if it is the only path.  A signal sent from
    anywhere else is a death with no request beside it, which is the
    hole this file exists to close.
    """
    violations: List[str] = []
    for path, allowed in (
        (_BACKEND_DIR / "service_manager.py", {"kill_service"}),
        # The server's own signal handler re-delivers the signal to
        # itself so the process dies — it signals nobody else's process.
        (_BACKEND_DIR / "server.py", {"_signal_handler"}),
    ):
        for lineno, owner, probe in _kill_calls(path):
            if owner in allowed or probe:
                continue
            violations.append(f"{path.name}:{lineno} in {owner}()")
    assert not violations, (
        f"a service kill must go through service_manager.kill_service so "
        f"that the request is written down first; stray signals: "
        f"{violations!r}"
    )
