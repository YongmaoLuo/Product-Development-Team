"""Unit tests for the service-restart agent + orchestrator.

Covers:
  * Prompt construction — protected ports and kill guards appear in
    the system prompt; probe evidence lands in the user prompt.
  * ``_parse_agent_output`` — non-dict payload, missing health_check
    downgrade, happy path.
  * ``run`` — query_json invoked with the ``service_restart`` scene
    (medium tier) and the restricted tool list; provider-type warning
    for non-Claude tools.
  * ``_fast_path_restart_port`` — deny-listed tunnel cmdlines, missing
    cmdline, protected port, and a stubbed happy path.
  * ``attempt_service_restart`` — fast path short-circuits the LLM;
    failure falls through to the agent; the independent post-check is
    the only success signal; protected ports are refused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _agent(coding_tool=None):
    from service_restart_agent import ServiceRestartAgent
    return ServiceRestartAgent(coding_tool, "plan-x", "VP-001", registry=None)


def test_system_prompt_names_protected_ports() -> None:
    prompt = _agent().build_system_prompt()
    for port in ("8000", "8002", "8003"):
        assert port in prompt
    assert "NEVER kill" in prompt


def test_system_prompt_forbids_unknown_holder_kill() -> None:
    prompt = _agent().build_system_prompt()
    assert "unknown process" in prompt.lower()
    assert "REPORT" in prompt


def test_user_prompt_includes_probe_evidence() -> None:
    from service_freshness import PortProbe
    probes = [
        PortProbe(port=8080, listening=True, pids=[42],
                  cmdlines=["/p/venv/bin/uvicorn api.api_server:app"]),
        PortProbe(port=9200),
    ]
    prompt = _agent().build_user_prompt(Path("/proj"), [8080, 9200], probes)
    assert "8080" in prompt and "9200" in prompt
    assert "uvicorn" in prompt
    assert "NO LISTENER" in prompt
    assert "/proj" in prompt


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def test_parse_non_dict_payload_fails() -> None:
    result = _agent()._parse_agent_output("garbage", 0.0)
    assert result.success is False
    assert result.agent_used is True
    assert "non-dict" in result.diagnostics


def test_parse_success_without_health_check_downgraded() -> None:
    raw = {"success": True, "commands_run": ["pkill x"], "diagnostics": ""}
    result = _agent()._parse_agent_output(raw, 0.0)
    assert result.success is False
    assert "health_check" in result.diagnostics


def test_parse_happy_path() -> None:
    import time as _time
    raw = {
        "success": True,
        "commands_run": ["kill 1", "nohup uvicorn app:app &"],
        "diagnostics": "",
        "health_check": "HTTP 200 after 6s",
    }
    result = _agent()._parse_agent_output(raw, _time.monotonic())
    assert result.success is True
    assert result.commands_run == ["kill 1", "nohup uvicorn app:app &"]
    assert 0 <= result.duration_seconds < 5


# ---------------------------------------------------------------------------
# run() — scene + tool restriction plumbed through
# ---------------------------------------------------------------------------

class _FakeCodingTool:
    """Records query_json kwargs and returns a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []
        self.settings = None

    def query_json(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.payload


def test_run_passes_scene_and_restricted_tools() -> None:
    from service_restart_agent import ServiceRestartAgent
    fake = _FakeCodingTool(
        {"success": True, "commands_run": ["c"], "health_check": "HTTP 200"}
    )
    agent = ServiceRestartAgent(fake, "plan-x", "VP-001", registry=None)
    result = agent.run(Path("/proj"), [8080], [])
    assert result.success is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.get("scene") == "service_restart"
    assert call.get("allowed_tools") == ["Bash", "Read", "Grep", "Glob"]
    assert "system_instruction" in call


def test_run_agent_exception_becomes_failure_result() -> None:
    from service_restart_agent import ServiceRestartAgent

    class _Boom:
        settings = None

        def query_json(self, *a, **k):
            raise RuntimeError("provider down")

    agent = ServiceRestartAgent(_Boom(), "plan-x", "VP-001", registry=None)
    result = agent.run(Path("/proj"), [8080], [])
    assert result.success is False
    assert "provider down" in result.diagnostics
    assert result.agent_used is True


# ---------------------------------------------------------------------------
# Fast path
# ---------------------------------------------------------------------------

def test_fast_path_skips_protected_port() -> None:
    from service_restart_agent import _fast_path_restart_port
    from service_freshness import PortProbe
    assert _fast_path_restart_port(
        Path("/tmp"), PortProbe(port=8000, listening=True, pids=[1],
                                cmdlines=["server"]),
    ) is None


def test_fast_path_skips_when_no_cmdline_captured() -> None:
    from service_restart_agent import _fast_path_restart_port
    from service_freshness import PortProbe
    assert _fast_path_restart_port(
        Path("/tmp"), PortProbe(port=8080, listening=True, pids=[1]),
    ) is None


@pytest.mark.parametrize("cmdline", [
    "ssh -L 8080:db.internal:5432 bastion",
    "socat TCP-LISTEN:8080,fork TCP:backend:8080",
    "kubectl port-forward svc/api 8080:8080",
])
def test_fast_path_denies_tunnel_cmdlines(cmdline) -> None:
    from service_restart_agent import _fast_path_restart_port
    from service_freshness import PortProbe
    probe = PortProbe(port=8080, listening=True, pids=[9],
                      cmdlines=[cmdline])
    assert _fast_path_restart_port(Path("/tmp"), probe) is None


def test_fast_path_happy_path(tmp_path, monkeypatch) -> None:
    from service_restart_agent import _fast_path_restart_port
    from service_freshness import PortProbe

    ran: list[list[str]] = []
    popped: list[list[str]] = []

    def fake_run(argv, **kwargs):
        ran.append(argv)
        return None

    def fake_popen(argv, **kwargs):
        popped.append(argv)
        return None

    monkeypatch.setattr("service_restart_agent.subprocess.run", fake_run)
    monkeypatch.setattr("service_restart_agent.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "service_restart_agent.wait_for_ready", lambda *a, **k: True,
    )
    monkeypatch.setattr("service_restart_agent.time.sleep", lambda *_: None)

    probe = PortProbe(port=8080, listening=True, pids=[7, 8],
                      cmdlines=["/p/venv1/bin/uvicorn api.api_server:app"])
    commands = _fast_path_restart_port(tmp_path, probe)
    assert commands is not None
    assert ran == [["kill", "7"], ["kill", "8"]]
    assert popped[0][:2] == ["nohup", "/p/venv1/bin/uvicorn"]
    assert any("nohup" in c for c in commands)


def test_fast_path_readiness_timeout_returns_none(tmp_path, monkeypatch) -> None:
    from service_restart_agent import _fast_path_restart_port
    from service_freshness import PortProbe
    monkeypatch.setattr("service_restart_agent.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("service_restart_agent.subprocess.Popen", lambda *a, **k: None)
    monkeypatch.setattr(
        "service_restart_agent.wait_for_ready", lambda *a, **k: False,
    )
    monkeypatch.setattr("service_restart_agent.time.sleep", lambda *_: None)
    probe = PortProbe(port=8080, listening=True, pids=[7],
                      cmdlines=["uvicorn app:app"])
    assert _fast_path_restart_port(tmp_path, probe) is None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def test_orchestrator_fast_path_avoids_llm(tmp_path, monkeypatch) -> None:
    from service_restart_agent import attempt_service_restart
    from service_freshness import PortProbe

    monkeypatch.setattr(
        "service_restart_agent._fast_path_restart_port",
        lambda *a, **k: ["kill 7", "nohup x &"],
    )
    monkeypatch.setattr(
        "service_restart_agent.probe_port",
        lambda port: PortProbe(port=port, listening=True),
    )
    monkeypatch.setattr(
        "service_restart_agent.wait_for_ready", lambda *a, **k: True,
    )
    called: list[bool] = []

    class _NoAgent:
        def __init__(self, *a, **k):
            called.append(True)

    monkeypatch.setattr(
        "service_restart_agent.ServiceRestartAgent", _NoAgent,
    )

    probes = [PortProbe(port=8080, listening=True, pids=[7],
                        cmdlines=["uvicorn app:app"])]
    result = attempt_service_restart(
        tmp_path, [8080], probes, coding_tool=object(),
        plan_id="plan-x",
    )
    assert result.success is True
    assert result.agent_used is False
    assert called == []
    assert result.ports_restarted == [8080]


def test_orchestrator_falls_through_to_agent_on_fast_path_failure(
    tmp_path, monkeypatch,
) -> None:
    from service_restart_agent import attempt_service_restart, RestartResult
    from service_freshness import PortProbe

    monkeypatch.setattr(
        "service_restart_agent._fast_path_restart_port", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "service_restart_agent.probe_port",
        lambda port: PortProbe(port=port, listening=True),
    )
    monkeypatch.setattr(
        "service_restart_agent.wait_for_ready", lambda *a, **k: True,
    )

    agent_calls: list[tuple] = []

    class _StubAgent:
        def __init__(self, coding_tool, plan_id, vp_id, registry=None):
            agent_calls.append((plan_id, vp_id))

        def run(self, project_dir, ports, probes):
            return RestartResult(
                success=True, agent_used=True,
                commands_run=["nohup discovered &"],
            )

    monkeypatch.setattr(
        "service_restart_agent.ServiceRestartAgent", _StubAgent,
    )

    probes = [PortProbe(port=8080)]  # no listener → no cmdline → LLM path
    result = attempt_service_restart(
        tmp_path, [8080], probes, coding_tool=object(), plan_id="plan-x",
    )
    assert result.success is True
    assert result.agent_used is True
    assert agent_calls == [("plan-x", "service-freshness")]
    assert result.ports_restarted == [8080]


def test_orchestrator_post_check_failure_overrides_agent_claim(
    tmp_path, monkeypatch,
) -> None:
    """Agent claims success but the port is still dead → success=False."""
    from service_restart_agent import attempt_service_restart, RestartResult
    from service_freshness import PortProbe

    monkeypatch.setattr(
        "service_restart_agent._fast_path_restart_port", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "service_restart_agent.probe_port",
        lambda port: PortProbe(port=port),  # never listening
    )
    monkeypatch.setattr(
        "service_restart_agent.wait_for_ready", lambda *a, **k: False,
    )

    class _LyingAgent:
        def __init__(self, *a, **k):
            pass

        def run(self, *a, **k):
            return RestartResult(success=True, agent_used=True,
                                 commands_run=["x"])

    monkeypatch.setattr(
        "service_restart_agent.ServiceRestartAgent", _LyingAgent,
    )

    result = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=object(), plan_id="plan-x",
    )
    assert result.success is False
    assert result.post_check_passed is False
    assert "POST-CHECK FAILED" in result.diagnostics


def test_orchestrator_refuses_protected_ports(tmp_path, monkeypatch) -> None:
    from service_restart_agent import attempt_service_restart
    from service_freshness import PortProbe
    monkeypatch.setattr(
        "service_restart_agent.probe_port",
        lambda port: PortProbe(port=port, listening=True),
    )
    monkeypatch.setattr(
        "service_restart_agent.wait_for_ready", lambda *a, **k: False,
    )
    result = attempt_service_restart(
        tmp_path, [8000], [PortProbe(port=8000, listening=True, pids=[1],
                                     cmdlines=["ac-server"])],
        coding_tool=object(), plan_id="plan-x",
    )
    assert result.success is False
    assert result.ports_restarted == []
