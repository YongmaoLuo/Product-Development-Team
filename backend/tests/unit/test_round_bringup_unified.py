"""The round's service precondition — one check, every path.

Background
----------
The 2026-10-26 production round wrote ``services_ensured`` at 02:21:49
and said the ``api`` service was ready on port 64469.  Twelve
``api_test`` verification points ran between 02:24:33 and 02:25:01 and
every one of them was refused the connection.  Each of them was
preceded by a ``binary_freshness_pass`` — a statement about the binary
**on disk**, which is not a statement about the port.

The same URL was verified for twelve minutes by the ``ui_validation``
path and passed.  So the service *was* there, and the ``api_test``
path's own precondition did not know it.

What this file pins:

  1. The round's service is **still listening** when the round starts
     dispatching: the shared precondition finds a service that died
     while the round waited for quota, brings it back, and confirms it
     over HTTP with the very request a verification point would send —
     200, ``application/json``, ``status: ok``.
  2. A service that cannot be brought back blocks the verification
     point: no request is sent, and the verdict is BLOCKED — never
     FAILED, because a refused connection says nothing about the
     product.
  3. The ``api_test`` path and the ``ui_validation`` path share that
     one precondition — neither carries a check of its own.
  4. The loop's round-level preflight asks the same question the same
     way, so the two gates cannot disagree.

Hermeticity
-----------
Everything the test owns lives inside ``tmp_path``.  The only network
traffic is loopback traffic to a real HTTP server the test starts and
stops itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import server  # noqa: F401 — seeds sys.modules['server'] before the import
import verification_agent as va
import verification_loop as vl


# ---------------------------------------------------------------------------
# A real HTTP service
# ---------------------------------------------------------------------------


class _Service:
    """A real HTTP service that answers ``/health`` the way the PRD says.

    200, ``application/json``, and a body whose ``status`` is ``ok`` —
    the three facts the acceptance criterion names.  It is a real
    socket server because the point of the precondition is that it
    travels the same road a verification point's request travels.

    ``port`` may be given so the test can bind a service to a port it
    has already used — that is how "the service died and was started
    again on the same port" is expressed.
    """

    def __init__(self, port: int = 0, body_status: str = "ok"):
        self.body_status = body_status
        self.requests: List[Dict[str, Any]] = []
        service = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 — the handler API's own name
                service.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                })
                raw = json.dumps(
                    {"status": service.body_status},
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # noqa: A003 — silence the log
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _agent(tmp_path: Path, port: int):
    """An agent whose round declares one ``api`` service on *port*."""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )
    agent._normalize_service_declarations({
        "services": [{
            "name": "api",
            "port": port,
            "start_cmd": "python -m api.server",
            "cwd": ".",
            "health_url": f"http://127.0.0.1:{port}/health",
        }],
        "verification_points": [],
    })
    agent.start_verification_round(1)
    return agent


def _runtime_map(port: int, pid: int = 4242, ready: bool = True):
    """What the project's own service manager reports for a service."""
    import service_manager as sm

    return sm.ServiceRuntimeMap(runtimes={
        "api": sm.ServiceRuntime(
            name="api", port=port, pid=pid, origin="spawned", ready=ready,
            start_cmd="x",
        ),
    })


def _api_vp(vp_id: str, port: int, method: str) -> Dict[str, Any]:
    """A verification point that talks to the declared service."""
    return {
        "id": vp_id,
        "title": f"check {method}",
        "verification_method": method,
        "method": method,
        "priority": 1,
        "related_prd_criteria": [],
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/health",
            "headers": {},
            "timeout_seconds": 5,
        },
    }


# ---------------------------------------------------------------------------
# 1. The round's service is still listening when the round starts
# ---------------------------------------------------------------------------


