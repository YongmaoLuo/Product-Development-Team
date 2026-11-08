"""A VP whose request URL is a ``{{svc.*}}`` placeholder must still self-heal.

The failure this pins
---------------------
The plan under repair writes its api_test verification points against the
**service placeholder**, not against a literal address::

    "url": "{{svc.api.url}}/health"
    "url": "{{svc.api-mcode.url}}/api/system/active"

``_vp_target_port`` used to ``urlsplit`` that string directly.  A
placeholder has no netloc, so ``urlsplit(...).port`` is ``None``, the
method returned ``None``, and ``_relaunch_service_for_vp`` gave up
*without even probing the port*.  Every api_test VP whose URL was a
placeholder was therefore unhealable: a service that died between the
preflight and the request stayed dead, and the round recorded a bare
"connection refused" with nothing saying the service had been killed.

The contract this file pins:

  1. the placeholder resolves to the port the service really listens on
     — the **effective** declaration, i.e. the relocated one, because
     that is the port the preflight resolved ``{{svc.api.url}}`` to;
  2. when the placeholder cannot be expanded into a parseable URL, the
     service it *names* is still found, and its declared port is used;
  3. the relaunch fires for such a VP and puts the service back **on
     that same port** — the URL the VP holds must stay true;
  4. a service that comes up on a *different* port is a FAILED
     relaunch, and the reason says so — the URL is never rewritten to
     meet the service.

The default claude path is untouched by all of this: a plan that
declares no service, or a VP that points at a third-party endpoint, is
still left alone.

The second failure this file pins
--------------------------------
The service whose declared port was held by a stranger was **relocated**
by the round, and the plan's VP was resolved against the new port::

    "url": "{{svc.api.url}}/health"  ->  http://127.0.0.1:52720/health

Then the service died.  The round's runtime map still records it — on
port 52720, with the pid it had — but the snapshot no longer says
"ready", and the round used to answer "which service is this?" from the
*ready* snapshot alone.  The answer came back empty, the relaunch never
fired, and every api VP of the round ended in "connection refused" with
nothing in the log saying a service had been killed.

So this file also pins:

  5. a VP keeps naming its service after its URL was expanded — the
     binding is the one thing the expanded URL can no longer say, and it
     is kept **in memory only**: the plan on disk keeps its placeholders;
  6. a dead service is still recognised, and is brought back **on the
     port the VP's URL names**;
  7. a port that belongs to no declared service is named as such, rather
     than silently ignored;
  8. the mcode side stands or falls with the claude side.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import PortProbe  # noqa: E402
from service_manager import ServiceRuntime, ServiceRuntimeMap  # noqa: E402


#: The port the ``api`` service listens on in these tests.
API_PORT = 18991
#: The port the ``api-mcode`` service listens on in these tests.
MCODE_PORT = 18992
#: A port nothing in these tests declares.
UNDECLARED_PORT = 18993
#: The port the ``api`` service was **declared** on before the round had
#: to relocate it.
DECLARED_API_PORT = 18231
#: The port it was relocated to — the one the plan's URL was resolved
#: against, and the one the VP's URL still names.
RELOCATED_API_PORT = 52720
#: The same pair, for the mcode side.
DECLARED_MCODE_PORT = 18232
RELOCATED_MCODE_PORT = 52721


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def _declaration(name: str, port: int) -> ServiceDeclaration:
    """A declaration of the plan's own shape — no harness pinned, so the
    default claude path is what runs."""
    return ServiceDeclaration(
        name=name,
        port=port,
        start_cmd=f"export PDT_PORT={port}; exec python server.py",
    )


def _runtime(name: str, port: int, pid: int = 111) -> ServiceRuntime:
    return ServiceRuntime(
        name=name,
        port=port,
        pid=pid,
        start_cmd=f"export PDT_PORT={port}; exec python server.py",
        origin="spawned",
        ready=True,
    )


def _agent_with_services(tmp_path: Path, names: Dict[str, int]):
    """An agent that declared one service per name, each already up."""
    agent = _make_agent(tmp_path)
    agent._service_declarations = {
        name: _declaration(name, port) for name, port in names.items()
    }
    agent._service_runtime_map = ServiceRuntimeMap(
        runtimes={
            name: _runtime(name, port)
            for name, port in names.items()
        }
    )
    return agent


def _api_vp(url: str, vp_id: str = "VP-001") -> Dict[str, Any]:
    """An api_test VP, exactly as the plan writes it."""
    return {
        "id": vp_id,
        "title": "接口契约",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": url,
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [{"name": "状态码 200", "status": 200}],
    }


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_vp_target_port_resolves_service_placeholder(tmp_path: Path):
    """``{{svc.<name>.url}}`` must resolve to the service's real port."""
    agent = _agent_with_services(
        tmp_path, {"api": API_PORT, "api-mcode": MCODE_PORT},
    )

    # The plan's own spelling: the placeholder, then the endpoint path.
    assert agent._vp_target_port(
        _api_vp("{{svc.api.url}}/health")
    ) == API_PORT
    assert agent._vp_target_port(
        _api_vp("{{svc.api.url}}/api/system/active")
    ) == API_PORT
    # A service name with a dash in it, as the plan spells it.
    assert agent._vp_target_port(
        _api_vp("{{svc.api-mcode.url}}/health")
    ) == MCODE_PORT
    # Whitespace inside the braces — the planner is an LLM.
    assert agent._vp_target_port(
        _api_vp("{{ svc.api.url }}/health")
    ) == API_PORT
    # A placeholder for the port itself.
    assert agent._vp_target_port(
        _api_vp("http://127.0.0.1:{{svc.api.port}}/health")
    ) == API_PORT
    # A placeholder for a field the vocabulary does not carry cannot be
    # expanded into a URL — but the service it names is still found.
    assert agent._vp_target_port(
        _api_vp("{{svc.api.base_url}}/health")
    ) == API_PORT

    # The placeholder names a service the plan never declared: nothing
    # this round could own, so nothing to heal.
    assert agent._vp_target_port(
        _api_vp("{{svc.typo.url}}/health")
    ) is None
    # A third-party endpoint is not ours either.
    assert agent._vp_target_port(
        _api_vp("http://example.com/health")
    ) is None
    # The backend's own control plane is never ours to touch.
    assert agent._vp_target_port(
        _api_vp("http://127.0.0.1:8000/health")
    ) is None


