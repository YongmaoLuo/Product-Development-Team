"""The round's service preflight — the gate before the first verification point.

Background
----------
A verification round whose services are not there does not fail fast: it
spends one verification point per service the plan names, and every one
of them is graded on a connection refusal.  The refusal reads as "the
project answered wrong", the round reports FAILED verdicts, and the
repair chain goes after a product nobody ever looked at.

So the round asks, **before** it dispatches anything, whether the
services it is about to use are actually there:

  1. a TCP connection to the port the round's own instance listens on;
  2. the service's health URL, asked with the header a verification
     point's own request carries — the probe travels the same road the
     request will travel;
  3. the ``instance`` block the service publishes about itself, against
     the identity the round recorded when it started the service.

A service that fails any of the three is re-ensured **once** and looked
at again — a service that died after the round started it can come
back.  A service that is still not there after that ends the round as
BLOCKED: the round measured nothing about the product, so it must not
report a verdict about it.

What this file pins:

  1. Every service usable → the round dispatches its verification
     points.
  2. A service that is not there → the round is BLOCKED, no
     verification point is dispatched, and no FAILED verdict exists.
  3. The one re-ensure is spent: a service that comes back lets the
     round run; a service that does not ends the round.
  4. An instance that is not the round's own is refused, even though it
     answers 200.
  5. The refusal is written down: the ``preflight_failed`` event names
     the service, the port, the process and the reason.
  6. The refusal is written where the round was **dispatched**, so
     every caller records it — and only once.
  7. A blocked round is not judged: neither the auto loop nor the
     post-repair re-entry hands it to the judgment phase, which would
     read "no verdict" as a failure and spawn repair tasks.

Hermeticity
-----------
Everything the test owns lives inside ``tmp_path``.  The two cases that
need a plan directory of their own ask
:func:`config_paths.resolve_plans_dir` for it, and under pytest that
resolves into the session's temporary tree (``PDT_PLANS_DIR``), never
into the live plans tree.  The only network traffic is loopback
traffic to a real HTTP server the test starts and stops itself.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import server  # noqa: F401 — seeds sys.modules['server'] before the import
import verification_loop as vl


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Service:
    """A real HTTP service that answers ``/health`` and ``/health/data``.

    The preflight talks to the service over HTTP — the same road a
    verification point's request travels — so the test needs a service
    that really answers.  What it publishes about itself is
    configurable, because the point of the check is what happens when
    the answer says "I am somebody else".
    """

    def __init__(self, instance: Optional[dict] = None, rows: int = 4):
        self.rows = rows
        self.instance = instance
        self.requests: List[Dict[str, Any]] = []
        service = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - the handler API's own name
                service.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                })
                body: Dict[str, Any] = {"status": "ok"}
                if self.path == "/health/data":
                    body["plan_routing_rows"] = service.rows
                    if service.instance is not None:
                        body["instance"] = service.instance
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # noqa: A003 - silence the log
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def publish(self, instance: Optional[dict]) -> None:
        """Change what the service says about itself."""
        self.instance = instance

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _instance_block(boot_id: str, plans_dir: Path, state_db: Path) -> dict:
    """The ``instance`` block the backend publishes about itself."""
    return {
        "boot_id": boot_id,
        "pid": 4242,
        "pgid": 4242,
        "plans_dir": str(plans_dir),
        "state_db_path": str(state_db),
        "state_db_inode": int(state_db.stat().st_ino),
        "plan_routing_rows": 4,
    }


def _make_agent(tmp_path: Path, plan_name: str = "plan"):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / plan_name
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _api_plan(port: int) -> dict:
    """A plan that declares one service on *port*."""
    return {
        "services": [
            {
                "name": "api",
                "port": port,
                "start_cmd": "python -m api.server",
                "cwd": ".",
                "health_url": f"http://127.0.0.1:{port}/health",
            },
        ],
        "verification_points": [],
    }


def _runtime_map(port: int, pid: int = 4242, ready: bool = True):
    """What the project's own service manager reports for a service."""
    import service_manager as sm

    return sm.ServiceRuntimeMap(runtimes={
        "api": sm.ServiceRuntime(
            name="api", port=port, pid=pid, origin="spawned", ready=ready,
            start_cmd="x",
        ),
    })


