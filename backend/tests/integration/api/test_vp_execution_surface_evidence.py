"""The execution surface of a failed api_test: who graded it, what was alive.

The failure this pins
---------------------
A round graded nine api_test VPs with "connection refused" and nothing
else.  The report said *what* the request did; it never said *who* was
running the round, nor *what was listening* at the moment the request
went out.  So a round that was graded by a **different copy of the
framework** — one that did not contain the fixes under repair — looked
exactly like a round whose service had been killed between the phase
that started it and the phase that asked for it.

Those need different repairs, and the report has to be able to tell
them apart:

  * the service never came up;
  * the service was killed between the phases;
  * the code that graded the VP is not the code under test.

What this test does
-------------------
It drives the real api_test path with a request that cannot succeed —
nothing is listening on the port — and asserts that the verdict names
all three facts:

  1. an ``execution_surface`` section: the framework file, the two
     HEADs, and whether the framework lives inside the project tree;
  2. a ``service_liveness`` section: what was listening on the port at
     request time, and how long ago the services were ensured;
  3. an ``api_phase_service_snapshot`` event recorded **before** the
     request, so the round log carries the same facts even when nobody
     reads the verdict;
  4. a ``verification_executor_provenance`` event at executor
     construction, naming the tree the round is running from.

The status of a verdict that never reached the service
------------------------------------------------------
A request that never reached its service is a statement about the
**plumbing**, not about the product's behaviour, and main's rule says
so: ``_classify_transport_failure`` turns a verdict whose transport
errored — and which carries no ``status_code``, so nothing answered —
into ``BLOCKED`` with ``error_kind = service_unreachable``.

So the verdict below is still the thing an operator reads, and every
evidence cell is still booked on it; but its **status** is ``BLOCKED``:
the environment was not ready, the round is re-run rather than
repaired, and the deliverable is not implicated.  The three-way failure
class travels with it, because a report reader still has to tell
"never started" from "died before the request" from "the request
itself failed".
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from service_declaration import ServiceDeclaration  # noqa: E402
from service_manager import ServiceRuntime, ServiceRuntimeMap  # noqa: E402


#: The fields the provenance event must carry.  Fixed by contract: a
#: reader of the round log has to be able to tell *which tree* graded
#: the round without running anything.
PROVENANCE_FIELDS = {
    "framework_file",
    "framework_root",
    "framework_head",
    "project_dir",
    "project_head",
    "same_tree",
    "pid",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _vp_001(port: int) -> Dict[str, Any]:
    """VP-001 as the plan declares it — the criterion is not ours to move."""
    return {
        "id": "VP-001",
        "title": "默认后端服务的健康探针可用",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/health",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 5,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {"name": "健康状态为 ok", "json_path": "$.status", "equals": "ok"},
        ],
    }


def _vp_002() -> Dict[str, Any]:
    """VP-002 as the plan declares it — the criterion is not ours to move.

    The URL is the **placeholder** the plan writes
    (``{{svc.api.url}}/health/data``), because the port is declared once
    and re-resolved every round.
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


class _FakeDataPlane:
    """A service that answers ``/health`` and ``/health/data`` itself.

    It stands in for the backend server, because what this test is
    about is what the framework *records* about a data-plane field, not
    what the server does — and a stand-in makes all three observation
    states reachable without a database.

    ``health_data`` is the body ``/health/data`` answers with; the
    declared health endpoint answers ``{"status": "ok"}`` unless
    ``health`` says otherwise, and ``health_status`` says with which
    status code it answers.
    """

    def __init__(
        self,
        health_data: Dict[str, Any],
        health: Optional[Dict[str, Any]] = None,
        health_status: int = 200,
        data_raw: Optional[str] = None,
    ) -> None:
        self._health_data = dict(health_data)
        self._health = (
            dict(health) if health is not None else {"status": "ok"}
        )
        self._health_status = int(health_status)
        self._data_raw = data_raw
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
                if self.path.startswith("/health/data"):
                    if outer._data_raw is not None:
                        body = outer._data_raw.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    payload = outer._health_data
                    status = 200
                else:
                    payload = outer._health
                    status = outer._health_status
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _HangupListener:
    """A socket that listens, answers the readiness probe, and hangs up.

    It is a *listener* without being a service: the port answers, and
    the request still cannot succeed.  That is what the liveness probe
    sees, and it is what the snapshot has to record — a listener pid
    that is real, not a placeholder.

    The readiness probe is the one request that **is** answered: the
    round's service gate checks the declared health endpoint before it
    lets a request out, and a listener that hangs up on that one too
    would never reach the request path at all — the gate would block
    first, and the criterion's own failure would never be observed.
    So the readiness probe answers ``200`` on :attr:`ANSWERED_PATH`,
    and every other path — the criterion's own URL — hangs up.
    """

    #: The one path this listener answers.
    ANSWERED_PATH = "/readyz"

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                try:
                    conn.settimeout(1.0)
                    head = conn.recv(1024).decode("latin-1", "replace")
                except OSError:
                    head = ""
                if f" {self.ANSWERED_PATH} " in head:
                    body = b'{"status":"ok"}'
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._sock.close()
        except OSError:
            pass


