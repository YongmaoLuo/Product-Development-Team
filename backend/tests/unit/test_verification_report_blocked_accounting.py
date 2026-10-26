"""An environment-blocked verification point is not a product failure.

2026-10-25 (repair-r1-32)
--------------------------
The production round pointed twelve ``api_test`` VPs at
``127.0.0.1:51725`` — a port nothing had ever listened on.  All twelve
died inside twenty-two seconds with the same ``URLError [Errno 61]``:
**one** event, amplified twelve times.  The report then said the product
broke twelve times over, and the repair path set out to fix a project
that was never asked.

Two things were wrong, and this file pins both.

  1. **The statistics.**  The FAILED count is derived from the report's
     ``verification_results`` (``verification/orchestrator.py``), so an
     entry the judgment LLM wrote down as ``FAILED`` counted as a
     product defect — even though the executor's verdict said the
     request never reached the service.  The fix is at the source: the
     report **keeps** the entry (annotated with the reason it was
     blocked) and the FAILED statistics stop counting it.  A genuine
     failure — the product answered and broke the contract — is never
     re-read as an environment failure; that is the negative control
     these tests exist for.

  2. **The round.**  One environment hiccup ended the round.  The fix
     is one automatic re-run at the end of the round: bring the service
     back, probe it, and re-run the blocked verification points — **at
     most once**.  If the service is still unreachable the second time,
     the round is marked ``environment_blocked`` and the original
     evidence is kept.

The accounting is done where the report is built, because that is the
artifact every downstream reader takes its numbers from.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The address the production round could not reach.
DEAD_URL = "http://127.0.0.1:51725/api/plans"

#: The evidence a transport failure leaves behind.  ``error_kind`` and
#: ``reason_code`` are the two names the reader matches on; the errno
#: and the URL are the facts that make the claim checkable.
UNREACHABLE_EVIDENCE = {
    "vp_id": "VP-010",
    "request": {"method": "GET", "url": DEAD_URL},
    "error": "URLError: <urlopen error [Errno 61] Connection refused>",
    "error_kind": "service_unreachable",
    "reason_code": "service_unreachable",
    "errno": 61,
    "url": DEAD_URL,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ReportingTool:
    """A ``coding_tool`` stand-in whose judgment answers with a report.

    The judgment LLM is the one that used to write a ``BLOCKED``
    verification point down as ``FAILED`` — so the tests hand it a
    report that says exactly that.
    """

    def __init__(self, report: dict):
        self.report = report
        self.calls = 0

    def query_json(self, *args, **kwargs):
        self.calls += 1
        return json.loads(json.dumps(self.report))


def _make_agent(tmp_path: Path, tool=None):
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=tool,
    )


def _entry(report: dict, vp_id: str) -> dict:
    """The report's entry for *vp_id*."""
    for entry in report.get("verification_results", []):
        if isinstance(entry, dict) and str(entry.get("id")) == vp_id:
            return entry
    raise AssertionError(
        f"the report has no entry for {vp_id}: "
        f"{report.get('verification_results')!r}"
    )


def _blocked_result(vp_id: str = "VP-010") -> dict:
    """The executor's verdict for a VP whose service was unreachable."""
    return {
        "id": vp_id,
        "status": "BLOCKED",
        "actual_result": (
            "service_unreachable: the request to "
            f"{DEAD_URL} never reached the service"
        ),
        "evidence": json.dumps(dict(UNREACHABLE_EVIDENCE), ensure_ascii=False),
    }


def _failed_result(vp_id: str = "VP-011") -> dict:
    """The executor's verdict for a VP the product really failed."""
    return {
        "id": vp_id,
        "status": "FAILED",
        "actual_result": "assertion [status] failed: expected 200, got 500",
        "evidence": json.dumps({
            "vp_id": vp_id,
            "status_code": 500,
            "assertions_failed": ["[status] expected 200, got 500"],
        }, ensure_ascii=False),
    }


def _passed_result(vp_id: str = "VP-012") -> dict:
    return {
        "id": vp_id,
        "status": "PASSED",
        "actual_result": "all assertions passed",
        "evidence": json.dumps({"status_code": 200}, ensure_ascii=False),
    }


