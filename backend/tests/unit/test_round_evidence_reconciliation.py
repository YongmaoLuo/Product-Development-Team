"""The round's own evidence, checked against itself — and the plan status
that has to survive the process that started the service.

Two contracts, one file
-----------------------

**1. The plan-status contract (the acceptance criterion).**

    *the plan-level status must give ``phase=waiting_for_quota``, the
    execution section's ``execution_status`` the same value,
    ``execution_in_flight=false``, and the counts
    ``total=3 / in_progress=1 / pending=2 / failed=0``*

The counts are the part that carries the criterion.  ``in_progress=1``
with ``failed=0`` is what a plan looks like when the executor went away
while a task was still running — the shape the watchdog used to rewrite
as a crash (``executor_exited_with_unfinished_tasks``): the pause became
a ``failed`` row and the interrupted task was reaped back to ``pending``.
A quota wait is a pause, and a pause consumes nobody's turn.

The endpoint has to answer that **after the process that started the
service is gone**: the row's pid belongs to a process that has already
exited, and the in-memory registry still holds the record that process
left behind.  The answer must come from the durable rows, because that
is all that is left.

**2. The round-evidence contract.**

The 2026-10-26 round held two facts that cannot both be true:

  * twelve ``api_test`` points were refused the connection on port
    64469 at 02:25:01, every one of them with the transport's own
    ``URLError [Errno 61]``;
  * the ``ui_validation`` point verified **the same address** from
    02:25:01 onwards for twelve minutes without a hitch, and the
    round's own probe answered 200 at 02:37:49.

A refused connection and a served one cannot both be true of one address
at one moment.  The framework took both at face value, so the twelve
refusals were written down as twelve product defects.

So the round now checks its own evidence before the report is written: a
connection-class failure whose address was **served** somewhere in the
same round is not a verdict about the product.  It is marked
``invalid_env`` and gets one more look — the service is re-ensured and
the point is run again by the round's own dispatcher, under the same
criteria.  Only what the second look says may land: a failure that
survives it is a failure; a failure that does not is what it always was
— an environment that was not there.

The conflict is written down as it is found
(``round_evidence_conflict{url, failed_vps, succeeded_vp}``), because a
contradiction nobody can find afterwards is a contradiction that will be
found again.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# The plan-status contract
# ---------------------------------------------------------------------------

#: The plan id the acceptance criterion is about.
PLAN_ID = "20261026-vp-quota-parked"

#: The three tasks of the parked plan.
TASK_IDS = ("VP-001", "VP-002", "VP-003")

#: The task that was still running when the run parked for capacity.
IN_PROGRESS_TASK_ID = "VP-001"

#: The marker the executor writes when a run parks because every provider
#: had no capacity left (``agent._stop_reason_for_quota_wait``).
CAPACITY_MARKER = {
    "providers": ["MiniMax"],
    "needs_manual_topup": False,
    "waited_sec": 1800.0,
    "at": "2026-10-26T02:20:00",
}


def _free_port() -> int:
    """A port nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_SERVICE_SOURCE = """
import http.server
import socketserver
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *args):
        pass


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", int(sys.argv[1])), Handler) as httpd:
    httpd.serve_forever()
"""


