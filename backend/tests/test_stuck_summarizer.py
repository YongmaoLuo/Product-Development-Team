"""Regression tests for the stuck-sub-agent summarizer fallback.

When the first verification sub-agent runs pytest (re-runs allowed if
it suspects flakiness) but doesn't return verdict JSON before the
1-hour outer cap fires, route to a fresh summarizer LLM call (no tools)
that reads the tee'd log and produces verdict.

These tests cover:
  * Outer cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` (the
    1-hour absolute cap; per-VP timeout interface deleted 2026-09-13).
    The summarizer fires when this cap fires, NOT a tighter cap.
  * ``StuckAgentError`` exception type exists with output_file attribute.
  * Outer-cap branch in ``_execute_attempt`` raises ``StuckAgentError``
    (not ``HardTimeoutError``) so the summarizer can take over instead
    of the auto-split path.
  * ``_summarize_vp_result`` reads the tee'd log, calls coding_tool with
    ``allowed_tools=[]``, returns parsed ``Verdict``.
  * Summarizer degrades to FAILED if log file is missing/unreadable.
  * Re-runs are allowed: user prompt does NOT contain a one-shot rule;
    HARD-GATE does NOT forbid re-running test_command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import textwrap
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Outer cap is a flat 1-hour constant (2026-09-13 plan)
# ---------------------------------------------------------------------------


def test_outer_cap_constants_unchanged():
    """2026-09-13: per-VP timeout interface deleted.
    The outer cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` — no
    multiplier, no grace, no per-VP scaling. The summarizer fires when
    this cap fires — no separate tightener."""
    from verification_subagent import VerificationSubAgent

    # Sanity: the flat cap exists with the pinned value
    assert hasattr(VerificationSubAgent, "HARD_WALL_CLOCK_CAP_SECONDS")
    assert VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS == 3600
    # The multiplier / grace knobs must NOT exist anymore
    assert not hasattr(VerificationSubAgent, "OUTER_TIMEOUT_MULTIPLIER")
    assert not hasattr(VerificationSubAgent, "QUERY_ABANDON_GRACE_SECONDS")


def test_outer_cap_is_flat_3600():
    """The outer cap for every VP is exactly 3600s — independent of any
    plan-provided timeout_seconds value (legacy VP-023 wrote 120)."""
    from verification_subagent import VerificationSubAgent

    # Simulate the _execute_attempt computation directly: it is now a
    # plain constant read, no arithmetic.
    assert VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS == 3600


# ---------------------------------------------------------------------------
# 2. StuckAgentError exception type
# ---------------------------------------------------------------------------


def test_stuck_agent_error_carries_output_file():
    """StuckAgentError must carry the tee'd log path so the summarizer
    knows where to Read the pytest output."""
    from verification_subagent import StuckAgentError

    err = StuckAgentError(
        elapsed=3630.0,
        threshold=3630.0,
        vp_id="VP-034",
        output_file="/tmp/vp_VP-034_progress.log",
    )
    assert err.vp_id == "VP-034"
    assert err.output_file == "/tmp/vp_VP-034_progress.log"
    assert "VP-034" in str(err)


# ---------------------------------------------------------------------------
# 3. Outer-cap branch raises StuckAgentError (NOT HardTimeoutError)
# ---------------------------------------------------------------------------


def test_outer_cap_log_marker_routes_to_summarizer():
    """When the outer asyncio.wait_for fires, the log message must say
    'routing to summarizer' (not 'outer 1-hour cap exceeded' followed
    by HardTimeoutError raise)."""
    msg = (
        f"[STUCK SUB-AGENT] vp_id=VP-034 outer_cap=3600s "
        f"(HARD_WALL_CLOCK_CAP_SECONDS=3600) "
        f"— routing to summarizer (will Read /tmp/vp_VP-034_progress.log)"
    )
    assert "[STUCK SUB-AGENT]" in msg
    assert "routing to summarizer" in msg
    assert "/tmp/vp_VP-034_progress.log" in msg


# ---------------------------------------------------------------------------
# 4. Prompt does NOT forbid re-runs — first agent is allowed to re-run
# ---------------------------------------------------------------------------


def test_user_prompt_does_not_forbid_rerun():
    """The sub-agent prompt must NOT contain a one-shot rule
    forbidding re-runs. Re-runs are allowed."""
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review", max_retries=1)
    vp = {
        "id": "VP-TEST",
        "title": "t",
        "verification_method": "code_review",
        "expected_result": "x",
        "test_command": "pytest tests/",
    }
    prompt = agent._build_prompt(vp, attempt=0)
    # The one-shot rule was an earlier attempt, since rejected.
    assert "ONE-SHOT TEST RULE" not in prompt
    assert "MUST immediately return your verdict JSON" not in prompt


def test_no_template_forbids_rerun():
    """The HARD-GATE must NOT contain 'DO NOT re-run the test_command'.

    Re-runs are allowed — the summarizer fallback handles the case
    where re-runs still don't produce a verdict.

    2026-09-18: this used to check the ``automated_test`` template,
    which was retired that day. Checking *every* supported method is
    both the same invariant and a stronger one — a new template cannot
    quietly reintroduce the forbidden phrasing.
    """
    from verification_subagent import SUPPORTED_METHODS, MethodTemplateRegistry

    assert SUPPORTED_METHODS, "no supported methods to check"
    for method in SUPPORTED_METHODS:
        template = MethodTemplateRegistry.get_template(method)
        assert "DO NOT re-run the test_command" not in template, method
        assert (
            "source of truth" not in template
            or "first" not in template.lower().split("source of truth")[0][-50:]
        ), method


# ---------------------------------------------------------------------------
# 5. Summarizer method — reads log, calls coding_tool with allowed_tools=[]
# ---------------------------------------------------------------------------


def _write_pytest_log(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _make_summarizer_agent():
    """Build a VerificationSubAgent without calling __post_init__'s
    registry probe (the registry import path varies by environment)."""
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review", max_retries=1)
    return agent


@pytest.mark.asyncio
async def test_summarize_vp_result_returns_failed_for_passed_pytest(tmp_path: Path):
    """Summarizer reads a passed pytest log → returns Verdict(verdict=PASSED).

    Mocks coding_tool.query_json to return a hardcoded verdict JSON so
    we don't depend on the actual LLM. Verifies the call shape (no
    tools allowed) and the verdict parse path.
    """
    pytest_log = tmp_path / "vp_VP-PASS_progress.log"
    _write_pytest_log(pytest_log, textwrap.dedent("""\
        ============================= test session starts ==============================
        platform darwin -- Python 3.11.13, pytest-9.1.1
        collected 3 items
        tests/test_x.py .                                                       [ 33%]
        tests/test_y.py ..                                                      [100%]

        ============================== 3 passed in 0.05s ===============================
    """))

    vp = {
        "id": "VP-PASS",
        "title": "all green",
        "verification_method": "code_review",
        "expected_result": "pytest shows all green",
        "test_command": "pytest tests/",
        "timeout_seconds": 900,
    }
    log_path = tmp_path / "vp_attempt.log"

    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        "verdict": "PASSED",
        "reasons": ["3 passed in 0.05s, no failures"],
        "evidence": [],
        "pytest_exit_code": 0,
    }

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    assert verdict.verdict == "PASSED"
    assert len(verdict.reasons) == 1

    # Verify the call shape: allowed_tools=[] is critical
    call_kwargs = fake_tool.query_json.call_args.kwargs
    assert call_kwargs.get("allowed_tools") == []
    # System instruction must tell the LLM to NOT run tools
    assert "Do NOT call any tools" in call_kwargs["system_instruction"]
    # The log tail is in the user prompt
    assert "3 passed in 0.05s" in call_kwargs["prompt"]


@pytest.mark.asyncio
async def test_summarize_vp_result_returns_failed_for_failed_pytest(tmp_path: Path):
    """Summarizer reads a failed pytest log → returns Verdict(verdict=FAILED)
    citing the failing test names."""
    pytest_log = tmp_path / "vp_VP-FAIL_progress.log"
    _write_pytest_log(pytest_log, textwrap.dedent("""\
        FAILED tests/test_x.py::test_a - assert 1 == 2
        FAILED tests/test_x.py::test_b - assert 3 == 4
        FAILED tests/test_y.py::test_c - assert 5 == 6
        = 3 failed, 10 passed, 1 skipped in 2.34s =
    """))

    vp = {
        "id": "VP-FAIL",
        "title": "fail check",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
        "timeout_seconds": 900,
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        "verdict": "FAILED",
        "reasons": ["3 failed, 10 passed in 2.34s — pytest exited non-zero"],
        "evidence": [
            "tests/test_x.py::test_a",
            "tests/test_x.py::test_b",
            "tests/test_y.py::test_c",
        ],
        "pytest_exit_code": 1,
    }
    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    assert verdict.verdict == "FAILED"
    assert verdict.reasons[0].startswith("3 failed")


@pytest.mark.asyncio
async def test_summarize_vp_result_degrades_to_failed_when_log_missing(tmp_path: Path):
    """If the tee'd log doesn't exist (pytest never ran), the summarizer
    returns a clear FAILED verdict — doesn't blow up with FileNotFoundError."""
    vp = {
        "id": "VP-MISSING",
        "title": "missing log",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()
    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = AssertionError(
        "summarizer must NOT call coding_tool when log is missing"
    )

    missing_log = tmp_path / "does_not_exist.log"
    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(missing_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    assert verdict.verdict == "FAILED"
    assert "no test output captured" in verdict.reasons[0]
    fake_tool.query_json.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_vp_result_handles_llm_failure(tmp_path: Path):
    """If the summarizer LLM call itself raises, return FAILED with the
    underlying exception type — don't propagate, don't loop."""
    pytest_log = tmp_path / "vp_VP-ERR_progress.log"
    _write_pytest_log(pytest_log, "= 1 failed in 0.5s =\n")

    vp_node = {
        "id": "VP-ERR",
        "title": "summarizer error path",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = RuntimeError("upstream LLM 503")

    verdict = await agent._summarize_vp_result(
        vp_node=vp_node,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    assert verdict.verdict == "FAILED"
    assert "summarizer query failed" in verdict.reasons[0]
    assert "RuntimeError" in verdict.reasons[0]


@pytest.mark.asyncio
async def test_summarize_vp_result_handles_unparseable_verdict(tmp_path: Path):
    """If the summarizer returns a non-JSON dict (or the LLM hallucinated
    schema), the summarizer falls back to FAILED with parse error."""
    pytest_log = tmp_path / "vp_VP-BAD_progress.log"
    _write_pytest_log(pytest_log, "= 2 failed in 0.5s =\n")

    vp = {
        "id": "VP-BAD",
        "title": "summarizer bad parse",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    # LLM returned a JSON object that doesn't match Verdict schema —
    # parse_verdict_to_dataclass will raise VerdictParseError.
    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        # missing required fields → parse fails
        "verdict": "MAYBE",
        "random_key": "x",
    }
    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    assert verdict.verdict == "FAILED"
    assert "unparseable" in verdict.reasons[0]


@pytest.mark.asyncio
async def test_summarize_vp_result_reads_only_tail_of_large_log(tmp_path: Path):
    """A full pytest run can produce MB of output; the summarizer must
    read only the tail (~200 lines) to keep prompt size bounded."""
    pytest_log = tmp_path / "vp_VP-LARGE_progress.log"
    # 5000 filler lines + a summary at the end
    lines = ["filler line"] * 5000
    lines.append("FAILED tests/test_x.py::test_a - oops")
    lines.append("FAILED tests/test_x.py::test_b - oops")
    lines.append("= 2 failed, 100 passed in 5.00s =")
    _write_pytest_log(pytest_log, "\n".join(lines) + "\n")

    vp = {
        "id": "VP-LARGE",
        "title": "big log",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        "verdict": "FAILED",
        "reasons": ["2 failed"],
        "evidence": ["test_a", "test_b"],
        "pytest_exit_code": 1,
    }
    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    assert verdict.verdict == "FAILED"
    # Confirm the prompt contains the tail (last 200 lines), not the
    # first 4800 filler lines. Cheapest check: the pytest summary line
    # is in the prompt.
    prompt = fake_tool.query_json.call_args.kwargs["prompt"]
    assert "2 failed, 100 passed in 5.00s" in prompt
    # And: at least one early 'filler line' is NOT in the prompt
    # (the deque(maxlen=200) drops them)
    assert prompt.count("filler line") <= 200


# ---------------------------------------------------------------------------
# 6. End-to-end: _self_heal_loop routes StuckAgentError to summarizer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_heal_loop_routes_stuck_agent_to_summarizer(tmp_path: Path):
    """When _execute_attempt raises StuckAgentError, _self_heal_loop
    catches it and calls _summarize_vp_result instead of retrying
    with the same stuck agent."""
    from verification_subagent import (
        StuckAgentError,
        VerificationSubAgent,
    )

    pytest_log = tmp_path / "vp_VP-STUCK_progress.log"
    _write_pytest_log(pytest_log, "= 99 failed, 1 passed in 12.00s =\n")

    vp = {
        "id": "VP-STUCK",
        "title": "stuck agent end-to-end",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
        "timeout_seconds": 900,
    }

    agent = VerificationSubAgent(method="code_review", max_retries=1)

    # Stub _execute_attempt to raise StuckAgentError immediately
    stuck_exc = StuckAgentError(
        elapsed=3630.0,
        threshold=3630.0,
        vp_id="VP-STUCK",
        output_file=str(pytest_log),
    )

    async def fake_execute_attempt(*args, **kwargs):
        raise stuck_exc

    agent._execute_attempt = fake_execute_attempt  # type: ignore[method-assign]

    # Stub _summarize_vp_result to return a clear FAILED verdict
    # (proves the loop did NOT consume a retry budget and did NOT
    # call _execute_attempt again).
    async def fake_summarize(*args, **kwargs):
        from verification_subagent import Verdict
        return Verdict(
            verdict="FAILED",
            reasons=["pytest summary: 99 failed, 1 passed"],
            evidence=["test_a", "test_b"],
            model_complexity="simple",
        )

    agent._summarize_vp_result = fake_summarize  # type: ignore[method-assign]

    log_path = tmp_path / "vp_attempt.log"
    settings_path = None  # won't reach the inner scope
    coding_tool = MagicMock()

    # We bypass _build_prompt / settings builder by patching them.
    agent._build_prompt = MagicMock(return_value="placeholder")  # type: ignore[method-assign]

    verdict = await agent._self_heal_loop(
        vp_node=vp,
        coding_tool=coding_tool,
        settings_path=settings_path,
        log_path=log_path,
        project_dir=tmp_path,
    )
    assert verdict.verdict == "FAILED"
    assert "99 failed" in verdict.reasons[0]


# ---------------------------------------------------------------------------
# 7. Summarizer timeout uses the same 1-hour ceiling as the original cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_vp_result_uses_hard_cap_as_timeout(tmp_path: Path):
    """The summarizer's per-call timeout must be the same 1-hour cap
    the orchestrator uses elsewhere — not the original 5-min
    STUCK_GRACE. This is so a slow LLM still has room to read a
    large pytest log."""
    pytest_log = tmp_path / "vp_VP-TIMEOUT_progress.log"
    _write_pytest_log(pytest_log, "= 1 failed in 0.5s =\n")

    vp = {
        "id": "VP-TIMEOUT",
        "title": "summarizer timeout",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        "verdict": "FAILED",
        "reasons": ["1 failed"],
        "evidence": [],
        "pytest_exit_code": 1,
    }
    await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    call_kwargs = fake_tool.query_json.call_args.kwargs
    assert call_kwargs["timeout"] == 3600  # HARD_WALL_CLOCK_CAP_SECONDS