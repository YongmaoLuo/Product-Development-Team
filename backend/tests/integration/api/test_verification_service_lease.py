"""The round's **lease** on its services — and what the round does when it loses it.

The gap this pins
-----------------
2026-10-25, plan ``20261023-PDT-MiniMax-Code-CLI`` (round 1):

  * 21:03:48 — the round's preflight brought the ``api`` service up and
    wrote a lease on it into the plan's ledger
    (``managed_services.json`` → ``services[].lease``), naming the round
    (``<round_id>@<backend_pid>``) and the process that was serving the
    port.
  * 21:05:44 — **another** backend instance, sharing the same
    ``plans/`` directory, ran its shutdown hook and SIGTERM'd that very
    service.  The plan's service-event log recorded the reap with
    ``reason=server_shutdown``.
  * 21:06:17–19 — the round sent nine ``api_test`` requests, one per
    VP, and **every one** came back ``URLError [Errno 61] Connection
    refused``.

The report therefore carried nine identical failures and nothing else.
One round-level accident — a service taken away by a foreign process —
was dressed up as nine independent feature failures, and the repair
loop was pointed at nine "features" that had never been broken.

What was missing is the one question the round never asked: *is my
lease on this service still valid?*  The lease existed (r1-14), but
nothing in the round's api phase ever read it back, so the round could
not tell "my service was taken away" from "my service was never
there" — and both came out as the same sentence.

What this test does
-------------------
It drives the round's own lease bookkeeping, in the plan directory the
round uses, and pins four things:

  1. **the lease is readable and refreshable** — the round's heartbeat
     refreshes the ledger's ``lease.heartbeat_at``, and the refresh
     reaches the api phase (before the request) and the judgment phase,
     not only the preflight;
  2. **the loss is detected** — when the ledger no longer says the round
     owns the service, :meth:`_assert_round_lease` says so, and writes a
     ``round_lease_lost`` event carrying the three facts a reader needs:
     ``last_reap_reason`` (why the service went away), ``expected_pid``
     (the process the round expected to be serving) and
     ``last_heartbeat_at`` (the last time the round said it was still
     here);
  3. **the round stops instead of repeating itself** — the VP that hits
     the loss is **not** answered with a connection-refused conclusion,
     and the VPs after it are stopped rather than run;
  4. **a round that still holds its lease is not aborted** — the check
     has to be quiet when there is nothing wrong, or every round would
     die of its own repair.

And it pins the two things the round's own **event** had to say about
the request it made (2026-10-26):

  5. **the event says who answered** — the ``api_test_executed`` event
     carries a ``service_snapshot`` with the four facts that make the
     request interpretable afterwards: ``port``, ``listener_pid``,
     ``lease_owner`` and ``seconds_since_services_ensured``;
  6. **"no response" is not "the endpoint failed"** — a request to
     **the round's own service** that produced no response at all is
     escalated to a round-level ``api_phase_service_lost``, never
     written down as that verification point's own FAILED verdict, and
     the api phase stops there instead of asking a dead port the same
     question again.  An endpoint the round never brought up keeps its
     own verdict: its silence is its own business.

The foreign reap is reproduced the way it happened: the plan's ledger
is finalised (the entry is gone) and a ``reaped`` event with a reason is
written to the plan's service-event log.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from service_declaration import ServiceDeclaration  # noqa: E402
from service_manager import (  # noqa: E402
    LEDGER_FILENAME,
    ServiceRuntime,
    ServiceRuntimeMap,
    ensure_services,
    make_lease,
    read_ledger,
    read_service_events,
    record_runtime,
    round_lease_context,
)


#: The port the round's ``api`` service is on. Nothing listens on it:
#: these tests never need a live service — they are about the round's
#: bookkeeping, not about the server.
API_PORT = 18994


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    """A round agent over a plan directory of its own."""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
    )


def _lease_the_service(agent, port: int = API_PORT) -> ServiceRuntime:
    """Give the round a service it owns, and a lease on it.

    The lease is written through the real ledger writer, so the round
    reads back exactly what a live round would read back.
    """
    lease = make_lease(
        round_id=agent._round_id(),
        backend_pid=os.getpid(),
        port=port,
        listener_pid=os.getpid(),
        round_pid=os.getpid(),
    )
    runtime = ServiceRuntime(
        name="api",
        port=port,
        pid=os.getpid(),
        start_cmd="python server.py",
        origin="spawned",
        ready=True,
    )
    runtime.lease = lease.to_dict()
    record_runtime(
        agent.plan_dir, agent.plan_dir.name, runtime,
        project_dir=agent.project_dir, round_number=1,
    )
    agent._service_runtime_map = ServiceRuntimeMap(runtimes={"api": runtime})
    return runtime


def _foreign_reap(
    agent, runtime: ServiceRuntime, *, reason: str = "server_shutdown",
) -> None:
    """What another backend's shutdown hook did to this round's service.

    Two things, in this order, exactly as the reaper did them: the
    ``reaped`` event with its reason, and the ledger finalised — the
    entry is gone, so nobody can read the lease back any more.
    """
    from service_manager import record_service_event

    record_service_event(
        agent.plan_dir,
        "reaped",
        round_token="",
        run_id=agent.plan_dir.name,
        name=runtime.name,
        port=runtime.port,
        pid=runtime.pid,
        reason=(
            f"service {runtime.name!r} on port {runtime.port} was stopped "
            f"(reason={reason})"
        ),
    )
    ledger = agent.plan_dir / LEDGER_FILENAME
    if ledger.exists():
        ledger.unlink()


def _lease_heartbeat(agent) -> float:
    """The heartbeat the ledger currently records for this round's lease."""
    ledger = read_ledger(agent.plan_dir)
    beats = [
        float((entry.get("lease") or {}).get("heartbeat_at") or 0.0)
        for entry in ledger.get("services") or []
        if isinstance(entry, dict) and entry.get("lease")
    ]
    return max(beats) if beats else 0.0


