"""The instance behind a round's address — and the wait the round left.

Background
----------
2026-10-26: a verification point was refused a connection on port
``64469`` at 02:25:01.  The instance the round had ensured (pid
``43904``) had been shut down at 02:21:51, and the point was still
aiming at its URL at 02:24:33.  Later in the **same** round another
point — ``ui_validation`` — succeeded against that very URL.  So the
port had been taken over by another instance while the round was
running, and the framework could not say **who** that instance was or
**which plans tree** it served.

Two things went missing because of that:

  * the round could not attribute the refusal to the environment, so a
    connection failure was graded as a product defect; and
  * the criterion the round was verifying — *the waiting event must be
    readable* — could not be checked, because the round could not say
    whether the answer it got came from its own instance.

What this file pins
-------------------
  1. the window-wait log stays **answerable** after the round's service
     outlives its starter — the PRD criterion, read back through the
     endpoint the operator reads;
  2. a dispatch whose port changed hands is **attributed**: the
     evidence names the instance on both sides of the request, and the
     round's own rules mark the point ``invalid_env`` — never a verdict
     about the product;
  3. the round records its own handover, so a reader can tell that the
     round's address belonged to somebody else by the end;
  4. a service that does not identify itself is not gated — "not
     comparable" is not "wrong"; and
  5. a handover that never settles is never graded: the point keeps
     its ``invalid_env`` marker through the round's one second look.

The numbers the criterion is about are production's own
(``capacity_wait.DEFAULT_CAPACITY_WAIT_SEC``), and the log line is
written by the production disposition, so the answer is read back, not
written by hand.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import server
from agent import AutonomousAgent
from capacity_wait import DEFAULT_CAPACITY_WAIT_SEC, QuotaWaitExhausted
from coding_tool import ApiError
from execution_logger import ExecutionLogger
from task_manager import TaskManager


#: The backend package root — ``<repo>/backend``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The plan the failed probes used.  Reusing it keeps the criterion and
#: its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-parked"

#: The disposition's event — "the run parked because capacity ran out".
WAIT_EVENT = "execution_waiting_for_quota"

#: The provider whose 5h window is shut.
PROVIDER = "vendor-a"

#: The wait budget the import path documents: half an hour.  The
#: criterion asserts the seconds the wait spent — 1800.0 — so the number
#: under test is the one production waits out.
WAIT_BUDGET_SEC = DEFAULT_CAPACITY_WAIT_SEC

#: The gap between two looks at the provider while waiting.
WAIT_POLL_SEC = 30.0

#: The task the wait happened on.  Only its id is read.
_TASK = SimpleNamespace(id="14-2")


# ---------------------------------------------------------------------------
# The plan's own log — written by the production writers
# ---------------------------------------------------------------------------


def _plan_logger() -> ExecutionLogger:
    """The plan's own logger, pointed at the isolated plans tree."""
    return ExecutionLogger(PLAN_ID, plans_dir=Path(server.PLANS_DIR))


def _plan_dir() -> Path:
    """Where the plan's own directory lives."""
    return Path(server.PLANS_DIR) / PLAN_ID