def _collect_events(agent: Any, monkeypatch: Any) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Record every freshness event the agent logs."""
    events: List[Tuple[str, str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        agent,
        "_safe_log_freshness_event",
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        ),
    )
    return events


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_api_verdict_names_the_execution_surface(tmp_path, monkeypatch):
    """A failed api_test must name who graded it and what was alive."""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # Nothing is listening on this port: the request cannot succeed, so
    # the verdict is the thing an operator has to read.
    port = _free_port()

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    events = _collect_events(agent, monkeypatch)

    verdict = agent._run_api_test_vp(_vp_001(port))

    # --- 1. the request failed, and the verdict says so ----------------
    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert verdict["status"] == "BLOCKED", verdict
    # The verdict names the **plumbing**, not the product: the request
    # never reached the service, so the environment was not ready.
    assert "service_unreachable" in verdict["reasons"][0], verdict["reasons"]

    reasons = "\n".join(verdict["reasons"])

    # --- 2. the execution surface: which code graded this VP -----------
    assert "execution_surface" in reasons, verdict["reasons"]
    assert "same_tree=" in reasons, verdict["reasons"]
    assert "framework_head=" in reasons, verdict["reasons"]
    assert "project_head=" in reasons, verdict["reasons"]

    surface = verdict["evidence"]["execution_surface"]
    assert set(surface) == PROVENANCE_FIELDS, surface
    # The framework that graded this VP is the code under test: it lives
    # inside the project tree, and both trees are the same commit.
    assert surface["same_tree"] is True, surface
    assert surface["framework_head"] != "unknown", surface
    assert surface["framework_head"] == surface["project_head"], surface
    assert surface["pid"] == os.getpid(), surface

    # --- 3. the service liveness: what was alive at request time -------
    assert "service_liveness" in reasons, verdict["reasons"]
    assert "listener_pid" in reasons, verdict["reasons"]
    liveness = verdict["evidence"]["service_liveness"]
    assert "services" in liveness, liveness
    assert "seconds_since_services_ensured" in liveness, liveness

    # --- 4. the snapshot, recorded before the request ------------------
    snapshot_events = [
        event for event in events if event[1] == "api_phase_service_snapshot"
    ]
    assert len(snapshot_events) == 1, events
    vp_id, event_type, data = snapshot_events[0]
    assert vp_id == "VP-001"
    assert event_type == "api_phase_service_snapshot"
    assert "services" in data, data
    assert "seconds_since_services_ensured" in data, data

    executed = [
        index for index, event in enumerate(events)
        if event[1] == "api_test_executed"
    ]
    assert executed, events
    assert events.index(snapshot_events[0]) < executed[0], events

    # --- 5. the provenance of the executor ----------------------------
    agent._build_verification_executor({})
    provenance_events = [
        event for event in events
        if event[1] == "verification_executor_provenance"
    ]
    assert len(provenance_events) == 1, events
    _vp, _event, provenance = provenance_events[0]
    assert set(provenance) == PROVENANCE_FIELDS, provenance
    assert provenance["same_tree"] is True, provenance
    assert provenance["framework_head"] == provenance["project_head"], provenance


@pytest.mark.integration
def test_api_snapshot_names_the_listener_pid_of_a_live_service(
    tmp_path, monkeypatch,
):
    """The liveness section must name the process that is really listening."""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    listener = _HangupListener()
    try:
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        declaration = ServiceDeclaration(
            name="api",
            port=listener.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{listener.port}/readyz",
            ready_timeout_seconds=5,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent._service_declarations = {"api": declaration}
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api": ServiceRuntime(
                    name="api",
                    port=listener.port,
                    declared_port=listener.port,
                    pid=os.getpid(),
                    origin="adopted",
                    ready=True,
                )
            }
        )
        events = _collect_events(agent, monkeypatch)

        verdict = agent._run_api_test_vp(_vp_001(listener.port))

        # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
        # 的问题）。见本文件头的说明。
        assert verdict["status"] == "BLOCKED", verdict
        liveness = verdict["evidence"]["service_liveness"]
        service = liveness["services"]["api"]
        assert service["port"] == listener.port, service
        # The listener is this very process, and the probe has to say so.
        assert service["listener_pid"] == os.getpid(), service
        assert service["known_pid"] == os.getpid(), service

        reasons = "\n".join(verdict["reasons"])
        assert "listener_pid=" in reasons, verdict["reasons"]
        assert str(os.getpid()) in reasons, verdict["reasons"]

        snapshot_events = [
            event for event in events if event[1] == "api_phase_service_snapshot"
        ]
        assert len(snapshot_events) == 1, events
        _vp, _event, data = snapshot_events[0]
        assert data["services"]["api"]["listener_pid"] == os.getpid(), data
    finally:
        listener.close()


def _agent_with_service(plan_dir: Path, declaration: Any) -> Any:
    """A verification agent whose round owns exactly one declared service."""
    from verification_agent import VerificationAgent

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    agent._service_declarations = {declaration.name: declaration}
    agent._service_runtime_map = ServiceRuntimeMap(
        runtimes={
            declaration.name: ServiceRuntime(
                name=declaration.name,
                port=declaration.port,
                declared_port=declaration.port,
                pid=os.getpid(),
                origin="adopted",
                ready=True,
            )
        }
    )
    return agent


@pytest.mark.integration
def test_health_data_not_observed_is_labelled(tmp_path, monkeypatch):
    """A data-plane field that was not observed must say *why* it was not.

    The gap this pins
    -----------------
    VP-002 grades the **data plane**: ``GET {{svc.api.url}}/health/data``
    must answer 200, ``status`` must be ``ok``, and the answer must
    carry ``plan_routing_rows``.  When that field was missing, the
    report said only "the request failed" — and "the service was dead",
    "the endpoint does not carry the field" and "the round was grading
    the wrong tree" all produced the same sentence.

    So the framework now takes one **read-only** reachability
    registration of the declared health endpoint before the request,
    and records — separately from the probe — whether the field was
    observed:

      * ``observed`` — the field was seen, with its value;
      * ``not-observed(service-unreachable)`` — nothing answered;
      * ``not-observed(field-absent)`` — something answered, and the
        field was not in it.

    The registration is a **diagnosis, not an assertion**: the VP's own
    200 + ``status == ok`` + ``plan_routing_rows`` assertions remain the
    only basis for a PASSED.  Scenario A is the negative control for
    that: the probe sees a perfectly healthy service, and the VP still
    fails, because the field is not in the answer.
    """
    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. the service answers, but the endpoint carries no field ----
    service = _FakeDataPlane({"status": "ok"})
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{service.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)
        events = _collect_events(agent, monkeypatch)

        verdict = agent._run_api_test_vp(_vp_002())

        # The VP's own criterion failed: the field is not in the answer.
        assert verdict["status"] == "FAILED", verdict

        observation = verdict["evidence"]["plan_routing_rows_observation"]
        assert observation["field"] == "plan_routing_rows", observation
        assert observation["status"] == "not-observed(field-absent)", observation
        assert observation["value"] is None, observation

        # The registration is a separate entry: it says the service was
        # up, which is exactly what makes "the field is missing" a
        # different fault from "the service was dead".
        probe = verdict["evidence"]["health_url_probe"]
        assert probe["performed"] is True, probe
        assert probe["service"] == "api", probe
        assert probe["health_url"] == (
            f"http://127.0.0.1:{service.port}/health"
        ), probe
        assert probe["reachable"] is True, probe
        assert probe["status_code"] == 200, probe
        assert probe["error"] is None, probe

        # The probe saw a healthy service and the VP still failed: the
        # registration never stands in for the criterion.
        assert verdict["status"] == "FAILED", verdict

        probe_events = [
            event for event in events if event[1] == "data_plane_probe"
        ]
        assert len(probe_events) == 1, events
        _vp, _event, data = probe_events[0]
        assert data["reachable"] is True, data
    finally:
        service.close()

    # --- B. nothing answers at all -----------------------------------
    dead_port = _free_port()
    dead_declaration = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=1,
        log_path=str(tmp_path / "api-dead.log"),
        reap_on_exit=True,
    )
    dead_agent = _agent_with_service(plan_dir, dead_declaration)
    dead_events = _collect_events(dead_agent, monkeypatch)

    dead_verdict = dead_agent._run_api_test_vp(_vp_002())

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert dead_verdict["status"] == "BLOCKED", dead_verdict
    dead_observation = dead_verdict["evidence"]["plan_routing_rows_observation"]
    assert dead_observation["status"] == (
        "not-observed(service-unreachable)"
    ), dead_observation
    assert dead_observation["value"] is None, dead_observation

    dead_probe = dead_verdict["evidence"]["health_url_probe"]
    assert dead_probe["performed"] is True, dead_probe
    assert dead_probe["reachable"] is False, dead_probe
    assert dead_probe["status_code"] is None, dead_probe
    assert dead_probe["error"], dead_probe

    dead_probe_events = [
        event for event in dead_events if event[1] == "data_plane_probe"
    ]
    assert len(dead_probe_events) == 1, dead_events

    # --- C. the service carries the field ----------------------------
    healthy = _FakeDataPlane({"status": "ok", "plan_routing_rows": 7})
    try:
        healthy_declaration = ServiceDeclaration(
            name="api",
            port=healthy.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{healthy.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api-healthy.log"),
            reap_on_exit=True,
        )
        healthy_agent = _agent_with_service(plan_dir, healthy_declaration)

        healthy_verdict = healthy_agent._run_api_test_vp(_vp_002())

        # The VP's own assertions decided this one — not the probe.
        assert healthy_verdict["status"] == "PASSED", healthy_verdict
        healthy_observation = (
            healthy_verdict["evidence"]["plan_routing_rows_observation"]
        )
        assert healthy_observation["status"] == "observed", healthy_observation
        assert healthy_observation["value"] == 7, healthy_observation
    finally:
        healthy.close()


@pytest.mark.integration
def test_health_probe_cannot_substitute_for_the_criterion(tmp_path, monkeypatch):
    """The registration must never stand in for the VP's own criterion.

    The negative control for the data-plane cell: the declared health
    endpoint answers **with** the field, while the VP's own endpoint
    answers **without** it.  Were the observation decided by the probe,
    this would be reported as ``observed`` — and a data plane that never
    carried the field would be reported as readable.

    It is not.  The label comes from the VP's own response; the probe
    only explains.
    """
    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    service = _FakeDataPlane(
        health_data={"status": "ok"},
        health={"status": "ok", "plan_routing_rows": 99},
    )
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{service.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)

        verdict = agent._run_api_test_vp(_vp_002())

        # The VP's own criterion failed: its answer has no field.
        assert verdict["status"] == "FAILED", verdict
        observation = verdict["evidence"]["plan_routing_rows_observation"]
        assert observation["status"] == "not-observed(field-absent)", observation
        assert observation["value"] is None, observation

        # The probe *did* see the field — so the label above was not
        # produced by the probe's absence, and the probe's presence did
        # not overwrite it either.
        probe = verdict["evidence"]["health_url_probe"]
        assert probe["performed"] is True, probe
        assert probe["reachable"] is True, probe
        assert "plan_routing_rows" in probe["body_excerpt"], probe
    finally:
        service.close()


@pytest.mark.integration
def test_health_registration_is_skipped_when_it_must_not_leave_the_machine(
    tmp_path, monkeypatch,
):
    """A registration the framework must not make is recorded as skipped.

    Two ways it comes to that, and both have to be *recorded* rather
    than silently attempted:

      * the declared health URL points at another machine — the plan is
        generated content, and the framework does not make outbound
        calls on its word;
      * no declared service covers the VP at all — there is nothing to
        register.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. a health URL that is not this machine ----------------------
    # 192.0.2.0/24 is reserved for documentation (RFC 5737).  Were the
    # guard ever to regress, the registration would try to reach it and
    # this test would say so, instead of quietly calling a third party.
    service = _FakeDataPlane({"status": "ok", "plan_routing_rows": 3})
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url="http://192.0.2.1/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)

        verdict = agent._run_api_test_vp(_vp_002())

        probe = verdict["evidence"]["health_url_probe"]
        assert probe["performed"] is False, probe
        assert probe["reason"] == "health-url-not-loopback", probe
        assert probe["reachable"] is False, probe
        assert probe["status_code"] is None, probe

        # Skipping the registration changed nothing about the verdict:
        # the VP's own assertions still decided it.
        assert verdict["status"] == "PASSED", verdict
        observation = verdict["evidence"]["plan_routing_rows_observation"]
        assert observation["status"] == "observed", observation
        assert observation["value"] == 3, observation
    finally:
        service.close()

    # --- B. no declared service covers the VP -------------------------
    bare_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )

    bare_verdict = bare_agent._run_api_test_vp(_vp_001(_free_port()))

    bare_probe = bare_verdict["evidence"]["health_url_probe"]
    assert bare_probe["performed"] is False, bare_probe
    assert bare_probe["reason"] == "no-declared-service", bare_probe
    assert bare_probe["service"] is None, bare_probe

    # The same, for a VP whose URL is a placeholder: there is no port
    # to look up either, and that has to be the same answer.
    placeholder_verdict = bare_agent._run_api_test_vp(_vp_002())
    placeholder_probe = placeholder_verdict["evidence"]["health_url_probe"]
    assert placeholder_probe["performed"] is False, placeholder_probe
    assert placeholder_probe["reason"] == "no-declared-service", placeholder_probe


@pytest.mark.integration
def test_a_truncated_data_plane_answer_is_not_reported_as_field_absent(
    tmp_path, monkeypatch,
):
    """A body the framework could not parse is not a body without the field.

    The api runner keeps an excerpt of the answer, and a big answer is
    truncated in the middle.  The field may well be in the part that is
    still there — so "the answer was not JSON" must not be reported as
    "the endpoint does not carry the field", which is a different fault
    with a different repair.
    """
    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # A truncated JSON object: the field name is there, the object is
    # not parseable.
    service = _FakeDataPlane(
        health_data={"status": "ok"},
        data_raw='{"plan_routing_rows": 5, "status": "ok"',
    )
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{service.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)

        verdict = agent._run_api_test_vp(_vp_002())

        observation = verdict["evidence"]["plan_routing_rows_observation"]
        assert observation["status"] == "observed", observation
        # The value is unknown — the answer was not parseable — but the
        # field was in it, and saying "absent" would be a wrong fault.
        assert observation["value"] is None, observation
    finally:
        service.close()


@pytest.mark.integration
def test_health_endpoint_answering_404_is_still_a_reachable_service(
    tmp_path, monkeypatch,
):
    """An error status is an **answer**: the service is up, the URL is wrong.

    A 4xx from the health endpoint means the endpoint answered, so the
    service is reachable — and reporting that as
    ``not-observed(service-unreachable)`` would be a lie.  The
    registration has to tell "the service is down" and "the service
    answered with an error" apart.
    """
    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    service = _FakeDataPlane(
        health_data={"status": "ok", "plan_routing_rows": 3},
        health={"detail": "not found"},
        health_status=404,
    )
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{service.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)

        verdict = agent._run_api_test_vp(_vp_002())

        probe = verdict["evidence"]["health_url_probe"]
        assert probe["performed"] is True, probe
        assert probe["reachable"] is True, probe
        assert probe["status_code"] == 404, probe
        assert probe["error"] is None, probe

        # The VP was graded on its own endpoint, not on the 404.
        assert verdict["status"] == "PASSED", verdict
        observation = verdict["evidence"]["plan_routing_rows_observation"]
        assert observation["status"] == "observed", observation
        assert observation["value"] == 3, observation
    finally:
        service.close()
# ---------------------------------------------------------------------------
# The failure class — which of the three faults a failed api_test was
# ---------------------------------------------------------------------------


#: The three classes a failed ``api_test`` can have.  The class is
#: decided by three facts the framework already holds: the round's
#: **ready record** for the service, the **listener pid** the api-phase
#: snapshot found at request time, and whether the **request itself**
#: raised.
FAILURE_CLASS_NEVER_STARTED = "never-started"
FAILURE_CLASS_DIED_BEFORE_REQUEST = "died-before-request"
FAILURE_CLASS_REQUEST_FAILED = "request-failed"

#: The five fields the ``/api/system/active`` contract carries.  The
#: scheduler depends on them, so a report that says "the request
#: failed" without saying which of the five were seen says nothing at
#: all.
SYSTEM_ACTIVE_FIELDS = [
    "execution_running",
    "verification_running",
    "total_active",
    "details",
    "paused_executions",
]


def _vp_003(port: int) -> Dict[str, Any]:
    """VP-003 as the criterion states it — the criterion is not ours to move.

    Each of the five fields is asserted **by path**, one by one: a body
    that merely mentions a name is not what passes this.
    """
    return {
        "id": "VP-003",
        "title": "系统总览接口满足五字段契约",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/api/system/active",
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
        ],
    }


