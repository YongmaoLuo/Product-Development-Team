"""VP-003's closure on a **placeholder** URL, after the service was killed.

The gap this pins
-----------------
The plan writes its api_test verification points against the service
**placeholder**, not against a literal address::

    "url": "{{svc.api.url}}/api/system/active"

VP-003 is the system-overview contract: ``GET /api/system/active`` (with
the ``X-PDT-Request`` guard header) answers 200, a JSON content-type and
a body carrying five fields — ``execution_running``,
``verification_running``, ``total_active``, ``details`` and
``paused_executions``.

In the round that failed, the service on 57440 was SIGTERM'd at
06:34:58 (pid 263) and the api_test request came back ``URLError [Errno
61] Connection refused``: no status code, no body, and **not one** of the
five fields observed.  The round's surviving evidence for this endpoint
was VP-011's ``GET /health`` and ``GET /api/plans`` — neighbouring
endpoints that say nothing about the system overview, whose 200s cannot
stand in for this contract.  The endpoint was blank for the whole round.

Two framework defects had to be fixed for this criterion to be
observable at all, and both are about the **placeholder**:

  1. ``_vp_target_port`` could not parse a port out of a placeholder, so
     ``_relaunch_service_for_vp`` gave up before even probing the port —
     a service killed between the preflight and the request stayed dead
     and the request came back "connection refused".
  2. even with the relaunch fixed, the *request* still carried the raw
     placeholder: ``{{svc.api.url}}`` is not a URL, so the request could
     never have reached the endpoint that had just been brought back up.

What this test does
-------------------
Against the **real** backend server (the same ``python server.py`` the
plan's declaration starts):

  1. the service comes up on a declared port and answers
     ``/api/system/active`` with all five fields — so the criterion is
     *observable* before anything is broken;
  2. the service is killed with SIGTERM, exactly as the cross-plan sweep
     killed it, and the port is confirmed free;
  3. the api_test VP — written the way the plan writes it, with the
     ``{{svc.api.url}}`` placeholder — runs anyway, and must be graded
     **PASSED**: the framework's own relaunch puts the service back **on
     the same port** before the request, and the request must be made
     against the *resolved* URL;
  4. the answer is read out of the audit artifact the runner wrote, and
     the contract is checked field by field: 200, a JSON content-type,
     and the five fields **with the types the endpoint promises** — the
     counts are integers, ``details`` is a list of entries with the
     documented keys, and the counters agree with the lists they count.

Point 4 is the part the earlier round never had, and it is deliberately
not "the body mentions these five names".  A body that mentions
``execution_running`` proves nothing: ``null`` is a field too, and so is
a string where a count belongs.  So the endpoint is put into a state
with something genuinely in flight (a recovered verification round) and
something genuinely paused (a pause checkpoint on disk), and the test
insists on the numbers those two facts imply: ``verification_running ==
1``, ``total_active == 1``, one ``details`` entry with the documented
keys, one ``paused_executions`` entry with the documented keys.  A
server answering ``{"execution_running": 0, "verification_running": 0,
"total_active": 0, "details": [], "paused_executions": []}`` — five
fields, all hollow — cannot satisfy this criterion, which is the whole
point: the endpoint is the system overview, so an overview that reports
nothing is a broken overview.

The seeded state is real state, not a mock: the verification row goes
into the SQLite state database the server reads at boot (through the
repository, not raw SQL), and the pause checkpoint goes through
:func:`pause_state.write_pause_checkpoint`.  The server recovers both on
startup, exactly as it does for a plan that was interrupted.

The declaration is copied from the plan's own ``services`` block rather
than invented here, including the ``unset PDT_SECRET_FD_*`` step: those
environment variables name descriptors belonging to the *parent* process,
and a child that inherits the names without the descriptors reads
whatever now occupies that number.  In this repository that took uvicorn's
listening socket down with it (``OSError: [Errno 9] Bad file descriptor``
inside the event loop) — the plan's declaration drops the names for
exactly that reason.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import probe_port  # noqa: E402
from service_manager import ServiceRuntimeMap, ensure_services  # noqa: E402


#: The plan whose verification round the server recovers at boot.  Its
#: name is deliberately neutral: a real plan id is a private artefact
#: and this test needs nothing from it but a directory.
SEEDED_PLAN_ID = "vp003-placeholder-seed"

#: The plan carrying a pause checkpoint, so ``paused_executions`` has
#: something real to report.
PAUSED_PLAN_ID = "vp003-placeholder-paused"

#: The round number the recovered verification reports.  Two, so an
#: implementation that hard-codes "round 0" cannot pass by accident.
SEEDED_ROUND = 2

#: The five fields the criterion names.  A response missing any one of
#: them has broken the contract, whatever else it carries.
CONTRACT_FIELDS = (
    "execution_running",
    "verification_running",
    "total_active",
    "details",
    "paused_executions",
)

#: The fields the criterion calls **counts**: the endpoint's own
#: counters.  They must be integers — a string where a count belongs is
#: a broken contract even though the field name is present.
COUNT_FIELDS = ("execution_running", "verification_running", "total_active")

#: The fields the criterion calls an iterable structure: a list of
#: entries, each with the keys the endpoint documents.
LIST_FIELDS = ("details", "paused_executions")

#: The keys a ``details`` entry carries, per the endpoint's contract.
DETAIL_KEYS = frozenset({
    "plan_id", "type", "status", "round", "started_at", "updated_at",
})

#: The keys a ``paused_executions`` entry carries, per the endpoint's
#: contract.
PAUSED_KEYS = frozenset({
    "plan_id", "kind", "paused_at", "interrupted_task_count",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_state_db(db_path: Path, plans_dir: Path) -> None:
    """Give the server a state database with one verification in flight.

    The row is written through :class:`VerificationRepository` — the same
    path the framework writes it on — and the plan gets a directory under
    the plans tree, because the server's boot-time recovery walks the
    plan *directories* and asks the database about each one.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(
            SEEDED_PLAN_ID,
            "running",
            round=SEEDED_ROUND,
            max_rounds=3,
            runtime_state={"round": SEEDED_ROUND},
            started_at="2026-10-25T00:00:00Z",
        )
        conn.commit()
    finally:
        conn.close()

    (plans_dir / SEEDED_PLAN_ID).mkdir(parents=True, exist_ok=True)


