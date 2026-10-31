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

# A copy of the plan under test's ``services[0]`` — the ``pdt``
# declaration.  It stands in for the plan when the plan is not on this
# machine, so the case still starts a real server there.
#
# It is a *function*, not a constant: the port is allocated when the
# mirror is built.  A written-down port is a port another checkout may
# already hold, and the case would then fail for a reason that has
# nothing to do with what it is about.
def _pdt_service_mirror() -> dict:
    """The ``pdt`` declaration, mirrored around a free port."""
    import service_manager

    port = service_manager.allocate_free_port()
    if port is None:
        raise RuntimeError(
            "no free port for the pdt service mirror; the mirror does "
            "not fall back to a fixed one"
        )
    return {
        "name": "pdt",
        "port": port,
        "start_cmd": (
            "source backend/.venv/bin/activate && mkdir -p .pdt/verify/plans && "
            f"export PDT_PORT={port} PDT_STATE_DB_PATH=$PWD/.pdt/verify/state.db "
            "PDT_PLANS_DIR=$PWD/.pdt/verify/plans && exec python -m backend.server"
        ),
        "cwd": ".",
        "health_url": f"http://127.0.0.1:{port}/health",
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
        {"services": [_pdt_service_mirror()]}, project_dir,
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
    the case still starts a real service there.  The port is allocated
    when the mirror is built: a written-down port is a port another
    checkout may already hold.
    """
    import credentials
    import service_manager

    port = service_manager.allocate_free_port()
    if port is None:
        raise RuntimeError(
            "no free port for the env-probe service mirror; the mirror "
            "does not fall back to a fixed one"
        )

    prefix = credentials._FD_ENV_PREFIX
    check = (
        "import os,sys; "
        f'bad=any(k.startswith("{prefix}") for k in os.environ); '
        'ok="PDT_SVC_ENV_PROBE_PORT" in os.environ; '
        "sys.exit(0 if ok and not bad else 1)"
    )
    return {
        "name": "env-probe",
        "port": port,
        "start_cmd": (
            "source backend/.venv/bin/activate && "
            f"python -c '{check}' && "
            "exec python backend/tests/manual/tiny_service.py "
            f"--port {port}"
        ),
        "cwd": ".",
        "health_url": f"http://127.0.0.1:{port}/health",
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


# ---------------------------------------------------------------------------
# 2026-10-27 — the mirrored declarations take their port when they are built
# ---------------------------------------------------------------------------
#
# A hard-coded port is a port another checkout may already hold: the
# case then fails for a reason that has nothing to do with what it is
# about (a second run of the same suite, another worktree, a leftover
# service).  So the mirrors ask the framework for a free port when they
# are built — and every port-bearing field of a mirror carries that one
# port, so the command, the URL and the declaration cannot disagree.


def test_service_fixtures_take_their_port_at_runtime(monkeypatch):
    """Both mirrored declarations are built around one free port."""
    import service_manager

    monkeypatch.setattr(
        service_manager, "allocate_free_port", lambda: 45999,
    )

    mirrors = [_pdt_service_mirror(), _env_probe_service_mirror()]

    for mirror in mirrors:
        assert "45999" in str(mirror["port"]), mirror
        assert "45999" in mirror["health_url"], mirror
        assert "45999" in mirror["start_cmd"], mirror
        # The ports that used to be written into the file are gone.
        for stale in ("18090", "18091"):
            assert stale not in mirror["health_url"], mirror
            assert stale not in mirror["start_cmd"], mirror


def test_service_mirror_refuses_to_invent_a_port(monkeypatch):
    """No free port → the mirror refuses, rather than using a fixed one.

    Falling back to a written-down port is exactly the failure this
    change removes: two runs would share it and the second would fail
    for a reason that has nothing to do with the case.
    """
    import service_manager

    monkeypatch.setattr(
        service_manager, "allocate_free_port", lambda: None,
    )

    with pytest.raises(RuntimeError):
        _pdt_service_mirror()

    with pytest.raises(RuntimeError):
        _env_probe_service_mirror()


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
    # command it execs reports what it inherited.  The probe variable is
    # a plain non-secret one, and its value is deliberately not
    # port-shaped: nothing here binds a port, and a bare number in this
    # dict reads as an address that a fixture forgot to allocate.
    dump = tmp_path / "child_env.txt"
    parent_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PDT_SVC_ENV_PROBE_PORT": "probe-port",
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
    assert (
        child_env.get("PDT_SVC_ENV_PROBE_PORT")
        == parent_env["PDT_SVC_ENV_PROBE_PORT"]
    )
    assert child_env.get("PATH") == parent_env["PATH"]
    assert child_env.get("HOME") == parent_env["HOME"]


# ---------------------------------------------------------------------------
# 2026-10-24 — a declared service that dies *after* the round-start
# preflight (the twelve-VP incident)
# ---------------------------------------------------------------------------
#
# The round-start preflight brings the declared services up **once**, at
# the top of the round.  A service that dies after that stays dead for
# the rest of the round, and every VP that talks to it is then graded
# on the connection refusal: ``api_test`` reports FAILED, which reads
# as "the project is broken" when the truth is "the plumbing died".
# One dead service cost a production round twelve VPs.
#
# So the per-VP runner probes the ports it is about to use, and a dead
# one is brought back before the VP runs.  A service that cannot be
# brought back leaves its port blocked, and the **existing**
# blocked-port gate turns the VP into BLOCKED — not FAILED.

def _probe(listening: bool, port: int):
    """A ``PortProbe`` stand-in: is anything listening on *port*?"""
    from service_freshness import PortProbe

    return PortProbe(port=port, listening=listening)


def _run_all_vps(executor) -> list:
    """Run every VP of *executor* through the runner, in order."""
    async def _drive():
        return [
            await executor.sub_agent_runner(vp)
            for vp in executor.verification_plan["vps"]
        ]

    return asyncio.run(_drive())


def _declared_runner_agent(tmp_path: Path, monkeypatch, *, listening: bool):
    """An agent whose ``api`` service the preflight started — and which
    is now either alive or dead.

    The round-start preflight is not re-run here: what the preflight
    leaves behind is the point.  It left a **ready** runtime for
    ``api`` on 8080, and then the service died (or did not).
    """
    agent = _make_agent(tmp_path)
    plan = _declared_plan("curl {{svc.api.url}}/items")
    agent._normalize_service_declarations(plan)
    agent._service_runtime_map = _runtime_map(ready=[("api", 8080)])
    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda port: _probe(listening, port),
    )
    return agent, plan


def _passing_reviewer(agent, ran: Optional[list] = None):
    """A stub sub-agent whose PASSED survives the evidence check.

    ``code_review``'s PASSED is only kept when its citations can be
    checked against a real file, so the stub has to leave behind the
    same artifact the real reviewer would.
    """
    async def _run(vp):
        vp_id = str(vp.get("id", "unknown"))
        if ran is not None:
            ran.append(vp_id)
        source = agent.project_dir / "cited.py"
        source.write_text("value = 1\n", encoding="utf-8")
        directory = agent.plan_dir / "vp_artifacts" / vp_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "citations.json").write_text(
            json.dumps({"citations": [
                {"file": "cited.py", "line": 1, "snippet": "value = 1"},
            ]}),
            encoding="utf-8",
        )
        return {"status": "PASSED"}

    return _run


def test_dead_declared_service_is_re_ensured_before_a_vp(
    tmp_path: Path, monkeypatch
):
    """A service that died mid-round is brought back before its VP runs.

    Without the recovery the VP fires its request at a port nothing
    listens on and the refusal is graded as a project failure.

    The port answers again only once the re-ensure has run — that is
    what "the service is back" means, and it is what the round's
    adoption gate checks before it lets the VP go out (2026-10-25).
    """
    agent, plan = _declared_runner_agent(tmp_path, monkeypatch, listening=False)

    import service_manager as sm

    ensured: list[list[str]] = []
    alive: list[bool] = [False]

    def fake_ensure(project, declarations, round_number=0, **kwargs):
        ensured.append(sorted(declarations))
        alive[0] = True
        return _runtime_map(ready=[("api", 8080)])

    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda port: _probe(alive[0], port),
    )
    monkeypatch.setattr(sm, "ensure_services", fake_ensure)
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    ran: list[str] = []
    monkeypatch.setattr(
        agent, "_run_single_vp_async", _passing_reviewer(agent, ran),
    )

    executor = agent._build_verification_executor(
        agent._resolve_service_placeholders(plan)
    )
    results = _run_all_vps(executor)

    assert ensured == [["api"]], "the dead service was not re-ensured"
    assert ran == ["VP-001"], "the VP did not run after the recovery"
    assert results[0]["status"] == "PASSED"
    assert agent._blocked_service_ports == set()


def test_unrecoverable_declared_service_yields_blocked_not_failed(
    tmp_path: Path, monkeypatch
):
    """A service that cannot be brought back blocks its VPs.

    FAILED would say "the project answered wrong"; the truth is "there
    was nothing to answer".  The blocked-port gate already knows how to
    say that, so the recovery has to end there.
    """
    agent, plan = _declared_runner_agent(tmp_path, monkeypatch, listening=False)

    import service_manager as sm

    def fake_ensure(project, declarations, round_number=0, **kwargs):
        return _runtime_map(unready=[("api", 8080)])

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    ran: list[str] = []
    monkeypatch.setattr(
        agent, "_run_single_vp_async", _passing_reviewer(agent, ran),
    )

    executor = agent._build_verification_executor(
        agent._resolve_service_placeholders(plan)
    )
    results = _run_all_vps(executor)

    assert ran == [], "a VP ran against a service that is not there"
    assert results[0]["status"] == "BLOCKED"
    assert results[0]["status"] != "FAILED"
    assert agent._blocked_service_ports == {8080}


def test_api_test_against_a_dead_service_is_blocked_not_failed(
    tmp_path: Path, monkeypatch
):
    """The production shape: an ``api_test`` VP whose service is gone.

    ``api_test`` is graded by (request, response, assertions).  With no
    service there is no response, and the refusal used to be reported
    as FAILED — a statement about the project that the project never
    got to make.
    """
    agent = _make_agent(tmp_path)
    plan = _declared_plan()
    plan["verification_points"] = [
        {
            "id": "VP-001",
            "title": "items endpoint",
            "verification_method": "api_test",
            "uses_services": ["api"],
            "request": {"method": "GET", "url": "{{svc.api.url}}/items"},
            "assertions": [{"status": 200}],
        }
    ]
    agent._normalize_service_declarations(plan)
    agent._service_runtime_map = _runtime_map(ready=[("api", 8080)])
    monkeypatch.setattr(
        "service_freshness.probe_port", lambda port: _probe(False, port),
    )

    import service_manager as sm

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, declarations, round_number=0, **kwargs:
            _runtime_map(unready=[("api", 8080)]),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    executor = agent._build_verification_executor(
        agent._resolve_service_placeholders(plan)
    )
    results = _run_all_vps(executor)

    assert results[0]["status"] == "BLOCKED", results[0]
    assert agent._blocked_service_ports == {8080}


def test_healthy_declared_service_is_not_restarted_per_vp(
    tmp_path: Path, monkeypatch
):
    """A service that is alive must not be restarted — not once per VP.

    A restart re-seeds the fixture and clears the execution state, so
    a round that restarts its service for every VP destroys the very
    state the VPs are verifying.
    """
    agent = _make_agent(tmp_path)
    plan = _declared_plan(
        "curl {{svc.api.url}}/one",
        "curl {{svc.api.url}}/two",
        "curl {{svc.api.url}}/three",
    )
    agent._normalize_service_declarations(plan)
    agent._service_runtime_map = _runtime_map(ready=[("api", 8080)])
    monkeypatch.setattr(
        "service_freshness.probe_port", lambda port: _probe(True, port),
    )

    import service_manager as sm

    ensured: list[list[str]] = []

    def fake_ensure(project, declarations, round_number=0, **kwargs):
        ensured.append(sorted(declarations))
        return _runtime_map(ready=[("api", 8080)])

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_run_single_vp_async", _passing_reviewer(agent))

    executor = agent._build_verification_executor(
        agent._resolve_service_placeholders(plan)
    )
    results = _run_all_vps(executor)

    assert ensured == [], "a healthy service was re-ensured"
    assert [r["status"] for r in results] == ["PASSED"] * 3
    assert agent._blocked_service_ports == set()


# ---------------------------------------------------------------------------
# 2026-10-24 — the data plane the api_test VP is about to talk to
# ---------------------------------------------------------------------------
#
# "The port answers" is not the same question as "the instance answering
# is the one this round started, reading the fixture this round seeded".
# The production round was graded on the first question: the port was
# busy, HTTP answered 200 with ``status: ok``, and the answer came from
# a data plane that was not the round's fixture — so every later
# phase/plan assertion ran against the wrong database.
#
# So the round records a fingerprint at the preflight and re-checks it
# before every api_test VP is dispatched.


def _fingerprint(port=8080, pid=100, rows=4, expected=4):
    """A data-plane fingerprint, as the probe would report it."""
    from service_freshness import DataPlaneFingerprint

    return DataPlaneFingerprint(
        port=port, pid=pid, routing_rows=rows, expected_rows=expected,
        status="ok",
    )


class _DataPlane:
    """The probe's answers, in order; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.ports: list = []

    def __call__(self, port, **kwargs):
        self.ports.append(port)
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]