def _start_a_service_and_let_its_process_exit(port: int) -> int:
    """Start a real service, then let the process that started it exit.

    The pid that comes back belongs to a process that has **already
    exited** — which is the shape a plan's execution row carries once the
    process that started the service the run was talking to is gone.

    The point of starting a *real* service is that the row's pid is not a
    number the test made up: it is a number the operating system handed
    out to a process that really ran, and the test proves the process is
    gone before it uses its pid as evidence.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVICE_SOURCE, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 0.5):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError(
                f"the service never started listening on port {port}"
            )
        proc.terminate()
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:  # pragma: no cover — defensive
            proc.kill()
            proc.wait(timeout=20)
    pid = proc.pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    raise AssertionError(
        f"pid {pid} came back to life — it is not a dead pid, so the test "
        f"would be proving something about a live process"
    )


def _write_tasks_file(plan_dir: Path) -> None:
    """The plan's static task list, with the capacity marker on top.

    The marker is what makes the plan's own file say *why* the run
    stopped: the ``stop_reason`` names the verdict and the
    ``reason_detail`` names the capacity fact behind it.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": tid, "title": f"task {tid}", "description": f"do {tid}"}
        for tid in TASK_IDS
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": tasks,
                "stop_reason": "waiting_for_quota",
                "reason_detail": json.dumps(CAPACITY_MARKER),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _seed_parked_plan(*, project_dir: Path, dead_pid: int) -> None:
    """Write the routing row, the execution row and the ``plan_tasks`` rows.

    Every row goes through the repository production writes through, so
    the endpoint reads back exactly the shapes production produces.

      * ``plan_routing.current_phase`` = ``waiting_for_quota`` — the
        workflow phase the status object reports as ``phase``.
      * ``plan_execution.exec_status`` = ``waiting_for_quota`` — the
        execution record's own verdict.  Its ``exec_pid`` belongs to a
        process that has already exited: the row says "parked", and
        nothing in it says "failed".
      * ``plan_tasks`` — three rows.  One is still ``in_progress`` (the
        task the run was working on when it parked); the other two are
        ``pending`` and must stay that way.
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

    import server

    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "waiting_for_quota")

        ExecutionRepository(conn).update_fields(
            PLAN_ID,
            create_if_missing=True,
            exec_status="waiting_for_quota",
            stop_reason="waiting_for_quota",
            exec_pid=dead_pid,
            project_dir=str(project_dir),
            started_at="2026-10-26T02:20:00",
        )

        task_repo = PlanTaskRepository(conn)
        for tid in TASK_IDS:
            task_repo.add_task(
                PLAN_ID,
                {"id": tid, "title": f"task {tid}", "description": f"do {tid}"},
            )
        task_repo.update_task(
            PLAN_ID, IN_PROGRESS_TASK_ID, {"status": "in_progress"},
        )
    finally:
        conn.close()


def test_the_plan_status_endpoint_survives_the_exit_of_the_process_that_started_the_service(
    tmp_path: Path,
) -> None:
    """A parked plan still answers the truth after its process is gone.

    The plan is parked on capacity: its routing row says
    ``waiting_for_quota`` and its execution row says the same thing.  The
    pid the row carries belongs to a process that has already exited — it
    is the process that started the service the run was talking to, and
    it is not there any more.

    The endpoint still has to answer the operator's question, and it has
    to answer it from what is left:

      * ``phase`` is ``waiting_for_quota``;
      * the execution section's ``execution_status`` is the same value —
        a wait is a pause, not a crash, so it must never read as
        ``failed``;
      * ``execution_in_flight`` is ``false`` — a parked run is not a run;
      * the counts are ``total=3``, ``in_progress=1``, ``pending=2``,
        ``failed=0`` — the interrupted task is still work in flight, not
        a failure the executor's exit produced.
    """
    import server
    from fastapi.testclient import TestClient

    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / PLAN_ID
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # The process that started the service — a real one, which is then
    # gone by the time the endpoint is asked.
    dead_pid = _start_a_service_and_let_its_process_exit(_free_port())

    _write_tasks_file(plan_dir)
    _seed_parked_plan(project_dir=project_dir, dead_pid=dead_pid)

    # This instance never ran the plan: the process that did is gone, and
    # the record it left behind is all that is left of it.
    server._execution_state.clear()
    client = TestClient(server.app)

    resp = client.get(f"/api/plan/{PLAN_ID}/status")

    assert resp.status_code == 200, (
        f"a parked plan must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["phase"] == "waiting_for_quota", (
        f"the status object reported phase {body['phase']!r}; the plan is "
        f"parked for capacity, and a parked plan that reads as something "
        f"else is the plan an operator has to guess about"
    )

    execution = body.get("execution")
    assert isinstance(execution, dict), (
        f"the status object must carry the execution section; got "
        f"{execution!r}"
    )
    assert execution["execution_status"] == "waiting_for_quota", (
        f"the execution section reported "
        f"{execution['execution_status']!r}; the execution row says the "
        f"plan is parked — not running, and certainly not failed"
    )
    assert execution["execution_status"] != "failed", (
        "a quota wait must not read as a failed execution: "
        f"{execution['execution_status']!r}"
    )

    assert body["execution_in_flight"] is False, (
        f"a parked run is not a run in flight; got "
        f"{body['execution_in_flight']!r}"
    )

    counts = body.get("tasks")
    assert isinstance(counts, dict), (
        f"the status object must carry the task counts; got {counts!r}"
    )
    assert counts.get("total") == 3, f"unexpected counts: {counts!r}"
    assert counts.get("in_progress") == 1, (
        f"the task the run was working on when it parked must still read "
        f"as in progress: {counts!r}"
    )
    assert counts.get("pending") == 2, (
        f"the two tasks downstream of it must still be pending: {counts!r}"
    )
    assert counts.get("failed") == 0, (
        f"a quota wait must not fail anything: {counts!r}"
    )


# ---------------------------------------------------------------------------
# The round-evidence contract
# ---------------------------------------------------------------------------

#: The address the production round was refused on, and served on.
LIVE_URL = "http://127.0.0.1:64469/api/plans"

#: The refusal the transport left behind.
REFUSED_EVIDENCE = {
    "vp_id": "VP-001",
    "request": {"method": "GET", "url": LIVE_URL},
    "error": "URLError: <urlopen error [Errno 61] Connection refused>",
    "error_kind": "service_unreachable",
    "reason_code": "service_unreachable",
    "errno": 61,
    "url": LIVE_URL,
}


def _refused_result(vp_id: str = "VP-001") -> dict:
    """The executor's verdict for a point whose connection was refused."""
    evidence = dict(REFUSED_EVIDENCE)
    evidence["vp_id"] = vp_id
    return {
        "id": vp_id,
        "status": "BLOCKED",
        "actual_result": (
            f"service_unreachable: the request to {LIVE_URL} never reached "
            f"the service"
        ),
        "evidence": json.dumps(evidence, ensure_ascii=False),
    }


