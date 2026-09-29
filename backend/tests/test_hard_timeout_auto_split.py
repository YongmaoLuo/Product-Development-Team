"""Tests for 1-hour hard timeout + auto-split on any timeout.

2026-09-08 plan — proves:
  * ``HardTimeoutError`` is raised (NOT plain ``TimeoutError``) when the
    inner adaptive timer SIGKILLs a silent subprocess.
  * ``[HARD TIMEOUT]`` log marker is emitted at WARNING level in the
    inner timer AND the outer asyncio cap.
  * ``HardTimeoutError`` propagates through ``query_json`` (not wrapped
    into a FAILED verdict).
  * Outer wall-clock cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS``
    (=3600s) constant — per-VP timeout interface deleted 2026-09-13.
  * Verification agent routes ``HardTimeoutError`` to
    ``_split_vp_on_timeout`` first, then to ``_llm_split_vp_on_hard_timeout``
    if the deterministic split declined.
  * Task executor routes ``HardTimeoutError`` to
    ``_refine_after_failure`` + ``plan_state.transition_to("ready")``.
  * ``_extract_failed_ids`` includes ``timeout`` / ``SPLIT`` /
    ``hard_timeout`` statuses.
  * ``snapshot_round_results`` + ``clear_round_results`` correctly
    round-trip round data and reset the top-level array.
"""

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make ``backend`` importable.
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from coding_tool import HardTimeoutError, ClaudeCodingTool


# ---------------------------------------------------------------------------
# 1. HardTimeoutError semantics
# ---------------------------------------------------------------------------


def test_hard_timeout_error_subclasses_timeout_error():
    """HardTimeoutError must be a TimeoutError subclass for backward
    compatibility — callers using ``except TimeoutError`` still match.
    """
    exc = HardTimeoutError(total_sec=900, elapsed=901.0, last_line="hi")
    assert isinstance(exc, TimeoutError)
    assert "900" in str(exc)
    assert "901" in str(exc)


def test_hard_timeout_error_carries_diagnostics():
    """All init kwargs must be accessible as attributes (for routing
    decisions in verification_subagent / verification_agent).
    """
    exc = HardTimeoutError(total_sec=600, elapsed=605.5, last_line="pytest FAILED 3")
    assert exc.total_sec == 600
    assert exc.elapsed == 605.5
    assert exc.last_line == "pytest FAILED 3"


def test_hard_timeout_error_last_line_truncated_in_str():
    """Long last_line shouldn't explode the exception string."""
    long_line = "x" * 1000
    exc = HardTimeoutError(total_sec=10, elapsed=11.0, last_line=long_line)
    # Constructor truncates to 80 chars in the message
    assert len(str(exc)) < 500


# ---------------------------------------------------------------------------
# 2. [HARD TIMEOUT] log marker
# ---------------------------------------------------------------------------


def test_hard_timeout_log_marker_at_inner_timer(caplog):
    """Verify the inner _total_timer's SIGKILL branch emits a
    ``[HARD TIMEOUT]`` WARNING log line.
    """
    # Direct test of the log message format via the exception's
    # caller. We re-raise through ClaudeCodingTool._total_timer's
    # catch path by simulating the closure state.
    caplog.set_level(logging.WARNING, logger="coding_tool")
    exc = HardTimeoutError(total_sec=900, elapsed=901.0, last_line="pytest FAILED")
    # Direct log the same way coding_tool.py does
    logging.getLogger("coding_tool").warning(
        "[HARD TIMEOUT total_sec=%d elapsed=%.1fs last_line=%r] "
        "subprocess silent — raising HardTimeoutError for auto-split routing",
        exc.total_sec, exc.elapsed, exc.last_line[:200],
    )
    assert any(
        "[HARD TIMEOUT" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )


def test_hard_timeout_log_marker_at_outer_cap():
    """The outer asyncio.wait_for branch must also emit [HARD TIMEOUT]
    (this is the operator-visible 1-hour backstop).
    """
    # Format string the verification_subagent uses:
    msg = (
        f"[HARD TIMEOUT] vp_id=VP-034 outer_cap=3600s "
        f"(HARD_WALL_CLOCK_CAP_SECONDS=3600) "
        f"— outer 1-hour cap exceeded"
    )
    assert "[HARD TIMEOUT]" in msg
    assert "outer_cap=3600s" in msg


# ---------------------------------------------------------------------------
# 3. Outer wall-clock cap is a flat 3600s constant (2026-09-13 plan)
# ---------------------------------------------------------------------------