def _api_test_plan() -> dict:
    """A plan with one ``api_test`` VP against the declared ``api``."""
    plan = _declared_plan()
    plan["verification_points"] = [
        {
            "id": "VP-001",
            "title": "items endpoint",
            "verification_method": "api_test",
            "uses_services": ["api"],
            "request": {"method": "GET", "url": "{{svc.api.url}}/items"},
            "assertions": [{"status": 200}],
        }
    ]
    return plan


def _api_test_agent(tmp_path: Path, monkeypatch, probe: _DataPlane):
    """An agent whose ``api`` service answers *probe* on ``/health/data``."""
    agent = _make_agent(tmp_path)
    plan = _api_test_plan()
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )
    # The service is alive at the TCP level — the previous round's
    # liveness recovery must not fire, or it would mask what this round
    # is about.
    monkeypatch.setattr(
        "service_freshness.probe_port", lambda port: _probe(True, port),
    )
    monkeypatch.setattr("service_freshness.probe_data_plane", probe)
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )
    return agent, plan


def _stub_service_manager(monkeypatch, ensured: list) -> None:
    """Stub the project's ``ensure_services`` and record the calls."""
    import service_manager as sm

    def fake_ensure(project, declarations, round_number=0, **kwargs):
        ensured.append(sorted(declarations))
        return _runtime_map(ready=[("api", 8080)])

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)