def _served_result(vp_id: str = "VP-024") -> dict:
    """The executor's verdict for a point that was served on that address."""
    return {
        "id": vp_id,
        "status": "PASSED",
        "actual_result": "the page loaded and every assertion held",
        "evidence": json.dumps({
            "vp_id": vp_id,
            "status_code": 200,
            "request": {"method": "GET", "url": LIVE_URL},
        }, ensure_ascii=False),
    }


def _ui_validation_result(vp_id: str = "VP-024") -> dict:
    """A ``ui_validation`` point's verdict — no transport of its own.

    Its evidence carries no ``status_code``: the sub-agent drove a
    browser, and the address it drove is on the verification point.
    """
    return {
        "id": vp_id,
        "status": "PASSED",
        "actual_result": "the page loaded and every assertion held",
        "evidence": json.dumps({"vp_id": vp_id}, ensure_ascii=False),
    }


def _make_agent(tmp_path: Path):
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(plan_dir=plan_dir, project_dir=project_dir)


def _plan_data(*vps: dict) -> dict:
    return {"verification_points": list(vps)}


def _record_events(agent) -> List[tuple]:
    """Record the round's events instead of writing them to disk."""
    events: List[tuple] = []

    def _record(vp_id: str, event_type: str, data: dict) -> None:
        events.append((vp_id, event_type, data))

    agent.persistence.write_verification_point_log = _record
    return events


def _conflicted_round(tmp_path: Path):
    """A round whose own evidence contradicts itself.

    One point was refused the connection on ``LIVE_URL``; another point
    was served on the same address in the same round.
    """
    agent = _make_agent(tmp_path)
    plan_data = _plan_data(
        {
            "id": "VP-001",
            "verification_method": "api_test",
            "request": {"method": "GET", "url": LIVE_URL},
            "assertions": [{"status": 200}],
        },
        {
            "id": "VP-024",
            "verification_method": "ui_validation",
            "target_url": LIVE_URL,
        },
    )
    execution_results = {
        "verification_points": plan_data["verification_points"],
        "execution_results": [_refused_result(), _ui_validation_result()],
    }
    agent._executor_vps_by_id = {
        vp["id"]: vp for vp in plan_data["verification_points"]
    }
    return agent, plan_data, execution_results


def _result_by_id(execution_results: dict, vp_id: str) -> dict:
    for result in execution_results.get("execution_results", []):
        if str(result.get("id")) == vp_id:
            return result
    raise AssertionError(
        f"the round has no result for {vp_id}: "
        f"{execution_results.get('execution_results')!r}"
    )


