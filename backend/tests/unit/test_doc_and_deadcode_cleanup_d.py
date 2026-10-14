"""§4 — service_restart_agent's dispatch-failure honesty (2026-10-14).

``service_restart_agent.ServiceRestartAgent.run`` used to render *every*
exception as one sentence::

    agent invocation failed: {type(exc).__name__}: {exc}

That collapses two unrelated stories:

  * something local broke before the prompt ever left (temp settings
    file, watchdog registration) — nothing ran, ``commands_run=[]``
    is the truth;
  * the prompt reached the provider layer and the *report* was lost —
    the agent may well have run restart commands first.

and it always reported ``agent_used=True`` and an empty
``commands_run``. The empty list then travelled into the preflight
evidence pack (``attempt_service_restart`` extends the orchestrator
result with it; ``verification_agent`` logs ``to_dict()``) reading as
"it ran nothing" — the same defect already fixed in
``binary_rebuild_agent``.

These tests drive the **production entry point**
``attempt_service_restart`` (what ``verification_agent._run_service_freshness_preflight``
calls), never ``_dispatch_failure`` directly, so they fail if the fix is
reverted off the live path.

§1/§2/§3 of this change are pure documentation / dead-code deletion and
deliberately have **no** test: nothing pins a docstring, and a test
invented to "cover" them would be the vacuous kind.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RaisingCodingTool:
    """``query_json`` raises — the prompt WAS dispatched, the report died."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[dict] = []
        self.settings = None

    def query_json(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        raise self._exc


class _NoopSettingsBuilder:
    """Stand-in for verification_subagent.SettingsJsonBuilder."""

    def __init__(self, vp_id=None, plan_id=None, **kwargs) -> None:
        pass

    def build(self) -> str:
        return ""

    def cleanup(self) -> None:
        pass


class _ExplodingSettingsBuilder(_NoopSettingsBuilder):
    """Fails *before* dispatch — the temp settings file is the culprit."""

    def build(self) -> str:
        raise OSError("settings temp dir unwritable")


@pytest.fixture
def restart_env(monkeypatch):
    """Force the LLM path and stub the post-check probe.

    Returns a callable ``configure(post_check_ok)`` so each test can pick
    whether the independent re-probe succeeds.
    """
    from service_freshness import PortProbe

    monkeypatch.setattr(
        "service_restart_agent._fast_path_restart_port", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "service_restart_agent.probe_port",
        lambda port: PortProbe(port=port, listening=True),
    )

    state = {"ready": True}
    monkeypatch.setattr(
        "service_restart_agent.wait_for_ready",
        lambda *a, **k: state["ready"],
    )
    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _NoopSettingsBuilder,
    )

    def configure(post_check_ok: bool = True) -> None:
        state["ready"] = post_check_ok

    return configure


# ---------------------------------------------------------------------------
# dispatched=True — the agent may have run; commands_run must say "unknown"
# ---------------------------------------------------------------------------

def test_dispatched_but_report_lost_does_not_claim_zero_commands(
    tmp_path, restart_env,
) -> None:
    """A provider-layer failure must NOT record ``commands_run == []``.

    The agent reached the provider and may have restarted things; an
    empty list is a lie that propagates into the evidence pack.
    """
    from service_restart_agent import (
        AGENT_REPORT_UNAVAILABLE_COMMANDS, attempt_service_restart,
    )
    from service_freshness import PortProbe

    restart_env(post_check_ok=True)
    tool = _RaisingCodingTool(RuntimeError("SceneProviderUnavailable"))

    result = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=tool, plan_id="plan-x",
    )

    # The prompt really did leave the module — one dispatch, one failure.
    assert len(tool.calls) == 1

    assert result.commands_run == [AGENT_REPORT_UNAVAILABLE_COMMANDS], (
        "a lost report must record 'unknown', not 'nothing ran'"
    )
    assert "dispatched but its report never came back" in result.diagnostics
    assert "SceneProviderUnavailable" in result.diagnostics
    assert "post_check_passed is the only trustworthy signal" in (
        result.diagnostics
    )