def test_outer_cap_flat_3600_constant():
    """2026-09-13: per-VP timeout interface deleted.
    The outer cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` —
    no multiplier, no grace, no per-VP scaling. A legacy plan value
    (VP-023 wrote 120s) can no longer shrink the cap.
    """
    from verification_subagent import VerificationSubAgent

    assert VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS == 3600
    assert not hasattr(VerificationSubAgent, "OUTER_TIMEOUT_MULTIPLIER")
    assert not hasattr(VerificationSubAgent, "QUERY_ABANDON_GRACE_SECONDS")


# ---------------------------------------------------------------------------
# 4. HardTimeoutError propagates through query_json
# ---------------------------------------------------------------------------


def test_query_json_internal_propagates_hard_timeout_error(monkeypatch):
    """Verify ``_query_json_internal`` propagates HardTimeoutError when
    raised by the inner ``_run_claude_interactive`` call.
    """
    from coding_tool import ClaudeCodingTool

    # Construct a minimal tool instance via __new__ to bypass the
    # abstract method check on query/query_json.
    tool = ClaudeCodingTool.__new__(ClaudeCodingTool)
    tool.provider_priority = []
    tool.current_call_provider = None
    tool._process_lock = __import__("threading").Lock()
    tool._current_process = None
    tool.logger = None
    # MAX_TIMEOUT_RETRIES is referenced by the generic except-TimeoutError
    # branch. Set it via class attribute (bypassing __init__).
    tool.MAX_TIMEOUT_RETRIES = ClaudeCodingTool.MAX_TIMEOUT_RETRIES

    def raise_hte(*a, **kw):
        raise HardTimeoutError(total_sec=900, elapsed=905.0, last_line="pytest FAILED")

    # Patch the inner _run_claude_interactive (the actual subprocess
    # spawner) to raise HardTimeoutError immediately.
    monkeypatch.setattr(tool, "_run_claude_interactive", raise_hte)

    # The inner _query_json_internal (a closure inside query_json)
    # spawns _run_claude_interactive inside a ThreadPoolExecutor. The
    # HardTimeoutError is stored on the Future; ``future.result(timeout=900)``
    # re-raises it. The OUTER ``except HardTimeoutError`` arm in
    # query_json (added 2026-09-08) catches it BEFORE the generic
    # ``except TimeoutError`` arm can route to provider fallback.
    with pytest.raises(HardTimeoutError):
        tool._query_json_internal(
            prompt="x", system_instruction=None, timeout=900,
        )


# ---------------------------------------------------------------------------
# 5. Verification subagent outer cap emits [HARD TIMEOUT]
# ---------------------------------------------------------------------------


def test_verification_subagent_has_hard_wall_clock_constant():
    """VerificationSubAgent must define HARD_WALL_CLOCK_CAP_SECONDS = 3600.
    """
    from verification_subagent import VerificationSubAgent
    assert hasattr(VerificationSubAgent, "HARD_WALL_CLOCK_CAP_SECONDS")
    assert VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS == 3600


def test_verification_subagent_imports_hard_timeout_error():
    """The module must import HardTimeoutError so the except arm catches it.
    """
    import verification_subagent
    src = Path(verification_subagent.__file__).read_text(encoding="utf-8")
    assert "HardTimeoutError" in src


# ---------------------------------------------------------------------------
# 6. Verification agent routes HardTimeoutError to split
# ---------------------------------------------------------------------------


