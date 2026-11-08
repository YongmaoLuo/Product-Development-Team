"""VP-007's acceptance closure: ``GET /`` on a *relaunched* service.

The failure this pins
---------------------
VP-007's criterion is the **default path's** contract: ``GET /`` answers
**200**, with a ``Content-Type`` of ``text/html`` (a ``charset`` parameter
is allowed), and a body carrying the literal ``<title>Spec-Driven Dev
System</title>``.  This is the criterion behind PRD decision point 4
("默认 claude 路径零变化") together with constraint 2 — the root path is
the one a browser navigates to, so it must keep working exactly as
before, on its own, with no special request shape.

In round 3 the api_test request for this endpoint came back ``URLError
[Errno 61] Connection refused``: the service (pid 38271) had been
SIGTERM'd at 01:25:25, seven and a half minutes before the request at
01:32:58.  What survived for this endpoint was VP-010's side observation
— a ``curl`` to ``/`` on the *recovered* service that answered 200 and
carried a title.  That is a *旁证*: a neighbouring observation, not the
criterion.  The framework's own assertions never ran, and nothing in
the round ever asked whether the answer was ``text/html`` — a 200 with a
JSON body, or with the SPA shell served under the wrong media type,
would have satisfied every line anyone had written down.

What this test does
-------------------
It reproduces that timeline against the **real** backend server (the same
``python server.py`` the plan's declaration starts) and asserts the
criterion where it was never asserted:

  1. the service comes up on a declared port and answers ``/`` with the
     homepage — 200, ``text/html``, the title literal — so the
     criterion is *observable* before anything is broken;
  2. the same request **without** the ``X-PDT-Request`` guard header
     still answers 200, and an ``/api/`` path without it is still
     refused with 403.  That pair is what makes "默认 claude 路径零变化"
     load-bearing rather than decoration: the root is exempt from the
     guard *because the guard is on*, and a browser navigation — which
     cannot attach a custom header — is what depends on that;
  3. the service is killed with SIGTERM, exactly as the cross-plan sweep
     killed it, and the port is confirmed free;
  4. the api_test VP runs anyway, and must be graded **PASSED** — the
     framework's own relaunch (Leg B of the VP-001 fix) puts the service
     back **on the same port** before the request;
  5. the response is read out of the audit artifact the runner wrote:
     200, ``Content-Type: text/html``, and a body carrying the title
     literal — the three halves of the criterion, from the artifact the
     framework itself wrote, not from a side observation;
  6. a sibling static asset still resolves after the relaunch, and the
     process answering the port is the *new* one.

Point 5 is the part the earlier round never had.  Point 6 is the part
this endpoint is easy to break: the root is served by a route that sits
*before* the frontend catch-all, so a change to the relaunch path — one
that touches how the frontend directory is mounted — can quietly turn
``/`` into a 404 or into a JSON answer while every ``/api/`` endpoint
keeps passing.  The catch-all is checked here for the same reason: the
criterion is about the *default path*, and the default path is a static
page plus the assets it loads.

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


#: The title the criterion names, verbatim.  Not a regex, not a prefix:
#: the criterion says the response *contains* this literal.
ROOT_TITLE = "<title>Spec-Driven Dev System</title>"

#: The media type the criterion names.  A ``charset`` parameter is
#: allowed; a different media type is not, and a response with no
#: content type at all must not pass either.
ROOT_MEDIA_TYPE = "text/html"

#: The guard header every ``/api/*`` request must carry, and the value
#: it must carry.  Copied from :mod:`request_guard` rather than imported:
#: the test asserts what the *criterion* says, and a typo in the
#: framework's constant must not silently become the expectation.
GUARD_HEADER = "X-PDT-Request"
GUARD_HEADER_VALUE = "1"

#: A static asset the homepage loads, served by the frontend catch-all
#: rather than by the root route.  Its job here is to be a *control*:
#: if the relaunch broke the way static paths can break — the catch-all
#: swallowed by a mount, the frontend root resolved somewhere else —
#: this one stops answering too.
SIBLING_ASSET = "app.js"


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

    ``text/html; charset=utf-8`` -> ``text/html``.  The criterion allows
    the charset parameter; it does not allow a different media type, and
    a response that carried no content type at all must not pass either.
    """
    return content_type.split(";", 1)[0].strip().lower()