def _seed_pause_checkpoint(plans_dir: Path) -> None:
    """Park one plan with a pause checkpoint on disk.

    ``paused_executions`` is read from the plan directories rather than
    from memory — it is the half of the overview that survives a
    restart — so the checkpoint is what makes that field real.
    """
    from pause_state import write_pause_checkpoint

    write_pause_checkpoint(
        plans_dir / PAUSED_PLAN_ID,
        kind="execution",
        project_dir=None,
        reason="user_requested",
        interrupted_tasks=[{"task_id": "T-001", "status": "in_progress"}],
        unfinished_tasks=[],
    )


def _start_cmd(port: int, plans_dir: Path, db_path: Path) -> str:
    """The plan's own ``start_cmd`` shape, pointed at this test's paths.

    Copied from the plan's ``services`` block (service ``api``) so the
    thing under test is the same thing the round started: the real
    backend server, told which port to bind, which plans tree to use and
    — the part that matters here — **which SQLite database is its data
    plane**.
    """
    return (
        # The parent's secret descriptors must not leak into the child:
        # the names survive `exec`, the descriptors do not.
        "for v in $(env | grep '^PDT_SECRET_FD_' | cut -d= -f1); do "
        'unset "$v"; done; '
        # The provider mode is pinned here rather than trusted from the
        # machine the suite happens to run on: the child inherits this
        # process's environment, and the hermetic env pins
        # ``PDT_PROVIDER_MODE=cc-switch`` for the pytest process.  CC
        # Switch is a plugin of one operator's machine — on a clean
        # runner nothing is listening on its port, so the child's
        # startup aborts ("Application startup failed") and the service
        # never comes up.  The service under test must not depend on
        # whether the machine it runs on happens to have CC Switch.
        "export PDT_PROVIDER_MODE=inherit; "
        f"export PDT_PORT={port}; "
        f"export PDT_PLANS_DIR={plans_dir}; "
        f"export PDT_STATE_DB_PATH={db_path}; "
        "export PYTHONPATH=$PWD/..; "
        f"mkdir -p {plans_dir}; "
        "source .venv/bin/activate; exec python server.py"
    )


def _get_json(url: str, *, guarded: bool = True) -> Tuple[int, Dict[str, Any]]:
    """GET ``url`` and return ``(status, body)``.

    ``guarded=False`` deliberately omits the ``X-PDT-Request`` header, so
    a caller can see the request guard refuse the call.
    """
    headers = {"Accept": "application/json"}
    if guarded:
        headers["X-PDT-Request"] = "1"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}