class _FakeClock:
    """The wait, and the reading of the wait — both under the test's hand."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        """What the wait does instead of sleeping: record, then advance."""
        self.sleeps.append(float(seconds))
        self.now += float(seconds)

    def clock(self) -> float:
        """What the wait reads instead of the wall clock."""
        return self.now


class _RefusingTool:
    """A coding tool whose provider refuses and never comes back."""

    def __init__(self, provider: str = PROVIDER) -> None:
        self.provider = provider
        self.current_call_provider = provider
        self.calls = 0

    def query(self, *args, **kwargs):
        self.calls += 1
        raise ApiError("quota exceeded", status="429")


def _plan_agent(logger: ExecutionLogger, clock: _FakeClock) -> AutonomousAgent:
    """The agent, wired with just enough to run the wait.

    ``AutonomousAgent.__new__`` gives the real class's own attributes
    without running its constructor — the pattern the neighbouring
    capacity-wait suites use, and the reason the refusals the wait
    recognises are production's.
    """
    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.logger = logger
    agent.coding_tool = _RefusingTool()
    agent._task_progress_repo = None
    agent._session_task_completed_counts = {}
    agent._quota_wait_requested = False
    agent._capacity_wait_clock = clock.clock
    agent._capacity_wait_sleep = clock.sleep
    return agent


def _waited_out_wait(
    monkeypatch, agent: AutonomousAgent,
) -> QuotaWaitExhausted:
    """Run the real wait until the budget is gone.

    The budget and the poll interval are the ones the import path
    documents, read from the environment production reads them from,
    and the clock is faked — so the wait ends on the budget and not on
    wall-clock time.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", str(int(WAIT_BUDGET_SEC)))
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", str(int(WAIT_POLL_SEC)))

    with pytest.raises(QuotaWaitExhausted) as caught:
        agent._query_with_capacity_wait(
            agent.coding_tool.query, _TASK, attempt_bound=1,
        )
    return caught.value