@pytest.mark.integration
def test_relaunch_fires_for_placeholder_vp(tmp_path: Path, monkeypatch):
    """A placeholder URL must not make the relaunch path dead code."""
    import service_freshness
    import service_manager

    agent = _agent_with_services(tmp_path, {"api": API_PORT})

    monkeypatch.setattr(
        service_freshness, "probe_port",
        lambda p: PortProbe(port=p, listening=False),
    )

    restarted: Dict[str, Any] = {}

    def _fake_ensure(project_dir, declarations, **kwargs):
        restarted["declarations"] = dict(declarations)
        runtime = ServiceRuntime(
            name="api",
            port=API_PORT,
            pid=222,
            start_cmd=declarations["api"].start_cmd,
            origin="spawned",
            ready=True,
            detail="started (pid 222)",
        )
        return ServiceRuntimeMap(runtimes={"api": runtime})

    monkeypatch.setattr(service_manager, "ensure_services", _fake_ensure)
    monkeypatch.setattr(
        service_manager, "record_runtime", lambda *a, **k: None,
    )

    events: list = []
    monkeypatch.setattr(
        agent,
        "_safe_log_freshness_event",
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        ),
    )

    result = agent._relaunch_service_for_vp(
        _api_vp("{{svc.api.url}}/health")
    )

    assert result is not None, (
        "a VP whose URL is a service placeholder must still be self-healed"
    )
    assert result["ok"] is True, result
    assert result["service"] == "api", result
    assert result["port"] == API_PORT, result
    # The service was started again on the port the VP's URL names.
    assert restarted["declarations"]["api"].port == API_PORT, restarted

    # The round's log says the self-healing actually happened.
    relaunched = [e for e in events if e[1] == "service_relaunched_before_vp"]
    assert len(relaunched) == 1, events
    vp_id, event_type, data = relaunched[0]
    assert vp_id == "VP-001"
    assert event_type == "service_relaunched_before_vp"
    assert data["port"] == API_PORT
    assert data["old_pid"] == 111
    assert data["new_pid"] == 222


