"""VP-004's acceptance closure: the request guard survives a relaunch.

The failure this pins
---------------------
VP-004's criterion is the **security** half of the plan-list endpoint's
contract: ``GET /api/plans`` sent **without** the ``X-PDT-Request``
header must be refused with **403**, and the response must say which
header was missing.

In round 3 the api_test request for this endpoint came back
``URLError [Errno 61] Connection refused``: the service (pid 38271) had
been SIGTERM'd at 01:25:25, seven and a half minutes before the request.
So neither the 403 nor the explanation was ever observed.  The recovery
round only ever sent the *with-header* request (200 ``[]``) — which says
nothing at all about the guard.

That gap matters more than one endpoint's score.  The 403 **is** the
point of :mod:`request_guard`: the server binds loopback, and the
custom header is what stops a browser page on another origin from
driving the local API.  A self-healing mechanism that made the request
succeed by *injecting* ``X-PDT-Request``, rewriting the request, taking
an internal bypass, or switching the guard off would disable exactly
that protection — and it would do it *silently*, because the verdict
would still read PASSED.  The repair loop is supposed to restore the
**process**; it must never restore the *request* by weakening it.

What this test does
-------------------
It reproduces that timeline against the **real** backend server (the same
``python server.py`` the plan's declaration starts) and asserts the
criterion where it was never asserted:

  1. the service comes up and the criterion is *observable*: a request
     without the header is refused with 403 and the refusal names the
     missing header, while the same request **with** the header is
     served (200) — so the 403 is the guard speaking and not a broken
     endpoint;
  2. the service is killed with SIGTERM, exactly as the cross-plan
     sweep killed it, and the port is confirmed free;
  3. the api_test VP runs anyway and must be graded **PASSED** — the
     framework's own relaunch (Leg B of the VP-001 fix) puts the service
     back **on the same port** before the request;
  4. the response is read out of the audit artifact the runner wrote:
     403, and a body whose ``detail`` names the missing header;
  5. **the request the runner actually sent carried no guard header** —
     the artifact's own record of the request is the evidence that the
     framework did not sneak ``X-PDT-Request`` in to make the call
     succeed;
  6. and the guard is still standing on the relaunched service: a
     header-less request answered **again** with 403 (a 200 here is a
     regression, and the test fails on it), while the guarded request
     still answers 200 — so the relaunched service is serving, not
     refusing everything.

Point 6 is the half the brief calls the extra duty: the fallback may
restore the process, never the request's weakening.  Point 5 is what
makes that checkable rather than assertable — the framework records the
headers it sent, so "it did not inject the header" is a fact read out
of the artifact rather than a promise.

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


#: The guard header every ``/api/*`` request must carry, and the value
#: it must carry.  Copied from :mod:`request_guard` rather than imported:
#: the test asserts what the *criterion* says, and a typo in the
#: framework's constant must not silently become the expectation.
GUARD_HEADER = "X-PDT-Request"
GUARD_HEADER_VALUE = "1"

#: The plans seeded into the state database the server reads.  The
#: listing is not what this criterion is about, but a populated one
#: keeps the *guarded* request meaningful: a service that answers 200
#: for the guarded request and 403 for the bare one is serving, while a
#: service that answers 403 for everything would only be broken.
SEEDED_ROWS: Tuple[Tuple[str, str], ...] = (
    ("vp004-seed-a", "2026-10-25T00:00:00Z"),
    ("vp004-seed-b", "2026-10-24T00:00:00Z"),
)

#: The ids the seeded rows contribute to the listing.
SEEDED_IDS = frozenset(plan_id for plan_id, _ in SEEDED_ROWS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_state_db(db_path: Path, plans_dir: Path) -> None:
    """Give the server a state database with plans in the listing.

    The rows go into ``plan_routing`` — the table the route reads — with
    a non-terminal phase, because the default listing filters terminal
    phases out.  Each seeded plan also gets a directory under the plans
    tree, which is what a real plan has.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        for plan_id, updated_at in SEEDED_ROWS:
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES (?, 'interview', ?)",
                (plan_id, updated_at),
            )
        conn.commit()
    finally:
        conn.close()

    for plan_id, _ in SEEDED_ROWS:
        (plans_dir / plan_id).mkdir(parents=True, exist_ok=True)


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


