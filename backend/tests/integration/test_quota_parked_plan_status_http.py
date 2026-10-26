"""HTTP contract: a quota-parked plan still answers the plan-status endpoint.

The criterion this file pins
----------------------------
The capacity-wait acceptance criterion reads:

    *the plan enters ``waiting_for_quota`` while its downstream tasks stay
    ``pending`` — no ``executor_exited_with_unfinished_tasks``*

Two rounds of probing never produced evidence about that criterion.  The
first probes hit a recycled backend::

    GET /api/plan/20261024-vp-quota-parked/status  ->  connection refused

That is a fact about the *service's* lifetime, not about the plan: the
backend was not running when the request was made, so the answer says
nothing at all about the plan's phase.  A one-shot probe against a shared
port is not evidence about a plan.

What the criterion actually needs
---------------------------------
``GET /api/plan/{plan_id}/status`` is the one endpoint an operator (or a
card) reads to answer "where is this plan, and what is still left to do?".
For a quota-parked plan that answer has to say three things at once:

  * the plan is **parked** — ``phase`` is ``waiting_for_quota``;
  * the execution record is **parked too** — the execution section's
    ``execution_status`` is ``waiting_for_quota``, and in particular NOT
    ``failed``: a wait is a pause, not a crash;
  * the work that is left is still **work** — the counts say three tasks,
    one of them done, two of them still ``pending``, and nothing failed.

The counts are the part that catches the regression the criterion names.
``executor_exited_with_unfinished_tasks`` is exactly what a plan looks
like when the executor goes away while its tasks are still unfinished: the
downstream tasks stop being ``pending`` and become ``failed``.  So the
counts are asserted one by one — ``total`` / ``completed`` / ``pending`` /
``failed`` / ``in_progress`` / ``skipped`` — rather than as a blob.

``execution_in_flight`` is asserted by **presence and type only**.  The
criterion does not say whether a parked executor counts as in flight; it
says the field must exist and be a boolean, so a consumer can ask the
question without special-casing a missing key.  Pinning the value would
make this file red the day someone decides a parked executor still counts
as in flight — which would be a legitimate change, not a regression.

Hermeticity
------------
The plans tree and the state database live under the test's own
``tmp_path`` — the autouse ``isolated_plans_dir`` fixture points
``server.PLANS_DIR`` / ``server._state_db_path`` and the
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` env vars at it — so the
operator's live database and ``plans/`` tree are never touched.  The rows
are written through the real repositories, so the endpoint reads back the
shapes production writes.

The in-memory ``_execution_state`` is cleared on purpose, and the test
does not rely on it for anything: the answer has to be readable **from the
durable rows alone**.  A server that happens to hold the plan in memory
would answer from memory, and the case this contract is about — the one
after a restart — is exactly the one where memory is empty.  The
``exec_pid`` the row carries belongs to a process that has already exited,
so nothing in the row says "failed".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

import server


#: The plan id the failed probes used.  Reusing it keeps the criterion
#: and its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-parked"

#: The three tasks of the parked plan.  ``VP-002`` is the one that ran
#: into the provider's wall; ``VP-003`` is downstream of it and must
#: therefore still be ``pending``.
TASK_IDS = ("VP-001", "VP-002", "VP-003")

#: The one task that finished before the park.
COMPLETED_TASK_ID = "VP-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be running.

    A real process is started and waited for, so the number returned is
    one the operating system has already handed out and already taken
    back.  The assertion at the end is what makes that a fact rather
    than a hope: if the PID were somehow alive again, the test says so
    instead of silently probing a stranger's process.
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

    Static fields only — ``status`` lives in ``state.db`` (``plan_tasks``)
    since the runtime overlay landed there, and the status endpoint reads
    the runtime state from there too.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": tid, "title": f"task {tid}", "description": f"do {tid}"}
        for tid in TASK_IDS
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )


def _seed_parked_plan(
    *,
    project_dir: Optional[Path],
    plan_id: str = PLAN_ID,
) -> None:
    """Write the routing row, the execution row and the ``plan_tasks`` rows.

    Every row goes through the repository production writes through, so
    the endpoint reads back exactly the shapes production produces.

      * ``plan_routing.current_phase`` = ``waiting_for_quota`` — the
        workflow phase the status object reports as ``phase``.
      * ``plan_execution.exec_status`` = ``waiting_for_quota`` — the
        execution record's own verdict.  Its ``exec_pid`` belongs to a
        process that has already exited: the row says "parked", and
        nothing in it says "failed".
      * ``plan_tasks`` — one row per task, with only ``VP-001`` completed.
        ``VP-002`` is the task that hit the provider wall and ``VP-003``
        is downstream of it; both must still read as ``pending``.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "waiting_for_quota")

        row_fields: dict[str, Any] = {
            "exec_status": "waiting_for_quota",
            "exec_pid": _dead_pid(),
            "started_at": "2026-10-24T15:00:00",
        }
        if project_dir is not None:
            row_fields["project_dir"] = str(project_dir)
        ExecutionRepository(conn).update_fields(
            plan_id, create_if_missing=True, **row_fields,
        )

        task_repo = PlanTaskRepository(conn)
        for tid in TASK_IDS:
            task_repo.add_task(
                plan_id,
                {"id": tid, "title": f"task {tid}", "description": f"do {tid}"},
            )
        # Only the first task finished.  The second one is the one that
        # hit the provider wall; the third one is downstream of it.
        task_repo.update_task(
            plan_id, COMPLETED_TASK_ID, {"status": "completed"},
        )
    finally:
        conn.close()


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The lifespan is what restores ``_execution_state`` from the rows at
    boot.  Skipping it is deliberate: this contract is about the rows
    being readable **on their own**, which is the state a server is in
    after it has been restarted with a plan it has never executed.
    """
    server._execution_state.clear()
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# The fixture: a plan parked on quota
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_dir(tmp_path: Path) -> Path:
    """The plans tree of this test — never the operator's."""
    path = tmp_path / "plans"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# The parked plan answers the status endpoint
