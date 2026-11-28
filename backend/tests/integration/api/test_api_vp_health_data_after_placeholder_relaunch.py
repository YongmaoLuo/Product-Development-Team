"""VP-002's closure on a **placeholder** URL, after the service was killed.

The gap this pins
-----------------
The plan under repair writes its api_test verification points against the
service **placeholder**, not against a literal address::

    "url": "{{svc.api.url}}/health/data"

Two rounds failed on that endpoint, for two different reasons, and the
first fix only closed one of them:

  1. ``_vp_target_port`` could not parse a port out of a placeholder, so
     ``_relaunch_service_for_vp`` gave up before even probing the port.
     A service killed between the preflight and the request stayed dead
     and the request came back ``URLError [Errno 61] Connection
     refused`` — no status code, no body, no ``plan_routing_rows``.
  2. Even with the relaunch fixed, the *request itself* still carried the
     raw placeholder: ``run_api_verification`` is handed the VP verbatim
     and hands ``request["url"]`` straight to ``urllib``.  ``{{svc.api.url}}``
     is not a URL, so the request could never have reached the endpoint
     that was just brought back up.

So the endpoint was never observed on a relaunched service: the round's
surviving evidence covered only ``/`` and ``/api/plans``, and the data
plane had **zero** observations.  This test closes that hole end to end.

What this test does
-------------------
Against the **real** backend server (the same ``python server.py`` the
plan's declaration starts):

  1. the service comes up on a declared port and answers ``/health/data``
     with the rows this test seeded into a real SQLite database — so the
     criterion is *observable* and the data plane is *real*;
  2. the service is killed with SIGTERM, exactly as the cross-plan sweep
     killed it, and the port is confirmed free;
  3. the api_test VP — written the way the plan writes it, with the
     ``{{svc.api.url}}`` placeholder — runs anyway, and must be graded
     **PASSED**: the framework's own relaunch puts the service back **on
     the same port** before the request, and the request must be made
     against the *resolved* URL;
  4. the response is read out of the audit artifact the runner wrote:
     200, ``$.status == "ok"``, and ``plan_routing_rows`` equal to the
     number of rows that were seeded.

Point 4 is the part the earlier rounds never had.  ``plan_routing_rows``
being *present* is a weak signal: a server with no database at all
answers ``{"status": "ok", "plan_routing_rows": 0}``.  The seeded rows
are what turn "the field exists" into "the SQLite data plane is
readable", and they are what makes an empty library unable to satisfy
this criterion.  The rows live in a real SQLite file reached through
``PDT_STATE_DB_PATH`` — no stub, no hard-coded response body.

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
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import probe_port  # noqa: E402
from service_manager import ServiceRuntimeMap, ensure_services  # noqa: E402


#: How many ``plan_routing`` rows the data plane must report.  The
#: endpoint answers ``{"status": "ok", "plan_routing_rows": 0}`` for a
#: server that cannot read a database at all, so only a *non-zero* count
#: proves the SQLite path is alive.  Three rows make an accidental
#: match with a default impossible to overlook.
SEEDED_ROWS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_state_db(db_path: Path, rows: int) -> None:
    """Create the state database and give it ``rows`` routing rows."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        for index in range(rows):
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES (?, 'interview', '2026-10-25T00:00:00Z')",
                (f"vp002-placeholder-seed-{index}",),
            )
        conn.commit()
    finally:
        conn.close()


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


def _get_json(url: str) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:
        assert response.status == 200, (url, response.status)
        return json.loads(response.read().decode("utf-8"))


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


def _vp_002() -> Dict[str, Any]:
    """VP-002 as the plan declares it — the criterion is not ours to move.

    The URL is the **placeholder** the plan writes, not a literal
    address: that is the whole point of this test, because the
    placeholder is what made the endpoint unobservable in the first
    place.
    """
    return {
        "id": "VP-002",
        "title": "默认后端服务的数据面健康探针可用",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api.url}}/health/data",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {"name": "数据面状态为 ok", "json_path": "$.status", "equals": "ok"},
            {
                "name": "返回 plan_routing_rows",
                "json_path": "$.plan_routing_rows",
                "exists": True,
            },
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_health_data_after_placeholder_relaunch(tmp_path, monkeypatch):
    """``/health/data`` must really be asserted on the relaunched service."""
    from verification_agent import VerificationAgent

    work = tmp_path / "pdt-verify-claude"
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True)
    db_path = work / "state.db"
    _seed_state_db(db_path, SEEDED_ROWS)

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
        before = _get_json(f"http://127.0.0.1:{port}/health/data")
        assert before["status"] == "ok", before
        assert before["plan_routing_rows"] == SEEDED_ROWS, before

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
        verdict = agent._run_api_test_vp(_vp_002())

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-002"
        assert event_type == "service_relaunched_before_vp"
        assert data["port"] == port
        assert data["old_pid"] == first_pid
        assert data["new_pid"] not in (None, first_pid)

        # --- 4. the endpoint's own answer, read from the audit artifact
        artifact_path = (
            plan_dir / "vp_artifacts" / "VP-002" / "api_response.json"
        )
        assert artifact_path.exists(), "the runner wrote no audit artifact"
        artifact = json.loads(artifact_path.read_text())
        assert artifact["status_code"] == 200, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]

        # The request that produced the artifact must be the *resolved*
        # one: the raw placeholder is not a URL and could not have been
        # requested at all.
        assert "{{" not in artifact["request"]["url"], artifact["request"]

        payload = json.loads(artifact["response_body"])
        assert payload["status"] == "ok", payload
        assert "plan_routing_rows" in payload, payload
        assert payload["plan_routing_rows"] == SEEDED_ROWS, payload

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
