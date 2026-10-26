"""An unreachable service is an environment failure, not a product defect.

2026-10-25 (repair-r1-26)
--------------------------
The production round pointed twelve ``api_test`` VPs at
``127.0.0.1:51725`` — a port nothing had ever listened on.  All twelve
died inside twenty-two seconds with the same ``URLError [Errno 61]``,
and every one of them was written down as **FAILED**: twelve
independent contract violations, according to the report.  There was
exactly one event there — the environment was not serving — and the
verdict table said the product broke twelve times over.

The contract these tests pin:

  1. A request that **never got an answer** is ``BLOCKED``, never
     ``FAILED``.  The verdict carries ``error_kind =
     service_unreachable`` and its evidence keeps the URL that was
     actually called and the errno the transport reported — the two
     facts that make "the plumbing died" checkable.
  2. A request that **did** get an answer keeps the old semantics: a
     2xx/4xx that violates an assertion is ``FAILED``.  The
     classification layer only touches verdicts that never reached the
     service; it never re-judges a contract.
  3. A round in which the service was unreachable is **environment
     blocked** as a whole: the round writes a
     ``verification_round_blocked`` event, and every result of that
     round is recorded ``BLOCKED`` — the upper layer re-runs the round
     instead of repairing a product that was never asked.
"""

from __future__ import annotations

import errno as errno_module
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _api_vp(url: str, assertions=None, vp_id: str = "VP-001") -> dict:
    """An ``api_test`` VP — the shape the framework grades directly."""
    return {
        "id": vp_id,
        "verification_method": "api_test",
        "request": {"method": "GET", "url": url},
        "assertions": assertions if assertions is not None else [{"status": 200}],
    }


def _closed_port() -> int:
    """A port nothing listens on.

    The port is bound (so the kernel picks a free one) and released
    again, so the connect that follows is refused — the production
    shape, reproduced without hard-coding a port that some other
    process might own.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _log_entries(plan_dir: Path) -> list:
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


class _Handler(BaseHTTPRequestHandler):
    """A real HTTP server — the "a response came back" half of the contract."""

    def log_message(self, *args):  # silence the test output
        pass

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({"signal_type": "trend"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _respond


@pytest.fixture(scope="module")
def server_url():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class _FakeExecutor:
    """The executor's verdict table, without the executor."""

    def __init__(self, verdicts: list):
        self._verdicts = verdicts

    def collect_verdicts(self) -> list:
        return [dict(v) for v in self._verdicts]

    def _plan_retired_vps(self) -> dict:
        return {}


def _plan(count: int = 12) -> dict:
    return {
        "verification_points": [
            {
                "id": f"VP-{index:03d}",
                "title": f"vp {index}",
                "verification_method": "api_test",
            }
            for index in range(1, count + 1)
        ]
    }


# ---------------------------------------------------------------------------
# 1. A request that never got an answer is BLOCKED
# ---------------------------------------------------------------------------


def test_connection_refused_grades_blocked(tmp_path: Path):
    """The production shape: nothing is listening on the address.

    ``FAILED`` here would say "the project answered wrong".  Nothing
    answered at all — the round has to say so.
    """
    agent = _make_agent(tmp_path)
    port = _closed_port()
    url = f"http://127.0.0.1:{port}/health"

    verdict = agent._run_api_test_vp(_api_vp(url))

    assert verdict["status"] == "BLOCKED", verdict
    assert verdict["status"] != "FAILED"
    evidence = verdict["evidence"]
    assert evidence["error_kind"] == "service_unreachable"
    # The two facts that make the verdict checkable: which address was
    # called, and what the transport said about it.
    assert evidence["url"] == url
    assert evidence["errno"] == errno_module.ECONNREFUSED  # 61


def test_a_blocked_verdict_keeps_the_original_request_record(tmp_path: Path):
    """The reclassification adds to the evidence — it does not replace it."""
    agent = _make_agent(tmp_path)
    port = _closed_port()
    url = f"http://127.0.0.1:{port}/health"

    verdict = agent._run_api_test_vp(_api_vp(url))

    evidence = verdict["evidence"]
    assert evidence["request"]["url"] == url
    assert evidence["request"]["method"] == "GET"
    assert "URLError" in evidence["error"]


