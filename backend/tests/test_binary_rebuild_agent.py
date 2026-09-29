"""Unit tests for the generic binary-rebuild sub-agent (2026-09-07 plan).

Covers the two-layer trust model:
  * dumb fast-path (``rebuild_binary``) tried first, agent skipped on success
  * tool-restricted agent spawned only on fast-path failure
  * the orchestrator — never the agent's self-report — decides success via
    an independent ``check_binary_freshness`` re-check
"""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Ensure backend/ is importable when the suite is invoked from the repo root.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from binary_freshness import FreshnessReport
from binary_rebuild_agent import (
    BinaryRebuildAgent,
    RebuildResult,
    attempt_intelligent_rebuild,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stale_report() -> FreshnessReport:
    return FreshnessReport(
        kind="rust_python",
        status="FAILED",
        detail="libnative_ext.so mtime older than newest .rs mtime",
        evidence="binary=/x/target/release/libnative_ext.so",
        rebuild_command="maturin develop --release",
        binary_path="/x/target/release/libnative_ext.so",
        newest_source_mtime=2000.0,
        binary_mtime=1000.0,
    )


def _fresh_report() -> FreshnessReport:
    return FreshnessReport(
        kind="rust_python",
        status="PASSED",
        detail="libnative_ext.so is fresh",
        binary_path="/x/target/release/libnative_ext.so",
        newest_source_mtime=2000.0,
        binary_mtime=3000.0,
    )


class _RecordingCodingTool:
    """Fake Claude coding tool that records query_json kwargs."""

    def __init__(self, payload=None, exc=None):
        self.payload = payload if payload is not None else {
            "success": True,
            "binary_path": "/x/target/release/libnative_ext.so",
            "commands_run": ["maturin develop --release"],
            "diagnostics": "rebuilt with project venv maturin",
        }
        self.exc = exc
        self.calls = []

    def query_json(self, prompt, system_instruction=None, timeout=None,
                   allowed_tools=None, **kwargs):
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            "timeout": timeout,
            "allowed_tools": allowed_tools,
        })
        if self.exc is not None:
            raise self.exc
        return self.payload


# ---------------------------------------------------------------------------
# 1) Fast path succeeds → agent never spawned
# ---------------------------------------------------------------------------


def test_dumb_fast_path_succeeds_skips_agent():
    tool = _RecordingCodingTool()
    with patch("binary_rebuild_agent.rebuild_binary", return_value=True), \
         patch("binary_rebuild_agent.check_binary_freshness", return_value=_fresh_report()):
        result = attempt_intelligent_rebuild(
            Path("/x"), _stale_report(),
            coding_tool=tool, plan_id="p", vp_id="VP-1",
        )
    assert result.success is True
    assert result.agent_used is False
    assert tool.calls == [], "agent must not be spawned when fast-path succeeds"


# ---------------------------------------------------------------------------
# 2) Fast path fails → agent spawned with a discovery prompt
# ---------------------------------------------------------------------------


def test_agent_spawned_on_dumb_failure():
    tool = _RecordingCodingTool()
    with patch("binary_rebuild_agent.rebuild_binary", return_value=False), \
         patch("binary_rebuild_agent.check_binary_freshness", return_value=_fresh_report()):
        result = attempt_intelligent_rebuild(
            Path("/x"), _stale_report(),
            coding_tool=tool, plan_id="p", vp_id="VP-1",
        )
    assert len(tool.calls) == 1, "agent must be spawned exactly once"
    assert result.agent_used is True
    user_prompt = tool.calls[0]["prompt"]
    assert "Discovery checklist" in user_prompt
    assert "/x" in user_prompt


# ---------------------------------------------------------------------------
# 3) Tool restriction is enforced (approach A)
# ---------------------------------------------------------------------------


def test_agent_tool_restriction_enforced():
    tool = _RecordingCodingTool()
    with patch("binary_rebuild_agent.rebuild_binary", return_value=False), \
         patch("binary_rebuild_agent.check_binary_freshness", return_value=_fresh_report()):
        attempt_intelligent_rebuild(
            Path("/x"), _stale_report(),
            coding_tool=tool, plan_id="p", vp_id="VP-1",
        )
    assert tool.calls[0]["allowed_tools"] == ["Bash", "Read", "Grep", "Glob"], (
        "the agent must be hard-limited to read-only-ish tools (no Edit/Write)"
    )
    # Edit/Write must not be permitted.
    assert "Edit" not in tool.calls[0]["allowed_tools"]
    assert "Write" not in tool.calls[0]["allowed_tools"]


# ---------------------------------------------------------------------------
# 4) Agent hallucination caught by orchestrator's independent post-check
# ---------------------------------------------------------------------------