def _spy_api_runner(monkeypatch, dispatched: list) -> None:
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


def _run_api_test_vp(agent, plan):
    """Preflight, build the executor and run the plan's VPs."""
    agent._preflight_service_freshness(plan)
    executor = agent._build_verification_executor(
        agent._resolve_service_placeholders(plan)
    )
    return _run_all_vps(executor)


def test_api_test_is_blocked_when_the_instance_serves_another_data_plane(
    tmp_path: Path, monkeypatch
):
    """The production shape: the port answers, with another data plane.

    The api_test must not be dispatched at all — a request sent to the
    wrong instance measures the wrong database — and the verdict must be
    BLOCKED, never FAILED: FAILED would say "the project answered
    wrong", and the project was never asked.
    """
    probe = _DataPlane(
        _fingerprint(rows=4),             # the instance the round recorded
        _fingerprint(pid=999, rows=0),    # what answers now
        _fingerprint(pid=999, rows=0),    # ... and after the recovery
    )
    agent, plan = _api_test_agent(tmp_path, monkeypatch, probe)
    ensured: list = []
    _stub_service_manager(monkeypatch, ensured)
    dispatched: list = []
    _spy_api_runner(monkeypatch, dispatched)

    results = _run_api_test_vp(agent, plan)

    assert dispatched == [], "the api_test went to the wrong instance"
    assert results[0]["status"] == "BLOCKED", results[0]
    assert results[0]["status"] != "FAILED"
    evidence = results[0]["evidence"]
    assert evidence["recorded_data_plane"]["routing_rows"] == 4
    assert evidence["observed_data_plane"]["routing_rows"] == 0