def _api_vp(vp_id: str = "VP-003") -> Dict[str, Any]:
    """A plan-written api_test VP: the URL uses the service placeholder."""
    return {
        "id": vp_id,
        "title": "默认后端下系统状态接口的响应契约保持不变",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api.url}}/api/system/active",
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 5,
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
        ],
    }


def _as_text(verdict: Any) -> str:
    """The verdict as one searchable blob."""
    return json.dumps(verdict, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# The listener's identity — "is the process answering the port the one
# this round ensured?"
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_listener(port: int) -> Tuple[subprocess.Popen, int]:
    """A **separate** process listening on ``port``.

    The criterion under test is about *who* answers the port, so the
    answer has to come from a process other than this one: a socket
    opened by the test itself would be answered by the test's own pid,
    and the ledger would have to name this process to be wrong about
    it.  A real second process is the only honest way to say it.

    The listener accepts connections and never answers them — it is a
    port holder, not a server.
    """
    code = (
        "import socket, time\n"
        "sock = socket.socket()\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"sock.bind(('127.0.0.1', {port}))\n"
        "sock.listen(8)\n"
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:  # pragma: no cover - the listener failed to come up
        proc.kill()
        raise AssertionError(f"the listener never came up on port {port}")
    return proc, int(proc.pid)


def _lease_port(agent, port: int, listener_pid: int) -> ServiceRuntime:
    """The ledger says this round's service on ``port`` is served by
    ``listener_pid``.

    Written through the real ledger writer, so the round reads back
    exactly what a live round reads back: a lease naming the process
    that was answering the port when the round ensured the service.
    """
    lease = make_lease(
        round_id=agent._round_id(),
        backend_pid=os.getpid(),
        port=port,
        listener_pid=listener_pid,
        round_pid=os.getpid(),
    )
    runtime = ServiceRuntime(
        name="api",
        port=port,
        pid=listener_pid,
        start_cmd="python server.py",
        origin="spawned",
        ready=True,
    )
    runtime.lease = lease.to_dict()
    record_runtime(
        agent.plan_dir, agent.plan_dir.name, runtime,
        project_dir=agent.project_dir, round_number=1,
    )
    agent._service_runtime_map = ServiceRuntimeMap(runtimes={"api": runtime})
    return runtime


def _record_reap(
    agent, name: str, port: int, pid: int,
    reason: str = "server_shutdown",
) -> None:
    """The plan's own service-event log, as a reaper writes it.

    The ledger is left alone: the case under test is the one where the
    ledger still names a process and the port answers for somebody
    else — the reap that took the service away is the reason, and the
    reason is what the record has to carry.
    """
    from service_manager import record_service_event

    record_service_event(
        agent.plan_dir,
        "reaped",
        round_token="",
        run_id=agent.plan_dir.name,
        name=name,
        port=port,
        pid=pid,
        reason=(
            f"service {name!r} on port {port} was stopped "
            f"(reason={reason})"
        ),
    )


def _own_the_service(agent, port: int, name: str = "api") -> ServiceRuntime:
    """The round brought this service up — and it is gone now.

    The shape of the 2026-10-25 round: the preflight brought the
    service up (the round's runtime map names it, ready) and the
    request that followed found nothing listening on its port.

    The runtime map is the round's **own** record of the services it
    ensured — the same map the ``services_ensured`` event carries — so
    it is what says "this port was the round's service".  No lease is
    written: the ledger held none in that round, which is why the
    lease-based checks stayed quiet and the request went out to a port
    nobody was serving.
    """
    runtime = ServiceRuntime(
        name=name,
        port=port,
        pid=os.getpid(),
        start_cmd="python server.py",
        origin="spawned",
        ready=True,
    )
    agent._service_runtime_map = ServiceRuntimeMap(runtimes={name: runtime})
    return runtime


def _start_http_responder(
    port: int, *, status: int = 404,
    body: str = '{"detail":"not found"}',
    env: Optional[Dict[str, str]] = None,
    new_session: bool = False,
) -> Tuple[subprocess.Popen, int]:
    """A **real** HTTP server on ``port``, answering every request alike.

    Two things are under test here and both need a server that answers:

      * *who* answered the port — so the answer has to come from a
        process other than this one, and a socket opened by the test
        itself would be answered by the test's own pid;
      * the difference between **answered** and **nobody answered** —
        a port holder that never replies cannot tell the two apart.

    So: a real HTTP server, in a real second process.

    ``env`` adds to the server's own environment (it does not replace
    it) and ``new_session`` puts it in a process group of its own — the
    way the round starts its services, and the way a reader can tell
    "this process belongs to the group the round leased" from "this
    process does not".
    """
    code = (
        "import http.server\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        f"        payload = {body!r}.encode()\n"
        f"        self.send_response({status})\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(payload)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(payload)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "    def log_error(self, *args):\n"
        "        pass\n"
        f"http.server.HTTPServer(('127.0.0.1', {port}), Handler)"
        ".serve_forever()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, **(env or {})},
        start_new_session=new_session,
    )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:  # pragma: no cover - the server failed to come up
        proc.kill()
        raise AssertionError(f"the server never came up on port {port}")
    return proc, int(proc.pid)


def _vp_missing_plan_on_port(vp_id: str, port: int) -> Dict[str, Any]:
    """VP-006 as the criterion states it, aimed at a literal address.

    The criterion is a **negative** one: a plan that does not exist
    must be answered with 404 — and with the endpoint's own body, not
    the success structure.  Whether the 404 comes from the instance
    this round ensured is the question this file asks about it.
    """
    return {
        "id": vp_id,
        "title": "不存在的计划状态接口返回 404",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/plan/pdt-verify-missing-plan/state"
            ),
            "headers": {"Accept": "application/json", "X-PDT-Request": "1"},
            "body": None,
            "timeout_seconds": 5,
        },
        "assertions": [
            {"name": "状态码 404", "status": 404},
            {
                "name": "响应体不是成功结构",
                "body_not_contains": "current_phase",
            },
        ],
    }


