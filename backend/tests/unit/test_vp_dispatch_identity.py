"""TDD tests for the pre-dispatch **instance identity** check.

Background
----------
2026-10-25 production round: a verification point (VP-007) pointed at
``127.0.0.1:51725`` and the round could not tell whether the answer
came from the instance the round had started or from a stranger that
had taken the port.  Two rounds earlier the same class of question had
been graded wrong: the port answered ``200 status: ok`` and the data
behind it was another database.

So the round asks the service **who it is** before it sends a request:
``/health/data`` carries an ``instance`` block — the boot id minted
when the process started, the plans tree it serves, the state database
it reads.  Three comparisons decide whether the answer may be trusted:

  * the **boot id** must be the one this round's ``ensure`` recorded —
    a different boot id is a different process;
  * the **plans tree** must be the one this round's plan lives in —
    another tree is another set of plans;
  * the **state database's inode** must be the one this process has
    open — the same path with another inode is another file, i.e.
    another data plane.

Any of them failing means the request is **not** sent: a verdict about
somebody else's instance is worse than no verdict at all.  The VP is
BLOCKED with ``error_kind = service_instance_mismatch``, and the round
log carries an event of the same name, with both sides of every
comparison.

Test contract:

  1. A matching instance → the api_test is dispatched normally.
  2. A foreign boot id → BLOCKED, and the event names both boot ids.
  3. A foreign plans tree → BLOCKED.
  4. A foreign state database → BLOCKED.
  5. A service that does not identify itself → not gated at all.
  6. The round's own re-ensure moves the expectation with it — a
     service the round itself restarted must not be blocked against
     the boot id of the instance it replaced.
  7. The expected plans tree is the one the round's service
     **declared** when it was started — a fixture data plane is a
     legal deployment, and the round's own directories stay accepted.
     A tree that is neither declared nor the round's own is still a
     stranger (see the negative control at the end of this file).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures
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
        "started_at": "2026-10-25T00:00:00+00:00",
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


def _prepare(tmp_path: Path, monkeypatch, service: _Service, *, listening=True):
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
        lambda port: PortProbe(port=port, listening=listening),
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


def _spy_api_runner(monkeypatch, dispatched: List[str]) -> None:
    """Record every ``api_test`` dispatch; answer PASSED."""
    import verification_api_runner as var

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
        return _Result()

    monkeypatch.setattr(var, "run_api_verification", fake_run)


def _run_all_vps(executor) -> list:
    """Run every VP of *executor* through the runner, in order."""
    async def _drive():
        return [
            await executor.sub_agent_runner(vp)
            for vp in executor.verification_plan["vps"]
        ]

    return asyncio.run(_drive())


def _log_entries(plan_dir: Path) -> list:
    """Every entry the round wrote to its verification log."""
    entries = []
    for log_file in sorted((plan_dir / "logs").glob("verification_*.log")):
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:  # pragma: no cover - defensive
                continue
    return entries


def _mismatch_events(agent) -> list:
    return [
        entry for entry in _log_entries(agent.plan_dir)
        if entry.get("event_type") == "service_instance_mismatch"
    ]


# ---------------------------------------------------------------------------
# 1. A matching instance → the request goes out
# ---------------------------------------------------------------------------


def test_a_matching_instance_identity_dispatches_the_vp(
    tmp_path: Path, monkeypatch
):
    """The service is the instance the round started → nothing is gated.

    The check is a filter, not a wall: an instance that identifies
    itself as the round's own must not cost the round its request.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        agent._preflight_service_freshness(plan)
        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == ["VP-001"], dispatched
    assert results[0]["status"] == "PASSED", results[0]
    assert _mismatch_events(agent) == []


# ---------------------------------------------------------------------------
# 2. A foreign boot id → the request does not go out
# ---------------------------------------------------------------------------


