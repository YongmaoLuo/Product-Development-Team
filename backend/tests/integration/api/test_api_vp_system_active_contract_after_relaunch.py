"""VP-003's acceptance closure: ``/api/system/active`` on a *relaunched* service.

The failure this pins
---------------------
VP-003's criterion is the system-overview endpoint's own contract:
``GET /api/system/active`` (with the ``X-PDT-Request`` guard header)
answers 200 with a JSON body carrying five fields —
``execution_running``, ``verification_running``, ``total_active``,
``details`` and ``paused_executions``.

In round 3 the request came back ``URLError [Errno 61] Connection
refused``: no status code, no body, and not one of the five fields
observed.  The service had been SIGTERM'd fourteen seconds after
``services_ensured`` and the request followed seven and a half minutes
later.  The round's only surviving evidence for this endpoint was
VP-010's ``GET /`` and VP-011's ``GET /api/plans`` — neighbouring
endpoints that say nothing about the system overview, and whose 200s
were used as if they had.  This endpoint's contract was blank for the
whole round.

What this test does
-------------------
It reproduces that timeline against the **real** backend server (the same
``python server.py`` the plan's declaration starts) and asserts the
criterion where it was never asserted:

  1. the service comes up on a declared port and answers
     ``/api/system/active`` with all five fields — so the criterion is
     *observable* before anything is broken;
  2. the service is killed with SIGTERM, exactly as the cross-plan sweep
     killed it, and the port is confirmed free;
  3. the api_test VP runs anyway, and must be graded **PASSED** — the
     framework's own relaunch (Leg B of the VP-001 fix) puts the service
     back **on the same port** before the request;
  4. the five fields are read out of the audit artifact the runner wrote,
     one by one, and each one's *structure* is checked — not merely that
     the body mentions the five names.

Point 4 is the part the earlier round never had, and it is deliberately
not "the body contains these five strings".  A body that mentions
``execution_running`` proves nothing: ``null`` is a field too, and so is
a string where a count belongs.  So the endpoint is put into a state
with something genuinely in flight (a recovered verification round) and
something genuinely paused (a pause checkpoint on disk), and the test
then insists on the numbers those two facts imply:
``verification_running == 1``, ``total_active == 1``, one ``details``
entry with the documented keys, one ``paused_executions`` entry with the
documented keys.  A server that answered ``{"execution_running": 0,
"verification_running": 0, "total_active": 0, "details": [],
"paused_executions": []}`` — five fields, all hollow — cannot satisfy
this criterion, which is the whole point: the endpoint is the system
overview, so an overview that reports nothing is a broken overview.

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
from typing import Any, Dict, Optional, Tuple

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
SEEDED_PLAN_ID = "vp003-seed"

#: The plan carrying a pause checkpoint, so ``paused_executions`` has
#: something real to report.
PAUSED_PLAN_ID = "vp003-paused"

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


def _vp_003(port: int) -> Dict[str, Any]:
    """VP-003 as the criterion states it — the criterion is not ours to move.

    Each of the five fields is asserted **by path**, so a body that
    merely mentions the name is not what passes this.  The counts and
    the two lists' shapes are asserted too: the endpoint is the system
    overview, and an overview that reports the wrong number is broken
    even though it carries all five keys.
    """
    url = f"http://127.0.0.1:{port}/api/system/active"
    return {
        "id": "VP-003",
        "title": "系统总览接口在服务重启后仍满足五字段契约",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": url,
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
            # --- the five fields, one by one ---
            {
                "name": "execution_running 存在",
                "json_path": "$.execution_running", "exists": True,
            },
            {
                "name": "verification_running 存在",
                "json_path": "$.verification_running", "exists": True,
            },
            {
                "name": "total_active 存在",
                "json_path": "$.total_active", "exists": True,
            },
            {
                "name": "details 存在",
                "json_path": "$.details", "exists": True,
            },
            {
                "name": "paused_executions 存在",
                "json_path": "$.paused_executions", "exists": True,
            },
            # --- and their structure ---
            {
                "name": "没有执行中的执行轮次",
                "json_path": "$.execution_running", "equals": 0,
            },
            {
                "name": "有一个进行中的验证轮次",
                "json_path": "$.verification_running", "equals": 1,
            },
            {
                "name": "活跃总数与明细条数一致",
                "json_path": "$.total_active", "equals": 1,
            },
            {
                "name": "details 是一条明细",
                "json_path": "$.details", "length_equals": 1,
            },
            {
                "name": "details[0] 是验证类型",
                "json_path": "$.details[0].type", "equals": "verification",
            },
            {
                "name": "details[0] 是被恢复的 plan",
                "json_path": "$.details[0].plan_id", "equals": SEEDED_PLAN_ID,
            },
            {
                "name": "details[0] 状态是 running",
                "json_path": "$.details[0].status", "equals": "running",
            },
            {
                "name": "details[0] 轮次号正确",
                "json_path": "$.details[0].round", "equals": SEEDED_ROUND,
            },
            {
                "name": "paused_executions 是一条暂停记录",
                "json_path": "$.paused_executions", "length_equals": 1,
            },
            {
                "name": "paused_executions[0] 是被暂停的 plan",
                "json_path": "$.paused_executions[0].plan_id",
                "equals": PAUSED_PLAN_ID,
            },
            {
                "name": "paused_executions[0] 类型正确",
                "json_path": "$.paused_executions[0].kind", "equals": "execution",
            },
            {
                "name": "paused_executions[0] 统计了被中断的任务",
                "json_path": "$.paused_executions[0].interrupted_task_count",
                "equals": 1,
            },
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_api_vp_system_active_contract_after_relaunch(tmp_path, monkeypatch):
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
        verdict = agent._run_api_test_vp(_vp_003(port))

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

        payload = json.loads(artifact["response_body"])

        # The five fields, one by one — the criterion, not a summary of it.
        for field in CONTRACT_FIELDS:
            assert field in payload, (field, payload)
        assert payload["execution_running"] == 0, payload
        assert payload["verification_running"] == 1, payload
        assert payload["total_active"] == 1, payload

        # The structure of the two lists, checked on the parsed body —
        # a body that merely mentions the five names cannot pass this.
        assert isinstance(payload["details"], list), payload
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

        assert isinstance(payload["paused_executions"], list), payload
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
        assert payload["total_active"] == len(payload["details"]), payload
        assert payload["verification_running"] == sum(
            1 for entry in payload["details"] if entry["type"] == "verification"
        ), payload
        assert payload["execution_running"] == sum(
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