@pytest.mark.integration
def test_relaunch_fails_when_the_service_comes_up_on_another_port(
    tmp_path: Path, monkeypatch,
):
    """The port must not move: a mismatch is FAILED, and it says so."""
    import service_freshness
    import service_manager

    agent = _agent_with_services(tmp_path, {"api": API_PORT})

    monkeypatch.setattr(
        service_freshness, "probe_port",
        lambda p: PortProbe(port=p, listening=False),
    )

    def _fake_ensure(project_dir, declarations, **kwargs):
        runtime = ServiceRuntime(
            name="api",
            port=UNDECLARED_PORT,
            pid=222,
            start_cmd=declarations["api"].start_cmd,
            origin="spawned",
            ready=True,
            detail="started (pid 222)",
        )
        return ServiceRuntimeMap(runtimes={"api": runtime})

    monkeypatch.setattr(service_manager, "ensure_services", _fake_ensure)
    monkeypatch.setattr(
        service_manager, "record_runtime", lambda *a, **k: None,
    )

    result = agent._relaunch_service_for_vp(
        _api_vp("{{svc.api.url}}/health")
    )

    assert result is not None
    assert result["ok"] is False, result
    assert result["port"] == API_PORT, result
    # The reason names the mismatch: the VP's URL still points at
    # API_PORT, so a service on another port does not satisfy it.
    reason = f"{result.get('reason')} {result.get('detail')}"
    assert str(API_PORT) in reason, result
    assert str(UNDECLARED_PORT) in reason, result


# ---------------------------------------------------------------------------
# The relocated port, after the service died
# ---------------------------------------------------------------------------


def _relocated_agent(
    tmp_path: Path, name: str, declared: int, relocated: int, pid: int = 111,
    harness: str = "",
):
    """An agent whose one declared service was moved off its port.

    The declaration says ``declared``; the round's runtime map says the
    service really listens on ``relocated`` — the port a stranger was
    holding when the round started it.
    """
    agent = _make_agent(tmp_path)
    start_cmd = (
        f"export PDT_HARNESS={harness}; " if harness else ""
    ) + f"export PDT_PORT={declared}; exec python server.py"
    declaration = ServiceDeclaration(
        name=name, port=declared, start_cmd=start_cmd,
    )
    agent._service_declarations = {name: declaration}
    agent._service_runtime_map = ServiceRuntimeMap(
        runtimes={
            name: ServiceRuntime(
                name=name, port=relocated, pid=pid,
                start_cmd=start_cmd, origin="spawned", ready=True,
            ),
        },
    )
    return agent, declaration


def _resolved_vp(agent, url: str, vp_id: str = "VP-001"):
    """The VP as the round hands it to the executor: placeholders expanded."""
    plan = {"verification_points": [_api_vp(url, vp_id)]}
    return agent._resolve_service_placeholders(plan)["verification_points"][0]


def _kill_the_service(agent, name: str) -> None:
    """The service died: the round's snapshot no longer says "ready"."""
    agent._service_runtime_map.runtimes[name].ready = False


def _patch_relaunch(monkeypatch, port: int, pid: int = 222, name: str = "api"):
    """Make the port look dead and the relaunch come back on ``port``."""
    import service_freshness
    import service_manager

    monkeypatch.setattr(
        service_freshness, "probe_port",
        lambda p: PortProbe(port=p, listening=False),
    )

    restarted: Dict[str, Any] = {}

    def _fake_ensure(project_dir, declarations, **kwargs):
        restarted["declarations"] = dict(declarations)
        runtime = ServiceRuntime(
            name=name,
            port=port,
            pid=pid,
            start_cmd=declarations[name].start_cmd,
            origin="spawned",
            ready=True,
            detail=f"started (pid {pid})",
        )
        return ServiceRuntimeMap(runtimes={name: runtime})

    monkeypatch.setattr(service_manager, "ensure_services", _fake_ensure)
    monkeypatch.setattr(
        service_manager, "record_runtime", lambda *a, **k: None,
    )
    return restarted


def _collect_events(agent, monkeypatch):
    events: list = []
    monkeypatch.setattr(
        agent,
        "_safe_log_freshness_event",
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        ),
    )
    return events