def test_a_stale_data_plane_is_blocked_even_when_the_pid_is_unchanged(
    tmp_path: Path, monkeypatch
):
    """The pid alone is not the verdict — the data plane is.

    The production instance answered on the same port with the same pid
    but with another database behind it, so the row count is what has
    to be compared.
    """
    probe = _DataPlane(
        _fingerprint(rows=4),
        _fingerprint(pid=100, rows=0),
        _fingerprint(pid=100, rows=0),
    )
    agent, plan = _api_test_agent(tmp_path, monkeypatch, probe)
    ensured: list = []
    _stub_service_manager(monkeypatch, ensured)
    dispatched: list = []
    _spy_api_runner(monkeypatch, dispatched)

    results = _run_api_test_vp(agent, plan)

    assert dispatched == [], "the api_test went to the wrong instance"
    assert results[0]["status"] == "BLOCKED", results[0]


def test_one_re_ensure_restores_the_data_plane_and_the_vp_runs(
    tmp_path: Path, monkeypatch
):
    """One re-ensure, then the VP runs against the right instance.

    The recovery is the one the round is allowed to spend: it re-ensures
    the service, refreshes the declarations and the resolved
    placeholders, and the VP then goes out to the address the service
    actually serves.
    """
    probe = _DataPlane(
        _fingerprint(rows=4),            # the instance the round recorded
        _fingerprint(pid=999, rows=0),   # what answers now
        _fingerprint(pid=200, rows=4),   # what answers after the recovery
    )
    agent, plan = _api_test_agent(tmp_path, monkeypatch, probe)
    ensured: list = []
    _stub_service_manager(monkeypatch, ensured)
    dispatched: list = []
    _spy_api_runner(monkeypatch, dispatched)

    results = _run_api_test_vp(agent, plan)

    assert ensured == [["api", "chart"], ["api"]], ensured
    assert dispatched == ["VP-001"]
    assert results[0]["status"] == "PASSED", results[0]


