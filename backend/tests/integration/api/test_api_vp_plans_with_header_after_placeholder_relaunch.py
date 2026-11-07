"""VP-005's acceptance closure: ``/api/plans`` on a **placeholder** URL.

The failure this pins
---------------------
VP-005's criterion is the plan-list endpoint's contract: ``GET /api/plans``
carrying the ``X-PDT-Request`` guard header answers **200**, with a
``Content-Type`` of ``application/json`` (a ``charset`` parameter is
allowed), and a body that is a **JSON array**.

What survived for this endpoint was a **side observation**: a neighbouring
``curl -H 'X-PDT-Request: 1'`` answered ``200`` with the body ``[]`` and
``content-type: application/json``.  That is a 旁证 — a neighbouring
observation, not the criterion.  Two things were therefore never checked
by this VP itself:

  1. the **framework's own assertions never ran** for it — the api_test
     request came back ``URLError [Errno 61] Connection refused`` (the
     service had been SIGTERM'd by the cross-plan sweep minutes
     earlier), so no verdict was ever produced from the endpoint's own
     answer;
  2. the **response header** was never looked at by this point at all.
     Only the side observation saw a content type, and it saw it with a
     ``curl``, outside the framework — so nothing in the round ever
     asked the *criterion's* question of the *criterion's* request.

Why the URL is a placeholder
----------------------------
The plan writes the URL as the **service placeholder** —
``{{svc.api.url}}/api/plans`` — because the port is declared once and
re-resolved every round.  Two things used to make that spelling
unobservable:

  1. ``_vp_target_port`` could not read a port out of a placeholder, so
     the self-healing relaunch gave up *before it even probed* and the
     service stayed dead → ``Connection refused``;
  2. even once the relaunch could find the port, the *request* still
     carried the raw placeholder, which is not a URL at all.

Both are the placeholder-resolution fix this task reuses (VP-001's).
What this file adds is the **endpoint's own closure**: the criterion
asserted by the framework, on the placeholder URL, on a service that was
killed and had to be brought back.

What this test does
-------------------
Against the **real** backend server (the same ``python server.py`` the
plan's declaration starts):

  1. the service comes up and the criterion is *observable* before
     anything is broken: with the guard header the endpoint answers
     200, ``application/json``, and a JSON **array** holding the seeded
     plans — and without the header it is refused with 403, so the 200
     is the guarded contract answering and not an unguarded accident;
  2. the service is killed with SIGTERM, exactly as the cross-plan sweep
     killed it, and the port is confirmed free;
  3. the api_test VP — carrying the **placeholder** — runs anyway and
     must be graded **PASSED**: the framework's own relaunch (VP-001's
     fix) puts the service back **on the same port** before the request,
     and the request must be made against the *resolved* address;
  4. the response is read out of the audit artifact the runner wrote:
     200, a ``Content-Type`` of ``application/json`` (charset allowed),
     and a body that ``json.loads`` turns into a **list** of plan
     objects;
  5. the framework's own record says it checked the **header** — the
     half the side observation checked on its own and this VP never did.

Point 4 is deliberately not "the body contains a bracket".  A body of
``"[]"`` (a JSON *string*), ``{"plans": []}`` (a JSON *object*) and
``[]`` (a JSON *array*) all start with a bracket, and only the third one
satisfies the criterion.  So the VP this test declares asserts
``$[0].id`` — indexing resolves on a list and on nothing else — and
``length_equals`` on ``$``; the array-ness is then re-checked from the
artifact with ``json.loads`` and an ``isinstance`` test, because the
assertion vocabulary has no "is an array" subject and the criterion
deserves a check that does not go through it.

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

#: The plans seeded into the state database the server reads.  Two, not
#: one, so the listing the endpoint returns is a list with more than one
#: element — an endpoint that answered ``[]`` could not satisfy the
#: criterion the way a populated one can.  The ``updated_at`` values are
#: distinct because the route orders by ``updated_at DESC``; the newest
#: one is therefore the first element of the listing.
SEEDED_ROWS: Tuple[Tuple[str, str], ...] = (
    ("vp005-placeholder-seed-a", "2026-10-25T00:00:00Z"),
    ("vp005-placeholder-seed-b", "2026-10-24T00:00:00Z"),
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


def _media_type(content_type: str) -> str:
    """The media type of a ``Content-Type`` header, without its parameters.

    ``application/json; charset=utf-8`` -> ``application/json``.  The
    criterion allows the charset parameter; it does not allow a
    different media type, and a response that carried no content type at
    all must not pass either.
    """
    return content_type.split(";", 1)[0].strip().lower()


def _carries_guard_header(headers: Dict[str, Any]) -> bool:
    """Whether ``headers`` includes the guard header, in any spelling."""
    return any(
        str(name).strip().lower() == GUARD_HEADER.lower()
        for name in (headers or {})
    )


def _seed_state_db(db_path: Path, plans_dir: Path) -> None:
    """Give the server a state database with two plans in the listing.

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