def _fingerprint(port: int, rows: int = 4, expected: int = 4):
    from service_freshness import DataPlaneFingerprint

    return DataPlaneFingerprint(
        port=port, pid=100, routing_rows=rows, expected_rows=expected,
        status="ok",
    )


def _state_db(tmp_path: Path, monkeypatch) -> Path:
    """The state database this process has open — with a real inode."""
    path = tmp_path / "state.db"
    path.write_bytes(b"")
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(path))
    return path


def _prepare(
    tmp_path: Path, monkeypatch, service: Optional[_Service], port: int,
    plan_name: str = "plan",
):
    """An agent whose round already ensured the ``api`` service.

    *service* is the live HTTP server the round's own ensure produced;
    ``None`` means the round ensured a service that is not there.

    Returns the agent, the plan, and the **ensure ledger**: the list of
    runtime maps the project's service manager hands out, one per call.
    The first entry is the round's own start; a second entry is what a
    re-ensure produces.  The ledger is what lets a test say "the
    re-ensure brought the service back at another port" without
    stubbing the round's own code.
    """
    agent = _make_agent(tmp_path, plan_name)
    plan = _api_plan(port)
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    from service_freshness import PortProbe

    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda p: PortProbe(port=p, listening=service is not None),
    )
    monkeypatch.setattr(
        "service_freshness.probe_data_plane",
        lambda p, **kwargs: _fingerprint(p),
    )
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )

    import service_manager as sm

    runtime_port = service.port if service is not None else port
    ensure_ledger: Dict[str, Any] = {
        "results": [_runtime_map(runtime_port)],
        "calls": 0,
    }

    def _ensure(project, declarations, round_number=0, **kwargs):
        results = ensure_ledger["results"]
        index = min(ensure_ledger["calls"], len(results) - 1)
        ensure_ledger["calls"] += 1
        return results[index]

    monkeypatch.setattr(sm, "ensure_services", _ensure)
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    agent.start_verification_round(1)
    agent._preflight_service_freshness(plan)
    return agent, plan, ensure_ledger


def _preflight(agent) -> Optional[Dict[str, Any]]:
    """Run the loop's service preflight against *agent*."""
    return vl._run_service_preflight(
        agent, agent.plan_dir.name, agent.plan_dir,
    )


def _log_entries(plan_id: str, event_name: Optional[str] = None) -> list:
    """Every entry the plan's execution log carries."""
    from config_paths import resolve_plans_dir

    log_file = resolve_plans_dir() / plan_id / "execution.log"
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            continue
        if event_name is None or entry.get("event") == event_name:
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# 1. Every service usable → the round runs
# ---------------------------------------------------------------------------


def test_the_preflight_passes_when_every_service_answers(
    tmp_path: Path, monkeypatch,
):
    """A service that answers is a service the round may use.

    The probe travels the same road a verification point's request
    travels: it asks the health URL **with the request header**, so a
    service that would refuse that request is refused here too.
    """
    state_db = _state_db(tmp_path, monkeypatch)
    service = _Service()
    try:
        agent, _plan, _ledger = _prepare(
            tmp_path, monkeypatch, service, service.port,
        )
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        # The round recorded the instance it started; re-record it now
        # that the service publishes one.
        agent._record_service_instance_identity("api", service.port)

        failure = _preflight(agent)
    finally:
        service.close()

    assert failure is None, failure
    paths = [entry["path"] for entry in service.requests]
    assert "/health" in paths, service.requests
    assert "/health/data" in paths, service.requests
    # HTTP header names are case-insensitive, and the transport
    # normalises the case — so the header is looked up that way.
    headers = {
        name.lower(): value
        for entry in service.requests
        for name, value in entry["headers"].items()
    }
    assert headers.get("x-pdt-request") == "1", headers


# ---------------------------------------------------------------------------
# 2. A service that is not there → the round is blocked
# ---------------------------------------------------------------------------