def _kill_service(pid: Optional[int]) -> None:
    """SIGTERM the service's process group, the way the sweep did.

    The service is started detached (``start_new_session=True``), so it
    is a process-group leader and the group is the unit that can be
    signalled.
    """
    if not pid:
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _wait_until_port_free(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not probe_port(port).listening:
            return
        time.sleep(0.2)
    raise AssertionError(f"port {port} is still held {timeout}s after SIGTERM")


def _vp_003() -> Dict[str, Any]:
    """VP-003 as the plan declares it — the criterion is not ours to move.

    The URL is the **placeholder** the plan writes, not a literal
    address: that is the whole point of this test, because the
    placeholder is what made the endpoint unobservable in the first
    place.  The assertions are the plan's own, verbatim — 200, a JSON
    content-type, and the five fields present.
    """
    return {
        "id": "VP-003",
        "title": "默认后端下系统状态接口的响应契约保持不变",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api.url}}/api/system/active",
            "headers": {
                "Accept": "application/json",
                # The request guard answers 403 without this header, so
                # the criterion names it: the VP must carry it.
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {
                "name": "响应为 JSON",
                "header": "content-type",
                "contains": "application/json",
            },
            {
                "name": "含 execution_running",
                "json_path": "$.execution_running", "exists": True,
            },
            {
                "name": "含 verification_running",
                "json_path": "$.verification_running", "exists": True,
            },
            {
                "name": "含 total_active",
                "json_path": "$.total_active", "exists": True,
            },
            {
                "name": "含 details",
                "json_path": "$.details", "exists": True,
            },
            {
                "name": "含 paused_executions",
                "json_path": "$.paused_executions", "exists": True,
            },
        ],
    }