def _vp_005() -> Dict[str, Any]:
    """VP-005 as the criterion states it — the criterion is not ours to move.

    The URL is the **placeholder** the plan writes, not a literal
    address: that is the whole point of this test, because the
    placeholder is what made the criterion unobservable in the first
    place.

    The request carries the guard header, because "带 X-PDT-Request" is
    half of the criterion: the endpoint must serve *this* request.
    """
    return {
        "id": "VP-005",
        "title": "计划列表接口返回 JSON 数组",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api.url}}/api/plans",
            "headers": {
                "Accept": "application/json",
                GUARD_HEADER: GUARD_HEADER_VALUE,
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            # The half this point never checked: the response *header*.
            # The media type may carry a charset parameter, so the
            # assertion matches the media type inside the header value.
            {
                "name": "Content-Type 是 application/json",
                "header": "content-type",
                "contains": "application/json",
            },
            # ``$[0].id`` resolves on a list and on nothing else: a JSON
            # *object* body (``{"plans": [...]}``) or a JSON *string*
            # body (``"[]"``) cannot satisfy it.  This is how "the body
            # is a JSON array" is asserted with the vocabulary the
            # framework has.
            {
                "name": "响应体是计划对象组成的 JSON 数组",
                "json_path": "$[0].id",
                "exists": True,
            },
            {
                "name": "数组长度等于种子计划数",
                "json_path": "$",
                "length_equals": len(SEEDED_ROWS),
            },
        ],
    }


def _start_service(tmp_path: Path) -> Dict[str, Any]:
    """Bring the declared service up and hand back the round's state.

    Everything the test needs to reproduce the failed round: the plans
    tree the service is told to use, the plan directory the round would
    own, the declaration, and the runtime entry the round's runtime map
    would still point at (at the process that is about to die).
    """
    work = tmp_path / "pdt-verify-plans-placeholder"
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

    runtime_map = ensure_services(REPO_ROOT, {"api": declaration})
    runtime = runtime_map.runtimes["api"]
    assert runtime.ready, runtime.detail
    assert runtime.pid, runtime.detail

    return {
        "work": work,
        "plans_dir": plans_dir,
        "db_path": db_path,
        "plan_dir": plan_dir,
        "port": port,
        "declaration": declaration,
        "runtime": runtime,
    }


def _agent_for(fixture: Dict[str, Any]):
    """A verification agent wired to the round's declarations.

    The runtime map still points at the process that is about to be
    killed — that is exactly the state the api_test path used to meet.
    """
    from verification_agent import VerificationAgent

    agent = VerificationAgent(
        plan_dir=fixture["plan_dir"], project_dir=REPO_ROOT,
        coding_tool=None,
    )
    agent._service_declarations = {"api": fixture["declaration"]}
    agent._service_runtime_map = ServiceRuntimeMap(
        runtimes={"api": fixture["runtime"]}
    )
    return agent


def _artifact(fixture: Dict[str, Any], vp_id: str) -> Dict[str, Any]:
    """The audit artifact the api_test runner wrote for ``vp_id``."""
    path = fixture["plan_dir"] / "vp_artifacts" / vp_id / "api_response.json"
    assert path.exists(), f"the runner wrote no audit artifact for {vp_id}"
    return json.loads(path.read_text())