def _vp_on_port(vp_id: str, port: int) -> Dict[str, Any]:
    """VP-004 as the criterion states it, aimed at a literal address.

    The criterion is not ours to move: a request **without** the guard
    header must be refused with 403, and the refusal must name the
    missing header.  Whether the 403 comes from the instance this round
    ensured is the question this file asks about it.
    """
    return {
        "id": vp_id,
        "title": "缺少 X-PDT-Request 的请求被拒绝并说明缺失的请求头",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/api/plans",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 2,
        },
        "assertions": [
            {"name": "状态码 403", "status": 403},
            {
                "name": "响应说明缺少的请求头",
                "json_path": "$.detail",
                "contains": "X-PDT-Request",
            },
        ],
    }


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_heartbeat_refreshes_the_lease_throughout_the_api_phase(tmp_path):
    """The lease's heartbeat must follow the round, not stop at the preflight.

    The lease is a heartbeat, not a timestamp: a reaper in another
    process decides "is this service still in use?" by reading it.  A
    round that took the lease at the preflight and never refreshed it
    would have its services collected mid-round — which is the incident
    this file is about.
    """
    agent = _make_agent(tmp_path)
    _lease_the_service(agent)

    # The preflight's refresh: the lease exists and says "still here".
    agent._touch_round_liveness()
    first = _lease_heartbeat(agent)
    assert first > 0, "the preflight did not stamp the lease"

    # ... the api phase refreshes it again, before the request goes out.
    time.sleep(0.01)
    agent._run_api_test_vp(_api_vp())
    second = _lease_heartbeat(agent)
    assert second > first, (first, second)

    # ... and the judgment phase refreshes it once more.
    time.sleep(0.01)
    agent._judgment_heartbeat("report_judgment")
    third = _lease_heartbeat(agent)
    assert third > second, (second, third)


@pytest.mark.integration
def test_the_round_reports_the_lease_loss_instead_of_a_connection_refused(
    tmp_path,
):
    """A round that lost its lease must say so — once, and by name.

    The 2026-10-25 report carried nine identical "connection refused"
    failures.  The round knew none of the facts that explained them:
    it never read its own lease back, so it could not name the service
    that was taken away, the process it expected to be serving, or the
    reason the service went away.
    """
    agent = _make_agent(tmp_path)
    runtime = _lease_the_service(agent)
    agent._touch_round_liveness()

    _foreign_reap(agent, runtime)

    # The VP that hits the loss is NOT answered with a connection
    # refused: the request must never go out, and the verdict must name
    # the round-level fault.
    verdict = agent._run_api_test_vp(_api_vp())
    text = _as_text(verdict)
    assert verdict["status"] == "FAILED", verdict
    assert "round_lease_lost" in text, verdict
    assert "connection refused" not in text.lower(), verdict
    assert "errno 61" not in text.lower(), verdict

    # The loss itself carries the three facts a reader needs, all present.
    loss = agent._assert_round_lease()
    assert loss is not None, "the round did not notice it lost the lease"
    assert loss["expected_pid"] == runtime.pid, loss
    assert loss["last_heartbeat_at"] > 0, loss
    assert "server_shutdown" in loss["last_reap_reason"], loss

    # The event is written down — in the plan's own service-event log,
    # where the reap that caused it is also recorded.
    events = read_service_events(agent.plan_dir)
    lost = [e for e in events if e.get("event") == "round_lease_lost"]
    assert len(lost) == 1, events
    event = lost[0]
    assert event["last_reap_reason"], event
    assert event["expected_pid"] == runtime.pid, event
    assert event["last_heartbeat_at"], event
    assert event["round_token"], (
        "an unattributable loss is recorded as 'unknown', never empty"
    )


@pytest.mark.integration
def test_the_api_vps_after_the_lease_loss_are_stopped_not_failed(tmp_path):
    """One api VP reports the loss; the ones after it are stopped.

    Nine FAILED verdicts would be nine repair tasks — the very
    multiplication this fix exists to stop, only with a better sentence
    in it. The loss is a fact about the **round**: the VP that ran into
    it is failed (its criterion was never observed), and the VPs after
    it never ran, so they are stopped rather than failed.
    """
    agent = _make_agent(tmp_path)
    runtime = _lease_the_service(agent)
    agent._touch_round_liveness()
    _foreign_reap(agent, runtime)

    first = agent._run_api_test_vp(_api_vp("VP-003"))
    assert first["status"] == "FAILED", first

    second = agent._run_api_test_vp(_api_vp("VP-004"))
    text = _as_text(second)
    assert second["status"] == "SKIPPED", second
    assert "round_lease_lost" in text, second
    assert "connection refused" not in text.lower(), second


@pytest.mark.integration
def test_the_vps_after_the_lease_loss_are_stopped_not_run(tmp_path):
    """After the loss, the round stops. It does not keep asking.

    Every VP that ran after the service was taken away produced another
    "connection refused" — nine of them, from one accident.  The VPs
    that come after the loss must be stopped instead: they were never
    executed, so they have no verdict of their own to report.
    """
    agent = _make_agent(tmp_path)
    runtime = _lease_the_service(agent)
    _foreign_reap(agent, runtime)
    assert agent._assert_round_lease() is not None

    executor = agent._build_verification_executor({"verification_points": []})
    verdict = asyncio.run(
        executor.sub_agent_runner({"id": "VP-009", "method": "api_test"}),
    )

    assert verdict["status"] == "SKIPPED", verdict
    text = _as_text(verdict)
    assert "round_lease_lost" in text, verdict
    assert "connection refused" not in text.lower(), verdict