def _http_get(
    url: str, headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, str], str]:
    """GET ``url``; return ``(status, headers, body)`` — 4xx included.

    ``urlopen`` raises for a 4xx/5xx, but a refusal is a *response*
    here (the guard answers 403 on its own), so the error is unwrapped
    rather than swallowed: the status code is the thing under test.
    """
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            return (
                int(response.status),
                {k.lower(): v for k, v in response.headers.items()},
                body,
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the body is best-effort
            body = ""
        return (
            int(exc.code),
            {k.lower(): v for k, v in (exc.headers or {}).items()},
            body,
        )


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


def _carries_guard_header(headers: Dict[str, Any]) -> bool:
    """Whether ``headers`` includes the guard header, in any spelling."""
    return any(
        str(name).strip().lower() == GUARD_HEADER.lower()
        for name in (headers or {})
    )


def _vp_004(port: int) -> Dict[str, Any]:
    """VP-004 as the criterion states it — the criterion is not ours to move.

    The request carries **no** guard header.  That absence is the whole
    criterion: the endpoint must refuse it with 403 and say which header
    was missing.
    """
    return {
        "id": "VP-004",
        "title": "缺少 X-PDT-Request 的请求被拒绝并说明缺失的请求头",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/api/plans",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 403", "status": 403},
            {
                "name": "响应说明缺少的请求头",
                "json_path": "$.detail",
                "contains": GUARD_HEADER,
            },
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_relaunch_does_not_bypass_request_guard(tmp_path, monkeypatch):
    """The relaunch must restore the process, never weaken the request."""
    from verification_agent import VerificationAgent

    work = tmp_path / "pdt-verify-guard"
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True)
    db_path = work / "state.db"
    _seed_state_db(db_path, plans_dir)

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
        # The bare request is refused, and the refusal names the header.
        denied_status, _, denied_body = _http_get(
            f"http://127.0.0.1:{port}/api/plans",
            {"Accept": "application/json"},
        )
        assert denied_status == 403, (denied_status, denied_body)
        assert GUARD_HEADER in denied_body, denied_body

        # The guarded request is served, so the 403 above is the guard
        # speaking and not an endpoint that answers nothing at all.
        allowed_status, _, allowed_body = _http_get(
            f"http://127.0.0.1:{port}/api/plans",
            {GUARD_HEADER: GUARD_HEADER_VALUE, "Accept": "application/json"},
        )
        assert allowed_status == 200, (allowed_status, allowed_body)
        listing = json.loads(allowed_body)
        assert isinstance(listing, list), listing
        assert SEEDED_IDS <= {entry["id"] for entry in listing}, listing

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
        verdict = agent._run_api_test_vp(_vp_004(port))

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-004"
        assert event_type == "service_relaunched_before_vp"
        assert data["port"] == port
        assert data["old_pid"] == first_pid
        assert data["new_pid"] not in (None, first_pid)

        # --- 4. the endpoint's own answer, read from the audit artifact
        artifact_path = (
            plan_dir / "vp_artifacts" / "VP-004" / "api_response.json"
        )
        assert artifact_path.exists(), "the runner wrote no audit artifact"
        artifact = json.loads(artifact_path.read_text())
        assert artifact["status_code"] == 403, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]

        # The refusal names the missing header — the second half of the
        # criterion ("响应说明缺请求头").
        payload = json.loads(artifact["response_body"])
        assert payload.get("error") == "forbidden", payload
        detail = str(payload.get("detail") or "")
        assert GUARD_HEADER in detail, payload

        # --- 5. the request carried no guard header ---------------------
        # The artifact records the request the runner actually sent.  If
        # the fallback had injected the header to make the call succeed,
        # this is where it would show — and the 403 above would have been
        # an accident rather than the guard answering.
        sent_headers = artifact["request"]["headers"]
        assert not _carries_guard_header(sent_headers), sent_headers

        # --- 6. the guard is still standing on the relaunched service ---
        after_status, _, after_body = _http_get(
            f"http://127.0.0.1:{port}/api/plans",
            {"Accept": "application/json"},
        )
        assert after_status == 403, (
            "the relaunched service answered a header-less request with "
            f"{after_status} — the guard was bypassed: {after_body}"
        )
        assert GUARD_HEADER in after_body, after_body

        # ... and it is serving, not refusing everything.
        served_status, _, served_body = _http_get(
            f"http://127.0.0.1:{port}/api/plans",
            {GUARD_HEADER: GUARD_HEADER_VALUE, "Accept": "application/json"},
        )
        assert served_status == 200, (served_status, served_body)

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