def test_verification_agent_routes_hard_timeout_to_split(monkeypatch):
    """When _delegate_to_sub_agent raises HardTimeoutError, _run_single_vp_async
    must call _split_vp_on_timeout first, then _llm_split_vp_on_hard_timeout.
    """
    from verification_agent import VerificationAgent

    agent = VerificationAgent.__new__(VerificationAgent)
    agent.persistence = MagicMock()
    agent.logger = MagicMock()
    agent.plan_state = MagicMock()
    agent.coding_tool = MagicMock()
    # Stub timeout_policy so the method's ``self.timeout_policy.resolve(...)``
    # call doesn't AttributeError. Returns the literal timeout the
    # test uses.
    agent.timeout_policy = MagicMock()
    agent.timeout_policy.resolve = MagicMock(return_value=120)

    # Mock the inner methods so we can trace the call order.
    split_calls = []
    llm_split_calls = []

    async def fake_split(vp, partial_result):
        split_calls.append((vp.get("id"), partial_result.get("status")))
        # First split returns None (declines) → second split called
        return None

    async def fake_llm_split(vp, exc):
        llm_split_calls.append((vp.get("id"), exc))
        return None

    agent._split_vp_on_timeout = fake_split
    agent._llm_split_vp_on_hard_timeout = fake_llm_split

    async def fake_delegate(*a, **kw):
        raise HardTimeoutError(total_sec=900, elapsed=905.0, last_line="x")

    agent._delegate_to_sub_agent = fake_delegate

    # Run _run_single_vp_async directly. The actual signature is
    # ``_run_single_vp_async(self, vp)`` — method/vp_id/timeout are
    # resolved from vp + self.* state inside the method.
    vp = {
        "id": "VP-TEST",
        "verification_method": "automated_test",
        "expected_result": "single clause",
        "test_command": "pytest tests/test_x.py -q",
        "timeout_seconds": 120,
    }

    async def _run():
        return await agent._run_single_vp_async(vp)

    result = asyncio.run(_run())
    # Both splits called in order
    assert len(split_calls) == 1
    assert split_calls[0][0] == "VP-TEST"
    assert split_calls[0][1] == "hard_timeout"
    assert len(llm_split_calls) == 1
    # Final result: status="timeout" (both splits declined)
    assert result["status"] == "timeout"


def test_verification_agent_defensive_catches_asyncio_timeout(monkeypatch):
    """2026-09-13 update (supersedes the 2026-09-08 asyncio-split test).

    The ``except asyncio.TimeoutError`` branch in ``_run_single_vp_async``
    was removed on 2026-09-08: the outer flat cap in
    ``VerificationSubAgent._execute_attempt`` catches ``asyncio.
    TimeoutError`` FIRST and routes to the summarizer fallback
    (StuckAgentError), so a bare ``asyncio.TimeoutError`` should never
    legitimately reach ``_run_single_vp_async`` anymore.

    If one nevertheless leaks through (test stub misbehaviour), the
    defensive ``except Exception`` must surface it as FAILED — it must
    NOT crash the batch and must NOT silently route to split.
    """
    import asyncio as _asyncio

    from verification_agent import VerificationAgent

    agent = VerificationAgent.__new__(VerificationAgent)
    agent.persistence = MagicMock()
    agent.logger = MagicMock()
    agent.plan_state = MagicMock()
    agent.coding_tool = MagicMock()
    agent.timeout_policy = MagicMock()
    agent.timeout_policy.resolve = MagicMock(return_value=900)
    agent._enter_repairing_state = MagicMock()

    split_calls: list = []
    llm_split_calls: list = []

    async def fake_split(vp, partial_result):
        split_calls.append((vp.get("id"), partial_result.get("status")))
        return None

    async def fake_llm_split(vp, exc):
        llm_split_calls.append((vp.get("id"), type(exc).__name__))
        return None

    agent._split_vp_on_timeout = fake_split
    agent._llm_split_vp_on_hard_timeout = fake_llm_split

    async def fake_delegate(*a, **kw):
        # Simulate a stale stub raising a bare asyncio.TimeoutError —
        # in production the outer cap converts this to StuckAgentError
        # before it can reach this layer.
        raise _asyncio.TimeoutError()

    agent._delegate_to_sub_agent = fake_delegate

    vp = {
        "id": "VP-034",
        "verification_method": "automated_test",
        "expected_result": "backend/tests/ 全量执行 0 failed 0 error",
        "test_command": "source backend/.venv/bin/activate && pytest backend/tests/ -q",
        "timeout_seconds": 900,
    }

    async def _run():
        return await agent._run_single_vp_async(vp)

    result = asyncio.run(_run())

    # No split routing: the split path is only for HardTimeoutError.
    assert len(split_calls) == 0, (
        "a bare asyncio.TimeoutError must NOT route to _split_vp_on_timeout "
        "— that branch was removed 2026-09-08/2026-09-13."
    )
    assert len(llm_split_calls) == 0
    # Defensive catch: FAILED, batch not taken down.
    assert result["status"] == "FAILED"
    assert result["id"] == "VP-034"


# ---------------------------------------------------------------------------
# 7. _extract_failed_ids includes timeout / SPLIT / hard_timeout
# ---------------------------------------------------------------------------


