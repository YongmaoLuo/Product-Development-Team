"""VP-006's closure on a **placeholder** URL, after the service was killed.

The gap this pins
-----------------
VP-006's criterion is a **negative** one::

    GET {{svc.api.url}}/api/plan/pdt-verify-missing-plan/state
        (with X-PDT-Request)  ->  404

The plan writes its api_test URLs against the service **placeholder**
(``{{svc.api.url}}``), not against a literal address — the port is
declared once and re-resolved every round.

Two earlier rounds failed to observe this criterion, for two different
reasons:

  1. **Round 3** — the request came back ``URLError [Errno 61]
     Connection refused``: the service (pid 38271) had been SIGTERM'd at
     01:25:25, seven and a half minutes before the request.  The 404
     semantics were never observed — no status code, no body to assert on
     at all.  The only 404 line anyone had for this endpoint came from a
     *previous* round's log, which says nothing about the failing round.
  2. **Round 4** — the framework's self-healing relaunch never fired at
     all: ``_vp_target_port`` could not parse a port out of a
     placeholder, so the relaunch path gave up before even probing the
     port, and the service stayed dead.

A third round then added a closure for this endpoint on a **literal**
URL (``test_api_vp_missing_plan_state_404_after_relaunch.py``).  That
one proved the endpoint keeps answering 404 after a relaunch — but on a
URL the plan never writes.  On the placeholder URL the two failures above
still apply, and the endpoint therefore remained **unobserved** for the
shape the plan actually uses.

What this test does
-------------------
Against the **real** backend server (the same ``python server.py`` the
plan's declaration starts):

  1. the service comes up on a declared port and the criterion is
     observable *before* anything is broken: the missing plan answers
     404 with the guard header, and the same request without the header
     is refused with 403 — which is what makes "带 X-PDT-Request" a
     load-bearing half of the criterion rather than decoration (a 403
     would also be "not 200", and a criterion that cannot tell 403 from
     404 proves nothing);
  2. the service is killed with SIGTERM, exactly as the cross-plan sweep
     killed it, and the port is confirmed free;
  3. the api_test VP — written the way the plan writes it, with the
     ``{{svc.api.url}}`` placeholder — runs anyway, and must be graded
     **PASSED**: the framework's own relaunch (the VP-001 fix) puts the
     service back **on the same port** before the request, and the
     request must be made against the *resolved* URL;
  4. the response is read out of the audit artifact the runner wrote:
     404, a body that is the endpoint's own ``{"detail": ...}`` 404 and
     **not** the success structure;
  5. the plans directory is snapshotted **before** and **after** the
     request, and the missing plan must appear in neither.

Point 5 is the failure mode the endpoint is easy to get wrong.  The
relaunch touches the very directory the endpoint reads from
(``PLANS_DIR``), and the danger is a server that "helpfully" creates an
empty plan for an id it does not know and then answers 200 with
``{"plan_id": ..., "current_phase": ...}``.  A 404 on its own does not
rule that out — the body has to be shown not to be the success
structure, the plan directory has to still be absent afterwards, and the
state database has to still hold no routing row for the missing plan.
All three are checked here.

The declaration is copied from the plan's own ``services`` block rather
than invented here, including the ``unset PDT_SECRET_FD_*`` step: those
environment variables name descriptors belonging to the *parent* process,
and a child that inherits the names without the descriptors reads
whatever now occupies that number.  In this repository that took
uvicorn's listening socket down with it (``OSError: [Errno 9] Bad file
descriptor`` inside the event loop) — the plan's declaration drops the
names for exactly that reason.
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
from typing import Any, Dict, Optional, Set, Tuple

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import probe_port  # noqa: E402
from service_manager import ServiceRuntimeMap, ensure_services  # noqa: E402


#: The plan the criterion asks about: a plan id that does not exist.
#: It is a valid plan id (the validator allows ``[A-Za-z0-9._-]``), so
#: the request reaches the handler and is answered by the *plan*
#: lookup — not by the id validator's 400.
MISSING_PLAN_ID = "pdt-verify-missing-plan"

#: The control plan: it exists, so the same endpoint answers 200 with
#: the success structure.  Without it, "the endpoint answers 404" and
#: "the endpoint answers 404 to everything" look the same.
PRESENT_PLAN_ID = "pdt-verify-present-plan"

#: The guard header every ``/api/*`` request must carry, and the value
#: it must carry.  Copied from :mod:`request_guard` rather than imported:
#: the test asserts what the *criterion* says, and a typo in the
#: framework's constant must not silently become the expectation.
GUARD_HEADER = "X-PDT-Request"
GUARD_HEADER_VALUE = "1"

#: A key of the success structure — the body ``GET /api/plan/{id}/state``
#: answers with when the plan exists.  The 404 body must not contain it.
SUCCESS_STRUCTURE_KEY = "current_phase"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_state_db(db_path: Path, plans_dir: Path) -> None:
    """Give the server a state database that knows one plan.

    The row goes into ``plan_routing`` — the table the state endpoint
    reads — and the plan also gets a directory under the plans tree,
    which is what a real plan has.  The missing plan gets neither: that
    is the whole point of the criterion.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, 'interview', '2026-10-25T00:00:00Z')",
            (PRESENT_PLAN_ID,),
        )
        conn.commit()
    finally:
        conn.close()

    (plans_dir / PRESENT_PLAN_ID).mkdir(parents=True, exist_ok=True)