def test_a_refusal_contradicted_by_a_success_is_not_a_product_failure(
    tmp_path: Path,
) -> None:
    """The two facts cannot both be true, so the refusal is not a verdict.

    The ``ui_validation`` point was served on the very address the
    ``api_test`` point was refused on, in the same round.  One of the two
    is wrong, and the round cannot tell which — so the refusal may not be
    written down as a product defect until the point has been run again.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)

    conflicts = agent._detect_round_evidence_conflicts(
        plan_data, execution_results,
    )

    assert len(conflicts) == 1, (
        f"the round's own evidence contradicts itself exactly once; got "
        f"{conflicts!r}"
    )
    conflict = conflicts[0]
    assert conflict["failed_vps"] == ["VP-001"], conflict
    assert conflict["succeeded_vp"] == "VP-024", conflict
    assert conflict["url"] == LIVE_URL, conflict


def test_the_conflict_is_written_down_with_both_sides(tmp_path: Path) -> None:
    """A contradiction nobody can find afterwards will be found again.

    The event names the address, the points that failed on it and the
    point that was served on it — the three facts a reader needs to
    check the round's own claim.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    events = _record_events(agent)

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    conflicts = [
        data for _, event_type, data in events
        if event_type == "round_evidence_conflict"
    ]
    assert len(conflicts) == 1, (
        f"the round must write exactly one conflict event; got {events!r}"
    )
    conflict = conflicts[0]
    assert conflict["url"] == LIVE_URL, conflict
    assert conflict["failed_vps"] == ["VP-001"], conflict
    assert conflict["succeeded_vp"] == "VP-024", conflict


def test_the_contradicted_point_is_marked_invalid_env(tmp_path: Path) -> None:
    """``invalid_env`` says one thing: this is not about the product.

    The evidence is kept — the refusal, the address, the point that was
    served on it — because the point of the marker is that a reader can
    check it.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    _record_events(agent)

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    entry = _result_by_id(execution_results, "VP-001")
    assert entry["status"] == "invalid_env", (
        f"a refusal the round's own evidence contradicts must not stay a "
        f"verdict; got {entry['status']!r}"
    )
    evidence = json.loads(entry["evidence"])
    assert evidence["reason_code"] == "invalid_env", evidence
    assert evidence["invalid_env"] is True, evidence
    assert evidence["evidence_conflict"]["succeeded_vp"] == "VP-024", evidence
    assert LIVE_URL in str(entry["actual_result"]), entry


def test_the_contradicted_point_is_run_again(tmp_path: Path) -> None:
    """One more look — through the round's own dispatcher.

    The re-run is graded by the same rules as the first attempt: the
    same dispatcher, the same gates, the same evidence.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    _record_events(agent)

    rerun_calls: List[str] = []

    async def _fake_runner(vp):
        rerun_calls.append(str(vp.get("id")))
        return {
            "status": "PASSED",
            "reasons": ["all assertions passed"],
            "evidence": {"status_code": 200},
        }

    agent._vp_dispatch_runner = _fake_runner

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    assert rerun_calls == ["VP-001"], (
        f"the contradicted point must be run again; got {rerun_calls!r}"
    )
    entry = _result_by_id(execution_results, "VP-001")
    assert entry["status"] == "PASSED", (
        f"the re-run's verdict must replace the contradicted one; got "
        f"{entry['status']!r}"
    )