@pytest.mark.integration
def test_a_round_that_still_holds_its_lease_is_not_aborted(tmp_path):
    """The check must be quiet when there is nothing wrong.

    A lease check that fires on a healthy round would kill every round
    that declared a service — the repair would be worse than the
    incident.
    """
    agent = _make_agent(tmp_path)
    _lease_the_service(agent)
    agent._touch_round_liveness()

    assert agent._assert_round_lease() is None
    assert getattr(agent, "_round_lease_lost", None) is None

    # ... and the VPs keep running.
    executor = agent._build_verification_executor({"verification_points": []})
    verdict = asyncio.run(
        executor.sub_agent_runner({"id": "VP-009", "method": "api_test"}),
    )
    assert verdict["status"] != "SKIPPED" or "round_lease_lost" not in (
        _as_text(verdict)
    ), verdict


@pytest.mark.integration
def test_the_vp_is_blocked_when_the_port_answers_for_a_stranger(
    tmp_path, monkeypatch,
):
    """The 403 has to come from the instance this round ensured.

    The lease says which process was serving the port when the round
    ensured the service.  The port answers — somebody is on it — but
    not for the process the lease names: the service was taken away
    and something else took its place.

    A request sent now would be graded on **that** process's answer.
    For VP-004 that is not a detail: the criterion is "the default
    backend refuses a header-less request", and a 403 from a stranger
    proves nothing about the backend this round declared.  So the
    request must not go out, and the verdict must say what was found
    instead of what was answered.
    """
    agent = _make_agent(tmp_path)
    port = _free_port()
    proc, stranger_pid = _start_listener(port)
    try:
        # The ledger names this process as the one serving the port;
        # the port is answered by the listener instead.
        _lease_port(agent, port, listener_pid=os.getpid())
        _record_reap(agent, "api", port, os.getpid())

        events: List[tuple] = []
        monkeypatch.setattr(
            agent,
            "_safe_log_freshness_event",
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            ),
        )

        verdict = agent._run_api_test_vp(_vp_on_port("VP-004", port))
    finally:
        proc.kill()
        proc.wait(timeout=10)

    text = _as_text(verdict)
    assert verdict["status"] == "BLOCKED", verdict
    assert "BLOCKED_SERVICE_LOST" in text, verdict
    # The criterion was not observed — and it is not dressed up as a
    # transport failure either.
    assert "connection refused" not in text.lower(), verdict

    # The request never went out: no audit artifact, no executed event.
    artifact = agent.plan_dir / "vp_artifacts" / "VP-004" / "api_response.json"
    assert not artifact.exists(), artifact
    assert not [e for e in events if e[1] == "api_test_executed"], events

    # The loss is written down with the facts a reader needs.
    lost = [e for e in events if e[1] == "api_phase_service_lost"]
    assert len(lost) == 1, events
    data = lost[0][2]
    assert data["port"] == port, data
    assert data["expected_pid"] == os.getpid(), data
    assert data["actual_pid"] == stranger_pid, data
    assert data["lease_owner"], data
    assert "server_shutdown" in data["last_reap_reason"], data


@pytest.mark.integration
def test_the_vp_proceeds_when_the_port_answers_for_the_leased_process(
    tmp_path, monkeypatch,
):
    """The check must be quiet when the port answers for the leased one.

    A check that fires on every request would stop the round from
    verifying anything at all — and the lease's ``listener_pid`` is
    exactly the process the round ensured, so the two must agree on a
    healthy round.
    """
    agent = _make_agent(tmp_path)
    port = _free_port()
    proc, listener_pid = _start_listener(port)
    try:
        _lease_port(agent, port, listener_pid=listener_pid)

        events: List[tuple] = []
        monkeypatch.setattr(
            agent,
            "_safe_log_freshness_event",
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            ),
        )

        verdict = agent._run_api_test_vp(_vp_on_port("VP-004", port))
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert "BLOCKED_SERVICE_LOST" not in _as_text(verdict), verdict
    assert not [e for e in events if e[1] == "api_phase_service_lost"], events
    # ... and the request did go out.
    assert [e for e in events if e[1] == "api_test_executed"], events


# ---------------------------------------------------------------------------
# The request's own outcome — an HTTP verdict, or a transport fault
# ---------------------------------------------------------------------------
#
# 2026-10-25, observed on a live round: nine ``api_test_executed`` events
# were written between 21:06:17 and 21:06:19, and the service had been
# taken away at 21:05:44.  Every event said the same three things —
# status, status code, failed assertions — and **none of them said
# which port the request went to, who was listening on it, or who owned
# the lease on it**.  A reader could not tell "the route answered wrong"
# from "there was nobody listening at all" without going to a different
# file (``logs/service_address_events.jsonl``) and reconstructing the
# round by hand.
#
# The event is the framework's own record of the request, so it has to
# carry the four facts that make the request interpretable:
# ``port``, ``listener_pid``, ``lease_owner`` and
# ``seconds_since_services_ensured``.
#
# And a request that produced **no response at all** is not a verdict
# about the endpoint: the service is gone, which is a fact about the
# **round**.  It is escalated as one, and the api phase stops there
# instead of asking a dead port the same question nine times.