class _FakeSystemActive:
    """A service that answers ``/api/system/active`` with the five fields.

    It stands in for the backend server, because what this test is
    about is what the framework *records* about the contract, not what
    the server does.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = dict(payload)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
                body = json.dumps(outer._payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.mark.integration
def test_failure_class_three_way(tmp_path, monkeypatch):
    """A failed api_test must say which of the three faults it was.

    The gap this pins
    -----------------
    A round graded a VP with "connection refused" and nothing else.  The
    sentence is the same for three faults that have three different
    repairs:

      * ``never-started`` — the round never brought the service up;
      * ``died-before-request`` — the service **was** up and died
        before the request went out (the 12:03 SIGTERM);
      * ``request-failed`` — the service is listening and the request
        still failed.

    The class is decided by three facts the framework already has: the
    round's **ready record** for the service, the **listener pid** in
    the snapshot taken at request time, and whether the **request
    itself** raised.  The basis travels with the class, because the
    class alone cannot be checked afterwards.
    """
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. never-started: the round never had a service there -------
    bare = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    verdict = bare._run_api_test_vp(_vp_001(_free_port()))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert verdict["status"] == "BLOCKED", verdict
    assert verdict["evidence"]["failure_class"] == (
        FAILURE_CLASS_NEVER_STARTED
    ), verdict["evidence"]
    basis = verdict["evidence"]["failure_class_basis"]
    assert basis["service_ready"] is False, basis
    assert basis["listener_pid"] is None, basis
    # The request itself failed — that is the fact the class rests on
    # next to the ready record and the listener pid.
    assert basis["request_error"], basis
    assert basis["response_received"] is False, basis
    assert "failure_class" in "\n".join(verdict["reasons"]), verdict["reasons"]

    # --- B. died-before-request: the service was up and is gone ------
    dead_port = _free_port()
    declaration = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        # The service cannot serve this port whatever the round does,
        # so the relaunch fails fast instead of waiting for a readiness
        # that will never come.
        ready_timeout_seconds=0,
        log_path=str(tmp_path / "api-dead.log"),
        reap_on_exit=True,
    )
    dead_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    dead_agent._service_declarations = {"api": declaration}
    dead_agent._service_runtime_map = ServiceRuntimeMap(
        runtimes={
            "api": ServiceRuntime(
                name="api",
                port=dead_port,
                declared_port=dead_port,
                # The pid the round last saw.  The process is gone and
                # the port it answered on is free again.
                pid=4242,
                origin="spawned",
                ready=True,
            )
        }
    )
    died = dead_agent._run_api_test_vp(_vp_001(dead_port))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert died["status"] == "BLOCKED", died
    assert died["evidence"]["failure_class"] == (
        FAILURE_CLASS_DIED_BEFORE_REQUEST
    ), died["evidence"]
    died_basis = died["evidence"]["failure_class_basis"]
    # The round *had* the service up — and there is nothing listening
    # on the port the request uses now.
    assert died_basis["service_ready"] is True, died_basis
    assert died_basis["listener_pid"] is None, died_basis
    assert died_basis["service"] == "api", died_basis
    assert died_basis["port"] == dead_port, died_basis

    # --- C. request-failed: the service is listening, the request fails
    listener = _HangupListener()
    try:
        live_declaration = ServiceDeclaration(
            name="api",
            port=listener.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{listener.port}/readyz",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api-live.log"),
            reap_on_exit=True,
        )
        live_agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        live_agent._service_declarations = {"api": live_declaration}
        live_agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api": ServiceRuntime(
                    name="api",
                    port=listener.port,
                    declared_port=listener.port,
                    pid=os.getpid(),
                    origin="adopted",
                    ready=True,
                )
            }
        )
        failed = live_agent._run_api_test_vp(_vp_001(listener.port))

        # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
        # 的问题）。见本文件头的说明。
        assert failed["status"] == "BLOCKED", failed
        assert failed["evidence"]["failure_class"] == (
            FAILURE_CLASS_REQUEST_FAILED
        ), failed["evidence"]
        failed_basis = failed["evidence"]["failure_class_basis"]
        # The service is there — a listener pid is a real process on the
        # port — and the request still failed.  That is a third fault,
        # and it must not be reported as "the service was gone".
        assert failed_basis["service_ready"] is True, failed_basis
        assert failed_basis["listener_pid"] == os.getpid(), failed_basis
        assert failed_basis["request_error"], failed_basis
    finally:
        listener.close()


@pytest.mark.integration
def test_the_five_field_contract_is_recorded_field_by_field(
    tmp_path, monkeypatch,
):
    """The contract the scheduler depends on must be recorded, field by field.

    The gap this pins
    -----------------
    VP-003 grades the ``/api/system/active`` **contract**: 200,
    ``application/json``, and the five fields — ``execution_running``,
    ``verification_running``, ``total_active``, ``details`` and
    ``paused_executions`` — the scheduler reads.  When the request was
    refused, the report said "the request failed" and stopped: five
    fields, zero observations, and no list saying which five were
    missed.

    So the framework records the contract **field by field**:

      * when the response carried them, each field is recorded with its
        **type** — the contract is about shapes;
      * when they were not observed, they are listed under
        ``not_observed`` rather than left out, because an entry that is
        simply absent cannot be told from a field that was never
        looked for.

    The criterion itself is not moved: the observations never decide a
    verdict.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. the service answers: every field is recorded with its type
    service = _FakeSystemActive({
        "execution_running": 0,
        "verification_running": 1,
        "total_active": 1,
        "details": [
            {
                "type": "verification",
                "plan_id": "plan-under-repair",
                "status": "running",
                "round": 1,
            }
        ],
        "paused_executions": [],
    })
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{service.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)

        verdict = agent._run_api_test_vp(_vp_003(service.port))

        assert verdict["status"] == "PASSED", verdict
        observation = verdict["evidence"]["contract_observation"]
        assert observation["fields"] == SYSTEM_ACTIVE_FIELDS, observation
        assert observation["not_observed"] == [], observation
        assert observation["observed"] == {
            "execution_running": "int",
            "verification_running": "int",
            "total_active": "int",
            "details": "list",
            "paused_executions": "list",
        }, observation
        # The default side pins no backend, so the contract cell carries
        # no source: the side that pins no backend keeps behaving
        # exactly as it did.
        assert "source" not in observation, observation
    finally:
        service.close()

    # --- B. nothing answers: the five fields are listed, not dropped --
    bare_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    dead_verdict = bare_agent._run_api_test_vp(_vp_003(_free_port()))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert dead_verdict["status"] == "BLOCKED", dead_verdict
    dead_observation = dead_verdict["evidence"]["contract_observation"]
    assert dead_observation["observed"] == {}, dead_observation
    assert dead_observation["not_observed"] == SYSTEM_ACTIVE_FIELDS, (
        dead_observation
    )
    assert dead_observation["fields"] == SYSTEM_ACTIVE_FIELDS, dead_observation
    # The same fault the failure class names, and the two agree.
    assert dead_verdict["evidence"]["failure_class"] == (
        FAILURE_CLASS_NEVER_STARTED
    ), dead_verdict["evidence"]


# ---------------------------------------------------------------------------
# The negative expectation — a guard that was never asked is not a guard
# that held
# ---------------------------------------------------------------------------


def _vp_004(port: int) -> Dict[str, Any]:
    """VP-004 as the plan declares it — the criterion is not ours to move.

    The request guard answers ``403`` to a request that does not carry
    ``X-PDT-Request``, and the answer has to **name** the header that is
    missing.  That is the PRD's criterion (decision point 4, constraint
    2): the refactoring must not have weakened the guard that was
    already there.
    """
    return {
        "id": "VP-004",
        "title": "缺少 X-PDT-Request 的 /api/plans 请求被守卫拒绝",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/api/plans",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 5,
        },
        "assertions": [
            {"name": "状态码 403", "status": 403},
            {"name": "响应体点名缺失的请求头", "body_contains": "X-PDT-Request"},
        ],
    }