def test_a_failure_that_survives_the_re_run_may_land_as_failed(
    tmp_path: Path,
) -> None:
    """The second look decides — and a failure that survives it is real.

    The first refusal was not trusted because the round's own evidence
    contradicted it.  The re-run is against a service that is serving, so
    a failure now is a failure of the product and may be written down as
    one.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    _record_events(agent)

    async def _fake_runner(vp):
        return {
            "status": "FAILED",
            "reasons": ["assertion [status] failed: expected 200, got 500"],
            "evidence": {
                "status_code": 500,
                "assertions_failed": ["[status] expected 200, got 500"],
            },
        }

    agent._vp_dispatch_runner = _fake_runner

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    entry = _result_by_id(execution_results, "VP-001")
    assert entry["status"] == "FAILED", (
        f"a failure that survives the re-run is a failure; got "
        f"{entry['status']!r}"
    )


def test_a_service_that_is_still_down_keeps_the_point_out_of_the_failures(
    tmp_path: Path,
) -> None:
    """The environment is still not there — so it is still not a failure.

    The point stays ``invalid_env`` and the round says it was blocked by
    the environment: the upper layer re-runs the round instead of
    repairing a product nobody managed to ask.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    _record_events(agent)
    agent._ensure_and_probe_blocked_services = lambda plan, ids: False

    rerun_calls: List[str] = []

    async def _fake_runner(vp):
        rerun_calls.append(str(vp.get("id")))
        return {"status": "PASSED", "evidence": {"status_code": 200}}

    agent._vp_dispatch_runner = _fake_runner

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    assert rerun_calls == [], (
        f"a service that is still unreachable must not be asked again; got "
        f"{rerun_calls!r}"
    )
    entry = _result_by_id(execution_results, "VP-001")
    assert entry["status"] == "invalid_env", (
        f"the contradicted point must not become a failure; got "
        f"{entry['status']!r}"
    )
    assert agent._environment_blocked is True, (
        "the round must be marked environment blocked when the service is "
        "still unreachable"
    )


def test_a_refusal_without_a_success_is_not_a_conflict(tmp_path: Path) -> None:
    """The negative control: nothing served that address, so nothing clashes.

    A service that was down for the whole round produces twelve
    refusals and **no** contradiction — the round is environment
    blocked, which is a different (and already handled) answer.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    execution_results["execution_results"] = [_refused_result()]

    conflicts = agent._detect_round_evidence_conflicts(
        plan_data, execution_results,
    )

    assert conflicts == [], (
        f"a refusal nobody contradicts is not a conflict; got {conflicts!r}"
    )


def test_a_point_that_was_served_is_never_touched(tmp_path: Path) -> None:
    """The negative control: the witness is not itself a candidate.

    The point that was served has no connection failure of its own, so
    the reconciliation has no business rewriting it.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    _record_events(agent)

    async def _fake_runner(vp):
        return {"status": "PASSED", "evidence": {"status_code": 200}}

    agent._vp_dispatch_runner = _fake_runner

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    entry = _result_by_id(execution_results, "VP-024")
    assert entry["status"] == "PASSED", (
        f"the point that was served must keep its verdict; got "
        f"{entry['status']!r}"
    )


def test_a_conflict_is_reconciled_at_most_once(tmp_path: Path) -> None:
    """One look per round: a second one buys nothing but cost."""
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    _record_events(agent)

    rerun_calls: List[str] = []

    async def _fake_runner(vp):
        rerun_calls.append(str(vp.get("id")))
        return {"status": "PASSED", "evidence": {"status_code": 200}}

    agent._vp_dispatch_runner = _fake_runner

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)
    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    assert rerun_calls == ["VP-001"], (
        f"the reconciliation happens at most once per round; got "
        f"{rerun_calls!r}"
    )


def test_the_rounds_own_probe_is_a_witness_too(tmp_path: Path) -> None:
    """The round's own probe is a witness, like any other answer.

    The 02:37:49 probe answered 200 on the very address the twelve
    points were refused on.  A probe that does not say which address it
    asked cannot contradict anything, so the probe's record carries the
    URL it was sent to.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    # Only the probe answers on that address in this round: the
    # ``ui_validation`` point is not here to be a witness.
    execution_results["execution_results"] = [_refused_result()]
    agent._service_self_check_records = {
        ("api", 64469): {
            "asked": True,
            "status_code": 200,
            "url": "http://127.0.0.1:64469/health",
            "error": "",
        },
    }

    conflicts = agent._detect_round_evidence_conflicts(
        plan_data, execution_results,
    )

    assert len(conflicts) == 1, (
        f"the round's own probe contradicts the refusal; got {conflicts!r}"
    )
    conflict = conflicts[0]
    assert conflict["failed_vps"] == ["VP-001"], conflict
    assert conflict["succeeded_vp"] == "service:api", conflict
    assert conflict["origin"] == "http://127.0.0.1:64469", conflict


def test_a_point_that_got_an_answer_is_not_a_refused_one(tmp_path: Path) -> None:
    """The negative control: a response is not a refusal.

    The two halves have to agree.  A result that carries a
    ``status_code`` was answered — the transport reached the service and
    the assertions decided — so it is not a candidate for the
    reconciliation, whatever else its evidence says.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    served = _served_result()
    served["status"] = "FAILED"
    served["actual_result"] = "connection refused on the second attempt"
    execution_results["execution_results"] = [served]

    conflicts = agent._detect_round_evidence_conflicts(
        plan_data, execution_results,
    )

    assert conflicts == [], (
        f"a point that was answered is not a refused one; got {conflicts!r}"
    )