def test_a_foreign_boot_id_blocks_the_vp(tmp_path: Path, monkeypatch):
    """Somebody else's process answered → the request is not sent.

    The request is **not** dispatched: an answer from another instance
    is a confident statement about somebody else's data.  The verdict
    is BLOCKED, never FAILED — FAILED would say "the project answered
    wrong", and the project was never asked.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        # The round's ensure recorded this instance ...
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._preflight_service_freshness(plan)
        # ... and then another process took the port.
        service.publish(
            _instance_block("boot-2", agent.plan_dir, state_db),
        )
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == [], "the request went to another instance"
    assert results[0]["status"] == "BLOCKED", results[0]
    assert results[0]["status"] != "FAILED", results[0]
    evidence = results[0]["evidence"]
    assert evidence["error_kind"] == "service_instance_mismatch", evidence
    assert "service_instance_mismatch" in " ".join(results[0]["reasons"]), (
        results[0]
    )

    events = _mismatch_events(agent)
    assert len(events) == 1, events
    data = events[0]["data"]
    assert data["expected_boot_id"] == "boot-1", data
    assert data["actual_boot_id"] == "boot-2", data
    assert data["mismatched_fields"] == ["boot_id"], data
    assert "expected_plans_dir" in data, data
    assert "actual_plans_dir" in data, data


# ---------------------------------------------------------------------------
# 3. A foreign plans tree → the request does not go out
# ---------------------------------------------------------------------------


def test_a_foreign_plans_dir_blocks_the_vp(tmp_path: Path, monkeypatch):
    """The answering instance serves another tree → BLOCKED.

    Same boot id, same data plane — and still the wrong instance: it
    is serving a different set of plans, so its answer says nothing
    about this round's plan.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._preflight_service_freshness(plan)
        service.publish(
            _instance_block(
                "boot-1", tmp_path / "another-tree", state_db,
            ),
        )
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == [], "the request went to another instance"
    assert results[0]["status"] == "BLOCKED", results[0]
    evidence = results[0]["evidence"]
    assert evidence["error_kind"] == "service_instance_mismatch", evidence

    events = _mismatch_events(agent)
    assert len(events) == 1, events
    data = events[0]["data"]
    assert data["mismatched_fields"] == ["plans_dir"], data
    assert data["expected_plans_dir"] != data["actual_plans_dir"], data
    assert data["actual_plans_dir"] == str(tmp_path / "another-tree"), data


# ---------------------------------------------------------------------------
# 4. A foreign state database → the request does not go out
# ---------------------------------------------------------------------------


def test_a_foreign_state_database_blocks_the_vp(tmp_path: Path, monkeypatch):
    """The same path, another file → BLOCKED.

    The instance publishes the inode of the database it reads.  A
    different inode is a different file — the same path with somebody
    else's data behind it, which is exactly the production shape.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        other_db = tmp_path / "other.db"
        other_db.write_bytes(b"")
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._preflight_service_freshness(plan)
        service.publish(
            _instance_block("boot-1", agent.plan_dir, other_db),
        )
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == [], "the request went to another instance"
    assert results[0]["status"] == "BLOCKED", results[0]
    evidence = results[0]["evidence"]
    assert evidence["error_kind"] == "service_instance_mismatch", evidence
    assert evidence["mismatched_fields"] == ["state_db_inode"], evidence


# ---------------------------------------------------------------------------
# 5. A service that does not identify itself → not gated
# ---------------------------------------------------------------------------


def test_a_service_without_an_instance_block_is_not_gated(
    tmp_path: Path, monkeypatch
):
    """A service that cannot be compared is not blocked.

    "Not comparable" is not "wrong".  A service that does not publish
    an ``instance`` block is judged by the gates that were always
    there — the data plane, the port, the health URL.
    """
    service = _Service()
    try:
        _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        service.publish(None)
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        agent._preflight_service_freshness(plan)
        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == ["VP-001"], dispatched
    assert results[0]["status"] == "PASSED", results[0]
    assert _mismatch_events(agent) == []


# ---------------------------------------------------------------------------
# 6. The round's own re-ensure moves the expectation with it
# ---------------------------------------------------------------------------


def test_a_re_ensured_service_is_not_blocked_against_its_old_boot_id(
    tmp_path: Path, monkeypatch
):
    """A service the round itself restarted must not be blocked.

    The round's own recovery starts a **new** instance — a new process,
    a new boot id.  The expectation follows it, or the round would
    block a service it brought back itself.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        # The service is "gone" as far as the liveness probe is
        # concerned, so the round's recovery fires.
        agent, plan = _prepare(
            tmp_path, monkeypatch, service, listening=False,
        )
        service.publish(
            _instance_block("boot-1", agent.plan_dir, state_db),
        )
        agent._preflight_service_freshness(plan)
        # The recovery brings the service back — as a new process.
        service.publish(
            _instance_block("boot-2", agent.plan_dir, state_db),
        )
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == ["VP-001"], dispatched
    assert results[0]["status"] == "PASSED", results[0]
    assert _mismatch_events(agent) == []