class _FakeGuard:
    """A service that answers the way the guard is configured to answer.

    ``status`` is the status it answers with and ``body`` what the
    answer says, so every fault this test needs is reachable without a
    real server: the guard held, the guard was bypassed, and the guard
    answered without naming the header that was missing.

    The **readiness probe** is the one request that is not the guard's
    business: the round's service gate checks the declared health
    endpoint before it lets a criterion's request out, and a fake that
    answered it with 403 would be blocked at the gate — the criterion
    would never be asked, and the fault this test is about would never
    be observed.  So ``/readyz`` answers ``200`` and every other path
    answers the way the guard is configured to answer.
    """

    #: The one path this fake answers as a healthy service.
    HEALTH_PATH = "/readyz"

    def __init__(self, status: int, body: str) -> None:
        self._status = int(status)
        self._body = body
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
                if self.path == outer.HEALTH_PATH:
                    body = b'{"status":"ok"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = outer._body.encode("utf-8")
                self.send_response(outer._status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _agent_for_guard(plan_dir: Path, guard: Any) -> Any:
    """An agent whose round owns exactly the service the guard answers on."""
    declaration = ServiceDeclaration(
        name="api",
        port=guard.port,
        start_cmd="true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{guard.port}/readyz",
        ready_timeout_seconds=1,
        log_path=str(plan_dir / "api.log"),
        reap_on_exit=True,
    )
    return _agent_with_service(plan_dir, declaration)


@pytest.mark.integration
def test_negative_expectation_not_observed_is_not_pass(tmp_path, monkeypatch):
    """A guard that was never asked is not a guard that held.

    The gap this pins
    -----------------
    VP-004 grades a **negative** criterion: a request without
    ``X-PDT-Request`` must be rejected with ``403``, and the answer has
    to name the header that is missing.  The round that produced the
    evidence this repairs graded the VP with "connection refused" — the
    service had been SIGTERMed between the phases — and the report said
    nothing about what the VP was *expecting*.  So "the request failed"
    reads the same whether the guard held, was bypassed, or was never
    asked at all.

    So the framework books the negative expectation down:

      * ``expected_status`` — what the VP's own assertions expect (403);
      * ``observed`` — one of ``observed``,
        ``not-observed(service-unreachable)``,
        ``not-observed(status-mismatch)``,
        ``not-observed(body-absent)``;
      * ``not_observed`` — the body texts the VP requires that were not
        seen, listed rather than dropped.

    "No response" is never "the guard held": the verdict stays FAILED
    and the expectation is recorded as *not observed*.  The criterion
    itself is not moved — the VP's own assertions still decide it, and
    they still pass when the guard really answers 403.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. nothing answered: the expectation was never observed -----
    bare = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    verdict = bare._run_api_test_vp(_vp_004(_free_port()))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert verdict["status"] == "BLOCKED", verdict
    expectation = verdict["evidence"]["negative_expectation"]
    assert expectation["expects_error_status"] is True, expectation
    assert expectation["expected_status"] == 403, expectation
    # No response at all: the status was not seen, and the text the
    # criterion demands must not vanish from the report either.
    assert expectation["observed_status"] is None, expectation
    assert expectation["observed"] == "not-observed(service-unreachable)", (
        expectation
    )
    assert expectation["not_observed"] == ["X-PDT-Request"], expectation
    assert expectation["body_targets"] == ["X-PDT-Request"], expectation

    reasons = "\n".join(verdict["reasons"])
    assert "expected_status=403" in reasons, verdict["reasons"]
    assert "observed=not-observed(service-unreachable)" in reasons, (
        verdict["reasons"]
    )
    assert "X-PDT-Request" in reasons, verdict["reasons"]

    # --- B. the guard held: 403, and the answer names the header -----
    guard = _FakeGuard(403, json.dumps({
        "detail": "missing X-PDT-Request header",
    }))
    try:
        agent = _agent_for_guard(plan_dir, guard)

        held = agent._run_api_test_vp(_vp_004(guard.port))

        # The VP's own criterion holds: the guard really rejected the
        # request and really named the header.
        assert held["status"] == "PASSED", held
        held_expectation = held["evidence"]["negative_expectation"]
        assert held_expectation["observed"] == "observed", held_expectation
        assert held_expectation["observed_status"] == 403, held_expectation
        assert held_expectation["not_observed"] == [], held_expectation
    finally:
        guard.close()

    # --- C. the guard was bypassed: 200 instead of 403 ---------------
    bypassed = _FakeGuard(200, json.dumps([]))
    try:
        agent = _agent_for_guard(plan_dir, bypassed)

        leaked = agent._run_api_test_vp(_vp_004(bypassed.port))

        # The guard answered — 200 instead of 403 — so the criterion was
        # **falsified**: this is a FAILED, not a BLOCKED.  Only a request
        # that never reached the service is BLOCKED.
        assert leaked["status"] == "FAILED", leaked
        leaked_expectation = leaked["evidence"]["negative_expectation"]
        assert leaked_expectation["observed"] == (
            "not-observed(status-mismatch)"
        ), leaked_expectation
        assert leaked_expectation["observed_status"] == 200, leaked_expectation
        assert leaked_expectation["not_observed"] == ["X-PDT-Request"], (
            leaked_expectation
        )
    finally:
        bypassed.close()

    # --- D. the guard answered, but lost the hint it used to give ---
    silent = _FakeGuard(403, json.dumps({"detail": "forbidden"}))
    try:
        agent = _agent_for_guard(plan_dir, silent)

        degraded = agent._run_api_test_vp(_vp_004(silent.port))

        # The guard answered (403) — the criterion was falsified by the
        # answer, so this is a FAILED, not a BLOCKED.
        assert degraded["status"] == "FAILED", degraded
        degraded_expectation = degraded["evidence"]["negative_expectation"]
        # The status was there — the guard rejected the request — but the
        # answer no longer names the header, and that difference must not
        # be swallowed.
        assert degraded_expectation["observed_status"] == 403, (
            degraded_expectation
        )
        assert degraded_expectation["observed"] == (
            "not-observed(body-absent)"
        ), degraded_expectation
        assert degraded_expectation["not_observed"] == ["X-PDT-Request"], (
            degraded_expectation
        )
    finally:
        silent.close()

    # --- E. the text sits past the excerpt the framework keeps -------
    padded = "x" * 5000 + " missing X-PDT-Request header"
    late = _FakeGuard(403, padded)
    try:
        agent = _agent_for_guard(plan_dir, late)

        truncated = agent._run_api_test_vp(_vp_004(late.port))

        # The runner graded the **whole** body and the guard really held.
        # The framework keeps an excerpt of the answer, and an excerpt
        # that was cut is not a reason to overturn the verdict — the
        # bookkeeping may say "not observed", but it may not invent a
        # failure the criterion never had.
        assert truncated["status"] == "PASSED", truncated
        excerpt_expectation = truncated["evidence"]["negative_expectation"]
        assert excerpt_expectation["observed"] == (
            "not-observed(body-absent)"
        ), excerpt_expectation
        assert excerpt_expectation["not_observed"] == ["X-PDT-Request"], (
            excerpt_expectation
        )
        assert "truncated" in excerpt_expectation["detail"], (
            excerpt_expectation
        )
    finally:
        late.close()

    # --- F. the request never went out: the bookkeeping is still there
    dead_port = _free_port()
    dead_declaration = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        # The service cannot serve this port whatever the round does, so
        # the relaunch fails fast instead of waiting for a readiness that
        # will never come — and the request is never made.
        ready_timeout_seconds=0,
        log_path=str(tmp_path / "api-dead.log"),
        reap_on_exit=True,
    )
    dead_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    dead_agent._service_declarations = {"api": dead_declaration}
    dead_agent._service_runtime_map = ServiceRuntimeMap(
        runtimes={
            "api": ServiceRuntime(
                name="api",
                port=dead_port,
                declared_port=dead_port,
                # The pid the round last saw.  The process is gone and
                # the port it answered on is free again.
                pid=4242,
                origin="spawned",
                ready=True,
            )
        }
    )

    never = dead_agent._run_api_test_vp(_vp_004(dead_port))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert never["status"] == "BLOCKED", never
    # A cell that only appears when the request went out is a cell that
    # cannot be checked afterwards: this path ends the api phase too.
    never_expectation = never["evidence"]["negative_expectation"]
    assert never_expectation["expected_status"] == 403, never_expectation
    assert never_expectation["observed_status"] is None, never_expectation
    assert never_expectation["observed"] == (
        "not-observed(service-unreachable)"
    ), never_expectation
    assert never_expectation["not_observed"] == ["X-PDT-Request"], (
        never_expectation
    )
    # The detail names what really happened — the request was never made.
    assert "never made" in never_expectation["detail"], never_expectation


# ---------------------------------------------------------------------------
# The response header — a dimension of its own
# ---------------------------------------------------------------------------


#: The header the criterion names.  Written out as a literal rather
#: than imported from the framework: the test asserts what the
#: criterion says, and a typo in the framework's constant must not
#: silently become the expectation.
CONTENT_TYPE_HEADER = "content-type"

#: Markers that identify an evidence field carrying a **side
#: observation** — a fact gathered somewhere else (another VP, a
#: sub-agent, a neighbouring round) and pasted into this VP's verdict.
SIDE_EVIDENCE_MARKERS = (
    "corroborat",
    "side_evidence",
    "side_observation",
    "旁证",
    "other_vp",
    "neighbour",
    "sub_agent",
)


def _side_evidence_fields(node: Any, path: str = "") -> List[str]:
    """Every evidence field that carries a side observation."""
    found: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key).lower()
            if any(marker in name for marker in SIDE_EVIDENCE_MARKERS):
                found.append(f"{path}/{key}")
            found.extend(_side_evidence_fields(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_side_evidence_fields(item, f"{path}[{index}]"))
    return found


def _vp_005(port: Optional[int] = None) -> Dict[str, Any]:
    """VP-005 as the criterion states it — the criterion is not ours to move.

    ``GET /api/plans`` carrying the guard header answers **200**, with a
    ``Content-Type`` of ``application/json`` (a ``charset`` parameter is
    allowed) and a body that is a **JSON array**.

    ``$[0].id`` resolves on a list and on nothing else, so the
    array-ness of the body is asserted with the vocabulary the framework
    has — a JSON *object* (``{"plans": [...]}``) or a JSON *string*
    (``"[]"``) cannot satisfy it.

    The criterion's URL is the **placeholder** the plan writes
    (``{{svc.api.url}}/api/plans``), and that is what this declares
    unless a port is given: the address written out is the form the
    framework is handed when the round could not expand the
    placeholder, and the form the "connection refused" of the round
    under repair had.
    """
    url = (
        "{{svc.api.url}}/api/plans" if port is None
        else f"http://127.0.0.1:{port}/api/plans"
    )
    return {
        "id": "VP-005",
        "title": "计划列表接口返回 JSON 数组",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": url,
            "headers": {
                # The request guard answers 403 without this header, so
                # the criterion names it: the VP must carry it.
                "X-PDT-Request": "1",
                "Accept": "application/json",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {
                "name": "Content-Type 是 application/json",
                "header": CONTENT_TYPE_HEADER,
                "contains": "application/json",
            },
            {
                "name": "响应体是计划对象组成的 JSON 数组",
                "json_path": "$[0].id",
                "exists": True,
            },
        ],
    }


class _FakePlansEndpoint:
    """A service that answers ``/api/plans`` with a chosen content type.

    It stands in for the backend server, because what this test is
    about is what the framework **records** about the header, not what
    the server does.  ``content_type`` is what the answer declares, and
    ``None`` means the answer declares nothing at all.
    """

    def __init__(
        self,
        content_type: Optional[str],
        body: str = '[{"id": "plan-a"}]',
        status: int = 200,
    ) -> None:
        self._content_type = content_type
        self._body = body
        self._status = int(status)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
                body = outer._body.encode("utf-8")
                self.send_response(outer._status)
                if outer._content_type is not None:
                    self.send_header("Content-Type", outer._content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.mark.integration
def test_content_type_not_observed_is_labelled(tmp_path, monkeypatch):
    """A content type that was not observed must say so — and never borrow.

    The gap this pins
    -----------------
    VP-005 grades the plan-list endpoint's **document shape**: the
    request carries the guard header, the answer is ``200``, the
    ``Content-Type`` is ``application/json`` and the body is a JSON
    array.  In the round this repairs the api_test request came back
    ``Connection refused`` — the service had been SIGTERMed minutes
    earlier — and what survived for this endpoint was a **side
    observation**: another VP's answer, on another port, said ``200``
    and ``application/json`` and the body ``[]``.

    That side observation is a 旁证: a neighbouring fact, not the
    criterion.  Written into this VP's evidence it would have made the
    criterion *look* observed for a request that never saw a response
    at all, and the report would have said "the endpoint answers JSON"
    about a round that had never asked.

    So the header is booked down on its own, out of this VP's own
    traffic:

      * ``observed`` — the VP's own response carried the header, with
        its value.  Observing the header is **not** the same as it
        matching the criterion: a ``text/plain`` answer is observed,
        and it is the VP's own assertion that fails;
      * ``not-observed(service-unreachable)`` — the VP's own request
        produced no response, so there was no header to observe;
      * ``not-observed(header-absent)`` — a response came back and it
        carried no header at all.

    The body keeps its own cells: the header is a **dimension** of the
    answer, not a property of its body, and neither cell is derived
    from the other.

    And no field carrying a side observation may appear in the
    evidence: a verdict that says "observed" on the strength of
    somebody else's answer is a verdict nobody can check afterwards.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. the endpoint answers JSON --------------------------------
    service = _FakePlansEndpoint(
        content_type="application/json; charset=utf-8",
    )
    agent = None
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{service.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)

        verdict = agent._run_api_test_vp(_vp_005())

        # The VP's own criterion held: 200, application/json, a JSON
        # array.  The framework asked, and the answer was there.
        assert verdict["status"] == "PASSED", verdict

        observation = verdict["evidence"]["content_type_observation"]
        assert observation["header"] == CONTENT_TYPE_HEADER, observation
        assert observation["status"] == "observed", observation
        assert observation["value"] == "application/json; charset=utf-8", (
            observation
        )

        # The header is a dimension of its own: the body of the very
        # same answer carries no data-plane field, and the two cells say
        # so independently.  Neither was derived from the other.
        assert verdict["evidence"]["plan_routing_rows_observation"][
            "status"
        ] == "not-observed(field-absent)", verdict["evidence"]

        # Nothing in the verdict came from another VP or a sub-agent.
        assert _side_evidence_fields(verdict["evidence"]) == [], (
            verdict["evidence"]
        )
    finally:
        service.close()

    # --- B. the audit copy is missing: the header is unknown ----------
    # The response headers live in the audit artifact the runner
    # writes, and the copy case A just wrote is still on disk — it
    # says ``application/json``.  When this run's copy cannot be
    # written, that stale copy is a neighbour's observation, and the
    # framework must not book it in as if this request had answered
    # with it: "I could not look" is not "it was observed".
    import verification_api_runner

    with monkeypatch.context() as patch:
        patch.setattr(
            verification_api_runner, "_write_artifact",
            lambda *args, **kwargs: None,
        )
        unreadable = _FakePlansEndpoint(
            content_type="application/json; charset=utf-8",
        )
        try:
            declaration = ServiceDeclaration(
                name="api",
                port=unreadable.port,
                start_cmd="true",
                cwd="backend",
                health_url=f"http://127.0.0.1:{unreadable.port}/health",
                ready_timeout_seconds=1,
                log_path=str(tmp_path / "api-unreadable.log"),
                reap_on_exit=True,
            )
            unreadable_agent = _agent_with_service(plan_dir, declaration)

            unreadable_verdict = unreadable_agent._run_api_test_vp(_vp_005())

            # The VP's own criterion held — the runner graded the live
            # response — and the bookkeeping still refuses to claim a
            # header it could not read.
            assert unreadable_verdict["status"] == "PASSED", (
                unreadable_verdict
            )
            unreadable_observation = (
                unreadable_verdict["evidence"]["content_type_observation"]
            )
            assert unreadable_observation["status"] == (
                "not-observed(headers-unreadable)"
            ), unreadable_observation
            assert unreadable_observation["value"] is None, (
                unreadable_observation
            )
        finally:
            unreadable.close()

    # --- C. the endpoint answers, but not JSON ------------------------
    # The header was observed — it says ``text/plain`` — and the VP's
    # own assertion is what fails.  "Observed" and "matching" are two
    # different things, and the cell must not confuse them.
    plain = _FakePlansEndpoint(content_type="text/plain")
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=plain.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{plain.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api-plain.log"),
            reap_on_exit=True,
        )
        plain_agent = _agent_with_service(plan_dir, declaration)

        plain_verdict = plain_agent._run_api_test_vp(_vp_005())

        assert plain_verdict["status"] == "FAILED", plain_verdict
        plain_observation = (
            plain_verdict["evidence"]["content_type_observation"]
        )
        assert plain_observation["status"] == "observed", plain_observation
        assert plain_observation["value"] == "text/plain", plain_observation
    finally:
        plain.close()

    # --- D. the endpoint answers without declaring what it answered --
    # A third fault, and not the second one: the response came back,
    # it just carried no ``Content-Type`` at all.  Saying "the service
    # did not answer" here would be a lie — the endpoint answered.
    silent = _FakePlansEndpoint(content_type=None)
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=silent.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{silent.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api-silent.log"),
            reap_on_exit=True,
        )
        silent_agent = _agent_with_service(plan_dir, declaration)

        silent_verdict = silent_agent._run_api_test_vp(_vp_005())

        assert silent_verdict["status"] == "FAILED", silent_verdict
        silent_observation = (
            silent_verdict["evidence"]["content_type_observation"]
        )
        assert silent_observation["status"] == (
            "not-observed(header-absent)"
        ), silent_observation
        assert silent_observation["value"] is None, silent_observation
    finally:
        silent.close()

    # --- E. nothing answers at all -----------------------------------
    # The round under repair: the service was gone, the request was
    # refused, and the criterion was never observed.  Another VP had
    # seen ``application/json`` on another port — that is a side
    # observation, and it is not this VP's evidence.
    dead_port = _free_port()
    dead_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )

    dead_verdict = dead_agent._run_api_test_vp(_vp_005(dead_port))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert dead_verdict["status"] == "BLOCKED", dead_verdict
    dead_observation = dead_verdict["evidence"]["content_type_observation"]
    assert dead_observation["status"] == (
        "not-observed(service-unreachable)"
    ), dead_observation
    assert dead_observation["value"] is None, dead_observation

    dead_reasons = "\n".join(dead_verdict["reasons"])
    assert "not-observed(service-unreachable)" in dead_reasons, (
        dead_verdict["reasons"]
    )
    assert _side_evidence_fields(dead_verdict["evidence"]) == [], (
        dead_verdict["evidence"]
    )

    # --- F. the observation is this VP's own, never a neighbour's -----
    # The agent that has just seen a real content type now grades a
    # request against a port where nothing answers.  What it saw a
    # moment ago is not this VP's evidence, and it may not be booked
    # in as if it were.
    borrowed = agent._run_api_test_vp(_vp_005(dead_port))
    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert borrowed["status"] == "BLOCKED", borrowed
    borrowed_observation = borrowed["evidence"]["content_type_observation"]
    assert borrowed_observation["status"] == (
        "not-observed(service-unreachable)"
    ), borrowed_observation
    assert borrowed_observation["value"] is None, borrowed_observation

    # --- G. a side observation may be logged, never judged -------------
    kept, dropped = agent._strip_side_evidence({
        "vp_id": "VP-005",
        "content_type_observation": {"status": "observed"},
        "corroborating_evidence": {
            "source": "another VP's answer",
            "content_type": "application/json",
        },
        "nested": {"side_observation": {"content_type": "application/json"}},
    })
    assert dropped == ["corroborating_evidence", "side_observation"], dropped
    assert kept == {
        "vp_id": "VP-005",
        "content_type_observation": {"status": "observed"},
        "nested": {},
    }, kept