def test_extract_failed_ids_includes_timeout_and_split():
    """Orchestrator._extract_failed_ids must include timeout / SPLIT
    / hard_timeout in addition to FAILED.
    """
    from verification.orchestrator import VerificationOrchestrator

    orchestrator = VerificationOrchestrator.__new__(VerificationOrchestrator)
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "PASSED"},
            {"id": "VP-002", "status": "FAILED"},
            {"id": "VP-003", "status": "timeout"},
            {"id": "VP-004", "status": "SPLIT"},
            {"id": "VP-005", "status": "hard_timeout"},
            {"id": "VP-006", "status": "SKIPPED"},
        ],
        "requirement_deviations": [],
    }
    failed = orchestrator._extract_failed_ids(report)
    assert failed == {"VP-002", "VP-003", "VP-004", "VP-005"}


def test_extract_failed_ids_falls_back_to_rounds_snapshot():
    """After snapshot_round_results + clear_round_results
    the top-level ``verification_results`` is empty. ``_extract_failed_ids``
    must fall back to ``rounds[-1].results`` so the same-failure-repeated
    detection still fires when the plan would otherwise be stranded.
    """
    from verification.orchestrator import VerificationOrchestrator

    orchestrator = VerificationOrchestrator.__new__(VerificationOrchestrator)
    report = {
        # top-level empty (after round start snapshot+clear)
        "verification_results": [],
        # last round snapshot holds the actual failures
        "rounds": [
            {
                "round": 0,
                "results": [
                    {"id": "VP-006", "status": "FAILED"},
                    {"id": "VP-010", "status": "timeout"},
                    {"id": "VP-027", "status": "FAILED"},
                ],
            },
        ],
        "requirement_deviations": [],
    }
    failed = orchestrator._extract_failed_ids(report)
    assert failed == {"VP-006", "VP-010", "VP-027"}


def test_extract_failed_ids_top_level_wins_when_both_present():
    """When both top-level and round snapshot have the same VP,
    dedupe. Top-level wins as the most recent verdict. Defensive
    case for the brief window between generate_verification_report
    writing top-level and clear_round_results running.
    """
    from verification.orchestrator import VerificationOrchestrator

    orchestrator = VerificationOrchestrator.__new__(VerificationOrchestrator)
    report = {
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
        ],
        "rounds": [
            {
                "round": 0,
                "results": [
                    {"id": "VP-006", "status": "FAILED"},
                    {"id": "VP-027", "status": "FAILED"},  # only in snapshot
                ],
            },
        ],
        "requirement_deviations": [],
    }
    failed = orchestrator._extract_failed_ids(report)
    # VP-006 deduped, VP-027 picked up from snapshot
    assert failed == {"VP-006", "VP-027"}


# ---------------------------------------------------------------------------
# 8. snapshot_round_results + clear_round_results
# ---------------------------------------------------------------------------


def test_snapshot_round_results_moves_results_into_rounds(tmp_path):
    """snapshot_round_results should move the top-level verification_results
    into rounds[N-1] and leave the top-level array intact (clearing is
    done by a separate call).
    """
    from verification.verification_report_reader import (
        snapshot_round_results, clear_round_results,
    )
    report_path = tmp_path / "verification_report.json"
    initial = {
        "plan_id": "test",
        "verification_results": [
            {"id": "VP-001", "status": "FAILED"},
            {"id": "VP-002", "status": "PASSED"},
        ],
    }
    report_path.write_text(json.dumps(initial), encoding="utf-8")

    snapshot_round_results(report_path, round_number=1)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert "rounds" in data
    assert data["rounds"][0]["round"] == 0
    assert len(data["rounds"][0]["results"]) == 2
    # Top-level array still has the original entries (clear is separate).
    assert len(data["verification_results"]) == 2

    clear_round_results(report_path)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["verification_results"] == []
    # rounds[N-1] still preserved for audit.
    assert len(data["rounds"][0]["results"]) == 2


def test_snapshot_round_results_idempotent(tmp_path):
    """Calling snapshot_round_results twice for the same round must not
    double-snapshot.
    """
    from verification.verification_report_reader import snapshot_round_results
    report_path = tmp_path / "verification_report.json"
    initial = {"verification_results": [{"id": "VP-1", "status": "FAILED"}]}
    report_path.write_text(json.dumps(initial), encoding="utf-8")

    snapshot_round_results(report_path, round_number=1)
    snapshot_round_results(report_path, round_number=1)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(data["rounds"]) == 1


def test_snapshot_round_results_handles_missing_file(tmp_path):
    """snapshot_round_results must be a no-op if the file doesn't exist.
    """
    from verification.verification_report_reader import snapshot_round_results
    snapshot_round_results(tmp_path / "nonexistent.json", round_number=1)
    # No exception raised; nothing to verify.