def _start_cmd(port: int, plans_dir: Path) -> str:
    """The plan's own ``start_cmd`` shape, pointed at this test's paths.

    Copied from the plan's ``services`` block (service ``api``) so the
    thing under test is the same thing the round started: the real
    backend server, told which port to bind and which plans tree to use.
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


def _vp_007(port: int) -> Dict[str, Any]:
    """VP-007 as the plan declares it — the criterion is not ours to move.

    The request carries only ``Accept: text/html``, which is the shape
    the failing round used.  The root path is not under ``/api/``, so it
    is not behind the guard header — a browser navigation to ``/`` cannot
    attach a custom header, and that exemption is exactly what decision
    point 4 pins down.
    """
    return {
        "id": "VP-007",
        "title": "默认路径返回首页 HTML",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/",
            "headers": {"Accept": "text/html"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {
                "name": "Content-Type 是 text/html",
                "header": "content-type",
                "contains": ROOT_MEDIA_TYPE,
            },
            {
                "name": "响应体包含首页标题",
                "body_contains": ROOT_TITLE,
            },
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_root_html_after_relaunch(tmp_path, monkeypatch):
    """``GET /`` must really be asserted on the relaunched service."""
    from verification_agent import VerificationAgent

    work = tmp_path / "pdt-verify-claude"
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True)

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
        start_cmd=_start_cmd(port, plans_dir),
        cwd="backend",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=60,
        log_path=str(work / "api_server.log"),
        reap_on_exit=True,
    )

    # --- 1. the service comes up and the criterion is observable -------
    runtime_map = ensure_services(REPO_ROOT, {"api": declaration})
    runtime = runtime_map.runtimes["api"]
    assert runtime.ready, runtime.detail
    first_pid = runtime.pid
    assert first_pid

    agent = None
    try:
        status, headers, body = _http_get(
            f"http://127.0.0.1:{port}/", {"Accept": "text/html"},
        )
        assert status == 200, (status, body[:200])
        assert _media_type(headers.get("content-type", "")) == ROOT_MEDIA_TYPE, headers
        assert ROOT_TITLE in body, body[:400]

        # --- 2. the guard is on, and the root is exempt from it --------
        # The root path answers *without* the guard header, because a
        # browser navigation cannot attach one.  That is the "默认 claude
        # 路径零变化" half of the criterion.
        naked_status, naked_headers, naked_body = _http_get(
            f"http://127.0.0.1:{port}/", {"Accept": "text/html"},
        )
        assert naked_status == 200, (naked_status, naked_body[:200])
        assert _media_type(naked_headers.get("content-type", "")) == ROOT_MEDIA_TYPE, naked_headers
        assert ROOT_TITLE in naked_body, naked_body[:400]

        # ... and the guard is not simply switched off: an ``/api/`` path
        # without the header is still refused.  Without this control,
        # "the root answers without a header" could also be true of a
        # server whose guard had been dropped entirely.
        denied_status, _, _ = _http_get(
            f"http://127.0.0.1:{port}/api/plans", {"Accept": "application/json"},
        )
        assert denied_status == 403, denied_status

        # --- 3. the service dies, the way the cross-plan sweep killed it
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

        # --- 4. the verification point runs, and must pass --------------
        verdict = agent._run_api_test_vp(_vp_007(port))

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-007"
        assert event_type == "service_relaunched_before_vp"
        assert data["port"] == port
        assert data["old_pid"] == first_pid
        assert data["new_pid"] not in (None, first_pid)

        # --- 5. the endpoint's own answer, read from the audit artifact
        artifact_path = (
            plan_dir / "vp_artifacts" / "VP-007" / "api_response.json"
        )
        assert artifact_path.exists(), "the runner wrote no audit artifact"
        artifact = json.loads(artifact_path.read_text())
        assert artifact["status_code"] == 200, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]

        # The framework itself checked the content type — the half the
        # side observation never looked at.  Its own record says so.
        passed = artifact["assertions_passed"]
        assert any(
            "header 'content-type'" in line for line in passed
        ), passed
        assert any("body:" in line for line in passed), passed

        # The header is text/html; a charset parameter is allowed.
        content_type = artifact["response_headers"].get("content-type", "")
        assert _media_type(content_type) == ROOT_MEDIA_TYPE, content_type

        # The body carries the title literal.  The audit artifact keeps
        # an excerpt of the body, and the title sits near the top of the
        # page — so it is in the excerpt even though the whole homepage
        # is longer than the excerpt limit.
        assert ROOT_TITLE in artifact["response_body"], artifact["response_body"][:400]

        # --- 6. the service is back, and the default path still serves -
        # The process answering the port is the *new* one.  Without this
        # the test would still pass if some stranger had grabbed the port
        # between the kill and the request.
        probe = probe_port(port)
        assert probe.listening
        assert data["new_pid"] in probe.pids, (data, probe.pids)

        # The root still answers with the homepage after the relaunch.
        after_status, after_headers, after_body = _http_get(
            f"http://127.0.0.1:{port}/", {"Accept": "text/html"},
        )
        assert after_status == 200, (after_status, after_body[:200])
        assert _media_type(after_headers.get("content-type", "")) == ROOT_MEDIA_TYPE, after_headers
        assert ROOT_TITLE in after_body, after_body[:400]

        # The sibling asset the homepage loads still resolves: the
        # frontend catch-all is intact after the relaunch.  This is the
        # half that a broken static mount takes down with it.
        asset_status, _, asset_body = _http_get(
            f"http://127.0.0.1:{port}/{SIBLING_ASSET}", {"Accept": "*/*"},
        )
        assert asset_status == 200, (asset_status, asset_body[:200])
        assert asset_body.strip(), "the sibling asset came back empty"
    finally:
        if agent is not None:
            current = agent._service_runtime_map
            if current is not None:
                for entry in (current.runtimes or {}).values():
                    _kill_service(getattr(entry, "pid", None))
        _kill_service(first_pid)