def test_a_matching_data_plane_dispatches_the_vp_without_re_ensuring(
    tmp_path: Path, monkeypatch
):
    """A healthy data plane is not touched.

    Re-ensuring a service that is serving the right fixture would
    re-seed it — and a round that restarts its service per VP destroys
    the very state its VPs are verifying.
    """
    probe = _DataPlane(
        _fingerprint(rows=4),
        _fingerprint(rows=4),
    )
    agent, plan = _api_test_agent(tmp_path, monkeypatch, probe)
    ensured: list = []
    _stub_service_manager(monkeypatch, ensured)
    dispatched: list = []
    _spy_api_runner(monkeypatch, dispatched)

    results = _run_api_test_vp(agent, plan)

    assert ensured == [["api", "chart"]], ensured
    assert dispatched == ["VP-001"]
    assert results[0]["status"] == "PASSED", results[0]


# ---------------------------------------------------------------------------
# 2026-10-25 — a re-ensure's result has to be verified before it is adopted
# ---------------------------------------------------------------------------
#
# The production round ended with its api_test pointing at
# 127.0.0.1:51725 — a port nothing had ever listened on, and a port no
# log had ever recorded as a service's address.  There is only one way
# that address can become a VP's request address: a re-ensure allocated
# it, wrote it into the runtime map **without a liveness probe**, and the
# plan's placeholders followed it there.
#
# So adopting a re-ensure's result goes through three gates:
#
#   1. the runtime says the service is **ready** and carries a **port**;
#   2. that port accepts a **TCP connection** (1 s);
#   3. the service's **health URL answers 200** (with the request
#      guard's header — the same road a VP's request travels).
#
# A result that fails any of them is not adopted: the old map stands, the
# address does not move, no request is sent, and the VP is BLOCKED
# (``service_unreachable``) — never FAILED, which would say "the project
# answered wrong" about a project that was never asked.

#: The port the production round's re-ensure allocated.  Nothing listens
#: on it — that is the whole point of the fixture.
DEAD_RELOCATED_PORT = 51725


def _api_test_agent_without_preflight(tmp_path: Path, monkeypatch):
    """An agent whose round already ensured the ``api`` service.

    The round-start preflight is not re-run here: what it left behind is
    the point.  It left a **ready** runtime for ``api`` on 8080 — and
    then the service died.
    """
    agent = _make_agent(tmp_path)
    plan = _api_test_plan()
    agent._normalize_service_declarations(plan)
    agent._service_runtime_map = _runtime_map(ready=[("api", 8080)])
    # The service is gone: the liveness recovery has to fire.
    monkeypatch.setattr(
        "service_freshness.probe_port", lambda port: _probe(False, port),
    )
    return agent, plan