# ---------------------------------------------------------------------------
# 7. The expected plans tree is the one the service **declared**
# ---------------------------------------------------------------------------
#
# 2026-10-27 round: the round's service is started against a fixture
# data plane on purpose (``PDT_PLANS_DIR=.pdt/vp_quota/plans`` inside the
# plan's ``start_cmd``).  It publishes that tree honestly; the gate only
# knew the round's own directories, so every verification point of the
# round stopped dead at ``identity_mismatch`` before one request went
# out.  The expectation now follows the declaration the round recorded
# from the service itself, the round's own directories stay tolerated,
# and the comparisons that catch a swapped instance keep their teeth.


def _recorded_agent(tmp_path: Path, plans_dir: Optional[Path]):
    """An agent that recorded *plans_dir* as the service's declaration.

    *plans_dir* is ``None`` for a service the round recorded no
    declaration for — the shape of an instance block without a plans
    tree.
    """
    agent = _make_agent(tmp_path)
    block: Dict[str, Any] = {"boot_id": "boot-1", "pid": 4242}
    if plans_dir is not None:
        block["plans_dir"] = str(plans_dir)
    agent._service_instance_identity["api"] = block
    return agent


def test_a_declared_fixture_data_plane_is_not_a_mismatch(tmp_path: Path):
    """The round's own service declared a fixture tree → not a stranger.

    The plan's ``start_cmd`` points the service at a fixture data
    plane, and the round recorded the service declaring exactly that.
    The round's plan directory lives somewhere else entirely; that no
    longer turns the round's own service into somebody else's.
    """
    fixture = tmp_path / "fixture" / "plans"
    agent = _recorded_agent(tmp_path, fixture)
    actual = {"boot_id": "boot-1", "pid": 4242, "plans_dir": str(fixture)}

    assert agent._compare_service_instance("api", actual, 12345) is None