def _assert_contract_types(payload: Dict[str, Any]) -> List[int]:
    """Check the five fields' **types**, and return the four counts.

    The criterion is not "the body mentions these five names" — ``null``
    is a field too, and so is a string where a count belongs.  So:

      * the three counters are integers (and not booleans, which are
        integers in Python and would slip through a naive check);
      * ``details`` and ``paused_executions`` are lists — the iterable
        structures the endpoint promises — and their lengths are the
        fourth number this overview reports.

    The counts are returned so the caller can hold them against the
    lists they count.
    """
    for field in COUNT_FIELDS:
        value = payload[field]
        assert isinstance(value, int), (field, value, type(value).__name__)
        assert not isinstance(value, bool), (field, value)

    for field in LIST_FIELDS:
        value = payload[field]
        assert isinstance(value, list), (field, value, type(value).__name__)

    return [
        payload["execution_running"],
        payload["verification_running"],
        payload["total_active"],
        len(payload["paused_executions"]),
    ]


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_system_active_contract_after_placeholder_relaunch(tmp_path, monkeypatch):
    """``/api/system/active`` must really be asserted on the relaunched service."""
    from verification_agent import VerificationAgent

    work = tmp_path / "pdt-verify-claude"
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True)
    db_path = work / "state.db"

    # The state the endpoint is supposed to report: one verification
    # round in flight (recovered from the database at boot) and one plan
    # parked with a pause checkpoint (read from disk).
    _seed_state_db(db_path, plans_dir)
    _seed_pause_checkpoint(plans_dir)

    # The plan directory lives under the plans tree the service is told
    # to use, exactly as in the round that failed — the boot sweep
    # scans that tree, and the ledger the relaunch writes lives in it.
    # The name is deliberately neutral: a real plan id is a private
    # artefact and this test needs nothing from it but a directory.
    plan_dir = plans_dir / "plan-under-repair"
    plan_dir.mkdir()

    port = _free_port()
    declaration = ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=_start_cmd(port, plans_dir, db_path),
        cwd="backend",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=60,
        log_path=str(work / "api_server.log"),
        reap_on_exit=True,
    )

    # --- 1. the service comes up and the criterion is observable ---------
    runtime_map = ensure_services(REPO_ROOT, {"api": declaration})
    runtime = runtime_map.runtimes["api"]
    assert runtime.ready, runtime.detail
    first_pid = runtime.pid
    assert first_pid

    agent = None
    try:
        status, before = _get_json(
            f"http://127.0.0.1:{port}/api/system/active"
        )
        assert status == 200, status
        assert set(CONTRACT_FIELDS) <= set(before), before
        assert before["verification_running"] == 1, before
        assert before["total_active"] == 1, before
        assert len(before["details"]) == 1, before
        assert len(before["paused_executions"]) == 1, before

        # The criterion names the guard header, so pin that it is
        # load-bearing: the same request without it is refused. A test
        # that only ever sent the header could not tell a guarded route
        # from an unguarded one.
        unguarded_status, _ = _get_json(
            f"http://127.0.0.1:{port}/api/system/active", guarded=False,
        )
        assert unguarded_status == 403, unguarded_status

        # --- 2. the service dies, the way the cross-plan sweep killed it
        _kill_service(first_pid)
        _wait_until_port_free(port)

        # The round's runtime map still points at the dead process: that
        # is exactly the state the api_test path used to meet.
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        agent._service_declarations = {"api": declaration}
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={"api": runtime}
        )

        events: list = []
        monkeypatch.setattr(
            agent,
            "_safe_log_freshness_event",
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            ),
        )

        # --- 3. the verification point runs, and must pass --------------
        verdict = agent._run_api_test_vp(_vp_003())

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-003"
        assert event_type == "service_relaunched_before_vp"
        assert data["port"] == port
        assert data["old_pid"] == first_pid
        assert data["new_pid"] not in (None, first_pid)

        # --- 4. the endpoint's own answer, read from the audit artifact
        artifact_path = (
            plan_dir / "vp_artifacts" / "VP-003" / "api_response.json"
        )
        assert artifact_path.exists(), "the runner wrote no audit artifact"
        artifact = json.loads(artifact_path.read_text())
        assert artifact["status_code"] == 200, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]

        # The request that produced the artifact must be the *resolved*
        # one: the raw placeholder is not a URL and could not have been
        # requested at all.
        request_url = artifact["request"]["url"]
        assert "{{" not in request_url, artifact["request"]
        assert request_url == (
            f"http://127.0.0.1:{port}/api/system/active"
        ), request_url

        # The evidence is this endpoint's, not a neighbour's: the request
        # named the overview, and the body is not the ``/health`` body
        # (``{"status": "ok"}``) that earlier rounds used as a stand-in.
        assert "/api/system/active" in request_url, request_url
        payload = json.loads(artifact["response_body"])
        assert "status" not in payload, payload

        # The content-type is part of the criterion, not a detail: the
        # consumer of this endpoint parses it as JSON.
        response_headers = artifact["response_headers"]
        assert "content-type" in response_headers, response_headers
        assert "application/json" in response_headers["content-type"], response_headers

        # ... and it was checked by the VP itself, not only by this
        # test's post-hoc reading of the artifact: the plan's own
        # content-type assertion is among the ones that passed.
        passed_lines = artifact["assertions_passed"]
        assert any(
            "content-type" in line for line in passed_lines
        ), passed_lines

        # The five fields, one by one — the criterion, not a summary of it.
        for field in CONTRACT_FIELDS:
            assert field in payload, (field, payload)

        # ... and their types: the counts are integers, the two lists are
        # lists. A body that merely mentions the five names cannot pass.
        counts = _assert_contract_types(payload)
        (
            execution_running,
            verification_running,
            total_active,
            paused_count,
        ) = counts
        assert execution_running == 0, payload
        assert verification_running == 1, payload
        assert total_active == 1, payload
        assert paused_count == 1, payload

        # The structure of the two lists, checked on the parsed body.
        assert len(payload["details"]) == 1, payload
        detail = payload["details"][0]
        assert isinstance(detail, dict), payload
        assert set(detail) == DETAIL_KEYS, detail
        assert detail["plan_id"] == SEEDED_PLAN_ID, detail
        assert detail["type"] == "verification", detail
        assert detail["status"] == "running", detail
        assert detail["round"] == SEEDED_ROUND, detail
        assert detail["started_at"], detail
        assert detail["updated_at"], detail

        assert len(payload["paused_executions"]) == 1, payload
        paused = payload["paused_executions"][0]
        assert isinstance(paused, dict), payload
        assert set(paused) == PAUSED_KEYS, paused
        assert paused["plan_id"] == PAUSED_PLAN_ID, paused
        assert paused["kind"] == "execution", paused
        assert paused["interrupted_task_count"] == 1, paused
        assert paused["paused_at"], paused

        # The counters and the list agree with each other: the overview
        # is one answer, not five independent ones.
        assert total_active == len(payload["details"]), payload
        assert verification_running == sum(
            1 for entry in payload["details"] if entry["type"] == "verification"
        ), payload
        assert execution_running == sum(
            1 for entry in payload["details"] if entry["type"] == "execution"
        ), payload

        # The service is back — on the port the request already named, and
        # the process answering there is the *new* one. Without this the
        # test would still pass if some stranger had grabbed the port
        # between the kill and the request.
        probe = probe_port(port)
        assert probe.listening
        assert data["new_pid"] in probe.pids, (data, probe.pids)
    finally:
        if agent is not None:
            current = agent._service_runtime_map
            if current is not None:
                for entry in (current.runtimes or {}).values():
                    _kill_service(getattr(entry, "pid", None))
        _kill_service(first_pid)