@pytest.mark.integration
def test_the_executed_event_carries_the_service_snapshot(
    tmp_path, monkeypatch,
):
    """The event says **who** answered, not only what was answered.

    A negative criterion — "an unknown plan is answered with 404" — is
    only evidence when the 404 came from the instance this round
    declared.  The same endpoint answers 404 on a stranger's process
    too, so the event has to name the instance it was graded on.
    """
    agent = _make_agent(tmp_path)
    port = _free_port()
    proc, server_pid = _start_http_responder(port, status=404)
    try:
        # The ledger says this round's service on that port is served by
        # this process — and it is.
        _lease_port(agent, port, listener_pid=server_pid)
        agent._services_ensured_at = time.monotonic()

        events: List[tuple] = []
        monkeypatch.setattr(
            agent,
            "_safe_log_freshness_event",
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            ),
        )

        verdict = agent._run_api_test_vp(
            _vp_missing_plan_on_port("VP-006", port),
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert verdict["status"] == "PASSED", verdict

    executed = [e for e in events if e[1] == "api_test_executed"]
    assert len(executed) == 1, events
    data = executed[0][2]
    snapshot = data.get("service_snapshot")
    assert snapshot, (
        f"the executed event carries no service snapshot: {data}"
    )
    assert set(snapshot) == {
        "port",
        "listener_pid",
        "lease_owner",
        "seconds_since_services_ensured",
    }, snapshot
    assert snapshot["port"] == port, snapshot
    assert snapshot["listener_pid"] == server_pid, snapshot
    assert snapshot["lease_owner"], snapshot
    assert isinstance(snapshot["seconds_since_services_ensured"], float), (
        snapshot
    )


@pytest.mark.integration
def test_a_refused_request_is_a_round_level_loss_not_a_vp_failure(
    tmp_path, monkeypatch,
):
    """Nobody answered: that is the round's fact, not the VP's.

    The 2026-10-25 report carried nine identical FAILED verdicts for
    one accident.  A request that produced **no response** says the
    service is not there any more — so the verdict must not be this
    verification point's own failure, and the fact must be written down
    once, as a round-level loss.
    """
    agent = _make_agent(tmp_path)
    # The port the round's own service used — and nothing is listening
    # on it now: the request is refused.
    port = _free_port()
    _own_the_service(agent, port)
    agent._services_ensured_at = time.monotonic()

    events: List[tuple] = []
    monkeypatch.setattr(
        agent,
        "_safe_log_freshness_event",
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        ),
    )

    verdict = agent._run_api_test_vp(
        _vp_missing_plan_on_port("VP-006", port),
    )

    text = _as_text(verdict)
    assert verdict["status"] != "FAILED", verdict
    assert verdict["status"] == "BLOCKED", verdict
    assert "BLOCKED_SERVICE_LOST" in text, verdict

    # The loss is written down **once**, as a round-level fact.
    lost = [e for e in events if e[1] == "api_phase_service_lost"]
    assert len(lost) == 1, events
    vp_id, _event_type, data = lost[0]
    assert vp_id == "__round__", lost
    assert data["scope"] == "round", data
    assert data["port"] == port, data
    assert "connection refused" in str(data["transport_error"]).lower(), data

    # ... and the report carries it at the top level, where a reader
    # looks for "what happened to this round".
    report = agent._round_error_report()
    assert report is not None, "the round did not report the loss"
    assert report["kind"] == "api_phase_service_lost", report


@pytest.mark.integration
def test_the_vps_after_a_refused_request_are_stopped_not_run(
    tmp_path, monkeypatch,
):
    """After the loss, the round stops. It does not keep asking.

    Every VP that ran after the service was taken away produced another
    "connection refused" — nine of them, from one accident.  The VPs
    that come after the loss must be stopped instead: they were never
    executed, so they have no verdict of their own to report, and the
    second request must not go out at all.
    """
    agent = _make_agent(tmp_path)
    port = _free_port()
    _own_the_service(agent, port)
    agent._services_ensured_at = time.monotonic()

    events: List[tuple] = []
    monkeypatch.setattr(
        agent,
        "_safe_log_freshness_event",
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        ),
    )

    first = agent._run_api_test_vp(
        _vp_missing_plan_on_port("VP-006", port),
    )
    assert first["status"] == "BLOCKED", first

    second = agent._run_api_test_vp(
        _vp_missing_plan_on_port("VP-007", port),
    )
    assert second["status"] == "SKIPPED", second
    assert "api_phase_service_lost" in _as_text(second), second

    # The second request never went out: one request, one event.
    executed = [e for e in events if e[1] == "api_test_executed"]
    assert len(executed) == 1, events


@pytest.mark.integration
def test_a_refused_request_to_an_endpoint_the_round_does_not_own_is_its_own(
    tmp_path, monkeypatch,
):
    """Silence is only the round's business when the service is its own.

    The other half of the rule, and the one that keeps the escalation
    honest: an endpoint this round never brought up — a service somebody
    else runs, an address outside this machine — is not the round's
    service.  Its silence says something about **that** endpoint, so the
    verdict stays this verification point's own failure and the round
    keeps going: stopping the whole api phase over an address the round
    does not own would trade one wrong answer for another.
    """
    agent = _make_agent(tmp_path)
    # Nothing is listening, and the round has no service on this port:
    # the round never brought anything up here.
    port = _free_port()
    agent._services_ensured_at = time.monotonic()

    events: List[tuple] = []
    monkeypatch.setattr(
        agent,
        "_safe_log_freshness_event",
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        ),
    )

    verdict = agent._run_api_test_vp(
        _vp_missing_plan_on_port("VP-006", port),
    )

    # main 的语义：请求一次都没发出去 → BLOCKED（管道没就绪，不是产品
    # 的问题）。见本文件头的说明。
    assert verdict["status"] == "BLOCKED", verdict
    lost = [e for e in events if e[1] == "api_phase_service_lost"]
    assert not lost, f"the round called an endpoint it does not own lost: {lost}"
    assert agent._round_error_report() is None, agent._round_error_report()