def test_a_fixture_data_plane_round_dispatches_its_vp(
    tmp_path: Path, monkeypatch,
):
    """The B14 shape end to end: the declaration is read, then honoured.

    The round's preflight reads the fixture declaration from the real
    ``/health/data`` of the service, and the VP's dispatch compares
    against it over the same road.  Before the fix the VP never left
    the round: the service was the round's own and the gate called it
    a stranger.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        fixture = tmp_path / "fixture" / "plans"
        service.publish(_instance_block("boot-1", fixture, state_db))
        agent._preflight_service_freshness(plan)
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == ["VP-001"], dispatched
    assert results[0]["status"] == "PASSED", results[0]
    assert _mismatch_events(agent) == []


def test_a_third_plans_tree_is_still_a_mismatch(tmp_path: Path):
    """Neither the declared tree nor the round's own → still BLOCKED.

    The negative control for the loosening above: recorded ≠ actual,
    and the actual tree is not one of the round's directories either,
    so this is a stranger and the request must not go out.
    """
    fixture = tmp_path / "fixture" / "plans"
    agent = _recorded_agent(tmp_path, fixture)
    actual = {
        "boot_id": "boot-1",
        "pid": 4242,
        "plans_dir": str(tmp_path / "somebody-else" / "plans"),
    }

    mismatch = agent._compare_service_instance("api", actual, 12345)

    assert mismatch is not None, "a stranger's plans tree was let through"
    assert mismatch["mismatched_fields"] == ["plans_dir"], mismatch
    assert mismatch["expected_plans_dir"] == str(fixture.resolve()), mismatch
    assert mismatch["actual_plans_dir"] == str(
        tmp_path / "somebody-else" / "plans"
    ), mismatch


def test_without_a_declaration_the_rounds_own_tree_is_expected(
    tmp_path: Path,
):
    """No recorded plans tree → the round's own directories, as before.

    A service that never published a plans tree is still gated against
    where the round lives: its own plans root passes, a third tree does
    not.  The fallback is the behaviour every earlier round relied on.
    """
    agent = _recorded_agent(tmp_path, None)
    own_root = agent.plan_dir.parent

    actual_own = {"boot_id": "boot-1", "plans_dir": str(own_root)}
    assert agent._compare_service_instance(
        "api", actual_own, 12345,
    ) is None

    actual_foreign = {
        "boot_id": "boot-1",
        "plans_dir": str(tmp_path / "a-third-tree" / "plans"),
    }
    mismatch = agent._compare_service_instance("api", actual_foreign, 12345)
    assert mismatch is not None, "a foreign tree was let through"
    assert mismatch["mismatched_fields"] == ["plans_dir"], mismatch


# ---------------------------------------------------------------------------
# 8. The same question for the state database
# ---------------------------------------------------------------------------


def _recorded_inode_agent(tmp_path: Path, inode: Optional[int]):
    """An agent that recorded *inode* as the service's declaration."""
    agent = _make_agent(tmp_path)
    block: Dict[str, Any] = {"boot_id": "boot-1", "pid": 4242}
    if inode is not None:
        block["state_db_inode"] = int(inode)
    agent._service_instance_identity["api"] = block
    return agent


def test_a_declared_fixture_state_database_is_not_a_mismatch(
    tmp_path: Path, monkeypatch,
):
    """The round's own service declared a fixture database → not a stranger.

    ``PDT_STATE_DB_PATH`` in the plan's ``start_cmd`` points the service
    at its own database, and the round recorded the service declaring
    exactly that inode.  The backend has another database open; that no
    longer turns the round's own service into somebody else's — the
    2026-10-28 shape of 20261024-429-plan.
    """
    _state_db(tmp_path, monkeypatch)
    agent = _recorded_inode_agent(tmp_path, 424242)
    actual = {"boot_id": "boot-1", "pid": 4242, "state_db_inode": 424242}

    assert agent._compare_service_instance("api", actual, 12345) is None


def test_a_third_state_database_is_still_a_mismatch(
    tmp_path: Path, monkeypatch,
):
    """Neither the declared database nor the backend's own → BLOCKED.

    The negative control for the loosening above: a third inode is a
    stranger's data plane and the request must not go out.
    """
    _state_db(tmp_path, monkeypatch)
    agent = _recorded_inode_agent(tmp_path, 424242)
    actual = {"boot_id": "boot-1", "pid": 4242, "state_db_inode": 999999}

    mismatch = agent._compare_service_instance("api", actual, 12345)

    assert mismatch is not None, "a stranger's database was let through"
    assert mismatch["mismatched_fields"] == ["state_db_inode"], mismatch
    assert mismatch["expected_state_db_inode"] == 424242, mismatch
    assert mismatch["actual_state_db_inode"] == 999999, mismatch


def test_without_a_declaration_the_rounds_own_database_is_expected(
    tmp_path: Path, monkeypatch,
):
    """No recorded inode → the backend's own database, as before."""
    state_db = _state_db(tmp_path, monkeypatch)
    agent = _recorded_inode_agent(tmp_path, None)
    own_inode = int(state_db.stat().st_ino)

    own = {"boot_id": "boot-1", "state_db_inode": own_inode}
    assert agent._compare_service_instance("api", own, 12345) is None

    foreign = {"boot_id": "boot-1", "state_db_inode": own_inode + 1}
    mismatch = agent._compare_service_instance("api", foreign, 12345)
    assert mismatch is not None, "a foreign database was let through"
    assert mismatch["mismatched_fields"] == ["state_db_inode"], mismatch