def test_the_preflight_refuses_a_service_that_is_not_there(
    tmp_path: Path, monkeypatch,
):
    """Nothing listens on the port → the round may not run.

    The refusal names the service, the port, the process the round
    thought was behind it, and the reason — those four facts are what
    an operator needs to act on the round.
    """
    _state_db(tmp_path, monkeypatch)
    dead = _Service()
    port = dead.port
    dead.close()

    agent, _plan, _ledger = _prepare(tmp_path, monkeypatch, None, port)
    failure = _preflight(agent)

    assert failure is not None, "the round would have run against a dead port"
    assert failure["service"] == "api", failure
    assert failure["port"] == port, failure
    assert failure["pid"] == 4242, failure
    assert failure["error_kind"] == "tcp_connect_failed", failure


# ---------------------------------------------------------------------------
# 3. The one re-ensure
# ---------------------------------------------------------------------------


def test_the_preflight_spends_one_reensure_and_lets_the_round_run(
    tmp_path: Path, monkeypatch,
):
    """A service that comes back is a service the round may use.

    The re-ensure is the round's one attempt at bringing a dead
    service back.  When it works, the round runs — blocking it would
    be a false alarm.
    """
    state_db = _state_db(tmp_path, monkeypatch)
    dead = _Service()
    port = dead.port
    dead.close()

    revived = _Service()
    try:
        revived.publish(
            _instance_block("boot-2", tmp_path / "plan", state_db),
        )
        agent, _plan, ledger = _prepare(
            tmp_path, monkeypatch, None, port,
        )
        # The re-ensure brings the service back — at another port, the
        # way a project that cannot take the old one does.
        ledger["results"] = [
            _runtime_map(port),
            _runtime_map(revived.port),
        ]

        failure = _preflight(agent)
        calls = ledger["calls"]
    finally:
        revived.close()

    assert failure is None, failure
    # One start, one re-ensure: the gate does not keep trying.
    assert calls == 2, calls


def test_the_preflight_refuses_after_one_failed_reensure(
    tmp_path: Path, monkeypatch,
):
    """A service that does not come back ends the round.

    The re-ensure is spent once: a round that kept retrying would
    spend a start attempt per verification point, which is the thing
    this gate exists to prevent.
    """
    _state_db(tmp_path, monkeypatch)
    dead = _Service()
    port = dead.port
    dead.close()

    agent, _plan, ledger = _prepare(tmp_path, monkeypatch, None, port)
    failure = _preflight(agent)

    assert failure is not None, "the round would have run against a dead port"
    # One start, one re-ensure — and no more.
    assert ledger["calls"] == 2, ledger["calls"]
    assert failure["error_kind"] == "tcp_connect_failed", failure


# ---------------------------------------------------------------------------
# 4. An instance that is not the round's own
# ---------------------------------------------------------------------------


def test_the_preflight_refuses_an_instance_that_is_not_the_rounds_own(
    tmp_path: Path, monkeypatch,
):
    """A stranger answered → the round may not use that answer.

    The stranger answers 200 on every path; what it does not do is
    answer as the instance the round started.  The comparison is the
    one the verification points' own dispatch uses — the boot id, the
    plans tree, the state database's inode.
    """
    state_db = _state_db(tmp_path, monkeypatch)
    service = _Service()
    try:
        agent, _plan, ledger = _prepare(
            tmp_path, monkeypatch, service, service.port,
        )
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._record_service_instance_identity("api", service.port)
        # Somebody else took the port.
        service.publish(
            _instance_block("boot-2", agent.plan_dir, state_db),
        )
        # The project's service manager will not put the round's own
        # service back on a port a stranger holds, and it cannot place
        # it anywhere else either.
        ledger["results"] = [
            _runtime_map(service.port),
            _runtime_map(service.port, ready=False),
        ]

        failure = _preflight(agent)
    finally:
        service.close()

    assert failure is not None, "the round would have used another instance"
    assert failure["error_kind"] == "identity_mismatch", failure
    assert failure["service"] == "api", failure


# ---------------------------------------------------------------------------
# 5. The refusal is written down
# ---------------------------------------------------------------------------


