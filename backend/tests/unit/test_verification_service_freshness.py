"""TDD tests for the service-freshness round-start preflight ("VP0").

Background
----------
2026earlier production plan post-mortem: every VP's *binary* freshness check
PASSED (the disk ``.so`` was fresh), yet VP-001/VP-002 failed with
``curl: HTTP_CODE:000`` because nothing listened on port 8080. A later
VP whose own script did ``pkill + relaunch + wait-ready`` PASSED — the
restart capability existed but nothing ran it *before* the
port-dependent VPs. The preflight (``VerificationAgent.
_preflight_service_freshness``) closes that ordering gap: it runs once
per round inside ``run_full_verification``, after plan generation and
before the executor dispatches the first VP.

Test contract:

  1. No ports in the plan → preflight is a no-op, no restart agent,
     nothing blocked.
  2. Dead port + successful restart → report action ``restarted``,
     no blocked ports, VPs run normally.
  3. Dead port + failed restart → the port lands in
     ``_blocked_service_ports`` and every VP referencing it is
     short-circuited to a BLOCKED verdict without invoking the
     sub-agent; VPs on other ports still run.
  4. Binary stale at preflight time → rebuild is attempted BEFORE the
     service restart (relaunching against a stale artefact would serve
     old code).
  5. The preflight never raises — an assessment error degrades to
     "proceed unblocked" and is audit-logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


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


def _plan(*target_urls: str) -> dict:
    """Build a plan whose VPs carry ``target_url``.

    Two 2026-09-18 changes shape this fixture:

      * ``code_review`` is used because ``api_test`` VPs no longer reach
        ``_run_single_vp_async`` (the framework grades them directly), and
        these tests stub that method to observe *whether a VP was
        dispatched at all*.
      * the port now travels in ``target_url`` rather than ``test_command``
        — VPs have no ``test_command`` any more, and ``target_url`` is one
        of the fields the executor schema actually carries, so it is what
        the blocked-port gate can read.
    """
    return {
        "verification_points": [
            {
                "id": f"VP-{i:03d}",
                "title": f"vp {i}",
                "verification_method": "code_review",
                "target_url": url,
            }
            for i, url in enumerate(target_urls, start=1)
        ]
    }


class _RestartResult:
    def __init__(self, success: bool, ports=None, diagnostics=""):
        self.success = success
        self.ports_restarted = list(ports or [])
        self.diagnostics = diagnostics
        self.agent_used = False
        self.commands_run = []
        self.post_check_passed = success

    def to_dict(self):
        return {
            "success": self.success,
            "ports_restarted": list(self.ports_restarted),
            "diagnostics": self.diagnostics,
            "post_check_passed": self.post_check_passed,
            "agent_used": self.agent_used,
            "commands_run": list(self.commands_run),
            "duration_seconds": 0.0,
        }


class _Freshness:
    """Minimal FreshnessReport stand-in."""

    def __init__(self, stale: bool):
        self.is_stale = stale
        self.kind = "rust_python"
        self.detail = "stub"
        self.evidence = "stub"
        self.status = "FAILED" if stale else "PASSED"
        self.rebuild_command = "cargo build"
        self.binary_path = None


def _patch_assess(monkeypatch, ports):
    """Stub service_freshness.assess_service_freshness to flag the given
    ports as restart-pending with empty probes."""
    from service_freshness import ServiceFreshnessReport, PortProbe

    def fake_assess(project_dir, plan_data):
        report = ServiceFreshnessReport(
            ports_requested=list(ports),
            probes=[PortProbe(port=p) for p in ports],
            decisions={p: "restart" for p in ports},
        )
        report.action = "restart_pending"
        report.detail = f"restart required for port(s) {sorted(ports)}"
        return report

    monkeypatch.setattr(
        "service_freshness.assess_service_freshness", fake_assess,
    )


# ---------------------------------------------------------------------------
# 1. No ports → no-op
# ---------------------------------------------------------------------------

def test_preflight_no_ports_is_noop(tmp_path, monkeypatch) -> None:
    agent = _make_agent(tmp_path)
    called: list[bool] = []
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: called.append(True),
    )
    report = agent._preflight_service_freshness(_plan("pytest -q"))
    assert report.action == "noop"
    assert called == []
    assert agent._blocked_service_ports == set()


# ---------------------------------------------------------------------------
# 2. Restart success → restarted, nothing blocked
# ---------------------------------------------------------------------------

def test_preflight_restart_success_marks_restarted(tmp_path, monkeypatch) -> None:
    agent = _make_agent(tmp_path)
    _patch_assess(monkeypatch, {8080})
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _Freshness(stale=False),
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: _RestartResult(True, ports=[8080]),
    )
    report = agent._preflight_service_freshness(
        _plan("curl http://127.0.0.1:8080/api/items"),
    )
    assert report.action == "restarted"
    assert agent._blocked_service_ports == set()


# ---------------------------------------------------------------------------
# 3. Restart failure → ports blocked; referencing VPs get BLOCKED verdicts
# ---------------------------------------------------------------------------

def test_preflight_restart_failure_blocks_ports(tmp_path, monkeypatch) -> None:
    agent = _make_agent(tmp_path)
    _patch_assess(monkeypatch, {8080})
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _Freshness(stale=False),
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: _RestartResult(
            False, diagnostics="POST-CHECK FAILED: port(s) [8080] still not ready",
        ),
    )
    report = agent._preflight_service_freshness(
        _plan("curl http://127.0.0.1:8080/api/items"),
    )
    assert report.action == "blocked"
    assert agent._blocked_service_ports == {8080}


def test_runner_short_circuits_blocked_port_vp(tmp_path, monkeypatch) -> None:
    """A VP referencing a blocked port must NOT reach the sub-agent."""
    agent = _make_agent(tmp_path)
    agent._blocked_service_ports = {8080}

    ran: list[list[str]] = []

    async def fake_run_single_vp(vp):
        ran.append([vp.get("id", "?")])
        # 2026-09-18: a PASSED ``code_review`` must come with a resolvable
        # citation, or the evidence contract downgrades it. The stub
        # stands in for the sub-agent, so it produces the artifact the
        # real one would have produced — including a real file to cite.
        source = agent.project_dir / "cited.py"
        source.write_text("value = 1\n", encoding="utf-8")
        directory = agent.plan_dir / "vp_artifacts" / "VP-002"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "citations.json").write_text(
            json.dumps({"citations": [
                {"file": "cited.py", "line": 1, "snippet": "value = 1"},
            ]}),
            encoding="utf-8",
        )
        return {"status": "PASSED"}

    monkeypatch.setattr(agent, "_run_single_vp_async", fake_run_single_vp)

    plan = _plan(
        "curl http://127.0.0.1:8080/api/items",   # VP-001 → blocked
        "cargo test --workspace",                   # VP-002 → runs
    )
    executor = agent._build_verification_executor(plan)

    async def drive():
        return [
            await executor.sub_agent_runner(vp)
            for vp in executor.verification_plan["vps"]
        ]

    results = asyncio.run(drive())
    assert ran == [["VP-002"]]
    blocked = results[0]
    assert blocked["status"] == "BLOCKED"
    assert "8080" in blocked["actual_result"]
    assert blocked["evidence"]["blocked_ports"] == [8080]
    assert results[1]["status"] == "PASSED"


# ---------------------------------------------------------------------------
# 4. Stale binary → rebuild BEFORE restart
# ---------------------------------------------------------------------------

def test_preflight_rebuilds_stale_binary_before_restart(
    tmp_path, monkeypatch,
) -> None:
    agent = _make_agent(tmp_path)
    _patch_assess(monkeypatch, {8080})
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _Freshness(stale=True),
    )

    order: list[str] = []

    def fake_rebuild(*a, **k):
        order.append("rebuild")
        return _RestartResult(True)  # shape-compatible enough

    def fake_restart(*a, **k):
        order.append("restart")
        return _RestartResult(True, ports=[8080])

    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", fake_rebuild,
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart", fake_restart,
    )

    report = agent._preflight_service_freshness(
        _plan("curl http://127.0.0.1:8080/api/items"),
    )
    assert order == ["rebuild", "restart"]
    assert report.action == "restarted"


def test_preflight_skips_rebuild_when_binary_fresh(tmp_path, monkeypatch) -> None:
    agent = _make_agent(tmp_path)
    _patch_assess(monkeypatch, {8080})
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _Freshness(stale=False),
    )
    rebuilt: list[bool] = []
    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild",
        lambda *a, **k: rebuilt.append(True),
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: _RestartResult(True, ports=[8080]),
    )
    agent._preflight_service_freshness(_plan("curl http://127.0.0.1:8080/x"))
    assert rebuilt == []


# ---------------------------------------------------------------------------
# 5. Never raises
# ---------------------------------------------------------------------------

def test_preflight_assess_error_never_raises(tmp_path, monkeypatch) -> None:
    agent = _make_agent(tmp_path)

    def boom(project_dir, plan_data):
        raise RuntimeError("lsof exploded")

    monkeypatch.setattr("service_freshness.assess_service_freshness", boom)
    report = agent._preflight_service_freshness(
        _plan("curl http://127.0.0.1:8080/x"),
    )
    # Degraded to proceed-unblocked; the error is audit-logged instead.
    assert agent._blocked_service_ports == set()
    assert report is not None


# ---------------------------------------------------------------------------
# Blocked-verdict shape (consumed by report / bridge / repair generator)
# ---------------------------------------------------------------------------

def test_blocked_verdict_carries_preflight_detail(tmp_path, monkeypatch) -> None:
    agent = _make_agent(tmp_path)
    _patch_assess(monkeypatch, {8080})
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _Freshness(stale=False),
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: _RestartResult(False, diagnostics="no start command found"),
    )
    agent._preflight_service_freshness(_plan("curl http://127.0.0.1:8080/x"))

    verdict = agent._build_service_freshness_blocked_verdict("VP-001", [8080])
    assert verdict["status"] == "BLOCKED"
    assert "8080" in verdict["actual_result"]
    assert "no start command found" in verdict["actual_result"]
    assert verdict["evidence"]["service_freshness"]["action"] == "blocked"


def test_blocked_ports_reset_each_preflight(tmp_path, monkeypatch) -> None:
    """A failed round-N preflight must not leak blocked ports into
    round N+1 when the plan no longer needs restarts."""
    agent = _make_agent(tmp_path)
    _patch_assess(monkeypatch, {8080})
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _Freshness(stale=False),
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: _RestartResult(False, diagnostics="x"),
    )
    agent._preflight_service_freshness(_plan("curl http://127.0.0.1:8080/x"))
    assert agent._blocked_service_ports == {8080}

    # Round 2: everything healthy.
    from service_freshness import ServiceFreshnessReport
    monkeypatch.setattr(
        "service_freshness.assess_service_freshness",
        lambda *a, **k: ServiceFreshnessReport(action="healthy"),
    )
    agent._preflight_service_freshness(_plan("curl http://127.0.0.1:8080/x"))
    assert agent._blocked_service_ports == set()


# ---------------------------------------------------------------------------
# 2026-09-18 C2 — the declared-services path
# ---------------------------------------------------------------------------


def _declared_plan(*commands: str) -> dict:
    plan = _plan(*commands)
    plan["services"] = [
        {"name": "chart", "port": 3000, "start_cmd": "npx next dev -p 3000",
         "cwd": "."},
        {"name": "api", "port": 8080, "start_cmd": "python -m api.server",
         "cwd": "."},
    ]
    return plan


def _runtime_map(ready=(), unready=()):
    import service_manager as sm
    runtimes = {}
    for name, port in ready:
        runtimes[name] = sm.ServiceRuntime(
            name=name, port=port, pid=100, origin="spawned", ready=True,
            start_cmd="x",
        )
    for name, port in unready:
        runtimes[name] = sm.ServiceRuntime(
            name=name, port=port, origin="failed", ready=False,
            detail="refusing to kill it",
        )
    return sm.ServiceRuntimeMap(runtimes=runtimes)


def test_declared_preflight_blocks_only_the_unready_ports(
    tmp_path: Path, monkeypatch
):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.api.url}}")
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    import service_manager as sm
    calls = []
    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, decls, round_number=0: calls.append(sorted(decls)) or
        _runtime_map(ready=[("api", 8080)], unready=[("chart", 3000)]),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    runtime_map = agent._preflight_service_freshness(plan)

    assert calls == [["api", "chart"]]
    assert runtime_map.ready_names == ["api"]
    assert agent._blocked_service_ports == {3000}
    assert agent._service_runtime_map is runtime_map


def test_declared_preflight_records_ownership(tmp_path: Path, monkeypatch):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.api.url}}")
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    import service_manager as sm
    recorded = {}
    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(ready=[("api", 8080)]),
    )
    monkeypatch.setattr(
        sm, "record_runtimes",
        lambda plan_dir, plan_id, rm, **k: recorded.update(
            {"plan_dir": plan_dir, "plan_id": plan_id, "map": rm}
        ),
    )

    agent._preflight_service_freshness(plan)

    assert recorded["plan_id"] == agent.plan_dir.name
    assert recorded["plan_dir"] == agent.plan_dir


def test_declared_preflight_never_kills_on_a_refusal(
    tmp_path: Path, monkeypatch
):
    """A refuse-everything runtime map must leave the round unblocked
    except for the offending port, and must not raise."""
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}")
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    import service_manager as sm
    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(unready=[("chart", 3000)]),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    agent._preflight_service_freshness(plan)

    assert agent._blocked_service_ports == {3000}


def test_legacy_plan_still_takes_the_scrape_path(
    tmp_path: Path, monkeypatch
):
    """No ``services`` key ⇒ pre-C2 behaviour, unchanged."""
    agent = _make_agent(tmp_path)
    plan = _plan("curl http://127.0.0.1:8080/x")
    agent._normalize_service_declarations(plan)
    assert agent._service_declarations == {}

    import service_manager as sm
    called = []
    monkeypatch.setattr(
        sm, "ensure_services", lambda *a, **k: called.append(1)
    )
    _patch_assess(monkeypatch, [])
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    agent._preflight_service_freshness(plan)

    assert called == []
    assert agent._blocked_service_ports == set()


def test_placeholders_are_resolved_in_memory_only(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}/api")
    agent._normalize_service_declarations(plan)

    resolved = agent._resolve_service_placeholders(plan)

    assert resolved["verification_points"][0]["target_url"] == (
        "curl http://127.0.0.1:3000/api"
    )
    assert plan["verification_points"][0]["target_url"] == (
        "curl {{svc.chart.url}}/api"
    )
    assert resolved is not plan


def test_placeholder_resolution_is_a_noop_without_declarations(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _plan("curl {{svc.chart.url}}/api")

    assert agent._resolve_service_placeholders(plan) is plan


def test_service_reference_report_shapes_issues(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.typo.port}}")
    agent._normalize_service_declarations(plan)

    report = agent._service_reference_report(plan)

    assert [r["id"] for r in report] == ["VP-001"]
    assert "typo" in report[0]["issues"][0]


def test_violation_fix_guidance_mentions_only_the_applicable_family(
    tmp_path: Path
):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.port}}")
    agent._normalize_service_declarations(plan)

    service_only = agent._violation_fix_guidance([{"id": "VP-001"}])
    assert "{{svc.<name>.port}}" in service_only

    # Each remaining family stays silent when it has no violations —
    # a plan rejected for one reason must not be lectured about another.
    assert agent._violation_fix_guidance([]) == ""
    api_only = agent._violation_fix_guidance([], [{"id": "VP-001"}])
    assert "json_path" in api_only
    assert "{{svc.<name>.port}}" not in api_only


# ---------------------------------------------------------------------------
# 2026-09-18 — relocation: the round follows the service, not the plan
# ---------------------------------------------------------------------------


def _relocated_runtime_map(name: str, declared: int, actual: int):
    import service_manager as sm
    return sm.ServiceRuntimeMap(runtimes={
        name: sm.ServiceRuntime(
            name=name, port=actual, declared_port=declared,
            pid=1, origin="spawned", ready=True, start_cmd="x",
        ),
    })


def test_effective_declarations_follow_a_relocation(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}/api")
    agent._normalize_service_declarations(plan)

    assert agent._effective_declarations()["chart"].port == 3000

    agent._service_runtime_map = _relocated_runtime_map("chart", 3000, 38921)

    assert agent._effective_declarations()["chart"].port == 38921


def test_placeholders_resolve_against_the_relocated_port(tmp_path: Path):
    """The whole point: a VP must not be pointed at the process that
    happens to be squatting the declared port."""
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}/api")
    agent._normalize_service_declarations(plan)
    agent._service_runtime_map = _relocated_runtime_map("chart", 3000, 38921)

    resolved = agent._resolve_service_placeholders(plan)

    assert resolved["verification_points"][0]["target_url"] == (
        "curl http://127.0.0.1:38921/api"
    )
    # the on-disk plan keeps the placeholder
    assert plan["verification_points"][0]["target_url"] == (
        "curl {{svc.chart.url}}/api"
    )


def test_effective_declarations_are_unchanged_without_a_runtime_map(
    tmp_path: Path,
):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}/api")
    agent._normalize_service_declarations(plan)

    assert agent._effective_declarations() == agent._service_declarations


def test_service_env_is_empty_without_a_runtime_map(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}/api")
    agent._normalize_service_declarations(plan)

    assert agent._service_env_for_vps() == {}


def test_service_env_advertises_the_relocated_address(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}/api")
    agent._normalize_service_declarations(plan)
    agent._service_runtime_map = _relocated_runtime_map("chart", 3000, 38921)

    env = agent._service_env_for_vps()

    assert env["PDT_SVC_CHART_PORT"] == "38921"
    assert env["PDT_SVC_CHART_URL"] == "http://127.0.0.1:38921"


def test_the_runner_tells_the_vp_where_the_services_are(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.chart.url}}/api")
    agent._normalize_service_declarations(plan)
    agent._service_runtime_map = _relocated_runtime_map("chart", 3000, 38921)
    seen: list[dict] = []

    async def _capture(vp):
        seen.append(dict(vp))
        return {"status": "FAILED", "reasons": [], "evidence": {}}

    agent._blocked_service_ports = set()
    agent._run_single_vp_async = _capture  # type: ignore[assignment]
    executor = agent._build_verification_executor(plan)

    asyncio.run(executor.sub_agent_runner(executor.verification_plan["vps"][0]))

    assert seen and seen[0]["service_env"]["PDT_SVC_CHART_PORT"] == "38921"


def test_the_vp_prompt_lists_the_managed_services():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review")
    prompt = agent._build_prompt({
        "id": "VP-001", "verification_method": "code_review",
        "service_env": {"PDT_SVC_CHART_PORT": "38921"},
    }, attempt=0)

    assert "PDT_SVC_CHART_PORT=38921" in prompt
    assert "do NOT start your own copy" in prompt


def test_the_vp_prompt_omits_the_block_without_services():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review")
    prompt = agent._build_prompt(
        {"id": "VP-001", "verification_method": "code_review"}, attempt=0,
    )

    assert "Managed services" not in prompt


def test_the_ledger_records_both_ports(tmp_path: Path):
    import service_manager as sm
    runtime_map = _relocated_runtime_map("chart", 3000, 38921)

    sm.record_runtimes(tmp_path, "p", runtime_map, project_dir=tmp_path)

    entry = sm.read_ledger(tmp_path)["services"][0]
    assert entry["port"] == 38921
    assert entry["declared_port"] == 3000


# ---------------------------------------------------------------------------
# 2026-10-24 — the preflight must spawn the *project's* service_manager
# ---------------------------------------------------------------------------
#
# The declared service's ``start_cmd`` already runs the project's own
# code (``python -m backend.server`` with ``cwd`` inside the project),
# so the project is what is under test.  The spawn machinery that
# launches it has to come from the same place: while the preflight
# called the *harness'* own ``service_manager``, a project that had
# fixed its spawn shape was still verified through the backend's old
# one — and the round failed for reasons that had nothing to do with
# the project.

#: The repo root — the checkout whose ``backend/service_manager.py`` the
#: loader is asked to resolve.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The plan whose declared services this suite exercises end to end.
_PLAN_ID = "20261023-service-spawn"

#: The operator's ``plans/`` tree, read at import time.  The suite's
#: autouse fixture points ``PDT_PLANS_DIR`` at each test's own tmp
#: directory (so a test cannot write into the live tree), which makes
#: import time the only moment the live path is still visible.
_PLANS_DIR = os.environ.get("PDT_PLANS_DIR")

#: A copy of the plan under test's ``services[0]`` — the ``pdt``
#: declaration.  It stands in for the plan when the plan is not on this
#: machine, so the case still starts a real server there.
_PDT_SERVICE_MIRROR = {
    "name": "pdt",
    "port": 18090,
    "start_cmd": (
        "source backend/.venv/bin/activate && mkdir -p .pdt/verify/plans && "
        "export PDT_PORT=18090 PDT_STATE_DB_PATH=$PWD/.pdt/verify/state.db "
        "PDT_PLANS_DIR=$PWD/.pdt/verify/plans && exec python -m backend.server"
    ),
    "cwd": ".",
    "health_url": "http://127.0.0.1:18090/health",
    "ready_timeout_seconds": 120,
    "log_path": ".pdt/verify/pdt_server.log",
    "reap_on_exit": True,
}

#: A stand-in for a project's own ``service_manager``.
#:
#: It records that it was called — and which file it was loaded from —
#: so the test can prove *which* module the preflight spawned.  The
#: flat ``from service_manager import ...`` is deliberate: it is the
#: import convention every ``backend/*.py`` module follows, and it is
#: what the real project module does too.
_PROJECT_SERVICE_MANAGER_SAMPLE = '''\
"""A project's own ``service_manager`` — the copy the preflight must use."""

