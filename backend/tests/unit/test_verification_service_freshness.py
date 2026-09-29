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
import sys
from pathlib import Path

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