# ---------------------------------------------------------------------------
# The error semantics — a 404 that was never seen, and a plan that has to
# stay missing
# ---------------------------------------------------------------------------


#: The plan the criterion asks about: a plan id that does not exist.  It
#: is a valid plan id (the validator allows ``[A-Za-z0-9._-]``), so the
#: request reaches the handler and is answered by the *plan* lookup — not
#: by the id validator's 400.
MISSING_PLAN_ID = "pdt-verify-missing-plan"


def _vp_006(port: Optional[int] = None) -> Dict[str, Any]:
    """VP-006 as the plan declares it — the criterion is not ours to move.

    ``GET {{svc.api.url}}/api/plan/pdt-verify-missing-plan/state`` must
    answer **404**: the caller can still tell "does not exist" from
    "exists" by the status code alone.

    The URL is the **placeholder** the plan writes unless a port is
    given: the placeholder is what made the endpoint unobservable in the
    first place, and the address written out is the form the framework
    is handed when the round could not expand it.
    """
    url = (
        "{{svc.api.url}}/api/plan/" + MISSING_PLAN_ID + "/state"
        if port is None
        else f"http://127.0.0.1:{port}/api/plan/{MISSING_PLAN_ID}/state"
    )
    return {
        "id": "VP-006",
        "title": "默认后端下查询不存在的 plan 仍返回 404",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": url,
            "headers": {
                # The request guard answers 403 without this header, so
                # the criterion names it: the VP must carry it.
                "X-PDT-Request": "1",
                "Accept": "application/json",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "未知 plan 返回 404", "status": 404},
        ],
    }