# ---------------------------------------------------------------------------


def test_parked_plan_status_exposes_phase_and_counts(plans_dir: Path) -> None:
    """A quota-parked plan reports its phase, its execution status and its counts.

    The plan is parked: its routing row says ``waiting_for_quota`` and its
    execution row carries the PID of a process that has already exited.
    The question the status endpoint has to answer is the operator's —
    "where is this plan, and what is still left to do?" — and the answer
    is in the rows.

    The counts are the criterion: the downstream task is still
    ``pending``.  It was not judged dead (``failed``) and it was not
    skipped — a quota wait is a pause, and a pause does not consume
    anybody's turn.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    plan_dir = plans_dir / PLAN_ID
    _write_tasks_file(plan_dir)
    _seed_parked_plan(project_dir=project_dir)

    client = _client()
    resp = client.get(f"/api/plan/{PLAN_ID}/status")

    assert resp.status_code == 200, (
        f"a parked plan must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["phase"] == "waiting_for_quota", (
        f"the status object reported phase {body['phase']!r}; the routing "
        f"row says the plan is parked — a parked plan that reads as "
        f"something else is the plan the operator has to guess about"
    )

    execution = body.get("execution")
    assert isinstance(execution, dict), (
        f"the status object must carry the execution section; got "
        f"{execution!r}"
    )
    assert execution["execution_status"] == "waiting_for_quota", (
        f"the execution section reported "
        f"{execution['execution_status']!r}; the execution row says the "
        f"plan is parked, not running and not failed"
    )
    assert execution["execution_status"] != "failed", (
        f"a quota wait must not read as a failed execution: "
        f"{execution['execution_status']!r}"
    )

    # Presence and type only — the criterion says the field must exist
    # and be a boolean, not which boolean it is.
    assert "execution_in_flight" in body, (
        f"the status object must carry 'execution_in_flight'; got keys "
        f"{sorted(body)!r}"
    )
    assert isinstance(body["execution_in_flight"], bool), (
        f"'execution_in_flight' must be a boolean; got "
        f"{body['execution_in_flight']!r}"
    )

    counts = body.get("tasks")
    assert isinstance(counts, dict), (
        f"the status object must carry the task counts; got {counts!r}"
    )
    assert counts.get("total") == 3, f"unexpected counts: {counts!r}"
    assert counts.get("completed") == 1, f"unexpected counts: {counts!r}"
    assert counts.get("failed") == 0, (
        f"a quota wait must not fail anything: {counts!r}"
    )
    assert counts.get("in_progress") == 0, f"unexpected counts: {counts!r}"
    assert counts.get("pending") == 2, (
        f"the task that hit the wall and the task downstream of it must "
        f"still be pending: {counts!r}"
    )
    assert counts.get("skipped") == 0, (
        f"a quota wait must not skip anything: {counts!r}"
    )

# ---------------------------------------------------------------------------
# One unreadable section must not blank the whole plan
# ---------------------------------------------------------------------------


def test_status_section_degrades_to_a_readable_payload(
    plans_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A section that cannot be read must not take the plan's state with it.

    ``_plan_status_payload`` reads the three detail sections through the
    same handlers the standalone endpoints use, and a handler that
    raises used to take its whole section down to ``None``.  The damage
    that does is not "one section is missing" — it is "the plan's state
    is unknown": a consumer reading ``$.execution.execution_status`` and
    ``$.tasks.*`` got a subject-not-found for a plan whose rows were
    sitting right there in ``state.db``.

    The degradation is therefore partial instead of total.  The
    execution section keeps the verdict the state-machine row carries
    (``exec_status``) — that verdict is durable and is the difference
    between "parked, waiting for capacity" and "failed" — and the task
    counts keep the four numbers every reader asks for.  Only the parts
    that could not be read are blank, and the section says it is
    degraded so a reader can tell a reduced answer from a complete one.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    plan_dir = plans_dir / PLAN_ID
    _write_tasks_file(plan_dir)
    _seed_parked_plan(project_dir=project_dir)

    def _boom(_plan_id):
        raise RuntimeError("execution section exploded")

    monkeypatch.setattr(server, "get_execution_progress", _boom)

    client = _client()
    resp = client.get(f"/api/plan/{PLAN_ID}/status")

    assert resp.status_code == 200, (
        f"a plan whose execution section cannot be read must still answer; "
        f"got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["phase"] == "waiting_for_quota", (
        f"the verdict must survive a body section failing; got "
        f"{body.get('phase')!r}"
    )

    execution = body.get("execution")
    assert isinstance(execution, dict), (
        f"the execution section must stay readable when the section read "
        f"fails; got {execution!r}"
    )
    assert execution["execution_status"] == "waiting_for_quota", (
        f"the degraded section must still carry the row's verdict; got "
        f"{execution.get('execution_status')!r}"
    )
    assert execution.get("degraded") is True, (
        "a degraded section must say so — a reader has to be able to "
        "tell a reduced answer from a complete one"
    )

    counts = body.get("tasks")
    assert isinstance(counts, dict), (
        f"the task counts must stay readable; got {counts!r}"
    )
    for key in ("total", "in_progress", "pending", "failed"):
        assert key in counts, (
            f"the task counts must always carry {key!r}; got "
            f"{sorted(counts)!r}"
        )
    assert counts["total"] == 3, f"unexpected counts: {counts!r}"
    assert counts["in_progress"] == 0, f"unexpected counts: {counts!r}"
    assert counts["pending"] == 2, (
        f"the task that hit the wall and the task downstream of it must "
        f"still be pending: {counts!r}"
    )
    assert counts["failed"] == 0, (
        f"a quota wait must not fail anything: {counts!r}"
    )


# ---------------------------------------------------------------------------
# The declared port belongs to a leftover of our own plan
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nobody is listening on right now."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_listening(port: int, timeout: float = 120.0) -> bool:
    """Wait until *port* answers."""
    import service_manager

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_manager.probe_port(port).listening:
            return True
        time.sleep(0.5)
    return False


def _http_get(port: int, path: str) -> tuple[int, str]:
    """A GET against a running server, with the request guard's header."""
    import request_guard

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _start_cmd(port: int, plans_dir: Path, state_db: Path) -> str:
    """The shell line a round starts this plan's backend with.

    The line is a *shell* line on purpose: the data plane is pinned with
    an ``export``, and the running process only carries the program.  That
    is exactly what made our own leftover look like a stranger — the
    declaration could never match the process it starts.

    The provider mode is pinned here for the same reason the data plane
    is: the round's own instance is started from this line and inherits
    the environment of the process that starts it, and the hermetic env
    pins ``PDT_PROVIDER_MODE=cc-switch`` for the pytest process.  On a
    clean runner nothing is listening on CC Switch's port, so an
    inherited mode aborts the child's startup ("Application startup
    failed") and the instance never comes up.  Pin it in the line: the
    instance must not depend on the machine it happens to start on.
    """
    return (
        f"export PDT_PROVIDER_MODE=inherit && "
        f"export PDT_PLANS_DIR={plans_dir} && "
        f"export PDT_STATE_DB_PATH={state_db} && "
        f"export PDT_PORT={port} && "
        f"exec {sys.executable} -m backend.server"
    )