def test_the_blocked_round_is_written_down_with_the_four_facts(
    tmp_path: Path, monkeypatch,
):
    """The round's refusal is a record an operator can act on.

    The event names the service, the port, the process and the reason
    — and the round it blocked, so a reader can tell "the plumbing was
    not there" from "the project answered wrong".
    """
    _state_db(tmp_path, monkeypatch)
    dead = _Service()
    port = dead.port
    dead.close()

    agent, _plan, _ledger = _prepare(tmp_path, monkeypatch, None, port)
    failure = _preflight(agent)
    assert failure is not None, failure

    vl._record_service_preflight_blocked(
        agent.plan_dir.name, agent.plan_dir, failure,
    )

    events = _log_entries(agent.plan_dir.name, "preflight_failed")
    assert len(events) == 1, events
    data = events[0]["data"]
    assert data["service"] == "api", data
    assert data["port"] == port, data
    assert data["pid"] == 4242, data
    assert data["error_kind"] == "tcp_connect_failed", data

    blocked = _log_entries(agent.plan_dir.name, "verification_round_blocked")
    assert len(blocked) == 1, blocked
    assert blocked[0]["data"]["error_kind"] == "service_preflight_failed", blocked


# ---------------------------------------------------------------------------
# 6. A blocked round produces no verdict about the product
# ---------------------------------------------------------------------------


def test_the_blocked_round_reports_no_failed_verdict(tmp_path: Path):
    """The round's report says BLOCKED and names no verdict at all.

    A FAILED verdict would say "the project answered wrong"; the
    project was never asked.  The round therefore produces no
    verification results — not an empty FAILED, not a skipped one.
    """
    from verification_agent import VerificationAgent

    agent = VerificationAgent(
        plan_dir=tmp_path / "plan",
        project_dir=tmp_path / "project",
        coding_tool=None,
    )
    report = agent._build_preflight_blocked_report({
        "service": "api",
        "port": 8080,
        "pid": 4242,
        "error_kind": "tcp_connect_failed",
        "detail": "nothing accepted a TCP connection on port 8080",
    })

    assert report["overall_status"] == "BLOCKED", report
    assert report["verification_results"] == [], report
    assert report["stop_reason"] == "service_preflight_failed", report
    assert not [
        r for r in report["verification_results"] if r.get("status") == "FAILED"
    ], report
    assert report["service_preflight_failed"]["service"] == "api", report
    assert report["service_preflight_failed"]["port"] == 8080, report


# ---------------------------------------------------------------------------
# 7. The round asks before it dispatches
# ---------------------------------------------------------------------------


def test_the_round_asks_the_preflight_before_it_dispatches(
    tmp_path: Path, monkeypatch,
):
    """The dispatch gives the round its preflight.

    The agent owns the round's services and the identity each one is
    expected to answer with; the loop owns the question "may this round
    dispatch its verification points at all?".  The dispatch is where
    the two meet: the agent calls the preflight right after it has
    brought the services up, and before the first verification point.
    """
    _state_db(tmp_path, monkeypatch)
    service = _Service()
    try:
        agent, _plan, _ledger = _prepare(
            tmp_path, monkeypatch, service, service.port,
        )

        asked: List[str] = []

        class _Orch:
            verification_agent = agent

            def start_verification_cycle(self, **kwargs):
                asked.append("cycle")
                return agent._run_round_service_preflight()

        vl._dispatch_verification_round(
            agent.plan_dir.name, agent.plan_dir, _Orch(), 1, resume=False,
        )
    finally:
        service.close()

    assert asked == ["cycle"], asked
    assert agent._round_service_preflight is not None, (
        "the round was dispatched without a preflight"
    )


# ---------------------------------------------------------------------------
# 8. The refusal is written where the round was dispatched
# ---------------------------------------------------------------------------


def _blocked_report() -> Dict[str, Any]:
    """The report a round that dispatched nothing comes back with."""
    return {
        "overall_status": "BLOCKED",
        "verification_results": [],
        "requirement_deviations": [],
        "summary": "the round dispatched no verification point",
        "stop_reason": "service_preflight_failed",
        "service_preflight_failed": {
            "service": "api",
            "port": 8080,
            "pid": 4242,
            "error_kind": "tcp_connect_failed",
            "detail": "nothing accepted a TCP connection on port 8080",
        },
    }