def _park_the_plan(agent: AutonomousAgent, exc: QuotaWaitExhausted) -> None:
    """Write the marker and park the plan, the way a run does.

    Two production writers, in the order a run has them:

    1. ``AutonomousAgent._stop_reason_for_quota_wait`` writes the
       ``waiting_for_quota`` marker into ``tasks.json``;
    2. ``routes.execution._enter_quota_wait`` reads that marker back
       and writes the disposition event into the plan's own log.

    Nothing here writes a log line by hand: the payload the endpoint
    reads is the one production wrote.
    """
    from routes import execution as routes_execution

    plan_dir = _plan_dir()
    plan_dir.mkdir(parents=True, exist_ok=True)
    agent.task_manager = TaskManager(
        project_dir=plan_dir, tasks_file=plan_dir / "tasks.json",
    )
    agent._stop_reason_for_quota_wait(
        exc, task=_TASK, attempt=1, max_attempts=1,
    )

    state = {"status": "running", "stop_reason": None, "ended_at": None}
    assert routes_execution._enter_quota_wait(
        PLAN_ID, plan_dir, state, plan_dir / "tasks.json",
    ) is True, "the marker must be recognised as a quota park"


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The contract is about the log on disk, which survives a restart —
    so the answer has to be readable without any in-memory state.
    """
    server._execution_state.clear()
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# The service — a real HTTP server that answers ``/health/data``
# ---------------------------------------------------------------------------


class _Service:
    """A real HTTP server that answers ``/health/data`` like the backend.

    The identity check talks to the service over HTTP — the same road a
    VP's request travels — so the test needs a service that really
    answers.  What it publishes about itself is configurable, because
    the point of the check is what happens when the answer says "I am
    somebody else".
    """

    def __init__(self, rows: int = 4, instance: Optional[dict] = None):
        self.rows = rows
        self.instance = instance
        self.requests: List[str] = []
        service = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - the handler API's own name
                service.requests.append(self.path)
                body: Dict[str, Any] = {
                    "status": "ok",
                    "plan_routing_rows": service.rows,
                }
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


def _instance_block(
    boot_id: str, plans_dir: Path, state_db: Path, **extra: Any,
) -> dict:
    """The ``instance`` block the backend publishes about itself."""
    block = {
        "boot_id": boot_id,
        "pid": 4242,
        "pgid": 4242,
        "cwd": str(plans_dir),
        "started_at": "2026-10-26T02:21:51+00:00",
        "plans_dir": str(plans_dir),
        "state_db_path": str(state_db),
        "state_db_inode": int(state_db.stat().st_ino),
        "plan_routing_rows": 4,
        "fixture_plan_ids": ["plan-under-test"],
    }
    block.update(extra)
    return block


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _api_test_plan(port: int) -> dict:
    """A plan with one ``api_test`` VP against the declared ``api``."""
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
        "verification_points": [
            {
                "id": "VP-001",
                "title": "items endpoint",
                "verification_method": "api_test",
                "uses_services": ["api"],
                "request": {"method": "GET", "url": "{{svc.api.url}}/items"},
                "assertions": [{"status": 200}],
            }
        ],
    }


def _state_db(tmp_path: Path, monkeypatch) -> Path:
    """The state database this process has open — with a real inode."""
    path = tmp_path / "state.db"
    path.write_bytes(b"")
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(path))
    return path


def _fingerprint(port: int, rows: int = 4, expected: int = 4):
    from service_freshness import DataPlaneFingerprint

    return DataPlaneFingerprint(
        port=port, pid=100, routing_rows=rows, expected_rows=expected,
        status="ok",
    )


def _runtime_map(port: int):
    import service_manager as sm

    return sm.ServiceRuntimeMap(runtimes={
        "api": sm.ServiceRuntime(
            name="api", port=port, pid=100, origin="spawned", ready=True,
            start_cmd="x",
        ),
    })


def _prepare(tmp_path: Path, monkeypatch, service: _Service):
    """An agent whose ``api`` service is the real HTTP server."""
    agent = _make_agent(tmp_path)
    plan = _api_test_plan(service.port)
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    from service_freshness import PortProbe

    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda port: PortProbe(port=port, listening=True),
    )
    monkeypatch.setattr(
        "service_freshness.probe_data_plane",
        lambda port, **kwargs: _fingerprint(port),
    )
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )

    import service_manager as sm

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, declarations, round_number=0, **kwargs:
            _runtime_map(service.port),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    agent.start_verification_round(1)
    return agent, plan


def _spy_api_runner(
    monkeypatch, service: Optional[_Service] = None,
    publish_per_dispatch: Optional[List[Optional[dict]]] = None,
) -> List[str]:
    """Record every dispatch — and hand the port over while it runs.

    This is the production shape: the port the round was talking to is
    taken by another process **while the request is in flight**.  The
    round's answer is then a mixture of two instances, and it has to be
    able to say so.

    ``publish_per_dispatch`` is what the service says about itself
    **after** each dispatch, in order — one entry per dispatch, the last
    one repeated.  A single entry is the ordinary handover: the port
    changes hands once.  Two entries are the handover that never settles:
    the port changes hands again on the re-run, and the round still
    cannot say whose answer it is holding.

    Without it the service keeps saying whatever it was already saying —
    which is the other half of the contract: a service that does not
    identify itself is not gated.
    """
    import verification_api_runner as var

    dispatched: List[str] = []

    class _Result:
        status = "PASSED"
        evidence = {"status_code": 200}

        def to_verdict_dict(self):
            return {
                "status": "PASSED",
                "reasons": ["ok"],
                "evidence": dict(self.evidence),
            }

    def fake_run(vp, **kwargs):
        dispatched.append(str(vp.get("id")))
        if service is not None and publish_per_dispatch:
            index = min(len(dispatched) - 1, len(publish_per_dispatch) - 1)
            service.publish(publish_per_dispatch[index])
        return _Result()

    monkeypatch.setattr(var, "run_api_verification", fake_run)
    return dispatched


def _run_all_vps(executor) -> list:
    """Run every VP of *executor* through the runner, in order.

    The verdicts are recorded in the executor the way the executor
    records them — so the round's own results envelope is built by the
    round's own code, not written by hand here.
    """
    async def _drive():
        verdicts = []
        for vp in executor.verification_plan["vps"]:
            verdict = await executor.sub_agent_runner(vp)
            executor.record_verdict(str(vp.get("id")), verdict)
            verdicts.append(verdict)
        return verdicts

    return asyncio.run(_drive())


def _evidence_of(result: Dict[str, Any]) -> Dict[str, Any]:
    """One result's evidence, as a dict."""
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        return evidence
    if isinstance(evidence, str):
        try:
            loaded = json.loads(evidence)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# 1. The criterion: the waiting event stays answerable