def test_the_rounds_service_is_still_listening_when_the_verification_round_starts(
    tmp_path: Path, monkeypatch,
):
    """A service that died while the round waited is back before it dispatches.

    The 2026-10-26 round's api service was ready at 02:21:49 and gone
    by 02:24:33, when its first ``api_test`` verification point asked
    it for ``/health``.  The precondition every path shares now finds
    the dead port, brings the service up through the round's own start
    path, and then asks the service **itself** — over HTTP, with the
    request a verification point would send.
    """
    service = _Service()
    port = service.port
    agent = _agent(tmp_path, port)

    # The service dies while the round waits for quota.
    service.close()

    restarted: List[_Service] = []

    def _ensure(project, declarations, round_number=0, **kwargs):
        # The round's own start path brings the service back — on the
        # same port, because nothing else took it.
        started = _Service(port=port)
        restarted.append(started)
        return _runtime_map(port, pid=os.getpid())

    import service_manager as sm

    monkeypatch.setattr(sm, "ensure_services", _ensure)
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    try:
        outcome = agent.ensure_service_ready(
            agent._service_declarations["api"], round_token="round-1",
        )
    finally:
        for started in restarted:
            started.close()

    assert outcome["ready"] is True, outcome
    # The three facts the acceptance criterion names.
    assert outcome["status_code"] == 200, outcome
    assert outcome["content_type"] == "application/json", outcome
    assert outcome["body_status"] == "ok", outcome
    # The service was brought back, not merely probed.
    assert len(restarted) == 1, restarted
    # The round's own token owns the instance it just started: a
    # service recorded without one belongs to nobody, and the first
    # reaper that comes along may stop it.
    assert sm.read_owners(agent.plan_dir).get("current_owner") == "round-1", (
        sm.read_owners(agent.plan_dir)
    )


def test_the_service_is_asked_over_http_the_way_a_verification_point_asks(
    tmp_path: Path, monkeypatch,
):
    """The self-check travels the same road the request will travel.

    Same client, same headers — a service that would refuse the
    verification point's request is refused here too, so the gate
    cannot pass a request the round is about to fail.
    """
    service = _Service()
    port = service.port
    agent = _agent(tmp_path, port)
    try:
        outcome = agent.ensure_service_ready(
            agent._service_declarations["api"],
        )
    finally:
        service.close()

    assert outcome["ready"] is True, outcome
    paths = [entry["path"] for entry in service.requests]
    assert "/health" in paths, service.requests
    headers = {
        name.lower(): value
        for entry in service.requests
        for name, value in entry["headers"].items()
    }
    assert headers.get("x-pdt-request") == "1", headers


# ---------------------------------------------------------------------------
# 2. A service that cannot be brought back blocks the verification point
# ---------------------------------------------------------------------------


def test_a_service_that_cannot_be_brought_back_blocks_the_verification_point(
    tmp_path: Path, monkeypatch,
):
    """No request is sent, and the verdict is BLOCKED — never FAILED.

    A refused connection is a statement about the plumbing.  A FAILED
    verdict would say "the project answered wrong", and the project was
    never asked.
    """
    dead = _Service()
    port = dead.port
    dead.close()
    agent = _agent(tmp_path, port)

    import service_manager as sm

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, declarations, round_number=0, **kwargs: (
            _runtime_map(port, ready=False)
        ),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    outcome = agent.ensure_service_ready(
        agent._service_declarations["api"],
    )
    assert outcome["ready"] is False, outcome
    assert outcome["error_kind"] == "tcp_connect_failed", outcome

    blocked = asyncio.run(
        agent.ensure_vp_services_ready(
            "VP-001", _api_vp("VP-001", port, "api_test"),
        ),
    )
    assert blocked is not None, "a dead service must block its verification point"
    assert blocked["status"] == "BLOCKED", blocked


# ---------------------------------------------------------------------------
# 3. The api_test path and the ui_validation path share one precondition
# ---------------------------------------------------------------------------