def test_the_dispatch_writes_the_refusal_down_once(
    tmp_path: Path, monkeypatch,
):
    """The refusal is recorded where the round was dispatched.

    Every dispatch goes through one place, so the refusal is written
    there — once, whichever caller asked for the round.  A record only
    the auto loop wrote would leave the post-repair re-entry without
    one, and the two callers would have to remember to write it
    themselves.
    """
    from config_paths import resolve_plans_dir

    plan_id = "20261025-preflight-dispatch"
    plan_dir = resolve_plans_dir() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    class _Orch:
        def start_verification_cycle(self, **kwargs):
            return _blocked_report()

    report, conflict = vl._dispatch_verification_round(
        plan_id, plan_dir, _Orch(), 1, resume=False,
    )

    assert conflict is None, conflict
    assert report is not None, "the dispatch threw the report away"

    events = _log_entries(plan_id, "preflight_failed")
    assert len(events) == 1, events
    data = events[0]["data"]
    assert data["service"] == "api", data
    assert data["port"] == 8080, data
    assert data["pid"] == 4242, data
    assert data["error_kind"] == "tcp_connect_failed", data

    blocked = _log_entries(plan_id, "verification_round_blocked")
    assert len(blocked) == 1, blocked
    assert blocked[0]["data"]["error_kind"] == "service_preflight_failed", blocked


# ---------------------------------------------------------------------------
# 9. The post-repair re-entry does not judge a blocked round
# ---------------------------------------------------------------------------


def test_the_post_repair_reentry_does_not_judge_a_blocked_round(
    tmp_path: Path, monkeypatch,
):
    """A blocked round is not judged — not in the re-entry either.

    The post-repair re-entry is the same chain the auto loop runs, and
    it hands whatever the round returned to the judgment phase.  A
    round that dispatched nothing carries no verdict, and the judgment
    phase reads "no verdict" as a failure — it would spawn repair tasks
    for a product nobody ever looked at.

    So the re-entry stops at the blocked round: the chain ends there,
    and the judgment phase never runs.
    """
    from config_paths import resolve_plans_dir

    plan_id = "20261025-preflight-reentry"
    plan_dir = resolve_plans_dir() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    judged: List[str] = []

    class _Orch:
        def start_verification_cycle(self, **kwargs):
            return _blocked_report()

        def check_cycle_conditions(self, report, round_number=1):
            judged.append("judged")
            return {
                "status": "verification_failed",
                "repair_tasks": [{"id": "VP-999", "title": "not a real failure"}],
            }

    monkeypatch.setattr(
        vl._server, "VerificationOrchestrator",
        lambda *a, **k: _Orch(),
    )
    monkeypatch.setattr(
        vl._server, "create_coding_tool", lambda *a, **k: None,
    )

    callback = vl._make_repair_on_complete(plan_id, project_dir)
    callback(0)

    assert judged == [], (
        "the blocked round was judged: the judgment phase reads it as a "
        "failure and sends the repair chain after a product nobody ever "
        "looked at"
    )
    events = _log_entries(plan_id, "preflight_failed")
    assert len(events) == 1, events
    assert events[0]["data"]["error_kind"] == "tcp_connect_failed", events


# ---------------------------------------------------------------------------
# 10. The four acceptance points the plan's gate names
# ---------------------------------------------------------------------------
#
# The plan closes its work on four named cases.  Their names describe
# the ROUND's behaviour — what the round does with the gate's answer —
# and not the gate's return value, so the four cases below are driven
# through the round's own dispatcher.
#
# They are not aliases of the cases above: a gate that cannot fail is
# not a gate.  Each of the four pins a mechanism of its own, and each
# was checked by breaking that mechanism — the red runs are recorded
# in the commit that added them.
#
# ``run_full_verification`` is where the gate stands between "the
# services are up" and "the verification points run": it asks the
# preflight and, when the answer is a refusal, it returns the blocked
# report **without ever building an executor** — so no verification
# point is dispatched.