@pytest.mark.integration
def test_an_answered_request_is_not_escalated_to_a_round_loss(
    tmp_path, monkeypatch,
):
    """A 500 is an answer. It is this VP's own failure, not the round's.

    The control case, and the one that keeps the escalation honest: a
    request that came back — with any status code — was **answered**.
    Grading it is exactly what the api phase is for, so it must stay a
    per-VP FAILED verdict, and the round must not be told it lost its
    service.
    """
    agent = _make_agent(tmp_path)
    port = _free_port()
    proc, server_pid = _start_http_responder(
        port, status=500, body='{"detail":"boom"}',
    )
    try:
        _lease_port(agent, port, listener_pid=server_pid)
        agent._services_ensured_at = time.monotonic()

        events: List[tuple] = []
        monkeypatch.setattr(
            agent,
            "_safe_log_freshness_event",
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            ),
        )

        verdict = agent._run_api_test_vp(
            _vp_missing_plan_on_port("VP-006", port),
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert verdict["status"] == "FAILED", verdict
    assert not [
        e for e in events if e[1] == "api_phase_service_lost"
    ], events
    assert agent._round_error_report() is None, agent._round_error_report()

    # ... and the event still names the instance that answered.
    executed = [e for e in events if e[1] == "api_test_executed"]
    assert len(executed) == 1, events
    snapshot = executed[0][2].get("service_snapshot")
    assert snapshot, executed[0][2]
    assert snapshot["listener_pid"] == server_pid, snapshot


# ---------------------------------------------------------------------------
# The mcode side — its own lease, its own listener, its own verdict
# ---------------------------------------------------------------------------
#
# 2026-10-26, plan ``20261023-PDT-MiniMax-Code-CLI`` (round 1):
# the plan declares **two** services — ``api`` (the default claude
# backend) and ``api-mcode`` (the same server, told to run under the
# mcode backend) — and VP-008 grades the mcode side's ``GET /health``.
#
# Two things were missing, and both of them are about the *mcode* side
# specifically:
#
#   1. **the lease did not name its service.**  The round's owner is one
#      token, and the two services hold two leases under it — but
#      nothing in a lease said *which* service it was on, so the two
#      were interchangeable and nothing could answer "is the process
#      answering the mcode port the one I leased *for api-mcode*?".
#   2. **the backend check only asked about the environment.**  It read
#      ``PDT_HARNESS`` out of whichever process answered the port.  But
#      the claude instance inherits the backend's own environment, so
#      when the backend itself runs under the mcode harness, the claude
#      instance *claims* ``PDT_HARNESS=mcode`` while running the claude
#      backend.  A criterion about the mcode backend could then be
#      graded on the claude instance's answer, and the check called it
#      the mcode backend.
#
# The four tests below pin the fix:
#
#   1. each service holds its **own** lease, naming itself — one round
#      owner, two ports, two listeners;
#   2. the mcode service being **absent** is an explicit failure, not a
#      silent "there is nothing to confirm";
#   3. the verification point that grades the mcode side **fails** when
#      the service is gone and cannot be brought back, and the loss is
#      written down under the service's own name;
#   4. a process that is not the one this round leased for ``api-mcode``
#      cannot answer the mcode criterion — not even when it claims the
#      mcode harness in its environment.

#: The backend the mcode-side declaration pins.  Spelled out here rather
#: than imported: the test asserts what the *declaration* says, and a
#: typo in the framework's constant must not become the expectation.
MCODE_HARNESS = "mcode"

#: The variable that selects the backend.  Named by the criterion, so it
#: is spelled out here too.
HARNESS_ENV_VAR = "PDT_HARNESS"


def _tiny_server_script(path: Path, port: int) -> str:
    """Write a one-file HTTP server that answers anything with 200.

    The lease is about *who* is serving a port, so the services in these
    tests have to be real processes on real ports: a runtime whose pid
    is a fiction is not leased the way a live service is.
    """
    path.write_text(
        "import http.server\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = b'{\"status\": \"ok\"}'\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        f"http.server.HTTPServer(('127.0.0.1', {port}), Handler)"
        ".serve_forever()\n",
        encoding="utf-8",
    )
    return str(path)


def _service_declaration(
    name: str, port: int, work: Path, *, harness: str = "",
) -> ServiceDeclaration:
    """A declared service: the claude one, or the mcode one.

    The only difference between the two is the export — which is the
    whole point: the mcode side is the same server told to run under
    another backend.
    """
    slug = name.replace("-", "_")
    script = work / f"{slug}_server.py"
    export = f"export {HARNESS_ENV_VAR}={harness}; " if harness else ""
    return ServiceDeclaration(
        name=name,
        port=port,
        start_cmd=(
            f"{export}exec {sys.executable} "
            f"{_tiny_server_script(script, port)}"
        ),
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=30,
        log_path=str(work / f"{slug}_server.log"),
    )


def _declare_services(agent, work: Path) -> Dict[str, ServiceDeclaration]:
    """The plan's two services, as this round declares them."""
    declarations = {
        "api": _service_declaration("api", _free_port(), work),
        "api-mcode": _service_declaration(
            "api-mcode", _free_port(), work, harness=MCODE_HARNESS,
        ),
    }
    agent._service_declarations = declarations
    return declarations


def _stop_services(runtime_map) -> None:
    """Stop whatever the round started — these tests' own services."""
    if runtime_map is None:
        return
    for runtime in (getattr(runtime_map, "runtimes", None) or {}).values():
        pid = getattr(runtime, "pid", None)
        if not pid:
            continue
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _vp_mcode(vp_id: str = "VP-008") -> Dict[str, Any]:
    """VP-008 as the plan declares it: the mcode side's health probe.

    The URL is the **placeholder** the plan writes, so the request is
    aimed at the service by name — the claude side's address is never
    a candidate for it.
    """
    return {
        "id": vp_id,
        "title": "mcode 侧服务健康探针可用",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api-mcode.url}}/health",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 10,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {"name": "健康状态为 ok", "json_path": "$.status", "equals": "ok"},
        ],
    }