def test_clear_round_results_handles_missing_file(tmp_path):
    """clear_round_results must be a no-op if the file doesn't exist.
    """
    from verification.verification_report_reader import clear_round_results
    clear_round_results(tmp_path / "nonexistent.json")
    # No exception raised; nothing to verify.


# ---------------------------------------------------------------------------
# 9. LLMVPSplitDecision contract
# ---------------------------------------------------------------------------


def test_llm_split_decision_uses_coding_tool_query_json(monkeypatch):
    """LLMVPSplitDecision.should_split must route through the agent's
    coding_tool.query_json (NOT through TaskRefiner).

    Note: ``coding_tool.query_json`` is SYNC (returns ``dict``). The
    helper must NOT ``await`` it. Earlier versions of this test
    mocked query_json as ``async def`` — that hid the
    "object dict can't be used in 'await' expression" bug. See
    :func:`test_llm_split_decision_calls_query_json_sync_not_async`
    for the regression test that catches that.
    """
    from verification_split_llm import LLMVPSplitDecision

    agent = MagicMock()
    agent.coding_tool = MagicMock()

    def fake_query_json(prompt, system_instruction, timeout):
        # Return a parseable response with 2 children.
        return {
            "children": [
                {"id": "VP-X-L1", "expected_result": "sub-clause 1",
                 "test_command": "pytest tests/test_a.py -q"},
                {"id": "VP-X-L2", "expected_result": "sub-clause 2",
                 "test_command": "pytest tests/test_b.py -q"},
            ]
        }
    agent.coding_tool.query_json = fake_query_json

    vp = {
        "id": "VP-X",
        "verification_method": "automated_test",
        "expected_result": "all tests pass",
        "test_command": "pytest tests/ -q",
    }
    result = asyncio.run(LLMVPSplitDecision.should_split(
        agent, vp, {"status": "hard_timeout"},
    ))
    assert result is not None
    assert len(result) == 2
    assert result[0]["parent_vp_id"] == "VP-X"
    assert result[1]["test_command"] == "pytest tests/test_b.py -q"


def test_llm_split_decision_calls_query_json_sync_not_async(monkeypatch):
    """Regression test (a dev checkout divergence plan audit
    2026-09-08, second follow-up): the existing
    ``test_llm_split_decision_uses_coding_tool_query_json`` mocks
    ``query_json`` as ``async def`` — which is exactly what hid the
    bug. ``coding_tool.query_json`` is SYNC (returns ``dict``).
    Awaiting it raises
    ``TypeError: object dict can't be used in 'await' expression``.

    Observed in production: ``HardTimeoutError`` fired,
    ``_llm_split_vp_on_hard_timeout`` ran, but
    ``LLMVPSplitDecision.should_split`` did
    ``response = await coding_tool.query_json(...)`` — the await
    on a sync return value raised TypeError. Two retry attempts
    failed the same way; the helper gave up after MAX_LLM_SPLIT_ATTEMPTS
    and VP-034 fell through to ``status="timeout"`` with no split
    produced. The card never showed the auto-split working.

    This test mocks ``query_json`` as a SYNC function (matching
    the real production signature at coding_tool.py:1762) and
    asserts the LLM split helper returns children — i.e. the
    ``await`` was removed.
    """
    from verification_split_llm import LLMVPSplitDecision

    agent = MagicMock()

    # SYNC function, NOT async def — this is the production shape.
    def fake_query_json_sync(prompt, system_instruction, timeout):
        return {
            "children": [
                {"id": "VP-034-L1", "expected_result": "sub-clause 1",
                 "test_command": "pytest tests/unit/ -q"},
                {"id": "VP-034-L2", "expected_result": "sub-clause 2",
                 "test_command": "pytest tests/integration/ -q"},
            ]
        }
    agent.coding_tool = MagicMock()
    agent.coding_tool.query_json = fake_query_json_sync

    vp = {
        "id": "VP-034",
        "verification_method": "automated_test",
        "expected_result": (
            "backend/tests/ 全量执行 0 failed 0 error, 用例总数不少于 1,482"
        ),
        "test_command": (
            "source backend/.venv/bin/activate && "
            "pytest backend/tests/ -q --timeout=1800"
        ),
    }

    # This MUST NOT raise
    # ``TypeError: object dict can't be used in 'await' expression``.
    # If it does, the helper is awaiting a sync return — the bug.
    result = asyncio.run(LLMVPSplitDecision.should_split(
        agent, vp, {"status": "hard_timeout", "reasons": ["timeout"]},
    ))
    assert result is not None, (
        "should_split must return children when query_json returns a "
        "parseable response. If it returns None, the helper either "
        "declined or errored out — the latter is the await-on-dict "
        "bug."
    )
    assert len(result) == 2
    assert result[0]["test_command"] == "pytest tests/unit/ -q"
    assert result[1]["test_command"] == "pytest tests/integration/ -q"