def _verification_point(vp_id: str) -> Dict[str, Any]:
    """One verification point the round is asked to run."""
    return {
        "id": vp_id,
        "verification_method": "api_test",
        "target": "http://127.0.0.1:8080/health",
        "acceptance_criteria": [f"{vp_id} passes"],
    }


class _RecordingExecutor:
    """The executor the round would dispatch its verification points to.

    It keeps the points it was handed and how often it was run, so a
    test can say "the round dispatched every point" — or "the round
    dispatched nothing" — with no model in the loop.
    """

    def __init__(self, vps: List[Dict[str, Any]]):
        self.vps = list(vps)
        self.peak_in_flight = 0
        self.runs = 0

    async def run(self, max_parallel: int = 0) -> None:
        self.runs += 1
        self.peak_in_flight = len(self.vps)


def _round_over(agent, monkeypatch, vps: List[str]) -> tuple:
    """Wire *agent* for a round over *vps*; return the executors built.

    The plan the agent would ask a model for, and the report it would
    ask a model to write, are the two ends that need a model, so they
    are fixed here.  Everything between them — the round preflight, the
    decision to dispatch, the executor — is the product's own code.
    """
    plan_data: Dict[str, Any] = {
        "services": [],
        "verification_points": [
            _verification_point(vp_id) for vp_id in vps
        ],
    }
    monkeypatch.setattr(
        agent, "generate_verification_plan", lambda *a, **k: plan_data,
    )
    # The freshness preflight (VP0) is the gate's *neighbour*: it is
    # the one that starts the services.  The gate under test here is
    # asked after it, so the neighbour is stubbed out to keep the two
    # apart.
    monkeypatch.setattr(
        agent, "_preflight_service_freshness", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        agent, "_flush_deferred_evidence_checks", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        agent, "_rerun_environment_blocked_vps",
        lambda plan, results: results,
    )
    monkeypatch.setattr(
        agent, "_build_execution_results_from_executor",
        lambda plan, executor: {"verification_points": list(executor.vps)},
    )
    monkeypatch.setattr(
        agent, "generate_verification_report",
        lambda results: {
            "overall_status": "PASSED",
            "verification_results": list(
                results.get("verification_points", [])
            ),
            "requirement_deviations": [],
            "summary": "the round ran",
        },
    )
    built: List[_RecordingExecutor] = []

    def _build_executor(plan: Dict[str, Any]) -> _RecordingExecutor:
        # The conversion is the product's own step: the plan's points
        # become the executor's points, and the executor is what the
        # round dispatches them to.
        converted = agent._convert_plan_to_executor_schema(plan)
        executor = _RecordingExecutor(converted.get("vps", []))
        built.append(executor)
        return executor

    monkeypatch.setattr(
        agent, "_build_verification_executor", _build_executor,
    )
    return plan_data, built


def _ask_the_gate(agent) -> Optional[Dict[str, Any]]:
    """Ask the round's gate the way the round asks it."""
    return vl._run_service_preflight(
        agent, agent.plan_dir.name, agent.plan_dir,
    )


def test_preflight_success_runs_all_vps(tmp_path: Path, monkeypatch):
    """Every service answers → every verification point is dispatched.

    The gate is a gate, not a filter: a preflight that passes must
    leave the whole plan's points to be run.  A gate that quietly
    dropped one would grade the round on a part of the product and
    call it PASSED.
    """
    state_db = _state_db(tmp_path, monkeypatch)
    service = _Service()
    try:
        agent, _plan, _ledger = _prepare(
            tmp_path, monkeypatch, service, service.port,
        )
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._record_service_instance_identity("api", service.port)
        agent.set_round_service_preflight(lambda: _ask_the_gate(agent))

        _plan_data, built = _round_over(
            agent, monkeypatch, ["VP-001", "VP-002"],
        )
        report = agent.run_full_verification(1)
    finally:
        service.close()

    assert report.get("stop_reason") != "service_preflight_failed", report
    assert len(built) == 1, built
    executor = built[0]
    assert executor.runs == 1, executor.runs
    assert [vp["id"] for vp in executor.vps] == ["VP-001", "VP-002"], (
        executor.vps
    )