import json
from pathlib import Path

from service_manager import ServiceRuntime, ServiceRuntimeMap

_MARKER = Path(__file__).with_name("spawn_calls.json")


def _record(entry):
    calls = []
    if _MARKER.is_file():
        calls = json.loads(_MARKER.read_text(encoding="utf-8"))
    calls.append(entry)
    _MARKER.write_text(json.dumps(calls, indent=2), encoding="utf-8")


def ensure_services(project_dir, declarations, *, round_number=0, **kwargs):
    _record({
        "who": "ensure_services",
        "file": __file__,
        "project_dir": str(project_dir),
        "services": sorted(declarations),
        "round_number": round_number,
    })
    runtimes = {}
    for name, decl in declarations.items():
        runtimes[name] = ServiceRuntime(
            name=name, port=decl.port, start_cmd=decl.start_cmd,
            cwd=decl.cwd, origin="spawned", ready=True, pid=4242,
        )
    return ServiceRuntimeMap(runtimes=runtimes)


def record_runtimes(plan_dir, plan_id, runtime_map, *, project_dir,
                    round_number=0):
    _record({
        "who": "record_runtimes",
        "file": __file__,
        "plan_dir": str(plan_dir),
        "plan_id": plan_id,
        "round_number": round_number,
    })
    return None