def _plan_dirs(plans_dir: Path) -> Set[str]:
    """The names of every plan directory under the plans tree."""
    if not plans_dir.exists():
        return set()
    return {entry.name for entry in plans_dir.iterdir() if entry.is_dir()}


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
    here (the guard answers 403 on its own, the endpoint answers 404 on
    its own), so the error is unwrapped rather than swallowed: the
    status code is the thing under test.
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


def _routing_rows(db_path: Path) -> list:
    """Every ``plan_id`` the state database holds a routing row for."""
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        rows = conn.execute("SELECT plan_id FROM plan_routing").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _vp_006() -> Dict[str, Any]:
    """VP-006 as the plan declares it — the criterion is not ours to move.

    The URL is the **placeholder** the plan writes, not a literal
    address: that is the whole point of this test, because the
    placeholder is what made the endpoint unobservable in the first
    place.  A literal URL would exercise a path the plan never takes.
    """
    return {
        "id": "VP-006",
        "title": "不存在的计划状态接口返回 404",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"{{{{svc.api.url}}}}/api/plan/{MISSING_PLAN_ID}/state",
            "headers": {
                GUARD_HEADER: GUARD_HEADER_VALUE,
                "Accept": "application/json",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 404", "status": 404},
            # The 404 body must not be the success structure.  A server
            # that invented an empty plan for an unknown id would answer
            # 200 — but a body check that only looked for the id would
            # let it through; this one pins the negative from the body
            # side as well.
            {
                "name": "响应体不是成功结构",
                "body_not_contains": SUCCESS_STRUCTURE_KEY,
            },
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_missing_plan_404_after_placeholder_relaunch(tmp_path, monkeypatch):
    """``/api/plan/{id}/state`` must answer 404 for a missing plan, on the
    placeholder URL, on the service the framework itself brought back."""
    from verification_agent import VerificationAgent

    work = tmp_path / "pdt-verify-claude"
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

    missing_plan_dir = plans_dir / MISSING_PLAN_ID

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
        status, _headers, body = _http_get(
            f"http://127.0.0.1:{port}/api/plan/{MISSING_PLAN_ID}/state",
            {GUARD_HEADER: GUARD_HEADER_VALUE, "Accept": "application/json"},
        )
        assert status == 404, (status, body)
        # The endpoint answered, not some other layer: its 404 is a JSON
        # document carrying a ``detail``, and it is not the success
        # structure.
        payload = json.loads(body)
        assert "detail" in payload, payload
        # The raw body, not just the top-level keys: a success structure
        # nested under ``detail`` would still be a success structure.
        assert SUCCESS_STRUCTURE_KEY not in body, body
        assert "plan_id" not in body, body

        # --- 2. the guard header is load-bearing -------------------------
        # Without it the request never reaches the handler, so the
        # answer is 403 — a different status, and one that would also
        # satisfy a sloppy "not 200" criterion.
        denied_status, _, _ = _http_get(
            f"http://127.0.0.1:{port}/api/plan/{MISSING_PLAN_ID}/state",
            {"Accept": "application/json"},
        )
        assert denied_status == 403, denied_status

        # --- 3. the control: a plan that exists answers 200 ------------
        present_status, _, present_body = _http_get(
            f"http://127.0.0.1:{port}/api/plan/{PRESENT_PLAN_ID}/state",
            {GUARD_HEADER: GUARD_HEADER_VALUE, "Accept": "application/json"},
        )
        assert present_status == 200, (present_status, present_body)
        present_payload = json.loads(present_body)
        assert SUCCESS_STRUCTURE_KEY in present_payload, present_payload

        # --- 4. the service dies, the way the cross-plan sweep killed it
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

        # The plans tree **before** the request: the relaunch and the
        # request are both about to touch it.
        plans_before = _plan_dirs(plans_dir)
        assert MISSING_PLAN_ID not in plans_before, plans_before
        assert not missing_plan_dir.exists()

        # --- 5. the verification point runs, and must pass --------------
        verdict = agent._run_api_test_vp(_vp_006())

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-006"
        assert event_type == "service_relaunched_before_vp"
        assert data["port"] == port
        assert data["old_pid"] == first_pid
        assert data["new_pid"] not in (None, first_pid)

        # --- 6. the endpoint's own answer, read from the audit artifact
        artifact_path = (
            plan_dir / "vp_artifacts" / "VP-006" / "api_response.json"
        )
        assert artifact_path.exists(), "the runner wrote no audit artifact"
        artifact = json.loads(artifact_path.read_text())
        assert artifact["status_code"] == 404, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]

        # The request that produced the artifact must be the *resolved*
        # one: the raw placeholder is not a URL and could not have been
        # requested at all.
        requested_url = artifact["request"]["url"]
        assert "{{" not in requested_url, artifact["request"]
        assert requested_url == (
            f"http://127.0.0.1:{port}/api/plan/{MISSING_PLAN_ID}/state"
        ), requested_url

        # The body is the endpoint's own 404 — and it is not the success
        # structure.  This is the half a 404 status code alone does not
        # say: a server that minted an empty plan for the unknown id
        # would have answered 200 with exactly that structure.
        raw_body = artifact["response_body"]
        body_payload = json.loads(raw_body)
        assert "detail" in body_payload, body_payload
        assert SUCCESS_STRUCTURE_KEY not in raw_body, raw_body
        assert "plan_id" not in raw_body, raw_body

        # --- 7. the plans tree did not gain the plan --------------------
        # Neither the relaunch nor the request may leave a plan behind
        # for an id that does not exist.
        plans_after = _plan_dirs(plans_dir)
        assert MISSING_PLAN_ID not in plans_after, plans_after
        assert not missing_plan_dir.exists(), (
            f"the relaunch or the request created {missing_plan_dir}"
        )

        rows = _routing_rows(db_path)
        # Non-vacuity first: the query really reads this database and
        # really finds the seeded plan's row.  Without it, "the missing
        # plan has no row" would also be true of a query that returned
        # nothing at all.
        assert PRESENT_PLAN_ID in rows, rows
        assert MISSING_PLAN_ID not in rows, (
            "the relaunch wrote a routing row for the missing plan"
        )

        # The control still answers 200 after the relaunch: the service
        # is alive and discriminating, not merely alive.
        after_status, _, after_body = _http_get(
            f"http://127.0.0.1:{port}/api/plan/{PRESENT_PLAN_ID}/state",
            {GUARD_HEADER: GUARD_HEADER_VALUE, "Accept": "application/json"},
        )
        assert after_status == 200, (after_status, after_body)
        assert SUCCESS_STRUCTURE_KEY in json.loads(after_body), after_body

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