def test_agent_hallucination_caught_by_post_check():
    # Agent claims success but the artefact is STILL stale on disk.
    tool = _RecordingCodingTool(payload={
        "success": True,
        "binary_path": "/x/target/release/libnative_ext.so",
        "commands_run": ["maturin develop --release"],
        "diagnostics": "looks good to me",
    })
    with patch("binary_rebuild_agent.rebuild_binary", return_value=False), \
         patch("binary_rebuild_agent.check_binary_freshness", return_value=_stale_report()):
        result = attempt_intelligent_rebuild(
            Path("/x"), _stale_report(),
            coding_tool=tool, plan_id="p", vp_id="VP-1",
        )
    assert result.success is False, (
        "orchestrator's independent re-check must override the agent's "
        "self-reported success"
    )
    assert "SELF-REPORT MISMATCH" in result.diagnostics
    assert result.post_check_passed is False


# ---------------------------------------------------------------------------
# 5) Agent timeout → clean failure (never raises)
# ---------------------------------------------------------------------------


def test_agent_timeout_returns_failure():
    tool = _RecordingCodingTool(exc=TimeoutError("Query timed out after 300 seconds"))
    with patch("binary_rebuild_agent.rebuild_binary", return_value=False), \
         patch("binary_rebuild_agent.check_binary_freshness", return_value=_stale_report()):
        result = attempt_intelligent_rebuild(
            Path("/x"), _stale_report(),
            coding_tool=tool, plan_id="p", vp_id="VP-1",
        )
    assert result.success is False
    assert "TimeoutError" in result.diagnostics


# ---------------------------------------------------------------------------
# 6) Agent diagnostics enrich the BLOCKED verdict evidence
# ---------------------------------------------------------------------------


def test_agent_result_enriches_blocked_verdict():
    from verification_agent import VerificationAgent

    agent = VerificationAgent.__new__(VerificationAgent)
    rebuild_result = RebuildResult(
        success=False,
        agent_used=True,
        diagnostics="maturin not found on backend PATH; tried 3 approaches",
        commands_run=["which maturin", "cargo build --release"],
        post_check_passed=False,
    )
    verdict = agent._build_binary_freshness_blocked_verdict(
        "VP-1", _stale_report(), rebuild_result,
    )
    assert verdict["status"] == "BLOCKED"
    attempt = verdict["evidence"]["rebuild_attempt"]
    assert attempt["agent_used"] is True
    assert "maturin not found" in attempt["diagnostics"]
    assert "cargo build --release" in attempt["commands_run"]


def test_blocked_verdict_without_rebuild_result_unchanged():
    """Backward-compat: omitting rebuild_result keeps the old evidence shape."""
    from verification_agent import VerificationAgent

    agent = VerificationAgent.__new__(VerificationAgent)
    verdict = agent._build_binary_freshness_blocked_verdict("VP-1", _stale_report())
    assert "rebuild_attempt" not in verdict["evidence"]
    assert verdict["status"] == "BLOCKED"


# ---------------------------------------------------------------------------
# 7) System prompt forbids source modification (approach C)
# ---------------------------------------------------------------------------


def test_rebuild_agent_system_prompt_forbids_source_modification():
    agent = BinaryRebuildAgent(Mock(), plan_id="p", vp_id="VP-1")
    prompt = agent.build_system_prompt()
    assert "DO NOT modify" in prompt
    assert "source code" in prompt
    assert "sed -i" in prompt
    # Output contract is pinned for query_json parsing.
    assert '"success"' in prompt and '"commands_run"' in prompt


# ---------------------------------------------------------------------------
# 8) Both paths fail → success=False with full diagnostics (orchestrator)
# ---------------------------------------------------------------------------


def test_orchestrator_falls_back_to_blocked_if_both_paths_fail():
    tool = _RecordingCodingTool(payload={
        "success": False,
        "binary_path": None,
        "commands_run": ["cargo build --release"],
        "diagnostics": "compilation error in signal.rs:312",
    })
    with patch("binary_rebuild_agent.rebuild_binary", return_value=False), \
         patch("binary_rebuild_agent.check_binary_freshness", return_value=_stale_report()):
        result = attempt_intelligent_rebuild(
            Path("/x"), _stale_report(),
            coding_tool=tool, plan_id="p", vp_id="VP-1",
        )
    assert result.success is False
    assert "compilation error" in result.diagnostics
    assert result.post_check_passed is False


# ---------------------------------------------------------------------------
# Extra: agent claiming success with no binary_path is downgraded
# ---------------------------------------------------------------------------


def test_agent_success_without_binary_path_downgraded():
    agent = BinaryRebuildAgent(Mock(), plan_id="p", vp_id="VP-1")
    result = agent._parse_agent_output({"success": True, "commands_run": ["x"]}, start=0.0)
    assert result.success is False
    assert "no binary_path" in result.diagnostics