def _kill_everything(agent, fixture: Dict[str, Any]) -> None:
    """Stop the service this test started, whichever process it is now."""
    if agent is not None:
        current = getattr(agent, "_service_runtime_map", None)
        if current is not None:
            for entry in (current.runtimes or {}).values():
                _kill_service(getattr(entry, "pid", None))
    _kill_service(fixture["runtime"].pid)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_plans_with_header_after_placeholder_relaunch(tmp_path, monkeypatch):
    """``/api/plans`` must really be asserted on the relaunched service.

    The criterion has three halves — 200, ``application/json``, and a
    JSON **array** body — and this point checks all three **itself**,
    on the URL the plan actually writes (``{{svc.api.url}}/api/plans``),
    on a service that was killed and had to be brought back.
    """
    fixture = _start_service(tmp_path)
    port = fixture["port"]
    first_pid = fixture["runtime"].pid

    agent = None
    try:
        # --- 1. the criterion is observable before anything dies -------
        status, headers, body = _http_get(
            f"http://127.0.0.1:{port}/api/plans",
            {GUARD_HEADER: GUARD_HEADER_VALUE, "Accept": "application/json"},
        )
        assert status == 200, (status, body)
        assert _media_type(headers.get("content-type", "")) == "application/json", headers
        listing = json.loads(body)
        assert isinstance(listing, list), listing
        assert SEEDED_IDS <= {entry["id"] for entry in listing}, listing

        # The guard header is load-bearing: without it the endpoint is
        # refused, so the 200 above is the guarded contract answering
        # and not some unguarded accident.
        denied_status, _, _ = _http_get(
            f"http://127.0.0.1:{port}/api/plans",
            {"Accept": "application/json"},
        )
        assert denied_status == 403, denied_status

        # --- 2. the service dies, the way the cross-plan sweep killed it
        _kill_service(first_pid)
        _wait_until_port_free(port)

        agent = _agent_for(fixture)
        events: list = []
        monkeypatch.setattr(
            agent,
            "_safe_log_freshness_event",
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            ),
        )

        # --- 3. the verification point runs, and must pass --------------
        # The URL is the placeholder the plan writes, so this run also
        # pins that the request is made against the *resolved* address.
        verdict = agent._run_api_test_vp(_vp_005())

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-005"
        assert event_type == "service_relaunched_before_vp"
        assert data["port"] == port
        assert data["old_pid"] == first_pid
        assert data["new_pid"] not in (None, first_pid)

        # --- 4. the endpoint's own answer, read from the audit artifact
        artifact = _artifact(fixture, "VP-005")
        assert artifact["status_code"] == 200, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]

        # The framework itself checked the content type — the half the
        # side observation checked on its own and this point never did.
        # Its own record says so.
        passed = artifact["assertions_passed"]
        assert any(
            "header 'content-type'" in line for line in passed
        ), passed

        # The header is application/json; a charset parameter is allowed.
        content_type = artifact["response_headers"].get("content-type", "")
        assert _media_type(content_type) == "application/json", content_type

        # The body is a JSON *array* — checked here with json.loads and
        # an isinstance test, because the assertion vocabulary has no
        # "is an array" subject and the criterion deserves a check that
        # does not go through it.
        assert artifact["response_body_truncated"] is False, artifact
        payload = json.loads(artifact["response_body"])
        assert isinstance(payload, list), payload
        assert len(payload) == len(SEEDED_ROWS), payload
        assert all(isinstance(entry, dict) for entry in payload), payload
        assert SEEDED_IDS <= {entry["id"] for entry in payload}, payload

        # The request that produced the artifact must be the *resolved*
        # one — the raw placeholder is not a URL and could not have been
        # requested at all — and it must carry the guard header the
        # criterion names.
        assert "{{" not in artifact["request"]["url"], artifact["request"]
        assert _carries_guard_header(artifact["request"]["headers"]), (
            artifact["request"]["headers"]
        )

        # The service is back — on the port the request already named, and
        # the process answering there is the *new* one. Without this the
        # test would still pass if some stranger had grabbed the port
        # between the kill and the request.
        probe = probe_port(port)
        assert probe.listening
        assert data["new_pid"] in probe.pids, (data, probe.pids)
    finally:
        _kill_everything(agent, fixture)