def test_vp_gate_blocks_when_the_data_plane_is_empty(tmp_path: Path, monkeypatch):
    """A re-ensure that produced nothing must not become the VP's address.

    The re-ensure reports the service **ready** — on a port nothing is
    listening on.  That is the production shape: the runtime says the
    service came up, the address is a port nobody ever bound.

    The gates refuse it, and the refusal has to be visible in the
    verdict: the request is not sent, the address does not move, the old
    map stands, and the VP is BLOCKED with ``service_unreachable``.
    """
    agent, plan = _api_test_agent_without_preflight(tmp_path, monkeypatch)

    import service_manager as sm

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, declarations, round_number=0, **kwargs:
            _runtime_map(ready=[("api", DEAD_RELOCATED_PORT)]),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)
    dispatched: list = []
    _spy_api_runner(monkeypatch, dispatched)

    executor = agent._build_verification_executor(
        agent._resolve_service_placeholders(plan)
    )
    results = _run_all_vps(executor)

    assert dispatched == [], "the request went to an address nothing served"
    assert results[0]["status"] == "BLOCKED", results[0]
    assert results[0]["status"] != "FAILED", results[0]
    assert "service_unreachable" in " ".join(results[0]["reasons"]), results[0]
    # The old map stands: the port nobody listens on was not adopted.
    assert agent._service_runtime_map.runtimes["api"].port == 8080
    # ... and the address the VPs are pointed at did not move either.
    assert agent._effective_declarations()["api"].port == 8080


class _HealthEndpoint:
    """A real HTTP server that answers the health URL with 200.

    The gates do not take the runtime's word for it — they talk to the
    service.  So the success path needs a service that really answers.
    """

    def __init__(self, status: int = 200):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        #: One entry per request: the guard header the request carried.
        self.requests: list = []
        endpoint = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - the handler API's own name
                endpoint.requests.append(
                    self.headers.get("X-PDT-Request"),
                )
                body = b'{"status": "ok"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: A003 - silence access log
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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


def test_a_re_ensure_that_answers_is_adopted(tmp_path: Path, monkeypatch):
    """The other side of the gate: a re-ensure that really answers.

    The three gates are a filter, not a wall.  A re-ensure that hands
    back a live instance — one that accepts a connection and answers
    its health URL with 200 — is adopted, the address moves to it, and
    the round records which instance it verified.
    """
    endpoint = _HealthEndpoint()
    try:
        agent = _make_agent(tmp_path)
        plan = _api_test_plan()
        plan["services"] = [
            {
                "name": "api",
                "port": 8080,
                "start_cmd": "python -m api.server",
                "cwd": ".",
                "health_url": "http://127.0.0.1:8080/health",
            },
        ]
        agent._normalize_service_declarations(plan)
        agent._service_runtime_map = _runtime_map(ready=[("api", 8080)])
        # The service is gone: the liveness recovery has to fire.
        monkeypatch.setattr(
            "service_freshness.probe_port", lambda port: _probe(False, port),
        )
        agent.start_verification_round(1)

        import service_manager as sm

        monkeypatch.setattr(
            sm, "ensure_services",
            lambda project, declarations, round_number=0, **kwargs:
                _runtime_map(ready=[("api", endpoint.port)]),
        )
        monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)
        dispatched: list = []
        _spy_api_runner(monkeypatch, dispatched)

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        endpoint.close()

    assert dispatched == ["VP-001"], dispatched
    assert results[0]["status"] == "PASSED", results[0]
    # The verified instance is the one the round points at now.
    assert agent._service_runtime_map.runtimes["api"].port == endpoint.port
    assert agent._effective_declarations()["api"].port == endpoint.port
    # ... and the round said which instance it verified.
    verified = [
        e for e in _log_entries(agent.plan_dir)
        if e.get("event_type") == "service_reensure_verified"
    ]
    assert len(verified) == 1, verified
    data = verified[0]["data"]
    assert data["port"] == endpoint.port
    assert data["pid"] == 100
    assert data["boot_id"]
    # The health URL was asked over the guarded road.
    assert endpoint.requests == ["1"], endpoint.requests