def test_the_api_test_path_and_the_ui_validation_path_share_one_precondition(
    tmp_path: Path, monkeypatch,
):
    """Neither path carries a check of its own.

    The 2026-10-26 round had two paths and two answers: the
    ``ui_validation`` path reached the service and the ``api_test``
    path did not.  Two paths, two checks, two answers.
    """
    service = _Service()
    port = service.port
    agent = _agent(tmp_path, port)
    try:
        asked: List[str] = []

        async def _gate(vp_id, vp, **kwargs):
            asked.append(str(
                vp.get("verification_method") or vp.get("method") or ""
            ))
            return {
                "status": "BLOCKED",
                "reasons": ["the shared precondition refused this point"],
                "actual_result": "the shared precondition refused this point",
                "evidence": {"vp_id": vp_id},
                "framework_checks": [],
            }

        monkeypatch.setattr(agent, "ensure_vp_services_ready", _gate)

        executor = agent._build_verification_executor(plan_data={
            "verification_points": [
                _api_vp("VP-001", port, "api_test"),
                _api_vp("VP-002", port, "ui_validation"),
            ],
        })
        sub_runner = executor.sub_agent_runner

        api_verdict = asyncio.run(sub_runner({
            "id": "VP-001", "method": "api_test",
            "request": {"method": "GET", "url": f"http://127.0.0.1:{port}/health"},
        }))
        ui_verdict = asyncio.run(sub_runner({
            "id": "VP-002", "method": "ui_validation",
            "request": {"method": "GET", "url": f"http://127.0.0.1:{port}/health"},
        }))
    finally:
        service.close()

    assert asked == ["api_test", "ui_validation"], asked
    assert api_verdict["status"] == "BLOCKED", api_verdict
    assert ui_verdict["status"] == "BLOCKED", ui_verdict


# ---------------------------------------------------------------------------
# 4. The loop's round-level gate asks the same question
# ---------------------------------------------------------------------------


def test_the_verification_loop_preflight_asks_the_same_question(
    tmp_path: Path, monkeypatch,
):
    """The round-level gate is the same precondition, not a second one.

    The loop's preflight is the gate before a round's first
    verification point; the per-point precondition is the gate before
    each point.  Two gates that ask the question differently are two
    gates that can disagree — and a round that dispatches points at a
    service it has already refused is exactly what happened.
    """
    dead = _Service()
    port = dead.port
    dead.close()
    agent = _agent(tmp_path, port)

    import service_manager as sm

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, declarations, round_number=0, **kwargs: (
            _runtime_map(port, ready=False)
        ),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    asked: List[str] = []
    real = agent.ensure_service_ready

    def _spy(decl, *args, **kwargs):
        asked.append(str(getattr(decl, "name", "")))
        return real(decl, *args, **kwargs)

    monkeypatch.setattr(agent, "ensure_service_ready", _spy)

    failure = vl._run_service_preflight(
        agent, agent.plan_dir.name, agent.plan_dir,
    )

    assert failure is not None, "the round would have run against a dead port"
    assert asked == ["api"], asked
    assert failure["error_kind"] == "tcp_connect_failed", failure


# ---------------------------------------------------------------------------
# 5. The environment-blocked re-run gate asks the same question
# ---------------------------------------------------------------------------


def test_the_environment_blocked_rerun_asks_the_service_the_same_way(
    tmp_path: Path, monkeypatch,
):
    """The re-run gate is the one precondition, not a probe of its own.

    A round whose points were blocked by an unreachable service gets
    one more look: the service is brought back and asked again.  That
    look is the same question the round's other gates ask — and it has
    to be asked **by the same code**, or the re-run may hand back a
    service the round's own precondition would have refused.
    """
    service = _Service()
    port = service.port
    agent = _agent(tmp_path, port)

    # The round's re-ensure brought the service back on its port.
    agent._ensure_declared_services = lambda targets: _runtime_map(
        port, pid=os.getpid(),
    )

    asked: List[Any] = []
    real = agent._service_health_check

    def _spy(decl, port_asked):
        asked.append((str(getattr(decl, "name", "")), int(port_asked)))
        return real(decl, port_asked)

    monkeypatch.setattr(agent, "_service_health_check", _spy)

    try:
        healthy = agent._ensure_and_probe_blocked_services(
            {"verification_points": [_api_vp("VP-001", port, "api_test")]},
            ["VP-001"],
        )
    finally:
        service.close()

    assert healthy is True, "the service answers; the re-run may go ahead"
    assert ("api", port) in asked, (
        "the re-run gate must ask the service through the one "
        f"precondition; asked={asked!r}"
    )