def test_lost_report_still_lets_the_post_check_decide_success(
    tmp_path, restart_env,
) -> None:
    """Contract guard: the independent re-probe still adjudicates.

    ``success`` must come from the post-check, not from the agent's
    (missing) report — the same property ``binary_rebuild_agent``
    documents for ``post_check_passed``.
    """
    from service_restart_agent import attempt_service_restart
    from service_freshness import PortProbe

    restart_env(post_check_ok=True)
    tool = _RaisingCodingTool(RuntimeError("provider pool exhausted"))

    result = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=tool, plan_id="plan-x",
    )
    assert result.post_check_passed is True
    assert result.success is True

    restart_env(post_check_ok=False)
    result2 = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=_RaisingCodingTool(RuntimeError("boom")),
        plan_id="plan-x",
    )
    assert result2.post_check_passed is False
    assert result2.success is False


def test_unknown_commands_never_leak_into_ports_restarted(
    tmp_path, restart_env,
) -> None:
    """The sentinel is a *command note*, not a restarted-port claim."""
    from service_restart_agent import (
        AGENT_REPORT_UNAVAILABLE_COMMANDS, attempt_service_restart,
    )
    from service_freshness import PortProbe

    restart_env(post_check_ok=True)
    result = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=_RaisingCodingTool(RuntimeError("lost")),
        plan_id="plan-x",
    )
    assert AGENT_REPORT_UNAVAILABLE_COMMANDS in result.commands_run
    assert AGENT_REPORT_UNAVAILABLE_COMMANDS not in result.ports_restarted
    # ``ports_restarted`` is never extended from a failed agent result.
    assert result.ports_restarted == []


# ---------------------------------------------------------------------------
# dispatched=False — nothing left the building; empty commands_run is honest
# ---------------------------------------------------------------------------

def test_never_dispatched_reports_empty_commands_and_no_agent_used(
    tmp_path, restart_env, monkeypatch,
) -> None:
    """A pre-dispatch failure means ``commands_run == []`` is the truth."""
    from service_restart_agent import attempt_service_restart
    from service_freshness import PortProbe

    restart_env(post_check_ok=True)
    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _ExplodingSettingsBuilder,
    )

    tool = _NoopCodingToolNeverCalled()
    result = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=tool, plan_id="plan-x",
    )

    # Nothing ran: the prompt never reached the provider at all.
    assert tool.calls == []
    assert result.commands_run == []
    assert "never dispatched" in result.diagnostics
    assert "OSError" in result.diagnostics


class _NoopCodingToolNeverCalled:
    """Fails the test if ``query_json`` is ever reached."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.settings = None

    def query_json(self, prompt, **kwargs):  # pragma: no cover
        self.calls.append({"prompt": prompt, **kwargs})
        raise AssertionError(
            "dispatched even though the pre-dispatch step failed"
        )


# ---------------------------------------------------------------------------
# The two branches must actually be distinguishable
# ---------------------------------------------------------------------------

def test_dispatch_failure_wording_is_not_the_old_one_sentence(
    tmp_path, restart_env, monkeypatch,
) -> None:
    """Both branches refuse to emit ``agent invocation failed: ...``."""
    from service_restart_agent import attempt_service_restart
    from service_freshness import PortProbe

    restart_env(post_check_ok=True)
    dispatched = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=_RaisingCodingTool(RuntimeError("x")), plan_id="p",
    )
    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _ExplodingSettingsBuilder,
    )
    never = attempt_service_restart(
        tmp_path, [8080], [PortProbe(port=8080)],
        coding_tool=_NoopCodingToolNeverCalled(), plan_id="p",
    )

    for res in (dispatched, never):
        assert "agent invocation failed" not in res.diagnostics

    # ...and they are not the same sentence.
    assert dispatched.diagnostics != never.diagnostics
    assert dispatched.commands_run != never.commands_run