# ---------------------------------------------------------------------------
# 2026-10-25 — the re-ensure's result is adopted only when it is proven
# ---------------------------------------------------------------------------
#
# The 2026-10-24 production round was graded on a question nobody had
# asked: the service answered 200 on ``/health`` while the database
# behind it held **zero** routing rows — so every assertion that
# followed ran against an empty table, and the round reported the
# project as broken.
#
# So a re-ensure's result is not adopted because it says ``ready``:
# it is adopted because the instance on that port was **asked** and
# answered.  These three cases are the ways that can go wrong:
#
#   1. the port has no listener — there is nothing there to ask;
#   2. the health URL does not answer 200 — the service is not serving;
#   3. the relocation failed — there is no instance at all, and the
#      port the runtime names belongs to somebody else.
#
# In every case the round keeps its hands off the address it already
# had: an address nobody serves is not an address a request may be
# sent to.

#: A program the service manager starts.  It answers ``/health`` and
#: ``/health/data``, and reads both answers from files — so a test can
#: change what a *running* instance says without restarting it.
_SERVICE_PROGRAM = '''
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEALTH_FILE = {health_file!r}
ROWS_FILE = {rows_file!r}
PORT = {port}


def _read_int(path, default):
    try:
        with open(path) as fp:
            return int(fp.read().strip())
    except Exception:
        return default


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0].endswith("/health/data"):
            status = 200
            body = json.dumps({{
                "status": "ok",
                "plan_routing_rows": _read_int(ROWS_FILE, 0),
                "instance": {{"boot_id": "boot-1234"}},
            }}).encode()
        else:
            status = _read_int(HEALTH_FILE, 200)
            body = b"{{\\"status\\": \\"ok\\"}}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


def _free_port() -> int:
    """A port nothing is listening on right now."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _service_declaration(
    tmp_path: Path, name: str, *, health: int, rows: int, timeout: int = 20,
):
    """A declaration whose service answers *health* and *rows*.

    Returns ``(declaration, health_file, rows_file)``.  The two files
    are how a test changes what the running instance says.
    """
    import shlex

    from service_declaration import ServiceDeclaration

    port = _free_port()
    health_file = tmp_path / f"{name}-health"
    rows_file = tmp_path / f"{name}-rows"
    health_file.write_text(str(health), encoding="utf-8")
    rows_file.write_text(str(rows), encoding="utf-8")
    program = _SERVICE_PROGRAM.format(
        port=port, health_file=str(health_file), rows_file=str(rows_file),
    )
    declaration = ServiceDeclaration(
        name=name,
        port=port,
        start_cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=timeout,
    )
    return declaration, health_file, rows_file


def _stop(sm, runtime, declaration) -> None:
    """Stop a service the manager started, if it is still running."""
    if not getattr(runtime, "pid", None):
        return
    try:
        sm.stop_service(runtime, declaration)
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


class _ForeignListener:
    """A process that holds a port and is nobody's business.

    It answers like a service would, but its command line is the test
    runner's — so the service manager cannot identify it as the
    declared service, and gets out of its way.
    """

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - the handler API's own name
                body = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def test_reensure_not_adopted_when_port_has_no_listener(tmp_path, monkeypatch):
    """A re-ensure that names a port nothing serves is not adopted.

    The production round's re-ensure reported a service that was
    **ready**, on a port (51725) nothing had ever listened on, and the
    round adopted the address without asking.  The VPs then pointed at
    it and were graded on the connection refusal.

    So the result is asked, not believed: a port nobody serves is not
    an address, and the round keeps the one it had.
    """
    import service_manager as sm
    from service_declaration import ServiceDeclaration

    # The round's own fixture holds four routing rows.
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    # The port is free and the start command exits at once: nothing ever
    # listens there.
    declaration = ServiceDeclaration(
        name="api", port=_free_port(), start_cmd="false", cwd=".",
        ready_timeout_seconds=2,
    )
    runtime_map = sm.ensure_services(
        project_dir, {"api": declaration}, plan_dir=plan_dir,
    )
    runtime = runtime_map.runtimes["api"]
    try:
        assert runtime.ready is False, runtime.detail
        assert runtime.usable is False, (
            "a runtime that is not ready is not an address"
        )
        assert runtime_map.ready_names == [], (
            "the round must not adopt an address nobody serves"
        )
        assert runtime.port in runtime_map.unready_ports
        # ... and it knows what it found: no instance, no data plane.
        assert getattr(runtime, "data_plane", None) is not None, (
            "the re-ensure's result has to carry the data plane it found"
        )
        assert runtime.data_plane.proved is False
    finally:
        _stop(sm, runtime, declaration)


def test_reensure_adopted_only_after_health_200(tmp_path, monkeypatch):
    """The result is adopted once the instance answers — not before.

    Three phases, one rule: the round adopts the address only after
    the instance on it has been **asked** and has answered.

      1. the health URL says 503 — the instance is up and not serving;
      2. it says 200 and serves the round's four routing rows — adopted,
         and the round records what it adopted;
      3. it says 200 and serves **no** rows — not adopted.  Every
         assertion that followed would have been graded against an
         empty table, which is exactly how the 2026-10-24 round was
         graded.
    """
    import service_manager as sm

    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    # 1. The health URL says 503: the instance is up and not serving.
    sick, _, _ = _service_declaration(
        tmp_path, "api-sick", health=503, rows=4, timeout=3,
    )
    runtime_map = sm.ensure_services(
        project_dir, {"api": sick}, plan_dir=plan_dir,
    )
    sick_runtime = runtime_map.runtimes["api"]
    try:
        assert sick_runtime.ready is False, sick_runtime.detail
        assert sick_runtime.usable is False
        assert runtime_map.ready_names == []
    finally:
        _stop(sm, sick_runtime, sick)

    # 2. The same service answering 200 and serving the round's rows:
    #    adopted, and the round can say what it adopted.
    well, _, _ = _service_declaration(
        tmp_path, "api-well", health=200, rows=4,
    )
    runtime_map = sm.ensure_services(
        project_dir, {"api": well}, plan_dir=plan_dir,
    )
    well_runtime = runtime_map.runtimes["api"]
    try:
        assert well_runtime.ready is True, well_runtime.detail
        assert well_runtime.usable is True
        assert runtime_map.ready_names == ["api"]
        assert well_runtime.data_plane is not None
        assert well_runtime.data_plane.plan_routing_rows == 4
        assert well_runtime.data_plane.boot_id == "boot-1234"
    finally:
        _stop(sm, well_runtime, well)

    # 3. Health 200, but the instance serves no rows: not adopted.
    hollow, _, _ = _service_declaration(
        tmp_path, "api-hollow", health=200, rows=0, timeout=3,
    )
    runtime_map = sm.ensure_services(
        project_dir, {"api": hollow}, plan_dir=plan_dir,
    )
    hollow_runtime = runtime_map.runtimes["api"]
    try:
        assert hollow_runtime.ready is False, hollow_runtime.detail
        assert hollow_runtime.usable is False
        assert runtime_map.ready_names == []
        assert hollow_runtime.data_plane is not None
        assert hollow_runtime.data_plane.verdict == sm.DATA_PLANE_EMPTY
    finally:
        _stop(sm, hollow_runtime, hollow)


def test_failed_relocation_never_used(tmp_path, monkeypatch):
    """A relocation that failed names a port that is not ours.

    Somebody else holds the declared port, so the round gets out of the
    way and starts its own copy elsewhere.  When **that** fails too,
    the runtime it returns names the declared port — which is still
    somebody else's.  That port must never be used as an address: the
    runtime says so in a field, and the round blocks it.
    """
    import service_manager as sm
    from service_declaration import ServiceDeclaration

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    squatter = _ForeignListener()
    try:
        declaration = ServiceDeclaration(
            name="api", port=squatter.port,
            start_cmd="pdt-nonexistent-service-binary", cwd=".",
            ready_timeout_seconds=1,
        )
        runtime_map = sm.ensure_services(
            project_dir, {"api": declaration}, plan_dir=plan_dir,
        )
        runtime = runtime_map.runtimes["api"]
    finally:
        squatter.close()

    assert runtime.origin == "failed", runtime.detail
    assert runtime.unusable is True, (
        "a relocation that could not place the service must say so in a "
        "field; the port it names is somebody else's"
    )
    assert runtime.usable is False
    assert runtime.ready is False
    assert runtime.port == squatter.port, (
        "the failed runtime names the declared port — and that port is "
        "the stranger's"
    )
    assert runtime_map.ready_names == []
    assert squatter.port in runtime_map.unready_ports
    assert runtime_map.blocked_ports == [squatter.port], (
        "the port the stranger holds must stay blocked"
    )