def test_a_name_that_does_not_resolve_is_blocked(
    tmp_path: Path, monkeypatch,
):
    """DNS failure — the same class of failure, a different errno."""
    agent = _make_agent(tmp_path)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(
            socket.gaierror(-2, "Name or service not known")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    verdict = agent._run_api_test_vp(
        _api_vp("http://no-such-host.invalid/health")
    )

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict["evidence"]
    assert evidence["error_kind"] == "service_unreachable"
    assert evidence["errno"] == -2
    assert "no-such-host.invalid" in evidence["url"]


def test_a_schema_rejection_is_not_reclassified(tmp_path: Path):
    """A verdict the service never saw is not an environment failure.

    A malformed VP is the plan's problem, not the plumbing's — it must
    stay FAILED so the plan gets regenerated.
    """
    agent = _make_agent(tmp_path)

    verdict = agent._run_api_test_vp({
        "id": "VP-001",
        "verification_method": "api_test",
        "request": {"method": "GET", "url": "http://127.0.0.1:1/x"},
        "assertions": [],
    })

    assert verdict["status"] == "FAILED", verdict
    assert "error_kind" not in verdict["evidence"]


# ---------------------------------------------------------------------------
# 2. A response that came back keeps the old semantics
# ---------------------------------------------------------------------------


def test_http_200_wrong_value_still_failed(
    tmp_path: Path, server_url: str,
):
    """The classification layer never re-judges a contract.

    The service answered **200** and the field says something else than
    the VP expects.  That is a product defect, and it must keep saying
    so — otherwise the layer would downgrade real defects as well.
    """
    agent = _make_agent(tmp_path)

    verdict = agent._run_api_test_vp(
        _api_vp(
            f"{server_url}/signal",
            [{
                "name": "signal type",
                "json_path": "$.signal_type",
                "equals": "consolidation",
            }],
        )
    )

    assert verdict["status"] == "FAILED", verdict
    assert "error_kind" not in verdict["evidence"]
    assert verdict["evidence"]["status_code"] == 200


# ---------------------------------------------------------------------------
# 3. The round is environment blocked as a whole
# ---------------------------------------------------------------------------


def test_round_marked_environment_blocked(tmp_path: Path):
    """Every VP of the round failed to connect → the round is blocked.

    The production shape: twelve VPs, one dead address, one event.
    The round writes ``verification_round_blocked`` and produces **no**
    FAILED at all — there is no product defect here to repair, only an
    environment that was not serving.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    port = _closed_port()
    url = f"http://127.0.0.1:{port}/health"

    verdicts = []
    for index in range(1, 13):
        vp_id = f"VP-{index:03d}"
        verdict = agent._run_api_test_vp(_api_vp(url, vp_id=vp_id))
        verdicts.append({
            "vp_id": vp_id,
            "status": verdict["status"],
            "reasons": verdict.get("reasons", []),
            "evidence": verdict.get("evidence", {}),
        })

    envelope = agent._build_execution_results_from_executor(
        _plan(12), _FakeExecutor(verdicts),
    )

    entries = _log_entries(agent.plan_dir)
    blocked = [
        e for e in entries
        if e.get("event_type") == "verification_round_blocked"
    ]
    assert len(blocked) == 1, entries
    data = blocked[0]["data"]
    assert data["status"] == "environment_blocked"
    assert data["error_kind"] == "service_unreachable"
    assert len(data["blocked_vps"]) == 12
    assert data["details"][0]["errno"] == errno_module.ECONNREFUSED

    results = envelope["execution_results"]
    assert len(results) == 12
    assert [r["status"] for r in results] == ["BLOCKED"] * 12
    assert [r for r in results if r["status"] == "FAILED"] == []


def test_every_result_of_an_environment_blocked_round_is_blocked(
    tmp_path: Path,
):
    """Twelve results in, twelve BLOCKED out.

    The production round produced twelve identical connection
    refusals.  Whatever the other VPs of the round measured, they
    measured it against a broken environment — so the round is
    re-run, not repaired, and every one of its results says so.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    port = _closed_port()
    agent._run_api_test_vp(_api_vp(f"http://127.0.0.1:{port}/health"))

    verdicts = [
        _verdict(f"VP-{index:03d}", "PASSED" if index % 2 else "FAILED")
        for index in range(1, 13)
    ]
    envelope = agent._build_execution_results_from_executor(
        _plan(12), _FakeExecutor(verdicts),
    )

    results = envelope["execution_results"]
    assert len(results) == 12
    assert {r["status"] for r in results} == {"BLOCKED"}
    for result in results:
        evidence = json.loads(result["evidence"])
        assert evidence["environment_blocked"] is True
        assert evidence["reason_code"] == "environment_blocked"
        assert "environment_blocked" in result["actual_result"]


def test_the_original_status_of_a_blocked_result_is_kept(tmp_path: Path):
    """Nothing is lost: the verdict the VP really got stays readable."""
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    port = _closed_port()
    agent._run_api_test_vp(_api_vp(f"http://127.0.0.1:{port}/health"))

    envelope = agent._build_execution_results_from_executor(
        _plan(2), _FakeExecutor([
            _verdict("VP-001", "FAILED"),
            _verdict("VP-002", "PASSED"),
        ]),
    )

    by_id = {r["id"]: r for r in envelope["execution_results"]}
    assert json.loads(by_id["VP-001"]["evidence"])["original_status"] == "FAILED"
    assert json.loads(by_id["VP-002"]["evidence"])["original_status"] == "PASSED"


def test_a_manual_check_stays_skipped_in_a_blocked_round(tmp_path: Path):
    """A VP that was never going to run is not blocked by the environment.

    ``manual_check`` VPs wait for a human; the environment is not why.
    Rewriting them to BLOCKED would throw away the one thing the
    status says.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    port = _closed_port()
    agent._run_api_test_vp(_api_vp(f"http://127.0.0.1:{port}/health"))

    envelope = agent._build_execution_results_from_executor(
        _plan(2), _FakeExecutor([
            _verdict("VP-001", "FAILED"),
            _verdict("VP-002", "SKIPPED"),
        ]),
    )

    by_id = {r["id"]: r for r in envelope["execution_results"]}
    assert by_id["VP-001"]["status"] == "BLOCKED"
    assert by_id["VP-002"]["status"] == "SKIPPED"


def test_a_healthy_round_is_not_marked_environment_blocked(tmp_path: Path):
    """The negative control: a round that reached the service is normal.

    Without this, "everything is BLOCKED" would pass every test above.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)

    envelope = agent._build_execution_results_from_executor(
        _plan(2), _FakeExecutor([
            _verdict("VP-001", "PASSED"),
            _verdict("VP-002", "FAILED"),
        ]),
    )

    results = envelope["execution_results"]
    assert [r["status"] for r in results] == ["PASSED", "FAILED"]
    entries = _log_entries(agent.plan_dir)
    assert [
        e for e in entries
        if e.get("event_type") == "verification_round_blocked"
    ] == []


def _verdict(vp_id: str, status: str) -> dict:
    return {
        "vp_id": vp_id,
        "status": status,
        "reasons": [f"{vp_id} said something"],
        "evidence": {"vp_id": vp_id},
    }