# ---------------------------------------------------------------------------


def test_the_window_wait_log_stays_answerable_after_the_round_service_outlives_its_starter(
    monkeypatch,
) -> None:
    """The waiting event is still readable — and the handover is attributed.

    The PRD criterion is one sentence: *the waiting event must be
    readable*.  Read back through the endpoint the operator reads, it
    says exactly this::

        total = 1
        event = execution_waiting_for_quota
        providers is not empty
        needs_manual_topup is false
        waited_sec = 1800.0

    The situation is the one the 2026-10-26 round was in: the process
    that started the run is gone (it parked the plan and exited), and
    the service still answering for the plan is a **different** process
    — the port was taken over by another instance while the round was
    running.  The log survives that, because the log is on disk; what
    has to survive it too is the round's own attribution, because an
    answer nobody can place is not evidence:

      * the evidence of a dispatch whose port changed hands names the
        instance on **both** sides of the request, and the round's own
        rules mark the point ``invalid_env`` — not a verdict about the
        product; and
      * the round records the handover, so a reader can tell that the
        round's address belonged to somebody else by the end.
    """
    # -- the starter parks the plan -----------------------------------
    logger = _plan_logger()
    clock = _FakeClock()
    agent = _plan_agent(logger, clock)

    exc = _waited_out_wait(monkeypatch, agent)
    assert exc.waited_sec == 1800.0, (
        f"the wait must report the seconds it actually waited out; got "
        f"{exc.waited_sec!r}"
    )
    _park_the_plan(agent, exc)

    # -- the round that outlived its starter ---------------------------
    service = _Service()
    try:
        state_db = _state_db(Path(server.PLANS_DIR).parent, monkeypatch)
        round_agent, plan = _prepare(
            Path(server.PLANS_DIR).parent, monkeypatch, service,
        )
        # The round's own instance — the one its ``ensure`` produced.
        service.publish(
            _instance_block("boot-1", round_agent.plan_dir, state_db),
        )
        round_agent._preflight_service_freshness(plan)
        # ... and then another process takes the port mid-dispatch.
        dispatched = _spy_api_runner(
            monkeypatch, service,
            [_instance_block("boot-2", round_agent.plan_dir, state_db)],
        )

        executor = round_agent._build_verification_executor(
            round_agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
        handover = round_agent.round_instance_handover()
        round_results = round_agent._build_execution_results_from_executor(
            plan, executor,
        )
    finally:
        service.close()

    # -- the handover is attributed ------------------------------------
    assert dispatched == ["VP-001"], dispatched
    assert results, "the round dispatched nothing"

    # The point, as the round records it: the evidence carries the
    # instance on **both** sides of the request.
    point = round_results["execution_results"][0]
    evidence = _evidence_of(point)
    before = evidence.get("serving_before")
    after = evidence.get("serving_after")
    assert isinstance(before, dict) and isinstance(after, dict), (
        f"the evidence must name the instance on both sides of the "
        f"request; got {evidence!r}"
    )
    assert before.get("boot_id") == "boot-1", evidence
    assert after.get("boot_id") == "boot-2", evidence
    assert before.get("plans_dir") == str(round_agent.plan_dir), evidence
    assert after.get("plans_dir") == str(round_agent.plan_dir), evidence
    assert before.get("pid") and after.get("pid"), evidence
    assert before.get("state_db_inode") is not None, evidence

    # The round's own rules mark the point: the address changed hands,
    # so the answer cannot be attributed to the instance the round
    # started, and the point is ``invalid_env`` — not a verdict about
    # the product.
    conflicts = round_agent._detect_round_evidence_conflicts(
        plan, round_results,
    )
    handovers = [
        conflict for conflict in conflicts
        if conflict.get("kind") == "instance_handover"
    ]
    assert len(handovers) == 1, conflicts
    assert handovers[0]["failed_vps"] == ["VP-001"], handovers[0]

    marked = round_agent._mark_invalid_env_results(round_results, conflicts)
    assert marked == ["VP-001"], marked
    point = round_results["execution_results"][0]
    assert point["status"] == "invalid_env", (
        f"a dispatch whose port changed hands is not a verdict about the "
        f"product; got {point!r}"
    )
    assert point["status"] != "FAILED", point

    assert handover["before"] is not None, (
        f"the round must remember the instance it started; got {handover!r}"
    )
    assert handover["before"].get("boot_id") == "boot-1", handover

    # -- and the criterion still holds ---------------------------------
    client = _client()
    resp = client.get(
        f"/api/execution/{PLAN_ID}/logs", params={"event": WAIT_EVENT},
    )
    assert resp.status_code == 200, (
        f"the plan's own log must answer the logs endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["total"] == 1, (
        f"exactly one waiting event must come back; got "
        f"{body.get('total')!r} entries: {body.get('entries')!r}"
    )

    entry = body["entries"][0]
    assert entry["event"] == WAIT_EVENT, (
        f"the endpoint answered with the wrong event: {entry!r}"
    )
    data = entry["data"]
    assert data.get("providers"), (
        f"the waiting event must name the providers that refused; got "
        f"{data!r}"
    )
    assert data.get("needs_manual_topup") is False, (
        f"a closed window is not a balance somebody has to top up; got "
        f"{data!r}"
    )
    assert data.get("waited_sec") == 1800.0, (
        f"the waiting event must carry the seconds that were waited out; "
        f"got {data!r}"
    )


# ---------------------------------------------------------------------------
# 2. A service that does not identify itself is not gated
# ---------------------------------------------------------------------------


def test_a_service_that_cannot_identify_itself_is_not_gated(
    tmp_path: Path, monkeypatch,
) -> None:
    """A service that cannot be compared is not blocked.

    "Not comparable" is not "wrong".  A service that does not publish
    an ``instance`` block is judged by the gates that were always
    there — the data plane, the port, the health URL — and a dispatch
    against it is not a handover either: there is no instance to have
    changed hands.
    """
    service = _Service()
    try:
        _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        service.publish(None)
        dispatched = _spy_api_runner(monkeypatch)

        agent._preflight_service_freshness(plan)
        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
        round_results = agent._build_execution_results_from_executor(
            plan, executor,
        )
    finally:
        service.close()

    assert dispatched == ["VP-001"], dispatched
    assert results[0]["status"] == "PASSED", results[0]
    point = round_results["execution_results"][0]
    assert point["status"] == "PASSED", point
    evidence = _evidence_of(point)
    assert evidence.get("serving_before") is None, evidence
    assert evidence.get("serving_after") is None, evidence


# ---------------------------------------------------------------------------
# 3. The round records its own handover
# ---------------------------------------------------------------------------


def test_the_round_records_the_instance_handover(
    tmp_path: Path, monkeypatch,
) -> None:
    """The round says which instance was serving, and which one is now.

    The round's own address can be taken over while the round is
    running.  A reader who only has the round's verdict cannot tell
    that, so the round writes the two snapshots down: the instance that
    served it at the start, and the one serving it now.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._preflight_service_freshness(plan)

        handover = agent.round_instance_handover()
    finally:
        service.close()

    assert handover["before"] is not None, handover
    assert handover["before"].get("boot_id") == "boot-1", handover
    assert "after" in handover, handover


# ---------------------------------------------------------------------------
# 4. The round's handover reaches the plan's own log
# ---------------------------------------------------------------------------


def test_the_round_handover_is_written_to_the_plans_own_log(
    tmp_path: Path, monkeypatch,
) -> None:
    """``round_instance_handover`` is an event in the plan's own log.

    The operator reads the plan's ``execution.log``; a fact that only
    lives in the round's internal state is a fact the operator cannot
    check.  So the round writes the two snapshots into the log it
    already writes, under one event name.

    The round is dispatched the way the loop dispatches it — through
    ``_dispatch_verification_round`` — so the record is made on the
    path a real round takes, not on a path only this test knows.
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    class _Agent:
        def round_instance_handover(self):
            return {
                "before": {"pid": 1, "boot_id": "boot-1"},
                "after": {"pid": 2, "boot_id": "boot-2"},
            }

    class _Orch:
        verification_agent = _Agent()

        def start_verification_cycle(self, **kwargs):
            return {"overall_status": "PASSED"}

    from verification_loop import _dispatch_verification_round

    report, conflict = _dispatch_verification_round(
        PLAN_ID, plan_dir, _Orch(), 1, resume=False,
    )
    assert conflict is None, conflict
    assert report is not None

    logger = ExecutionLogger(PLAN_ID, plans_dir=plans_dir)
    entries = logger.read_logs(PLAN_ID, plans_dir=plans_dir)
    handover = [
        entry for entry in entries
        if entry.get("event") == "round_instance_handover"
    ]
    assert len(handover) == 1, entries
    data = handover[0]["data"]
    assert data["before"]["boot_id"] == "boot-1", data
    assert data["after"]["boot_id"] == "boot-2", data


# ---------------------------------------------------------------------------
# 5. A handover that never settles is never graded as the product
# ---------------------------------------------------------------------------


def test_a_handover_that_never_settles_is_never_graded_as_the_product(
    tmp_path: Path, monkeypatch,
) -> None:
    """A point whose address keeps changing hands is not a product verdict.

    The round's own rules give a contradicted point **one** second look:
    the service is re-ensured, probed, and the point is run again by the
    round's own dispatcher.  When the second look finds the address in
    yet another pair of hands, the answer still cannot be attributed to
    the instance the round started — so the point stays ``invalid_env``.

    This is the whole chain, run by the round's own code: the first
    dispatch, the marking, the second look, the merge.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._preflight_service_freshness(plan)
        # The port changes hands on the first dispatch **and** on the
        # re-run: the handover never settles.
        dispatched = _spy_api_runner(monkeypatch, service, [
            _instance_block("boot-2", agent.plan_dir, state_db),
            _instance_block("boot-3", agent.plan_dir, state_db),
        ])

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        _run_all_vps(executor)
        round_results = agent._build_execution_results_from_executor(
            plan, executor,
        )
        agent._reconcile_round_evidence_conflicts(plan, round_results)
    finally:
        service.close()

    assert dispatched == ["VP-001", "VP-001"], (
        f"the contradicted point must get the round's one second look; got "
        f"{dispatched!r}"
    )
    point = round_results["execution_results"][0]
    assert point["status"] == "invalid_env", (
        f"an answer nobody can attribute is not a verdict about the "
        f"product; got {point!r}"
    )
    assert point["status"] != "FAILED", point
    assert point["status"] != "PASSED", point

    # The entry carries the handover the marking was based on.
    evidence = _evidence_of(point)
    assert evidence.get("serving_before", {}).get("boot_id") == "boot-1", (
        f"the entry must name the instance the round asked; got {evidence!r}"
    )
    assert evidence.get("serving_after", {}).get("boot_id") == "boot-2", (
        f"the entry must name the instance that answered; got {evidence!r}"
    )

    # The second look found the address in yet another pair of hands —
    # which is why the point was **not** replaced by the second look's
    # verdict, even though that verdict was a plain pass.
    snapshots = agent._serving_snapshots()["VP-001"]
    assert snapshots["serving_before"]["boot_id"] == "boot-2", snapshots
    assert snapshots["serving_after"]["boot_id"] == "boot-3", snapshots