def _llm_report(entries: list) -> dict:
    """A judgment report that repeats the executor's statuses."""
    return {
        "overall_status": (
            "FAILED" if any(e.get("status") == "FAILED" for e in entries)
            else "PASSED"
        ),
        "summary": "the judgment LLM's own words",
        "verification_results": entries,
        "requirement_deviations": [],
    }


# ---------------------------------------------------------------------------
# 1. The statistics: a blocked entry is not a failed entry
# ---------------------------------------------------------------------------


def test_blocked_not_counted_as_failed_when_the_service_was_unreachable(tmp_path):
    """The judgment said FAILED; the executor said the service was dead.

    The executor's verdict wins for this entry: it is kept in the report
    (the reader must be able to see it) but it is not a FAILED entry, so
    the FAILED statistics do not count it.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-010", "status": "FAILED", "actual_result": "connection refused"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_blocked_result()],
    })

    entry = _entry(report, "VP-010")
    assert entry["status"] == "BLOCKED", (
        "a verification point the environment stopped must not be "
        f"reported as FAILED; got {entry['status']!r}"
    )
    assert report["overall_status"] != "FAILED", (
        "one environment hiccup must not turn the product verdict red; "
        f"got {report['overall_status']!r}"
    )


def test_environment_blocked_entry_is_kept_in_the_report_with_its_reason(tmp_path):
    """Kept, not dropped — and the reason is written down."""
    tool = _ReportingTool(_llm_report([
        {"id": "VP-010", "status": "FAILED", "actual_result": "connection refused"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_blocked_result()],
    })

    entry = _entry(report, "VP-010")
    assert entry.get("environment_blocked") is True, (
        "the entry must say that the environment — not the product — "
        f"stopped it; got {entry!r}"
    )
    assert entry.get("reason_code") == "service_unreachable", (
        "the entry must carry the code that says why it was blocked; "
        f"got {entry.get('reason_code')!r}"
    )
    assert "service_unreachable" in str(entry.get("actual_result") or ""), (
        "the entry must be annotated with the reason it was blocked; "
        f"got {entry.get('actual_result')!r}"
    )


def test_report_names_the_environment_blocked_verification_points(tmp_path):
    """The report says which points the environment stopped, and why."""
    tool = _ReportingTool(_llm_report([
        {"id": "VP-010", "status": "FAILED", "actual_result": "connection refused"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_blocked_result()],
    })

    assert report.get("environment_blocked") is True, (
        "the report must say that the environment blocked part of it"
    )
    assert report.get("environment_blocked_vps") == ["VP-010"], (
        "the report must name the verification points it could not judge; "
        f"got {report.get('environment_blocked_vps')!r}"
    )
    assert str(report.get("environment_blocked_reason") or ""), (
        "the report must say why the environment blocked it"
    )


def test_real_assertion_failure_still_failed_and_keeps_the_round_red(tmp_path):
    """The negative control: a broken contract is still a failure.

    The product answered (the evidence carries a ``status_code``) and
    broke the assertion.  Nothing about that is the environment's
    fault, and the fix must not turn it into one.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-011", "status": "FAILED", "actual_result": "500 != 200"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_failed_result()],
    })

    entry = _entry(report, "VP-011")
    assert entry["status"] == "FAILED", (
        "a real failure must stay a failure; got " f"{entry['status']!r}"
    )
    assert report["overall_status"] == "FAILED", (
        "a real failure must keep the product verdict red; got "
        f"{report['overall_status']!r}"
    )