def _spawn_leftover(port: int, root: Path) -> subprocess.Popen:
    """The earlier round's instance: our program, another data plane.

    It is started with a *different* plans tree and state database, so it
    cannot see this plan's fixture — which is what made the round's
    status probe answer "404 Plan not found".
    """
    env = dict(os.environ)
    env.update({
        "PDT_PLANS_DIR": str(root / "other-plans"),
        "PDT_STATE_DB_PATH": str(root / "other-state.db"),
        "PDT_PORT": str(port),
        # The child inherits this process's environment, and the hermetic
        # env pins ``PDT_PROVIDER_MODE=cc-switch`` for the pytest
        # process.  The child must not inherit that mode: CC Switch is a
        # plugin of one operator's machine, and on a clean runner nothing
        # is listening on its port, so the child's startup aborts
        # ("Application startup failed") and the leftover never comes up.
        # Pin the mode here rather than trust the machine.
        "PDT_PROVIDER_MODE": "inherit",
    })
    log = open(root / "leftover.log", "wb")
    return subprocess.Popen(
        [sys.executable, "-m", "backend.server"],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _write_ledger(
    plan_dir: Path, project_dir: Path, port: int, pid: int, pgid: int,
    start_cmd: str,
) -> None:
    """The ledger the earlier round left behind: its pid, on our port."""
    import service_manager

    runtime = service_manager.ServiceRuntime(
        name="pdt-backend", port=port, start_cmd=start_cmd, cwd=".",
    )
    runtime.origin = "spawned"
    runtime.pid = pid
    runtime.pgid = pgid
    runtime.start_epoch = service_manager._process_start_epoch(pid)
    runtime.owner_token = ""
    runtimes = service_manager.ServiceRuntimeMap(
        runtimes={"pdt-backend": runtime},
    )
    service_manager.record_runtimes(
        plan_dir, PLAN_ID, runtimes, project_dir=project_dir, owner_token="",
    )


def test_vp_004_plan_status_reports_parked_phase_and_counts(
    plans_dir: Path, tmp_path: Path,
) -> None:
    """A leftover of our own plan must not make the plan unreadable.

    The round's evidence: the plan-status endpoint answered "404 Plan not
    found" while the plan was parked, and the same endpoint answered 200
    with the right counts on a later probe.  The cause was the port: an
    instance this plan's earlier round left behind was holding the
    **declared** port, serving a different data plane, and the framework
    treated that leftover as a stranger — so the round's own copy was
    moved to a free port while the effective declarations kept pointing
    at the leftover.

    The fix is to recognise our own leftover and take the declared port
    back.  This test pins the whole chain: the leftover answers 503 (it
    serves another, empty data plane, so it cannot even search it — and
    it must not claim the plan is gone), the round reclaims the declared
    port instead of relocating, and the plan status on that port answers
    the criterion — parked, nothing failed, the two downstream tasks
    still pending.
    """
    import service_manager
    from service_declaration import ServiceDeclaration

    project_dir = Path(__file__).resolve().parents[3]
    plan_dir = plans_dir / PLAN_ID
    _write_tasks_file(plan_dir)
    _seed_parked_plan(project_dir=project_dir)

    port = _free_port()
    leftover = _spawn_leftover(port, tmp_path)
    runtime = None
    try:
        assert _wait_listening(port), (
            f"the leftover never started listening on port {port}"
        )
        status, text = _http_get(port, f"/api/plan/{PLAN_ID}/status")
        # The leftover serves another data plane, and that plane is
        # **empty** — so it cannot even search it, and it must not claim
        # the plan is gone (2026-10-25, repair-r1-34).  "I cannot see
        # whether the plan exists" (503) is the answer that is true;
        # "Plan not found" (404) is the one that sends the caller
        # hunting for a plan that is still there.
        assert status == 503, (
            f"the leftover serves another data plane, so it must not see "
            f"this plan; its own tree is empty, so it cannot even search "
            f"it — got {status}: {text!r}"
        )
        body = json.loads(text)
        assert body.get("detail") == "plans dir unavailable", (
            f"the answer must say the tree could not be searched; got {body!r}"
        )

        _write_ledger(
            plan_dir, project_dir, port, leftover.pid,
            os.getpgid(leftover.pid),
            _start_cmd(port, tmp_path / "other-plans", tmp_path / "other-state.db"),
        )

        decl = ServiceDeclaration(
            name="pdt-backend", port=port,
            start_cmd=_start_cmd(port, plans_dir, tmp_path / "state.db"),
            cwd=".", ready_timeout_seconds=180,
            log_path=str(tmp_path / "reclaimed.log"),
        )
        runtimes = service_manager.ensure_services(
            project_dir, {"pdt-backend": decl},
            plan_dir=plan_dir, plan_id=PLAN_ID,
        )
        runtime = runtimes.runtimes["pdt-backend"]

        assert runtime.reclaimed is True, (
            f"a leftover of our own plan must be reclaimed, not worked "
            f"around: {runtime.detail!r}"
        )
        assert runtime.port == port, (
            f"the round must keep the declared port; got {runtime.port}"
        )
        assert runtime.ready is True, (
            f"the reclaimed instance did not come up: {runtime.detail!r}"
        )

        status, text = _http_get(port, f"/api/plan/{PLAN_ID}/status")
        assert status == 200, (
            f"the plan status endpoint on the declared port must answer; "
            f"got {status}: {text!r}"
        )
        body = json.loads(text)
        assert body["phase"] == "waiting_for_quota", f"unexpected body: {body!r}"
        execution = body.get("execution")
        assert isinstance(execution, dict), f"unexpected execution: {execution!r}"
        assert execution["execution_status"] == "waiting_for_quota", (
            f"unexpected execution status: {execution!r}"
        )
        counts = body.get("tasks")
        assert isinstance(counts, dict), f"unexpected counts: {counts!r}"
        assert counts["total"] == 3, f"unexpected counts: {counts!r}"
        assert counts["completed"] == 1, f"unexpected counts: {counts!r}"
        assert counts["pending"] == 2, (
            f"the two downstream tasks must still be pending: {counts!r}"
        )
        assert counts["failed"] == 0, (
            f"a quota wait must not fail anything: {counts!r}"
        )
    finally:
        if runtime is not None:
            service_manager.stop_service(runtime, None, grace_seconds=10.0)
        try:
            os.killpg(os.getpgid(leftover.pid), 15)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            leftover.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(leftover.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