def _closed_port() -> int:
    """A port nothing is listening on right now."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_a_resolved_vp_still_names_its_service(tmp_path: Path):
    """The expanded URL can no longer say which service it came from."""
    agent = _agent_with_services(tmp_path, {"api": API_PORT})

    resolved = _resolved_vp(agent, "{{svc.api.url}}/health")

    assert resolved["request"]["url"] == (
        f"http://127.0.0.1:{API_PORT}/health"
    )
    # The placeholder is gone from the URL ...
    assert "{{" not in resolved["request"]["url"]
    # ... but the VP still names the service it came from.
    assert agent._vp_placeholder_service(resolved) == "api"


@pytest.mark.integration
def test_the_service_binding_is_never_written_back_to_the_plan(
    tmp_path: Path,
):
    """The binding is the round's own bookkeeping, not the plan's."""
    agent = _agent_with_services(tmp_path, {"api": API_PORT})
    plan = {"verification_points": [_api_vp("{{svc.api.url}}/health")]}

    agent._resolve_service_placeholders(plan)

    # The plan the caller holds still carries the placeholder: the
    # on-disk plan keeps it, and every round re-resolves it.
    assert plan["verification_points"][0]["request"]["url"] == (
        "{{svc.api.url}}/health"
    )
    # And the resolved copy carries no bookkeeping of its own, so
    # nothing of the round's can reach the plan file.
    resolved = agent._resolve_service_placeholders(plan)[
        "verification_points"
    ][0]
    assert set(resolved) == set(plan["verification_points"][0])


@pytest.mark.integration
def test_relaunch_fires_for_a_resolved_vp_after_the_service_died(
    tmp_path: Path, monkeypatch,
):
    """A dead service is still recognised, and comes back on its port."""
    agent, _decl = _relocated_agent(
        tmp_path, "api", DECLARED_API_PORT, RELOCATED_API_PORT,
    )

    resolved = _resolved_vp(agent, "{{svc.api.url}}/health")
    assert resolved["request"]["url"] == (
        f"http://127.0.0.1:{RELOCATED_API_PORT}/health"
    )

    # The service died: the process is gone, so the round's snapshot no
    # longer says "ready" — but the port it was moved to is still the
    # port the VP's URL names.
    _kill_the_service(agent, "api")

    restarted = _patch_relaunch(monkeypatch, RELOCATED_API_PORT)
    events = _collect_events(agent, monkeypatch)

    result = agent._relaunch_service_for_vp(resolved)

    assert result is not None, (
        "a VP whose service died must still be self-healed"
    )
    assert result["ok"] is True, result
    assert result["service"] == "api", result
    assert result["port"] == RELOCATED_API_PORT, result
    assert result["old_pid"] == 111, result
    assert result["new_pid"] == 222, result
    assert result["start_cmd"], result

    # The service came back on the port the VP's URL names — the URL is
    # never rewritten to meet the service.
    assert restarted["declarations"]["api"].port == RELOCATED_API_PORT, (
        restarted
    )

    # The round's log says the self-healing actually happened.
    relaunched = [e for e in events if e[1] == "service_relaunched_before_vp"]
    assert len(relaunched) == 1, events
    vp_id, event_type, data = relaunched[0]
    assert vp_id == "VP-001"
    assert event_type == "service_relaunched_before_vp"
    assert data["service"] == "api", data
    assert data["port"] == RELOCATED_API_PORT, data
    assert data["old_pid"] == 111, data
    assert data["new_pid"] == 222, data
    assert data["start_cmd"], data

    # And the outcome of the relaunch is on the record too.
    outcomes = [e for e in events if e[1] == "service_relaunch"]
    assert len(outcomes) == 1, events
    assert outcomes[0][2]["ok"] is True, outcomes[0][2]
    assert outcomes[0][2]["port"] == RELOCATED_API_PORT, outcomes[0][2]


@pytest.mark.integration
def test_the_mcode_service_is_healed_the_same_way(
    tmp_path: Path, monkeypatch,
):
    """Both declared backends stand or fall together."""
    import service_manager

    agent, _decl = _relocated_agent(
        tmp_path, "api-mcode", DECLARED_MCODE_PORT, RELOCATED_MCODE_PORT,
        harness="mcode",
    )

    resolved = _resolved_vp(
        agent, "{{svc.api-mcode.url}}/health", vp_id="VP-008",
    )
    assert resolved["request"]["url"] == (
        f"http://127.0.0.1:{RELOCATED_MCODE_PORT}/health"
    )

    _kill_the_service(agent, "api-mcode")

    restarted = _patch_relaunch(
        monkeypatch, RELOCATED_MCODE_PORT, name="api-mcode",
    )
    events = _collect_events(agent, monkeypatch)

    # The relaunched process has to be the mcode backend: the
    # declaration pins it, so the framework checks the environment of
    # the process that came up.
    monkeypatch.setattr(
        service_manager, "process_environment",
        lambda pid: {"PDT_HARNESS": "mcode"},
    )

    result = agent._relaunch_service_for_vp(resolved)

    assert result is not None, (
        "the mcode service must be healed the same way the claude one is"
    )
    assert result["ok"] is True, result
    assert result["service"] == "api-mcode", result
    assert result["port"] == RELOCATED_MCODE_PORT, result
    assert result["old_pid"] == 111, result
    assert result["new_pid"] == 222, result
    assert restarted["declarations"]["api-mcode"].port == (
        RELOCATED_MCODE_PORT
    ), restarted

    relaunched = [e for e in events if e[1] == "service_relaunched_before_vp"]
    assert len(relaunched) == 1, events
    assert relaunched[0][2]["service"] == "api-mcode", relaunched[0][2]
    assert relaunched[0][2]["port"] == RELOCATED_MCODE_PORT, relaunched[0][2]
    # The command is on the record verbatim: it is what says which
    # backend the relaunched service runs under.
    assert "PDT_HARNESS=mcode" in relaunched[0][2]["start_cmd"], (
        relaunched[0][2]
    )


@pytest.mark.integration
def test_a_port_of_no_declared_service_is_named_as_such(
    tmp_path: Path, caplog,
):
    """Silence is not an answer: the port is reported as not ours."""
    agent = _agent_with_services(tmp_path, {"api": API_PORT})

    with caplog.at_level("WARNING", logger="verification_agent"):
        name, decl = agent._declaration_for_port(UNDECLARED_PORT)

    assert name == ""
    assert decl is None
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert str(UNDECLARED_PORT) in messages, messages
    assert "no declared service" in messages, messages


@pytest.mark.integration
def test_a_vp_that_stops_naming_a_service_is_unbound(tmp_path: Path):
    """A stale name would attribute the VP to a service it does not talk
    to."""
    agent = _agent_with_services(tmp_path, {"api": API_PORT})

    agent._resolve_service_placeholders(
        {"verification_points": [_api_vp("{{svc.api.url}}/health")]}
    )
    assert agent._vp_placeholder_service(
        _api_vp("{{svc.api.url}}/health")
    ) == "api"

    # The plan is rewritten with a literal URL: the VP names no service
    # any more, so the binding has to go with it.
    literal = f"http://127.0.0.1:{API_PORT}/health"
    agent._resolve_service_placeholders(
        {"verification_points": [_api_vp(literal)]}
    )
    assert agent._vp_placeholder_service(_api_vp(literal)) == ""


@pytest.mark.integration
def test_the_default_path_keeps_its_silence(
    tmp_path: Path, caplog,
):
    """A round that declares no service has no news to make about ports."""
    agent = _make_agent(tmp_path)

    with caplog.at_level("WARNING", logger="verification_agent"):
        name, decl = agent._declaration_for_port(UNDECLARED_PORT)

    assert (name, decl) == ("", None)
    assert not caplog.records, [r.getMessage() for r in caplog.records]


@pytest.mark.integration
def test_a_refused_connection_is_booked_as_a_transport_fault(
    tmp_path: Path,
):
    """A refused connection ran no assertion — it is neither a pass nor
    a broken contract."""
    agent = _make_agent(tmp_path)
    vp = _api_vp(f"http://127.0.0.1:{_closed_port()}/health", "VP-001")
    vp["request"]["timeout_seconds"] = 5

    verdict = agent._run_api_test_vp(vp)

    assert verdict["status"] != "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    # No response means no assertion ran: the criterion was **not
    # observed**, which is neither a satisfied criterion nor a broken
    # contract.
    assert evidence.get("assertions_executed") == 0, evidence
    assert not evidence.get("assertions_failed"), evidence
    reasons = " ".join(str(reason) for reason in (verdict.get("reasons") or []))
    assert "no response" in reasons, verdict