@pytest.mark.integration
def test_each_service_holds_its_own_lease_naming_it(tmp_path):
    """Two services, two leases — and each lease names its service.

    The round's owner is one token; the two services are two claims.
    A lease that does not say **which** service it is on cannot answer
    "is the process answering the mcode port the one I leased *for
    api-mcode*?" — and that is the question the mcode criterion turns
    on.
    """
    agent = _make_agent(tmp_path)
    work = tmp_path / "services"
    work.mkdir()
    declarations = _declare_services(agent, work)

    runtime_map = None
    try:
        with round_lease_context(agent.plan_dir, round_id=agent._round_id()):
            runtime_map = ensure_services(tmp_path, declarations)
        assert runtime_map.runtimes["api"].ready, (
            runtime_map.runtimes["api"].detail
        )
        assert runtime_map.runtimes["api-mcode"].ready, (
            runtime_map.runtimes["api-mcode"].detail
        )

        leases = {
            entry["name"]: entry.get("lease") or {}
            for entry in read_ledger(agent.plan_dir).get("services") or []
        }
        assert set(leases) == {"api", "api-mcode"}, leases

        # Each lease names the service it is on...
        assert leases["api"]["service"] == "api", leases
        assert leases["api-mcode"]["service"] == "api-mcode", leases

        # ... and the two are independent claims of one round: the same
        # owner, two ports, two listeners.
        assert leases["api"]["owner"] == leases["api-mcode"]["owner"], leases
        assert leases["api"]["port"] != leases["api-mcode"]["port"], leases
        assert (
            leases["api"]["listener_pid"]
            != leases["api-mcode"]["listener_pid"]
        ), leases
    finally:
        _stop_services(runtime_map)


@pytest.mark.integration
def test_the_absent_mcode_service_is_an_explicit_failure(tmp_path):
    """A service that is not there is a failure, not "nothing to check".

    The mcode criterion is about the mcode backend being **alive**.  When
    the service is not answering, the capability is unavailable, and the
    hard constraints call that an explicit failure: it must not be
    answered with a shrug ("there is no process to confirm"), and it must
    not be answered by whatever else may be listening.
    """
    agent = _make_agent(tmp_path)
    work = tmp_path / "services"
    work.mkdir()
    _declare_services(agent, work)

    result = agent._verify_service_backend_for_vp(_vp_mcode())

    assert result is not None, (
        "the absent mcode service was read as 'nothing to confirm'"
    )
    assert result["ok"] is False, result
    assert result["service"] == "api-mcode", result
    assert result["service_absent"] is True, result
    assert result["kind"] == "service_absent", result
    assert MCODE_HARNESS in str(result["detail"]), result


@pytest.mark.integration
def test_the_mcode_vp_fails_explicitly_when_the_service_cannot_be_brought_back(
    tmp_path, monkeypatch,
):
    """The service is gone and will not start: the criterion fails.

    Not a connection-refused story, and not a blocked one: the mcode
    capability is unavailable, so the verification point that grades it
    fails — explicitly, by name — and the loss is written down under the
    service's own name, so "the mcode service is not there" and "the api
    service is not there" stay two different facts.
    """
    import service_manager

    agent = _make_agent(tmp_path)
    work = tmp_path / "services"
    work.mkdir()
    _declare_services(agent, work)

    def _unready(project_dir, declarations, **kwargs):
        """The service cannot be started — nothing comes up."""
        runtimes = {}
        for name, decl in declarations.items():
            runtimes[name] = ServiceRuntime(
                name=name, port=decl.port, pid=None,
                start_cmd=decl.start_cmd, origin="failed", ready=False,
                detail="the service could not be started",
            )
        return ServiceRuntimeMap(runtimes=runtimes)

    monkeypatch.setattr(service_manager, "ensure_services", _unready)

    events: List[tuple] = []
    monkeypatch.setattr(
        agent,
        "_safe_log_freshness_event",
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        ),
    )

    verdict = agent._run_api_test_vp(_vp_mcode())

    assert verdict["status"] == "FAILED", verdict
    reasons = " ".join(str(r) for r in (verdict.get("reasons") or []))
    assert "api-mcode" in reasons, verdict
    assert MCODE_HARNESS in reasons, verdict

    lost = [e for e in events if e[1] == "api_phase_service_lost"]
    assert len(lost) == 1, events
    assert lost[0][2]["service"] == "api-mcode", lost
    assert lost[0][2]["transport_error_kind"] == "service_not_restarted", lost


@pytest.mark.integration
def test_another_instance_cannot_answer_the_mcode_criterion(tmp_path):
    """A process this round did not lease cannot answer for the mcode.

    The environment alone is not enough: the claude instance inherits
    the backend's own ``PDT_HARNESS=mcode``, so it *claims* the mcode
    backend while running the claude one.  The lease is what says which
    process this round ensured for ``api-mcode`` — and the answer of
    anybody else may not stand in for it.
    """
    agent = _make_agent(tmp_path)
    work = tmp_path / "services"
    work.mkdir()
    declarations = _declare_services(agent, work)
    mcode_port = declarations["api-mcode"].port

    # The stranger answers the mcode port, and claims the mcode harness
    # in its environment — exactly as the claude instance does when the
    # backend itself runs under the mcode harness.
    proc, stranger_pid = _start_http_responder(
        mcode_port, status=200, body='{"status": "ok"}',
        env={HARNESS_ENV_VAR: MCODE_HARNESS}, new_session=True,
    )
    try:
        # The round's lease for api-mcode names the process it ensured.
        # The test process stands in for it: what matters is that the
        # live listener is neither it nor in its process group.
        lease = make_lease(
            round_id=agent._round_id(),
            backend_pid=os.getpid(),
            port=mcode_port,
            listener_pid=os.getpid(),
            service="api-mcode",
        )
        runtime = ServiceRuntime(
            name="api-mcode", port=mcode_port, pid=os.getpid(),
            start_cmd="python server.py", origin="spawned", ready=True,
            pgid=os.getpgid(os.getpid()),
        )
        runtime.lease = lease.to_dict()
        record_runtime(
            agent.plan_dir, agent.plan_dir.name, runtime,
            project_dir=agent.project_dir, round_number=1,
        )
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={"api-mcode": runtime},
        )

        events: List[tuple] = []
        agent._safe_log_freshness_event = (  # type: ignore[method-assign]
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            )
        )

        verdict = agent._run_api_test_vp(_vp_mcode())
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert verdict["status"] == "FAILED", verdict
    reasons = " ".join(str(r) for r in (verdict.get("reasons") or []))
    assert "api-mcode" in reasons, verdict
    assert MCODE_HARNESS in reasons, verdict
    # ... and the reason says **which** fault it was: the service was
    # not gone, somebody else was answering it.  A repair loop reads
    # this sentence, so a note that said the same thing for every fault
    # would send it back to the same repair.
    assert (
        "the request was never made: the service answering the port is "
        "not the instance this round leased for it"
    ) in reasons, verdict

    # The loss is written down, under the service's own name, with the
    # two pids that disagree.
    lost = [e for e in events if e[1] == "api_phase_service_lost"]
    assert len(lost) == 1, events
    record = lost[0][2]
    assert record["service"] == "api-mcode", record
    assert record["expected_pid"] == os.getpid(), record
    assert record["actual_pid"] == stranger_pid, record
    # ... and it says **which kind** of loss it was, so a reader does not
    # have to tell "the service was gone" from "somebody else answered"
    # by reading the detail.
    assert record["transport_error_kind"] == "listener_mismatch", record