'''


def _install_project_service_manager(project_dir: Path) -> Path:
    """Write a project's own ``service_manager`` into *project_dir*.

    Returns the path it was written to.
    """
    backend = project_dir / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    target = backend / "service_manager.py"
    target.write_text(_PROJECT_SERVICE_MANAGER_SAMPLE, encoding="utf-8")
    return target


def _spawn_calls(project_dir: Path) -> list:
    """What the project's own ``service_manager`` recorded."""
    marker = project_dir / "backend" / "spawn_calls.json"
    if not marker.is_file():
        return []
    return json.loads(marker.read_text(encoding="utf-8"))


def test_declared_service_spawn_uses_project_service_manager(
    tmp_path: Path, monkeypatch
):
    """The preflight must spawn the project's own ``service_manager``.

    The project ships one; the preflight has to use *that* file and not
    the harness' copy, or the round verifies the backend's spawn shape
    while believing it verifies the project's.
    """
    from verification_agent import _load_service_manager

    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.api.url}}")
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    project_module = _install_project_service_manager(agent.project_dir)

    runtime_map = agent._preflight_service_freshness(plan)

    calls = _spawn_calls(agent.project_dir)
    assert calls, "the project's own service_manager was never called"
    ensure_call = next(c for c in calls if c["who"] == "ensure_services")
    assert Path(ensure_call["file"]) == project_module

    # ... and it is not the harness' own module.
    import service_manager as harness_sm
    assert Path(ensure_call["file"]) != Path(harness_sm.__file__)

    # The loader answers the same question the preflight asked.
    loaded = _load_service_manager(agent.project_dir)
    assert Path(loaded.__file__) == project_module

    assert runtime_map.ready_names == ["api", "chart"]
    assert agent._blocked_service_ports == set()


def test_declared_service_spawn_falls_back_to_own_module(
    tmp_path: Path, monkeypatch
):
    """No project module → the harness' own, and no exception."""
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.api.url}}")
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    assert not (agent.project_dir / "backend" / "service_manager.py").exists()

    import service_manager as sm
    calls = []
    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, decls, round_number=0: calls.append(sorted(decls)) or
        _runtime_map(ready=[("api", 8080)]),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    runtime_map = agent._preflight_service_freshness(plan)

    assert calls == [["api", "chart"]]
    assert runtime_map is not None
    assert runtime_map.ready_names == ["api"]


def test_declared_service_spawn_falls_back_when_project_module_is_broken(
    tmp_path: Path, monkeypatch
):
    """A project module that will not import must not take the round down."""
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.api.url}}")
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    backend = agent.project_dir / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "service_manager.py").write_text(
        "this is not python(", encoding="utf-8",
    )

    import service_manager as sm
    calls = []
    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, decls, round_number=0: calls.append(sorted(decls)) or
        _runtime_map(ready=[("api", 8080)]),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    runtime_map = agent._preflight_service_freshness(plan)

    assert calls == [["api", "chart"]]
    assert runtime_map.ready_names == ["api"]


def test_declared_service_spawn_falls_back_when_project_module_is_incomplete(
    tmp_path: Path, monkeypatch
):
    """A project module without the entry points must not take the round down."""
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.api.url}}")
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None
    )

    backend = agent.project_dir / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "service_manager.py").write_text(
        '"""A module called service_manager, but not one."""\n',
        encoding="utf-8",
    )

    import service_manager as sm
    calls = []
    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, decls, round_number=0: calls.append(sorted(decls)) or
        _runtime_map(ready=[("api", 8080)]),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    runtime_map = agent._preflight_service_freshness(plan)

    assert calls == [["api", "chart"]]
    assert runtime_map.ready_names == ["api"]


def test_project_service_manager_keeps_setsid_wrapper(tmp_path: Path):
    """Loading the project's module must not soften its spawn shape.

    PRD 3.3.2: the fix may not simply drop ``start_new_session`` — the
    equivalent has to be there.  The project's ``service_manager`` is
    now the module that does the spawning, so *its* shape is the one
    that has to keep the wrapper: strip the descriptor variables,
    ``setsid()``, exec the real command.

    The project here is a copy of this repository's own
    ``service_manager.py``, so the shape under test is the production
    one rather than a fixture that could drift away from it.
    """
    import inspect

    import credentials
    import service_manager as harness_sm
    from verification_agent import _load_service_manager

    project_dir = tmp_path / "project"
    backend = project_dir / "backend"
    backend.mkdir(parents=True)
    project_module = backend / "service_manager.py"
    project_module.write_text(
        (_REPO_ROOT / "backend" / "service_manager.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    module = _load_service_manager(project_dir)

    assert Path(module.__file__) == project_module
    assert Path(module.__file__) != Path(harness_sm.__file__)

    # 1. no start_new_session — the equivalent is the wrapper.
    spawn_source = inspect.getsource(module.start_service)
    assert "start_new_session" not in spawn_source

    # 2. the wrapper strips the descriptor variables, using the one
    #    prefix source the project shares with the harness.
    wrapper = module._DETACHED_WRAPPER_SOURCE
    assert "os.setsid()" in wrapper
    assert "os.execvp" in wrapper
    assert f"startswith({credentials._FD_ENV_PREFIX!r})" in wrapper
    assert "del os.environ[_name]" in wrapper


def _plan_dir_under_test() -> Optional[Path]:
    """The plan directory this suite exercises, or ``None``.

    The suite's autouse fixture redirects ``PDT_PLANS_DIR`` into each
    test's own tmp tree — that is what keeps a test from writing into
    the live ``plans/`` tree — so the live path has to be read *before*
    that happens.  Import time is the only moment that is true.
    """
    if not _PLANS_DIR:
        return None
    plan_dir = Path(_PLANS_DIR) / _PLAN_ID
    return plan_dir if (plan_dir / "verification_plan.json").is_file() else None


def _pdt_declaration(project_dir: Path):
    """The plan's ``pdt`` declaration.

    Read from the plan under test when that plan is on this machine,
    and from a mirror of the same declaration otherwise, so the case
    still starts a real server where the plan is not (CI).  The mirror
    is a copy of ``plans/20261023-service-spawn/verification_plan.json``'s
    ``services[0]`` — the declaration the plan under test ships.
    """
    from service_declaration import parse_declarations

    plan_dir = _plan_dir_under_test()
    if plan_dir is not None:
        plan = json.loads(
            (plan_dir / "verification_plan.json").read_text(encoding="utf-8")
        )
        declared = parse_declarations(plan, project_dir).by_name()
        if "pdt" in declared:
            return declared["pdt"]

    mirrored = parse_declarations(
        {"services": [_PDT_SERVICE_MIRROR]}, project_dir,
    ).by_name()
    return mirrored["pdt"]


@pytest.mark.integration
def test_declared_pdt_service_ready_in_harness_runtime(tmp_path: Path):
    """End to end: the declared pdt service really comes up.

    The readiness signal has to be a *real* backend, not a listening
    port: the log has to carry uvicorn's own "Application startup
    complete", and ``/health`` has to answer 200 with ``status`` ok.
    That is what tells the two failure modes apart — a service that
    never finished starting answers nothing at all.

    The service is started the way the round starts it (through the
    preflight) and is left running: a declared service outlives the
    call that started it — the round's own preflight does the same, and
    the VPs that follow are pointed at the live instance.
    """
    import urllib.request

    declaration = _pdt_declaration(_REPO_ROOT)

    # The plan directory stays in tmp — the ledger this preflight writes
    # must not land in the live plan's tree.
    agent = _make_agent(tmp_path)
    agent.project_dir = _REPO_ROOT
    agent._normalize_service_declarations(
        {"services": [declaration.to_dict()]},
    )

    runtime_map = agent._preflight_service_freshness(
        {"services": [declaration.to_dict()]}
    )

    runtime = runtime_map.runtimes["pdt"]
    assert runtime.ready is True, runtime.detail
    assert agent._blocked_service_ports == set()

    log_file = _REPO_ROOT / declaration.log_path
    log_text = log_file.read_text(encoding="utf-8", errors="replace")
    assert "Application startup complete" in log_text

    url = f"http://127.0.0.1:{declaration.port}/health"
    with urllib.request.urlopen(url, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    assert response.status == 200
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# 2026-10-24 — the environment a declared service is started in (VP-003)
# ---------------------------------------------------------------------------
#
# ``PDT_SECRET_FD_*`` names a file descriptor, and the number only means
# something in the process that published it: a child inherits the
# *variable* without inheriting the pipe behind it, so in the child the
# number names something else — the event loop's own selector — and
# ``credentials.read_secret_fd`` closes it.  The service then dies of
# ``Bad file descriptor`` before it is ready, and the round reports a
# port that nothing listens on.
#
# The wrapper the service is started through drops those variables and
# nothing else.  "Nothing else" is the half that is easy to get wrong:
# emptying the environment would also stop the crash, and would take
# every ordinary variable with it — the port the service was told to
# bind among them.


def _env_probe_service_mirror() -> dict:
    """A copy of the plan under test's ``services[1]`` — ``env-probe``.

    The declaration's own start command performs the two checks this
    verification point is about, before it execs the service: the
    environment must still carry ``PDT_SVC_ENV_PROBE_PORT``, and it
    must not carry a descriptor variable.  A service that fails either
    check exits non-zero and never listens — which is exactly the
    failure shape the point exists to catch.

    It stands in for the plan when the plan is not on this machine, so
    the case still starts a real service there.
    """
    import credentials

    prefix = credentials._FD_ENV_PREFIX
    check = (
        "import os,sys; "
        f'bad=any(k.startswith("{prefix}") for k in os.environ); '
        'ok="PDT_SVC_ENV_PROBE_PORT" in os.environ; '
        "sys.exit(0 if ok and not bad else 1)"
    )
    return {
        "name": "env-probe",
        "port": 18091,
        "start_cmd": (
            "source backend/.venv/bin/activate && "
            f"python -c '{check}' && "
            "exec python backend/tests/manual/tiny_service.py --port 18091"
        ),
        "cwd": ".",
        "health_url": "http://127.0.0.1:18091/health",
        "ready_timeout_seconds": 60,
        "log_path": ".pdt/verify/env_probe.log",
        "reap_on_exit": True,
    }


def _env_probe_declaration(project_dir: Path):
    """The plan's ``env-probe`` declaration.

    Read from the plan under test when that plan is on this machine,
    and from a mirror of the same declaration otherwise — the same
    two-source shape as :func:`_pdt_declaration`.
    """
    from service_declaration import parse_declarations

    plan_dir = _plan_dir_under_test()
    if plan_dir is not None:
        plan = json.loads(
            (plan_dir / "verification_plan.json").read_text(encoding="utf-8")
        )
        declared = parse_declarations(plan, project_dir).by_name()
        if "env-probe" in declared:
            return declared["env-probe"]

    mirrored = parse_declarations(
        {"services": [_env_probe_service_mirror()]}, project_dir,
    ).by_name()
    return mirrored["env-probe"]


def _process_environment(pid: int) -> Dict[str, str]:
    """The environment a running process is running with.

    ``/proc/<pid>/environ`` on Linux; ``ps eww`` elsewhere, where the
    environment is appended to the command line.  The ``ps`` form is a
    flat list of ``KEY=VALUE`` tokens, so a command line that carries
    assignments of its own would be read as environment — the callers
    here only ask about names the command does not use.
    """
    environ_file = Path(f"/proc/{pid}/environ")
    if environ_file.is_file():
        entries = []
        for chunk in environ_file.read_bytes().split(b"\0"):
            key, sep, value = chunk.partition(b"=")
            if not sep:
                continue
            entries.append((
                key.decode("utf-8", "replace"),
                value.decode("utf-8", "replace"),
            ))
        return dict(entries)

    try:
        completed = subprocess.run(
            ["ps", "eww", "-p", str(pid)],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    entries: Dict[str, str] = {}
    for token in completed.stdout.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            continue
        if not (key[0].isalpha() or key[0] == "_"):
            continue
        entries[key] = value
    return entries


def _health(port: int, timeout_seconds: float = 30.0):
    """``(status, body)`` from a service's ``/health`` route.

    Retried: the round's own readiness check can adopt a listener that
    is still starting, and one refused connection is not an answer.
    """
    import urllib.request

    deadline = time.monotonic() + timeout_seconds
    last_error = "no attempt made"
    while True:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=5,
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
                return response.status, body
        except Exception as exc:  # noqa: BLE001 - any failure means "not yet"
            last_error = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"port {port} answered no healthy request within "
                f"{timeout_seconds}s ({last_error})"
            )
        time.sleep(0.5)


def _service_script_name(declaration) -> str:
    """The service program a declaration starts — its file name.

    Taken from the declaration's own command, so the identity of "a
    process of ours" is not hardcoded here.
    """
    for token in reversed(declaration.start_cmd.split()):
        cleaned = token.strip("'\"")
        if cleaned.endswith(".py"):
            return cleaned.rsplit("/", 1)[-1]
    return ""


def _stop_leftover_env_probe(declaration) -> None:
    """Stop an env-probe a previous run of this suite left behind.

    The declaration's identity tokens include the text of its own
    start-command check, so a service that is already on the port does
    not *match* the declaration: the round treats it as a stranger,
    starts a copy on another port, and leaves the old one running.  One
    run would then leave two services behind, and the next run three.

    Only a process whose command line names this service is stopped.
    Anything else holding the port is left alone — the round's own
    ownership rule says a stranger is not ours to touch.
    """
    import service_manager as sm

    script = _service_script_name(declaration)
    if not script:
        return
    probe = sm.probe_port(declaration.port)
    if not probe.listening or not probe.pids:
        return
    if not any(script in cmdline for cmdline in probe.cmdlines or []):
        return
    runtime = sm.ServiceRuntime(
        name=declaration.name, port=declaration.port,
        start_cmd=declaration.start_cmd, cwd=declaration.cwd,
        pid=probe.pids[0],
    )
    stopped = sm.stop_service(runtime, declaration)
    assert stopped.get("port_free"), (
        f"could not release port {declaration.port}: {stopped.get('detail')}"
    )


@pytest.fixture(scope="module")
def env_probe_service(tmp_path_factory):
    """The plan's ``env-probe`` service, started once for this module.

    Started the way the round starts it — through the preflight, with
    the *project's* ``service_manager`` — and left running: a declared
    service outlives the call that started it, and the verification
    points that follow are pointed at the live instance.

    The parent environment is poisoned with a descriptor variable
    first.  Without one the case would prove nothing: "the child does
    not have it" is true of every spawn, including one that hands the
    parent's environment over wholesale.  With one planted, the child's
    environment says something about the spawn.
    """
    import credentials

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv(
        credentials._FD_ENV_PREFIX + "SPAWN_PROBE", "424242",
    )
    try:
        workdir = tmp_path_factory.mktemp("env-probe")
        declaration = _env_probe_declaration(_REPO_ROOT)
        _stop_leftover_env_probe(declaration)

        # The plan directory stays in tmp — the ledger this preflight
        # writes must not land in the live plan's tree.
        agent = _make_agent(workdir)
        agent.project_dir = _REPO_ROOT
        agent._normalize_service_declarations(
            {"services": [declaration.to_dict()]},
        )

        runtime_map = agent._preflight_service_freshness(
            {"services": [declaration.to_dict()]}
        )
        yield declaration, runtime_map.runtimes["env-probe"]
    finally:
        monkeypatch.undo()


@pytest.mark.integration
def test_env_probe_service_sees_no_secret_fd_vars(env_probe_service):
    """The service process does not inherit the descriptor variables.

    The plan's ``env-probe`` declaration performs its own check before
    it execs the service — the environment must carry the port
    variable and must not carry a descriptor variable — so a service
    that is up at all is already half the answer.  The other half is
    read from the process itself rather than taken on trust: the
    environment it is actually running with.
    """
    import credentials

    declaration, runtime = env_probe_service

    assert runtime.ready is True, runtime.detail

    status, body = _health(runtime.port)
    assert status == 200
    assert body["status"] == "ok"

    assert runtime.pid, runtime.detail
    environment = _process_environment(runtime.pid)
    leaked = sorted(
        name for name in environment
        if name.startswith(credentials._FD_ENV_PREFIX)
    )
    assert leaked == [], (
        f"the service process inherited {leaked}; the spawn has to drop "
        f"the descriptor variables"
    )

    # The parent really did carry one, so the assertion above is about
    # the spawn and not about a parent that never had any.
    assert credentials._FD_ENV_PREFIX + "SPAWN_PROBE" in os.environ


@pytest.mark.integration
def test_env_probe_service_keeps_normal_env_vars(env_probe_service):
    """The spawn drops the descriptor variables and keeps the rest.

    Clearing the environment would also stop the crash, and would take
    every ordinary variable with it — the port the service was told to
    bind among them.  So the other half of the criterion is that the
    environment survives: ``PDT_SVC_ENV_PROBE_PORT`` is still there,
    and the service process can read it.
    """
    declaration, runtime = env_probe_service

    assert runtime.ready is True, runtime.detail
    assert runtime.pid, runtime.detail

    environment = _process_environment(runtime.pid)

    # The port the service was told to bind is in its environment, and
    # it is the port it is actually serving on.
    assert environment.get("PDT_SVC_ENV_PROBE_PORT") == str(runtime.port)

    # The rest of the environment came along too — an environment that
    # had been emptied would not carry PATH.
    assert environment.get("PATH"), (
        "the service process has no PATH; the spawn emptied the "
        "environment instead of dropping the descriptor variables"
    )

    # And the service itself read the variable: the declaration's start
    # command refuses to exec the service unless it is in the
    # environment, so a running service is the process having read it.
    status, body = _health(runtime.port)
    assert status == 200
    assert body["status"] == "ok"


def test_wrapper_strips_only_fd_prefix(tmp_path: Path):
    """The wrapper drops the descriptor variables — and only those.

    Two ways to stop a child from inheriting a descriptor variable:
    delete the variables that name one (the fix), or empty the whole
    environment (a blunt instrument that also takes every ordinary
    variable with it, the port the service was told to bind among
    them).  The second is not a fix, it is the next bug.

    Both halves are checked: the source says what it does, and running
    it says what it does.
    """
    import credentials
    import service_manager as sm

    wrapper = sm._DETACHED_WRAPPER_SOURCE

    # -- the source ---------------------------------------------------
    assert f"startswith({credentials._FD_ENV_PREFIX!r})" in wrapper
    assert "del os.environ[_name]" in wrapper

    deletes = [
        line.strip() for line in wrapper.splitlines()
        if line.strip().startswith("del ")
    ]
    assert deletes == ["del os.environ[_name]"], deletes

    for whole_clear in (
        "os.environ.clear()",
        "os.environ =",
        "os.environ=",
        "os.environ.popitem()",
    ):
        assert whole_clear not in wrapper, (
            f"the wrapper clears the environment wholesale "
            f"({whole_clear!r}); only the descriptor variables may go"
        )

    # PRD 3.3.2: the wrapper is the equivalent of ``start_new_session``
    # — dropping it would put the service back into the caller's
    # session, and dropping the descriptor handling would put the
    # crash back.
    assert "os.setsid()" in wrapper
    assert "os.execvp" in wrapper

    # -- the behaviour ------------------------------------------------
    # The wrapper's own source, run over a known environment; the
    # command it execs reports what it inherited.
    dump = tmp_path / "child_env.txt"
    parent_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PDT_SVC_ENV_PROBE_PORT": "18091",
        credentials._FD_ENV_PREFIX + "ONE": "11",
        credentials._FD_ENV_PREFIX + "TWO": "22",
    }
    completed = subprocess.run(
        [
            sys.executable, "-c", wrapper,
            "/bin/sh", "-c", f"env > '{dump}'",
        ],
        env=parent_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    child_env = dict(
        line.split("=", 1)
        for line in dump.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert credentials._FD_ENV_PREFIX + "ONE" not in child_env
    assert credentials._FD_ENV_PREFIX + "TWO" not in child_env
    assert child_env.get("PDT_SVC_ENV_PROBE_PORT") == "18091"
    assert child_env.get("PATH") == parent_env["PATH"]
    assert child_env.get("HOME") == parent_env["HOME"]