def test_a_real_failure_next_to_a_blocked_entry_still_fails(tmp_path):
    """One blocked point does not launder the genuine failures beside it."""
    tool = _ReportingTool(_llm_report([
        {"id": "VP-010", "status": "FAILED", "actual_result": "connection refused"},
        {"id": "VP-011", "status": "FAILED", "actual_result": "500 != 200"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_blocked_result(), _failed_result()],
    })

    assert _entry(report, "VP-010")["status"] == "BLOCKED"
    assert _entry(report, "VP-011")["status"] == "FAILED"
    assert report["overall_status"] == "FAILED", (
        "the genuine failure must keep the round red; got "
        f"{report['overall_status']!r}"
    )


def test_only_environment_blocked_entries_leave_the_round_unjudged(tmp_path):
    """Nothing was judged: the round says so instead of claiming PASSED.

    ``SKIPPED`` is the verdict table's "nothing happened" value, and it
    is what the report must carry when every point was stopped by the
    environment — claiming ``PASSED`` there would be a verdict about a
    product nobody asked.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-010", "status": "FAILED", "actual_result": "connection refused"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_blocked_result()],
    })

    assert report["overall_status"] == "SKIPPED", (
        "a round in which nothing was judged must not claim PASSED; got "
        f"{report['overall_status']!r}"
    )


# ---------------------------------------------------------------------------
# 2. The round: one automatic re-run
# ---------------------------------------------------------------------------


class _FakeDecl:
    """A declared service — only its port is read by the tests."""

    def __init__(self, port: int):
        self.port = port
        self.name = "api"


class _FakeRuntime:
    """A runtime that says the service is up and where it listens."""

    def __init__(self, port: int):
        self.port = port
        self.ready = True
        self.origin = "spawned"
        self.unusable = False
        self.pid = 4242
        self.detail = "spawned by the test"


class _FakeRuntimeMap:
    def __init__(self, runtimes: dict):
        self.runtimes = runtimes
        self.ready_names = list(runtimes)
        self.unready_ports = []
        self.duration_seconds = 0.0


def _blocked_round(tmp_path, tool=None):
    """An agent whose round produced one environment-blocked VP."""
    agent = _make_agent(tmp_path, tool)
    agent._service_declarations = {"api": _FakeDecl(51725)}
    plan_data = {
        "verification_points": [{
            "id": "VP-010",
            "verification_method": "api_test",
            "request": {"method": "GET", "url": DEAD_URL},
            "assertions": [{"status": 200}],
            "uses_services": ["api"],
        }],
    }
    execution_results = {
        "verification_points": plan_data["verification_points"],
        "execution_results": [_blocked_result()],
    }
    return agent, plan_data, execution_results


def test_auto_rerun_after_ensure_probe_when_the_service_came_back(tmp_path, monkeypatch):
    """The service came back: the blocked point runs again, and its new
    verdict replaces the blocked one."""
    agent, plan_data, execution_results = _blocked_round(tmp_path)

    agent._ensure_declared_services = lambda declarations: _FakeRuntimeMap(
        {"api": _FakeRuntime(51725)},
    )
    monkeypatch.setattr(
        type(agent), "_service_port_is_live", lambda self, port: True,
    )
    monkeypatch.setattr(
        type(agent), "_runtime_health_url", lambda self, name, port: "",
    )

    rerun_calls = []

    async def _fake_runner(vp):
        rerun_calls.append(str(vp.get("id")))
        return {
            "status": "PASSED",
            "reasons": ["all assertions passed"],
            "evidence": {"status_code": 200},
        }

    agent._vp_dispatch_runner = _fake_runner
    agent._executor_vps_by_id = {
        "VP-010": {
            "id": "VP-010",
            "method": "api_test",
            "request": {"method": "GET", "url": DEAD_URL},
            "assertions": [{"status": 200}],
            "uses_services": ["api"],
        },
    }

    updated = agent._rerun_environment_blocked_vps(
        plan_data, execution_results,
    )

    assert rerun_calls == ["VP-010"], (
        "the blocked verification point must be re-run; got "
        f"{rerun_calls!r}"
    )
    entry = updated["execution_results"][0]
    assert entry["status"] == "PASSED", (
        "the re-run's verdict must replace the blocked one; got "
        f"{entry['status']!r}"
    )


def test_second_failure_marks_round_environment_blocked_and_keeps_the_evidence(
    tmp_path, monkeypatch,
):
    """The second look says the same thing: no re-run, the round is
    blocked as a whole, and the original evidence is kept."""
    agent, plan_data, execution_results = _blocked_round(tmp_path)

    agent._ensure_declared_services = lambda declarations: _FakeRuntimeMap(
        {"api": _FakeRuntime(51725)},
    )
    monkeypatch.setattr(
        type(agent), "_service_port_is_live", lambda self, port: True,
    )
    monkeypatch.setattr(
        type(agent), "_runtime_health_url",
        lambda self, name, port: "http://127.0.0.1:51725/health",
    )
    monkeypatch.setattr(
        "verification_agent._health_url_answers", lambda url: False,
    )

    rerun_calls = []

    async def _fake_runner(vp):
        rerun_calls.append(str(vp.get("id")))
        return {"status": "PASSED", "evidence": {}}

    agent._vp_dispatch_runner = _fake_runner
    agent._executor_vps_by_id = {
        "VP-010": {"id": "VP-010", "method": "api_test"},
    }

    updated = agent._rerun_environment_blocked_vps(
        plan_data, execution_results,
    )

    assert rerun_calls == [], (
        "a service that is still unreachable must not be asked again; got "
        f"{rerun_calls!r}"
    )
    assert agent._environment_blocked is True, (
        "the round must be marked environment blocked when the service is "
        "still unreachable"
    )
    entry = updated["execution_results"][0]
    assert entry["status"] == "BLOCKED", (
        "the blocked entry stays blocked; got " f"{entry['status']!r}"
    )
    assert DEAD_URL in str(entry.get("evidence") or ""), (
        "the original evidence must be kept; got "
        f"{entry.get('evidence')!r}"
    )


def test_the_re_run_happens_at_most_once(tmp_path, monkeypatch):
    """A second call does not re-run anything: one attempt per round."""
    agent, plan_data, execution_results = _blocked_round(tmp_path)

    agent._ensure_declared_services = lambda declarations: _FakeRuntimeMap(
        {"api": _FakeRuntime(51725)},
    )
    monkeypatch.setattr(
        type(agent), "_service_port_is_live", lambda self, port: True,
    )
    monkeypatch.setattr(
        type(agent), "_runtime_health_url", lambda self, name, port: "",
    )

    rerun_calls = []

    async def _fake_runner(vp):
        rerun_calls.append(str(vp.get("id")))
        return {"status": "PASSED", "evidence": {"status_code": 200}}

    agent._vp_dispatch_runner = _fake_runner
    agent._executor_vps_by_id = {
        "VP-010": {"id": "VP-010", "method": "api_test"},
    }

    agent._rerun_environment_blocked_vps(plan_data, execution_results)
    agent._rerun_environment_blocked_vps(plan_data, execution_results)

    assert rerun_calls == ["VP-010"], (
        "the re-run happens at most once per round; got " f"{rerun_calls!r}"
    )


def test_a_round_without_blocked_points_is_left_alone(tmp_path):
    """Nothing was blocked: the round does not touch the service."""
    agent = _make_agent(tmp_path)
    agent._service_declarations = {"api": _FakeDecl(51725)}

    def _boom(declarations):  # pragma: no cover - must not be reached
        raise AssertionError("a healthy round must not re-ensure anything")

    agent._ensure_declared_services = _boom

    execution_results = {
        "verification_points": [],
        "execution_results": [_passed_result()],
    }
    updated = agent._rerun_environment_blocked_vps([], execution_results)

    assert updated is execution_results
    assert agent._environment_blocked is False


# ---------------------------------------------------------------------------
# 3. The reason code survives the round-level rewrite
# ---------------------------------------------------------------------------


def test_the_round_level_rewrite_keeps_the_per_point_reason_code(tmp_path):
    """A blocked round rewrites every status — but not the reason.

    The rewrite replaces ``reason_code`` with the round's own code, so
    the per-point code has to be kept somewhere else: without it, a
    point that genuinely failed cannot be told apart from one the
    environment stopped.
    """
    agent = _make_agent(tmp_path)

    marked = agent._mark_results_environment_blocked([
        _blocked_result(),
        _failed_result(),
    ])

    blocked_evidence = json.loads(marked[0]["evidence"])
    failed_evidence = json.loads(marked[1]["evidence"])

    assert blocked_evidence.get("original_reason_code") == "service_unreachable", (
        "the per-point reason code must survive the round-level rewrite; "
        f"got {blocked_evidence.get('original_reason_code')!r}"
    )
    assert failed_evidence.get("original_reason_code") is None, (
        "a genuine failure must not be re-read as an environment "
        f"failure; got {failed_evidence.get('original_reason_code')!r}"
    )