#: The four fields VP-009 grades.  The criterion is not ours to move:
#: the mcode side's system overview must carry all four.
MCODE_CONTRACT_FIELDS = (
    "execution_running",
    "verification_running",
    "total_active",
    "details",
)


def _vp_mcode_contract(vp_id: str = "VP-009") -> Dict[str, Any]:
    """VP-009 as the plan writes it: the mcode side's four fields.

    The URL is the **placeholder** the plan writes, so the request is
    aimed at the mcode service by name — the claude side's address is
    never a candidate for it.
    """
    return {
        "id": vp_id,
        "title": "mcode 侧系统总览接口在服务重启后仍满足契约",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api-mcode.url}}/api/system/active",
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 10,
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


@pytest.mark.integration
def test_the_mcode_contract_is_not_booked_from_an_unclaimed_instance(tmp_path):
    """The four fields must come from the instance this round holds.

    The gap this pins
    -----------------
    2026-10-26, plan ``20261023-PDT-MiniMax-Code-CLI``: the round's
    ``api-mcode`` service was taken away by another backend's shutdown
    hook, and the plan's ledger was **finalised** with it — so the
    round was left holding no lease it could check the answer against.
    What answered the port afterwards was a process the round never
    ensured, and it *claimed* the mcode harness in its environment:
    the default backend inherits ``PDT_HARNESS`` from the backend that
    started it, so a claude instance claims the mcode harness while
    running the claude one.

    ``/api/system/active`` is served by the same server code on both
    sides, so that stranger answers the endpoint with 200, a JSON
    content type and **all four fields** — a body indistinguishable
    from the mcode instance's own.  A framework that books those fields
    has graded the mcode criterion on the claude instance's answer: the
    silent downgrade the hard constraints forbid.

    What the framework may book
    ----------------------------
    Only what it can point at.  The round holds a **claim** on the
    service — the lease, or, when the ledger holds none, the round's
    own record of the process it ensured — and the claim names a
    **process**.  The fields are booked only when that process is the
    one that was answering the port; otherwise every field says so, one
    by one, with ``reason=service_lost`` and the two pids that
    disagree.
    """
    agent = _make_agent(tmp_path)
    work = tmp_path / "services"
    work.mkdir()
    declarations = _declare_services(agent, work)
    mcode_port = declarations["api-mcode"].port

    # The stranger: a real HTTP server on the mcode port that answers
    # the endpoint with the four fields, and claims the mcode harness
    # in its environment — exactly as the claude instance does when the
    # backend itself runs under the mcode harness.
    stranger_body = json.dumps({
        "execution_running": 0,
        "verification_running": 0,
        "total_active": 0,
        "details": [],
    })
    proc, stranger_pid = _start_http_responder(
        mcode_port, status=200, body=stranger_body,
        env={HARNESS_ENV_VAR: MCODE_HARNESS}, new_session=True,
    )
    try:
        # The round's own record of the instance it ensured for the
        # mcode service — a different process — and **no lease**: the
        # reap that took the service away finalised the ledger with it,
        # so the round was left holding nothing to check the answer
        # against.
        runtime = ServiceRuntime(
            name="api-mcode", port=mcode_port, pid=os.getpid(),
            start_cmd="python server.py", origin="spawned", ready=True,
        )
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={"api-mcode": runtime},
        )

        events: List[tuple] = []
        agent._safe_log_freshness_event = (  # type: ignore[method-assign]
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            )
        )

        verdict = agent._run_api_test_vp(_vp_mcode_contract())
    finally:
        proc.kill()
        proc.wait(timeout=10)

    # The criterion is not graded on somebody else's answer.
    assert verdict["status"] == "FAILED", verdict
    reasons = " ".join(str(r) for r in (verdict.get("reasons") or []))
    assert "api-mcode" in reasons, verdict
    assert str(stranger_pid) in reasons, verdict

    # The loss is written down, with the two pids that disagree.
    lost = [e for e in events if e[1] == "api_phase_service_lost"]
    assert len(lost) == 1, events
    record = lost[0][2]
    assert record["service"] == "api-mcode", record
    assert record["expected_pid"] == os.getpid(), record
    assert record["actual_pid"] == stranger_pid, record

    # And the four fields are **not** booked from the stranger's body:
    # one by one, each saying why, with the pids that disagree.
    contract = verdict["evidence"]["contract_observation"]
    assert contract["observed"] == {}, contract
    assert contract["not_observed"] == list(MCODE_CONTRACT_FIELDS), contract
    for field in MCODE_CONTRACT_FIELDS:
        entry = contract["unobserved"][field]
        assert entry["status"] == "unobserved", entry
        assert entry["reason"] == "service_lost", entry
        assert entry["expected_pid"] == os.getpid(), entry
        assert entry["actual_pid"] == stranger_pid, entry
    # The contract is not credited to the mcode backend either.
    assert contract["source"]["confirmed"] is False, contract