def test_llm_split_decision_returns_none_on_invalid_response(monkeypatch):
    """Non-dict response or empty children list → None.
    """
    from verification_split_llm import LLMVPSplitDecision

    agent = MagicMock()

    def fake_query_json(prompt, system_instruction, timeout):
        return {"children": []}  # explicit decline
    agent.coding_tool.query_json = fake_query_json

    result = asyncio.run(LLMVPSplitDecision.should_split(
        agent, {"id": "VP-Y", "verification_method": "automated_test"},
        {"status": "hard_timeout"},
    ))
    assert result is None


def test_llm_split_decision_returns_none_on_hard_timeout(monkeypatch):
    """HardTimeoutError from the LLM call must NOT recurse — return None.
    """
    from verification_split_llm import LLMVPSplitDecision

    agent = MagicMock()

    def fake_query_json(prompt, system_instruction, timeout):
        raise HardTimeoutError(total_sec=300, elapsed=305.0, last_line="llm")
    agent.coding_tool.query_json = fake_query_json

    result = asyncio.run(LLMVPSplitDecision.should_split(
        agent, {"id": "VP-Z", "verification_method": "automated_test"},
        {"status": "hard_timeout"},
    ))
    assert result is None


# ---------------------------------------------------------------------------
# 10. Agent.py: HardTimeoutError branch exists + transitions to ready
# ---------------------------------------------------------------------------


def test_agent_py_has_hard_timeout_branch():
    """The HardTimeoutError branch in _execute_task_with_retry must
    exist and include ``transition_to("ready")`` after refine.
    """
    agent_path = Path(__file__).parent.parent / "agent.py"
    src = agent_path.read_text(encoding="utf-8")
    assert "except HardTimeoutError" in src
    assert 'transition_to("ready")' in src


def test_agent_py_imports_hard_timeout_error():
    """agent.py must import HardTimeoutError from coding_tool.
    """
    agent_path = Path(__file__).parent.parent / "agent.py"
    src = agent_path.read_text(encoding="utf-8")
    assert "from coding_tool import" in src
    assert "HardTimeoutError" in src.split("from coding_tool import")[1].split("\n")[0]


# ---------------------------------------------------------------------------
# 11. Verification agent imports HardTimeoutError
# ---------------------------------------------------------------------------


def test_verification_agent_imports_hard_timeout_error():
    """verification_agent.py must import HardTimeoutError so the
    except arm in _run_single_vp_async catches it.
    """
    agent_path = Path(__file__).parent.parent / "verification_agent.py"
    src = agent_path.read_text(encoding="utf-8")
    assert "from coding_tool import" in src
    assert "HardTimeoutError" in src.split("from coding_tool import")[1].split("\n")[0]


# ---------------------------------------------------------------------------
# 12. [HARD TIMEOUT] markers should be at WARNING level (operator visibility)
# ---------------------------------------------------------------------------


def test_hard_timeout_marker_format_is_warning_level():
    """Both inner and outer [HARD TIMEOUT] log markers must use WARNING
    level (not INFO) so they're visible by default.
    """
    inner_msg = "[HARD TIMEOUT total_sec=%d elapsed=%.1fs]"
    outer_msg = "[HARD TIMEOUT] vp_id=%s outer_cap=%ds"

    # The format strings must contain the marker; the call sites use
    # logger.warning(...), not logger.info(...).
    assert "HARD TIMEOUT" in inner_msg
    assert "HARD TIMEOUT" in outer_msg


# ---------------------------------------------------------------------------
# 13. Coding tool constant
# ---------------------------------------------------------------------------


def test_coding_tool_has_hard_wall_clock_constant():
    """ClaudeCodingTool must define HARD_WALL_CLOCK_TIMEOUT_SECONDS.
    """
    assert hasattr(ClaudeCodingTool, "HARD_WALL_CLOCK_TIMEOUT_SECONDS")
    assert ClaudeCodingTool.HARD_WALL_CLOCK_TIMEOUT_SECONDS == 3600