def test_preflight_failure_blocks_whole_round(tmp_path: Path, monkeypatch):
    """A service that is not there → the whole round is blocked.

    Not "one point failed": **nothing** is dispatched, and the round
    says BLOCKED.  A FAILED verdict would be a statement about a
    product nobody was asked — and it is what sent the repair chain
    after a project nobody ever looked at.
    """
    _state_db(tmp_path, monkeypatch)
    dead = _Service()
    port = dead.port
    dead.close()

    agent, _plan, _ledger = _prepare(tmp_path, monkeypatch, None, port)
    agent.set_round_service_preflight(lambda: _ask_the_gate(agent))

    _plan_data, built = _round_over(
        agent, monkeypatch, ["VP-001", "VP-002"],
    )
    report = agent.run_full_verification(1)

    assert built == [], (
        "the round dispatched a verification point without its service"
    )
    assert report["overall_status"] == "BLOCKED", report
    assert report["verification_results"] == [], report
    assert not [
        r for r in report["verification_results"] if r.get("status") == "FAILED"
    ], report
    assert report["stop_reason"] == "service_preflight_failed", report


def test_preflight_reensures_once(tmp_path: Path, monkeypatch):
    """The round spends one re-ensure — and only one.

    Two rounds, one number each.  A round whose service is there
    spends nothing: the gate does not ask for a start it does not
    need.  A round whose service is gone spends exactly one
    re-ensure — the start it already made, plus one retry, and no
    more.  A third attempt would spend a start per verification point,
    which is the very thing this gate exists to prevent.
    """
    state_db = _state_db(tmp_path, monkeypatch)
    service = _Service()
    try:
        agent, _plan, healthy_ledger = _prepare(
            tmp_path, monkeypatch, service, service.port,
            plan_name="healthy",
        )
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._record_service_instance_identity("api", service.port)
        before = healthy_ledger["calls"]
        healthy_failure = _ask_the_gate(agent)
        spent_healthy = healthy_ledger["calls"] - before
    finally:
        service.close()

    assert healthy_failure is None, healthy_failure
    assert spent_healthy == 0, (
        f"the gate spent {spent_healthy} re-ensures on a service that was "
        f"there"
    )

    dead = _Service()
    port = dead.port
    dead.close()

    agent, _plan, dead_ledger = _prepare(
        tmp_path, monkeypatch, None, port, plan_name="dead",
    )
    before = dead_ledger["calls"]
    dead_failure = _ask_the_gate(agent)
    spent_dead = dead_ledger["calls"] - before

    assert dead_failure is not None, (
        "the round would have run against a dead port"
    )
    assert spent_dead == 1, (
        f"the gate spent {spent_dead} re-ensures; it is allowed exactly one"
    )


def test_preflight_records_service_and_port(tmp_path: Path, monkeypatch):
    """The refusal names the service and the port it was refused on.

    The record is what an operator reads when the round stops: without
    the service and the port, "the round was blocked" says nothing
    about where to look.  This case drives the round through the
    loop's own dispatch, so the record is written by the product's
    code and not by the test.
    """
    _state_db(tmp_path, monkeypatch)
    dead = _Service()
    port = dead.port
    dead.close()

    agent, _plan, _ledger = _prepare(tmp_path, monkeypatch, None, port)
    _plan_data, built = _round_over(agent, monkeypatch, [])

    class _Orch:
        verification_agent = agent

        def start_verification_cycle(self, **kwargs):
            return agent.run_full_verification(
                kwargs.get("round_number", 1),
            )

    report, conflict = vl._dispatch_verification_round(
        agent.plan_dir.name, agent.plan_dir, _Orch(), 1, resume=False,
    )

    assert conflict is None, conflict
    assert report is not None, "the dispatch threw the report away"
    assert built == [], "the blocked round dispatched a verification point"

    events = _log_entries(agent.plan_dir.name, "preflight_failed")
    assert len(events) == 1, events
    data = events[0]["data"]
    assert data["service"] == "api", data
    assert data["port"] == port, data