class _FakeMissingPlanEndpoint:
    """A service that answers a missing plan's state the way it is told.

    ``status`` is the status it answers with, and ``create_plan`` says
    whether the answer arrives **together with the plan it said does not
    exist**.  That second switch is the failure the zero-side-effect
    condition exists to catch: a server that mints an empty plan for an
    unknown id and then answers 404 leaves the caller unable to tell
    "does not exist" from "was just created".
    """

    def __init__(
        self,
        plans_dir: Path,
        status: int = 404,
        create_plan: bool = False,
    ) -> None:
        self._plans_dir = Path(plans_dir)
        self._status = int(status)
        self._create_plan = bool(create_plan)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
                if self.path.rstrip("/") == "/health":
                    # The health endpoint answers for the round's own
                    # readiness probe; the plan's URL is what carries
                    # the criterion.
                    body = json.dumps({"status": "ok"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if outer._create_plan:
                    (outer._plans_dir / MISSING_PLAN_ID).mkdir(
                        parents=True, exist_ok=True,
                    )
                body = json.dumps({"detail": "Plan not found"}).encode("utf-8")
                self.send_response(outer._status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _agent_for_missing_plan(
    plan_dir: Path, port: int, plans_dir: Path,
) -> Any:
    """An agent whose round owns the service that answers for a plan.

    The declaration names the plans tree the service is told to use —
    the same shape the plan's own ``services`` block has — because that
    tree is where a plan's directory would appear, and it is the only
    tree the framework may look in.
    """
    declaration = ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=f"export PDT_PLANS_DIR={plans_dir}; true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=1,
        log_path=str(plan_dir / "api.log"),
        reap_on_exit=True,
    )
    return _agent_with_service(plan_dir, declaration)


@pytest.mark.integration
def test_404_not_observed_is_labelled(tmp_path, monkeypatch):
    """A 404 that was never seen must say so — and the side condition too.

    The gap this pins
    -----------------
    VP-006 grades the **error semantics** of a missing plan: ``GET
    {{svc.api.url}}/api/plan/pdt-verify-missing-plan/state`` must answer
    **404**, so the caller can still tell "does not exist" from "exists"
    by the status code alone.  In the round this repairs the api_test
    request came back ``Connection refused`` — the service had been
    SIGTERMed minutes earlier — and what survived for this endpoint was
    a **replay**: the 404 was observed afterwards, on a copy of the
    service, by somebody else.  The round under repair had observed
    nothing.

    So the error semantics are booked down, on their own:

      * ``negative_expectation`` — what the VP expects (404) and what
        was observed: ``not-observed(service-unreachable)`` when the
        request produced no response at all;
      * ``zero_side_effect_observation`` — the **side condition** the
        criterion leans on: a plan that does not exist must not be
        created by the request that asked for it.  The plans tree is
        snapshotted **before and after** the request, and the
        comparison says whether the missing plan was created.

    And the side condition may **not** pass by default.  "The service
    was unreachable, so nothing was created, so the condition holds" is
    a conclusion drawn from an execution that never happened: it is
    recorded as ``not-observed(service-unreachable)``, with the side
    effect left **unknown** rather than false.  The criterion itself is
    not moved — the VP's own assertions still decide it, and they still
    pass when the endpoint really answers 404.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. nothing answers: the 404 was never observed --------------
    dead_port = _free_port()
    dead_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )

    dead_verdict = dead_agent._run_api_test_vp(_vp_006(dead_port))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert dead_verdict["status"] == "BLOCKED", dead_verdict

    # The error semantics: what the VP expects, and what was seen.
    expectation = dead_verdict["evidence"]["negative_expectation"]
    assert expectation["expects_error_status"] is True, expectation
    assert expectation["expected_status"] == 404, expectation
    assert expectation["observed_status"] is None, expectation
    assert expectation["observed"] == "not-observed(service-unreachable)", (
        expectation
    )

    # The side condition: **not observed** — and unknown, not false.
    side = dead_verdict["evidence"]["zero_side_effect_observation"]
    assert side["condition"] == "no-plan-created", side
    assert side["plan_id"] == MISSING_PLAN_ID, side
    assert side["status"] == "not-observed(service-unreachable)", side
    # The point of the whole cell: "nothing ran" is not "nothing was
    # created".  The side effect stays unknown.
    assert side["side_effect"] is None, side

    dead_reasons = "\n".join(dead_verdict["reasons"])
    assert "expected_status=404" in dead_reasons, dead_verdict["reasons"]
    assert "not-observed(service-unreachable)" in dead_reasons, (
        dead_verdict["reasons"]
    )

    # --- B. the request never went out: the cell is still there -------
    # A service the round cannot bring back is the same fault with a
    # different reason, and a cell that only appears when the request
    # went out is a cell that cannot be checked afterwards.
    never_port = _free_port()
    never_plans_dir = tmp_path / "never-plans"
    never_plans_dir.mkdir(parents=True, exist_ok=True)
    never_declaration = ServiceDeclaration(
        name="api",
        port=never_port,
        start_cmd=f"export PDT_PLANS_DIR={never_plans_dir}; true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{never_port}/health",
        # The service cannot serve this port whatever the round does, so
        # the relaunch fails fast instead of waiting for a readiness that
        # will never come — and the request is never made.
        ready_timeout_seconds=0,
        log_path=str(tmp_path / "api-never.log"),
        reap_on_exit=True,
    )
    never_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    never_agent._service_declarations = {"api": never_declaration}
    never_agent._service_runtime_map = ServiceRuntimeMap(
        runtimes={
            "api": ServiceRuntime(
                name="api",
                port=never_port,
                declared_port=never_port,
                # The pid the round last saw.  The process is gone and
                # the port it answered on is free again.
                pid=4242,
                origin="spawned",
                ready=True,
            )
        }
    )

    never_verdict = never_agent._run_api_test_vp(_vp_006(never_port))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert never_verdict["status"] == "BLOCKED", never_verdict
    never_expectation = never_verdict["evidence"]["negative_expectation"]
    assert never_expectation["expected_status"] == 404, never_expectation
    assert never_expectation["observed"] == (
        "not-observed(service-unreachable)"
    ), never_expectation
    never_side = never_verdict["evidence"]["zero_side_effect_observation"]
    assert never_side["status"] == "not-observed(service-unreachable)", (
        never_side
    )
    assert never_side["side_effect"] is None, never_side
    # The tree the service was told to use is the one that is looked in.
    assert never_side["plans_dir"] == str(never_plans_dir), never_side

    # --- C. the endpoint answers 404, and the plan stays missing -----
    # The tree is this test's own — not the hermetic ``PDT_PLANS_DIR`` the
    # harness points at ``tmp_path/"plans"`` — so the snapshot compares
    # the tree the service was told to read and nothing else.
    plans_dir = tmp_path / "vp006-plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    service = _FakeMissingPlanEndpoint(plans_dir)
    try:
        agent = _agent_for_missing_plan(plan_dir, service.port, plans_dir)

        verdict = agent._run_api_test_vp(_vp_006(service.port))

        # The VP's own criterion held: the endpoint really answered 404.
        assert verdict["status"] == "PASSED", verdict
        held = verdict["evidence"]["negative_expectation"]
        assert held["observed"] == "observed", held
        assert held["observed_status"] == 404, held

        # And the side condition was observed too: the comparison of the
        # two snapshots says the missing plan was left alone.
        held_side = verdict["evidence"]["zero_side_effect_observation"]
        assert held_side["status"] == "observed", held_side
        assert held_side["side_effect"] is False, held_side
        assert held_side["plan_id"] == MISSING_PLAN_ID, held_side
        assert held_side["plans_dir"] == str(plans_dir), held_side
        assert held_side["before"]["exists"] is False, held_side
        assert held_side["after"]["exists"] is False, held_side
        assert not (plans_dir / MISSING_PLAN_ID).exists()
    finally:
        service.close()

    # --- D. the endpoint answers 404 — and creates the plan ----------
    # The 404 is there and it is worthless: the request minted the plan
    # it said does not exist, so the caller cannot tell the two apart.
    creating_plans_dir = tmp_path / "vp006-plans-creating"
    creating_plans_dir.mkdir(parents=True, exist_ok=True)
    creating = _FakeMissingPlanEndpoint(
        creating_plans_dir, create_plan=True,
    )
    try:
        agent = _agent_for_missing_plan(
            plan_dir, creating.port, creating_plans_dir,
        )

        verdict = agent._run_api_test_vp(_vp_006(creating.port))

        assert verdict["status"] == "FAILED", verdict
        leaked_side = verdict["evidence"]["zero_side_effect_observation"]
        assert leaked_side["status"] == "observed", leaked_side
        assert leaked_side["side_effect"] is True, leaked_side
        assert leaked_side["before"]["exists"] is False, leaked_side
        assert leaked_side["after"]["exists"] is True, leaked_side
        reasons = "\n".join(verdict["reasons"])
        assert "zero_side_effect" in reasons, verdict["reasons"]
    finally:
        creating.close()

    # --- E. a criterion that expects a success carries no side --------
    # The condition belongs to the criterion that declares it.  A VP
    # that expects 200 has no "the plan must not be created" clause,
    # and the framework does not invent one.
    ok_service = _FakeMissingPlanEndpoint(plans_dir, status=200)
    try:
        agent = _agent_for_missing_plan(
            plan_dir, ok_service.port, plans_dir,
        )
        ok_vp = _vp_006(ok_service.port)
        ok_vp["assertions"] = [{"name": "状态码 200", "status": 200}]

        ok_verdict = agent._run_api_test_vp(ok_vp)

        assert ok_verdict["status"] == "PASSED", ok_verdict
        assert "zero_side_effect_observation" not in ok_verdict["evidence"], (
            ok_verdict["evidence"]
        )
    finally:
        ok_service.close()


# ---------------------------------------------------------------------------
# The static entry — three halves, three books
# ---------------------------------------------------------------------------


#: The title the criterion names, verbatim.  Not a regex, not a prefix:
#: the criterion says the response *contains* this literal.
STATIC_ENTRY_TITLE = "<title>Spec-Driven Dev System</title>"

#: The media type the criterion names.  A ``charset`` parameter is
#: allowed; a different media type is not, and a response with no
#: content type at all must not pass either.
STATIC_ENTRY_MEDIA_TYPE = "text/html"

#: The three halves of the static entry's criterion.  They are booked
#: down separately, and all three are always present together.
STATIC_ENTRY_DIMENSIONS = ("status_code", "content_type", "title")


def _vp_007(port: Optional[int] = None) -> Dict[str, Any]:
    """VP-007 as the criterion states it — the criterion is not ours to move.

    ``GET {{svc.api.url}}/`` must answer **200**, with a
    ``Content-Type`` of ``text/html`` and a body carrying the title
    literal.  This is the contract behind PRD decision point 4 ("默认
    claude 路径零变化") together with constraint 2: the root path is the
    one a browser navigates to, so it has to keep working exactly as
    before, with no special request shape.

    The URL is the **placeholder** the plan writes unless a port is
    given — the placeholder is what made the endpoint unobservable in
    the first place, and the address written out is the form the
    framework is handed when the round could not expand it.
    """
    url = (
        "{{svc.api.url}}/" if port is None
        else f"http://127.0.0.1:{port}/"
    )
    return {
        "id": "VP-007",
        "title": "静态入口页返回 HTML 文档",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": url,
            "headers": {"Accept": "text/html"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {
                "name": "Content-Type 是 text/html",
                "header": CONTENT_TYPE_HEADER,
                "contains": STATIC_ENTRY_MEDIA_TYPE,
            },
            {
                "name": "响应体包含首页标题",
                "body_contains": STATIC_ENTRY_TITLE,
            },
        ],
    }


class _FakeStaticEntry:
    """A service that answers the root path with a document.

    ``content_type`` is what the answer declares and ``body`` what it
    carries, so every fault this test needs is reachable: the document
    the criterion expects, and a JSON envelope where a page should be.
    """

    def __init__(
        self,
        content_type: Optional[str] = "text/html; charset=utf-8",
        body: str = (
            "<!doctype html><html><head>"
            f"{STATIC_ENTRY_TITLE}"
            "</head><body><div id=\"app\"></div></body></html>"
        ),
        status: int = 200,
    ) -> None:
        self._content_type = content_type
        self._body = body
        self._status = int(status)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
                body = outer._body.encode("utf-8")
                self.send_response(outer._status)
                if outer._content_type is not None:
                    self.send_header("Content-Type", outer._content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.mark.integration
def test_three_items_each_labelled(tmp_path, monkeypatch):
    """The static entry's three halves are booked down one by one.

    The gap this pins
    -----------------
    VP-007 grades the **static UI entry**: ``GET {{svc.api.url}}/`` must
    answer **200**, with a ``Content-Type`` of ``text/html`` and a body
    carrying the title literal.  In the round this repairs the api_test
    request came back ``Connection refused`` — the service had been
    SIGTERMed minutes earlier — and what survived for this endpoint was
    a **side observation**: a browser, on a copy of the service and
    after the fact, saw ``200`` and ``<title>Spec-Driven Dev
    System</title>``.

    That side observation is a 旁证: a neighbouring fact, not the
    criterion.  Written into this VP's evidence it would have made the
    criterion *look* observed for a request that never saw a response
    at all.

    So the static entry's three halves are booked down on their own:

      * ``status_code`` — ``observed`` with the status the VP's **own**
        response carried, or ``not-observed(service-unreachable)`` when
        the request produced no response at all;
      * ``content_type`` — the same two ways, plus the two ways a
        response can come back without saying what it is;
      * ``title`` — the same two ways, plus
        ``not-observed(title-absent)`` for an answer that came back
        without the title.

    All three are in the evidence **at the same time**: a cell that
    only appears when one of them was seen cannot be checked
    afterwards.  And none of them decides the verdict — the criterion
    is unchanged, and "observed" is not "matching".
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. the static entry answers the document the criterion wants
    service = _FakeStaticEntry()
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=service.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{service.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api.log"),
            reap_on_exit=True,
        )
        agent = _agent_with_service(plan_dir, declaration)

        verdict = agent._run_api_test_vp(_vp_007())

        # The VP's own criterion held: 200, text/html, the title.
        assert verdict["status"] == "PASSED", verdict

        observation = verdict["evidence"]["static_entry_observation"]
        # All three halves are there, at the same time.
        for dimension in STATIC_ENTRY_DIMENSIONS:
            assert dimension in observation, observation
        assert observation["status_code"]["status"] == "observed", observation
        assert observation["status_code"]["value"] == 200, observation
        assert observation["content_type"]["status"] == "observed", observation
        assert observation["content_type"]["value"] == (
            "text/html; charset=utf-8"
        ), observation
        assert observation["title"]["status"] == "observed", observation
        assert observation["title"]["value"] == STATIC_ENTRY_TITLE, observation
    finally:
        service.close()

    # --- B. nothing answers: none of the three was observed -----------
    dead_port = _free_port()
    dead_agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )

    dead_verdict = dead_agent._run_api_test_vp(_vp_007(dead_port))

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert dead_verdict["status"] == "BLOCKED", dead_verdict
    dead_observation = dead_verdict["evidence"]["static_entry_observation"]
    for dimension in STATIC_ENTRY_DIMENSIONS:
        assert dimension in dead_observation, dead_observation
        assert dead_observation[dimension]["status"] == (
            "not-observed(service-unreachable)"
        ), (dimension, dead_observation)
        assert dead_observation[dimension]["value"] is None, (
            dimension, dead_observation
        )

    # The report says the same thing, in the same words.
    dead_reasons = "\n".join(dead_verdict["reasons"])
    assert "static_entry_observation" in dead_reasons, dead_verdict["reasons"]
    for dimension in STATIC_ENTRY_DIMENSIONS:
        assert dimension in dead_reasons, dead_verdict["reasons"]
    assert "not-observed(service-unreachable)" in dead_reasons, (
        dead_verdict["reasons"]
    )

    # --- C. the endpoint answered, but not with a document -----------
    # The status and the media type were observed; they are simply not
    # the ones the criterion wants, and the title is not there at all.
    # "Observed" and "matching" are two different things, and the cells
    # must not confuse them.
    envelope = _FakeStaticEntry(
        content_type="application/json",
        body=json.dumps({"detail": "not found"}),
    )
    try:
        declaration = ServiceDeclaration(
            name="api",
            port=envelope.port,
            start_cmd="true",
            cwd="backend",
            health_url=f"http://127.0.0.1:{envelope.port}/health",
            ready_timeout_seconds=1,
            log_path=str(tmp_path / "api-envelope.log"),
            reap_on_exit=True,
        )
        envelope_agent = _agent_with_service(plan_dir, declaration)

        envelope_verdict = envelope_agent._run_api_test_vp(_vp_007())

        # The VP's own criterion failed: no HTML, no title.
        assert envelope_verdict["status"] == "FAILED", envelope_verdict
        envelope_observation = (
            envelope_verdict["evidence"]["static_entry_observation"]
        )
        assert envelope_observation["status_code"]["status"] == "observed", (
            envelope_observation
        )
        assert envelope_observation["status_code"]["value"] == 200, (
            envelope_observation
        )
        assert envelope_observation["content_type"]["status"] == "observed", (
            envelope_observation
        )
        assert envelope_observation["content_type"]["value"] == (
            "application/json"
        ), envelope_observation
        # The title was not in the answer, and that is said plainly.
        assert envelope_observation["title"]["status"] == (
            "not-observed(title-absent)"
        ), envelope_observation
        assert envelope_observation["title"]["value"] is None, (
            envelope_observation
        )
    finally:
        envelope.close()

    # --- D. the observation is this VP's own, never a neighbour's -----
    # The agent has just seen a real document; it now grades a request
    # against a port where nothing answers.  What it saw a moment ago
    # is not this VP's evidence, and it may not be booked in as if it
    # were.
    borrowed = envelope_agent._run_api_test_vp(_vp_007(dead_port))
    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert borrowed["status"] == "BLOCKED", borrowed
    borrowed_observation = borrowed["evidence"]["static_entry_observation"]
    assert borrowed_observation["title"]["status"] == (
        "not-observed(service-unreachable)"
    ), borrowed_observation
    assert borrowed_observation["title"]["value"] is None, borrowed_observation
    # And nothing in the verdict came from another VP or a sub-agent.
    assert _side_evidence_fields(borrowed["evidence"]) == [], (
        borrowed["evidence"]
    )


# ---------------------------------------------------------------------------
# The mcode identity — was the declared backend actually in effect?
# ---------------------------------------------------------------------------


#: The backend the mcode-side declaration pins.  Written as a literal
#: rather than imported: the test asserts what the *declaration* says,
#: and a typo in the framework's constant must not become the
#: expectation.
MCODE_HARNESS = "mcode"

#: The variable that selects the backend.  Named by the criterion, so it
#: is spelled out here too.
HARNESS_ENV_VAR = "PDT_HARNESS"


#: The program a live "service" runs in this test.  It answers
#: ``/health`` with the body the criterion asks for, so what a test
#: reaches it for is the **backend**, never the endpoint.
_LIVE_SERVICE_PROGRAM = """\
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
"""


def _spawn_live_service(
    tmp_path: Path, name: str, harness: Optional[str],
) -> Tuple[Any, int]:
    """A live service on a free port, running under *harness*.

    The child is a **separate process**, so the framework has to read
    its backend out of the operating system: this test never tells the
    framework what the answer is.  ``harness`` is the value of
    ``PDT_HARNESS`` in the child's own environment, or ``None`` for a
    service that pins no backend at all.

    Returns the process and the port it listens on.
    """
    script = tmp_path / f"{name}.py"
    script.write_text(_LIVE_SERVICE_PROGRAM)
    env = dict(os.environ)
    env.pop(HARNESS_ENV_VAR, None)
    if harness is not None:
        env[HARNESS_ENV_VAR] = harness
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE, env=env, text=True,
    )
    port = int(proc.stdout.readline().strip())
    return proc, port


def _claude_declaration(port: int, log_path: Path) -> ServiceDeclaration:
    """The default (claude) side's declaration — it pins no backend."""
    return ServiceDeclaration(
        name="api",
        port=port,
        start_cmd="true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=0,
        log_path=str(log_path),
        reap_on_exit=True,
    )


def _mcode_declaration(port: int, log_path: Path) -> ServiceDeclaration:
    """The mcode side's declaration, as the plan writes it.

    The command is the plan's own shape: the export is the *only*
    difference between "the mcode backend" and "a server that happens
    to answer on that port".  ``ready_timeout_seconds=0`` keeps the
    relaunch attempt cheap — the command exits at once, so the service
    cannot come up on this port whatever the round does.
    """
    return ServiceDeclaration(
        name="api-mcode",
        port=port,
        start_cmd=f"export {HARNESS_ENV_VAR}={MCODE_HARNESS}; true",
        cwd="backend",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=0,
        log_path=str(log_path),
        reap_on_exit=True,
    )


def _vp_008() -> Dict[str, Any]:
    """VP-008 as the criterion states it — the criterion is not ours to move.

    The mcode side's ``GET /health``: 200, and a body whose ``status``
    is ``ok``.  Nothing about the backend the service runs under is in
    here: whether the mcode backend was *actually* in effect is the
    framework's job to establish, and this test is what holds it to
    that.
    """
    return {
        "id": "VP-008",
        "title": "mcode 侧服务健康探针可用",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api-mcode.url}}/health",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {"name": "健康状态为 ok", "json_path": "$.status", "equals": "ok"},
        ],
    }