def test_the_witness_survives_the_round_level_rewrite(tmp_path: Path) -> None:
    """A point that really passed is still a point that passed.

    This is the production shape.  The round was blocked as a whole —
    twelve points were refused — so every one of its results was
    rewritten to BLOCKED, and the one point that was served on the same
    address was rewritten with them.  The rewrite keeps the verdict each
    result really got in the evidence (``original_status``), and that
    is what the witness is read from: a point that was served can still
    say so, no matter what the round-level rule did to its status.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    rewritten = _ui_validation_result()
    rewritten["status"] = "BLOCKED"
    rewritten["evidence"] = json.dumps({
        "vp_id": "VP-024",
        "original_status": "PASSED",
        "reason_code": "environment_blocked",
        "environment_blocked": True,
    }, ensure_ascii=False)
    execution_results["execution_results"] = [
        _refused_result(), rewritten,
    ]

    conflicts = agent._detect_round_evidence_conflicts(
        plan_data, execution_results,
    )

    assert len(conflicts) == 1, (
        f"the round-level rewrite must not hide the witness; got "
        f"{conflicts!r}"
    )
    assert conflicts[0]["succeeded_vp"] == "VP-024", conflicts[0]


def test_a_second_refusal_stays_out_of_the_failures(tmp_path: Path) -> None:
    """A second refusal does not make the first one a product defect.

    The point is re-run after the service is re-ensured.  If the
    service is still not there, the re-run's verdict is a refusal too —
    and a refusal may not land as ``FAILED``, however many times it is
    repeated.
    """
    agent, plan_data, execution_results = _conflicted_round(tmp_path)
    _record_events(agent)

    async def _fake_runner(vp):
        return {
            "status": "FAILED",
            "reasons": ["service_unreachable — the request never reached "
                        "the service"],
            "evidence": {
                "error": "URLError: <urlopen error [Errno 61] Connection "
                         "refused>",
                "url": LIVE_URL,
            },
        }

    agent._vp_dispatch_runner = _fake_runner

    agent._reconcile_round_evidence_conflicts(plan_data, execution_results)

    entry = _result_by_id(execution_results, "VP-001")
    assert entry["status"] == "invalid_env", (
        f"a re-run that is still a refusal must not land as a failure; got "
        f"{entry['status']!r}"
    )


def test_an_invalid_env_entry_is_not_counted_as_a_failure(tmp_path: Path) -> None:
    """The accounting: the report keeps the entry and stops counting it.

    The judgment LLM is free to write a point down as ``FAILED``; the
    executor's verdict is the authority.  A point the round's own
    evidence contradicts is neither a failure nor a pass — it is kept in
    the report, annotated, and left out of the statistics.
    """
    from verification_agent import INVALID_ENV

    agent = _make_agent(tmp_path)
    execution_results = {
        "verification_points": [],
        "execution_results": [{
            "id": "VP-001",
            "status": INVALID_ENV,
            "actual_result": "invalid_env: the round's own evidence "
                             "contradicts this failure",
            "evidence": json.dumps({
                "vp_id": "VP-001",
                "reason_code": INVALID_ENV,
                "invalid_env": True,
            }, ensure_ascii=False),
        }],
    }
    report_data = {
        "overall_status": "FAILED",
        "summary": "the judgment LLM's own words",
        "verification_results": [{
            "id": "VP-001",
            "status": "FAILED",
            "actual_result": "connection refused",
        }],
        "requirement_deviations": [],
    }

    updated = agent._exclude_environment_blocked_from_failures(
        report_data, execution_results,
    )

    entry = updated["verification_results"][0]
    assert entry["status"] == INVALID_ENV, (
        f"a point the round's own evidence contradicts must not be "
        f"reported as FAILED; got {entry['status']!r}"
    )
    assert updated["overall_status"] != "FAILED", (
        f"one contradicted refusal must not turn the product verdict red; "
        f"got {updated['overall_status']!r}"
    )