@pytest.mark.integration
def test_mcode_harness_unobserved_is_explicit(tmp_path, monkeypatch):
    """Whether the mcode backend was in effect must be booked down.

    The gap this pins
    -----------------
    VP-008 grades the **mcode side**: ``GET {{svc.api-mcode.url}}/health``
    must answer 200 with ``status == ok``.  The service it names is
    declared with ``export PDT_HARNESS=mcode``, so the criterion is
    about a particular *backend* — a plain server that happens to
    answer on that port satisfies the endpoint and nothing else.

    The round this repairs graded VP-008 with ``Connection refused``
    and said nothing about the backend: the mcode service was
    SIGTERMed at 12:03:45 and the api_test went out at 12:08:46.  What
    survived was a **replay** — a 200 from an mcode copy somebody else
    restarted on a *different* port afterwards.  The round had observed
    nothing, and its report could not tell "the service was gone" from
    "the backend was not in effect".

    So the framework books the identity down, on its own:

      * the **snapshot** records the ``listener_pid`` of the mcode
        service and the ``PDT_HARNESS`` read out of that very process,
        and writes ``unknown`` when the environment could not be read;
      * the **verdict** says which of the three answers it has:
        ``observed`` (the backend is in effect), ``mismatch`` (it is
        provably not), or ``not-observed(service-unreachable)`` (the
        service was gone, so there is no evidence either way).

    And the third answer may never be dressed up as the first: an mcode
    copy on another port is not this VP's evidence, and "the service
    was unreachable" is not "the backend was in effect".
    """
    from verification_agent import VerificationAgent

    # Nothing in this process may leak a backend into the children: the
    # declarations are the only thing that decides which backend a
    # service runs under.
    monkeypatch.delenv(HARNESS_ENV_VAR, raising=False)

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. the mcode service is gone -------------------------------
    # A live mcode copy on **another** port: that process really is the
    # mcode backend, and it is not this VP's service.  A framework
    # that looked for "is there an mcode process somewhere" would book
    # its environment in as this VP's evidence.
    neighbour_proc, _neighbour_port = _spawn_live_service(
        tmp_path, "neighbour", MCODE_HARNESS,
    )
    try:
        dead_port = _free_port()
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        agent._service_declarations = {
            "api": _claude_declaration(
                _free_port(), tmp_path / "api.log",
            ),
            "api-mcode": _mcode_declaration(
                dead_port, tmp_path / "api-mcode.log",
            ),
        }
        events = _collect_events(agent, monkeypatch)

        verdict = agent._run_api_test_vp(_vp_008())

        assert verdict["status"] == "FAILED", verdict

        # The identity cell: the backend was NOT observed, and the
        # reason why is that there was nothing to observe it on.
        identity = verdict["evidence"]["harness_identity"]
        assert "api-mcode" in identity, identity
        mcode = identity["api-mcode"]
        assert mcode["env_var"] == HARNESS_ENV_VAR, mcode
        assert mcode["declared"] == MCODE_HARNESS, mcode
        assert mcode["listener_pid"] is None, mcode
        assert mcode["observed"] == "unknown", mcode
        assert mcode["status"] == "not-observed(service-unreachable)", mcode

        # The claude side pins no backend, so it contributes no cell at
        # all — the default path keeps behaving exactly as it did.
        assert "api" not in identity, identity

        # The neighbour's mcode copy is not this VP's evidence.
        assert mcode["observed"] != MCODE_HARNESS, mcode
        assert str(neighbour_proc.pid) not in json.dumps(identity), identity

        # The verdict says it in words: there is no evidence this round
        # whether the mcode backend was in effect.
        reasons = "\n".join(verdict["reasons"])
        assert "harness_identity" in reasons, verdict["reasons"]
        assert "not-observed(service-unreachable)" in reasons, (
            verdict["reasons"]
        )
        assert "no evidence" in reasons, verdict["reasons"]
        assert MCODE_HARNESS in reasons, verdict["reasons"]

        # And the snapshot — the record that survives when nobody reads
        # the verdict — carries the same facts.
        snapshot_events = [
            event for event in events
            if event[1] == "api_phase_service_snapshot"
        ]
        assert len(snapshot_events) == 1, events
        _vp, _event, data = snapshot_events[0]
        assert data["services"]["api-mcode"]["listener_pid"] is None, data
        snapshot_identity = data["harness_identity"]["api-mcode"]
        assert snapshot_identity["observed"] == "unknown", data
        assert snapshot_identity["status"] == (
            "not-observed(service-unreachable)"
        ), data
    finally:
        neighbour_proc.terminate()
        neighbour_proc.wait(timeout=5)

    # --- B. the mcode service is alive, and really is the mcode -----
    live_proc, live_port = _spawn_live_service(
        tmp_path, "live", MCODE_HARNESS,
    )
    try:
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        agent._service_declarations = {
            "api": _claude_declaration(
                _free_port(), tmp_path / "api.log",
            ),
            "api-mcode": _mcode_declaration(
                live_port, tmp_path / "api-mcode.log",
            ),
        }
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api-mcode": ServiceRuntime(
                    name="api-mcode",
                    port=live_port,
                    declared_port=live_port,
                    pid=live_proc.pid,
                    origin="spawned",
                    ready=True,
                )
            }
        )
        _collect_events(agent, monkeypatch)

        verdict = agent._run_api_test_vp(_vp_008())

        # The criterion itself held: the endpoint really answered.
        assert verdict["status"] == "PASSED", verdict

        # And the backend it was graded against was read out of the
        # operating system, not asserted by the plan.
        identity = verdict["evidence"]["harness_identity"]
        mcode = identity["api-mcode"]
        assert mcode["status"] == "observed", mcode
        assert mcode["observed"] == MCODE_HARNESS, mcode
        assert mcode["listener_pid"] == live_proc.pid, mcode
    finally:
        live_proc.terminate()
        live_proc.wait(timeout=5)

    # --- C. the mcode service is alive, and is NOT the mcode --------
    # The endpoint answers 200 with ``status == ok`` all the same: the
    # response alone cannot tell the two backends apart.  This is the
    # third answer, and it is not the first one.
    wrong_proc, wrong_port = _spawn_live_service(tmp_path, "wrong", None)
    try:
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        agent._service_declarations = {
            "api-mcode": _mcode_declaration(
                wrong_port, tmp_path / "api-mcode.log",
            ),
        }
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api-mcode": ServiceRuntime(
                    name="api-mcode",
                    port=wrong_port,
                    declared_port=wrong_port,
                    pid=wrong_proc.pid,
                    origin="spawned",
                    ready=True,
                )
            }
        )
        _collect_events(agent, monkeypatch)

        verdict = agent._run_api_test_vp(_vp_008())

        # The backend is provably not in effect: an acceptance failure,
        # not something to paper over.
        assert verdict["status"] == "FAILED", verdict
        identity = verdict["evidence"]["harness_identity"]
        mcode = identity["api-mcode"]
        assert mcode["status"] == "mismatch", mcode
        assert mcode["observed"] is None, mcode
        assert mcode["listener_pid"] == wrong_proc.pid, mcode

        reasons = "\n".join(verdict["reasons"])
        assert "mismatch" in reasons, verdict["reasons"]
        # "not in effect" is not "no evidence": the two are told apart.
        # (The *content type* cell says "not-observed" here — the
        # request was never made — which is a different dimension and a
        # different fact.)
        identity_reasons = [
            str(reason) for reason in verdict["reasons"]
            if "harness_identity" in str(reason)
        ]
        assert len(identity_reasons) == 1, verdict["reasons"]
        assert "not-observed(service-unreachable)" not in identity_reasons[0], (
            identity_reasons
        )
    finally:
        wrong_proc.terminate()
        wrong_proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# VP-009 — the four-field contract, and the backend it came from
# ---------------------------------------------------------------------------


#: The four fields VP-009's criterion names.  ``paused_executions`` is
#: not among them: the mcode side's contract is four fields wide, and
#: the claude side's five-field contract is a different cell.
MCODE_ACTIVE_FIELDS = [
    "execution_running",
    "verification_running",
    "total_active",
    "details",
]


def _vp_009() -> Dict[str, Any]:
    """VP-009 as the criterion states it — the criterion is not ours to move.

    The URL is the **placeholder** the plan writes, the guard header is
    the one the criterion names, and the four fields are asserted **by
    path**, one by one: a body that merely mentions a name is not what
    passes this.
    """
    return {
        "id": "VP-009",
        "title": "mcode 侧系统总览接口满足四字段契约",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api-mcode.url}}/api/system/active",
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
        ],
    }


#: The program a live "mcode service" runs in this test.  It answers
#: the system-overview endpoint with the four fields the criterion
#: names, and the health endpoint the declaration names.  The request
#: guard is real here too: without ``X-PDT-Request`` the endpoint
#: answers 403, exactly as the backend does.
_CONTRACT_SERVICE_PROGRAM = """\
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTRACT = {
    "execution_running": 0,
    "verification_running": 1,
    "total_active": 1,
    "details": [
        {
            "type": "verification",
            "plan_id": "plan-under-repair",
            "status": "running",
            "round": 1,
        }
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.startswith("/api/system/active"):
            if not self.headers.get("X-PDT-Request"):
                body = b'{"detail":"missing X-PDT-Request"}'
                self.send_response(403)
            else:
                body = json.dumps(CONTRACT).encode("utf-8")
                self.send_response(200)
        else:
            body = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
"""


def _spawn_contract_service(
    tmp_path: Path, name: str, harness: Optional[str],
) -> Tuple[Any, int]:
    """A live service answering the four-field contract, under *harness*.

    The child is a **separate process**, so the framework has to read
    the backend out of the operating system: this test never tells the
    framework what the answer is.  ``harness`` is the value of
    ``PDT_HARNESS`` in the child's own environment, or ``None`` for a
    service that pins no backend at all.

    Returns the process and the port it listens on.
    """
    script = tmp_path / f"{name}-contract.py"
    script.write_text(_CONTRACT_SERVICE_PROGRAM)
    env = dict(os.environ)
    env.pop(HARNESS_ENV_VAR, None)
    if harness is not None:
        env[HARNESS_ENV_VAR] = harness
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE, env=env, text=True,
    )
    port = int(proc.stdout.readline().strip())
    return proc, port


@pytest.mark.integration
def test_four_fields_listed_when_unobserved(tmp_path, monkeypatch):
    """The mcode side's four fields, one by one — and where they came from.

    The gap this pins
    -----------------
    VP-009 grades the **mcode side's** system overview:
    ``GET {{svc.api-mcode.url}}/api/system/active`` with the
    ``X-PDT-Request`` guard header must answer 200, ``application/json``,
    and a body carrying ``execution_running``, ``verification_running``,
    ``total_active`` and ``details``.

    The round this repairs graded that VP with ``Connection refused``
    and said nothing about either of the two things a reader needs:

      * **which** of the four fields were seen — the report said "the
        request failed", and a field that is simply absent cannot be
        told from a field that was never looked for;
      * **where** the contract came from — ``/api/system/active`` is
        served by the same server code on both sides, so a body with
        the four fields proves nothing about the backend it came from.

    So the framework records the contract **field by field** *and* says
    which backend it belongs to:

      * the service was unreachable → the four fields are **listed**,
        one by one, under ``not_observed``, and the cell says there is
        no evidence which backend the contract would have come from;
      * the service answered and the listener really is the mcode
        backend → each field is recorded with its **type**, and the
        cell says the contract came from the declared mcode backend;
      * the service answered and the listener is **not** the mcode
        backend → the verdict is FAILED, and the cell says which
        backend answered the port — so the reader learns that the
        contract did not come from the mcode instance.

    The criterion itself is not moved: the observations never decide a
    verdict, and the default claude path — which pins no backend at
    all — keeps behaving exactly as it did.
    """
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification_agent import VerificationAgent

    # Nothing in this process may leak a backend into the children: the
    # declarations are the only thing that decides which backend a
    # service runs under.
    monkeypatch.delenv(HARNESS_ENV_VAR, raising=False)

    plan_dir = tmp_path / "plan-under-repair"
    plan_dir.mkdir()

    # --- A. the mcode service is gone --------------------------------
    # The four fields are listed, one by one, and the cell says that
    # there is no evidence which backend the contract would have come
    # from — the service was not there to be asked.
    dead_port = _free_port()
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )
    agent._service_declarations = {
        "api-mcode": _mcode_declaration(
            dead_port, tmp_path / "api-mcode.log",
        ),
    }

    dead_verdict = agent._run_api_test_vp(_vp_009())

    assert dead_verdict["status"] == "FAILED", dead_verdict
    dead = dead_verdict["evidence"]["contract_observation"]
    assert dead["fields"] == MCODE_ACTIVE_FIELDS, dead
    assert dead["observed"] == {}, dead
    # One by one: a field that is not in the list was never looked for.
    assert dead["not_observed"] == MCODE_ACTIVE_FIELDS, dead
    assert dead["source"]["status"] == (
        "not-observed(service-unreachable)"
    ), dead
    assert dead["source"]["confirmed"] is False, dead
    assert "no evidence" in dead["detail"], dead

    # --- B. the service answers, and it really is the mcode backend --
    live_proc, live_port = _spawn_contract_service(
        tmp_path, "live", MCODE_HARNESS,
    )
    try:
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        agent._service_declarations = {
            "api-mcode": _mcode_declaration(
                live_port, tmp_path / "api-mcode.log",
            ),
        }
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api-mcode": ServiceRuntime(
                    name="api-mcode",
                    port=live_port,
                    declared_port=live_port,
                    pid=live_proc.pid,
                    origin="spawned",
                    ready=True,
                )
            }
        )

        live_verdict = agent._run_api_test_vp(_vp_009())

        # The criterion itself held: the endpoint really answered.
        assert live_verdict["status"] == "PASSED", live_verdict
        live = live_verdict["evidence"]["contract_observation"]
        assert live["not_observed"] == [], live
        # Each field with its type — the contract is about shapes.
        assert live["observed"] == {
            "execution_running": "int",
            "verification_running": "int",
            "total_active": "int",
            "details": "list",
        }, live
        # And the backend those four types came from: the confirmation
        # comes first, and the contract is credited to it.
        assert live["source"]["confirmed"] is True, live
        assert live["source"]["observed"] == MCODE_HARNESS, live
        assert live["source"]["listener_pid"] == live_proc.pid, live
        assert "the contract came from the declared mcode backend" in (
            live["detail"]
        ), live
    finally:
        live_proc.terminate()
        live_proc.wait(timeout=5)

    # --- C. the service answers, and it is NOT the mcode backend ----
    # The endpoint answers 200 with the four fields all the same: the
    # response cannot tell the two backends apart.  The contract may
    # not be credited to the mcode instance anyway — and the cell has
    # to say which backend answered the port.
    wrong_proc, wrong_port = _spawn_contract_service(tmp_path, "wrong", None)
    try:
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        agent._service_declarations = {
            "api-mcode": _mcode_declaration(
                wrong_port, tmp_path / "api-mcode.log",
            ),
        }
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api-mcode": ServiceRuntime(
                    name="api-mcode",
                    port=wrong_port,
                    declared_port=wrong_port,
                    pid=wrong_proc.pid,
                    origin="spawned",
                    ready=True,
                )
            }
        )

        wrong_verdict = agent._run_api_test_vp(_vp_009())

        # The backend is provably not in effect: an acceptance failure,
        # not something to paper over.
        assert wrong_verdict["status"] == "FAILED", wrong_verdict
        wrong = wrong_verdict["evidence"]["contract_observation"]
        assert wrong["observed"] == {}, wrong
        assert wrong["not_observed"] == MCODE_ACTIVE_FIELDS, wrong
        assert wrong["source"]["status"] == "mismatch", wrong
        assert wrong["source"]["confirmed"] is False, wrong
        # The detail says which backend answered the port: the contract
        # did not come from the mcode instance.
        assert "did not come from the declared mcode backend" in (
            wrong["detail"]
        ), wrong
        assert "PDT_HARNESS=None" in wrong["detail"], wrong
    finally:
        wrong_proc.terminate()
        wrong_proc.wait(timeout=5)
