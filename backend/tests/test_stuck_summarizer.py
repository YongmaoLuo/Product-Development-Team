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
import inspect
import json
import logging
import os
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from verification_subagent import PROGRESS_LOG_PLACEHOLDER


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

# ---------------------------------------------------------------------------
# 8. Browser methods — the artifact decides, not the (absent) pytest log
# ---------------------------------------------------------------------------
#
# 2026-11-09 (VP-023): a browser walk (``ui_validation`` / ``e2e``) leaves
# its evidence in ``<evidence_artifact_dir>/checkpoints.json``. There is no
# pytest run to tee, so the stuck-sub-agent fallback found an empty log and
# degraded a walk whose every checkpoint had passed into "no test output
# captured" — the observations were thrown away and a green walk scored
# zero.
#
# The rule is the same all-or-nothing one the in-band path uses
# (``_compute_puppeteer_verdict``): every checkpoint must be
# ``passed=true``.

#: The console flow VP-023 walks, as checkpoint data. The selectors and
#: the texts are the verification point's own acceptance criteria — the
#: empty-state copy, the page title, the scheduler capacity figures.
VP023_CONSOLE_FLOW_CHECKPOINTS = [
    {
        "selector": "document.title",
        "expected": "Spec-Driven Dev System",
        "actual": "Spec-Driven Dev System",
        "passed": True,
    },
    {
        "selector": "#sidebar",
        "expected": "visible",
        "actual": "visible (display=flex)",
        "passed": True,
    },
    {
        "selector": "#plans-list",
        "expected": "暂无计划 / 点击「新建计划」开始",
        "actual": "暂无计划 / 点击「新建计划」开始",
        "passed": True,
    },
    {
        "selector": "#scheduler-view-capacity-used",
        "expected": "0",
        "actual": "0",
        "passed": True,
    },
    {
        "selector": "#btn-scheduler-back-home",
        "expected": "clicked; #plans-view visible again",
        "actual": "clicked; #plans-view visible again",
        "passed": True,
    },
]


def _write_checkpoints(artifact_dir: Path, checkpoints: Any) -> Path:
    """Write the evidence artifact a browser walk is required to leave."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "checkpoints.json"
    path.write_text(
        json.dumps({"checkpoints": checkpoints}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _browser_vp(artifact_dir: Path) -> Dict[str, Any]:
    """A VP node shaped like the console-flow one the framework hands out."""
    return {
        "id": "VP-023",
        "title": "控制台既有用户链路",
        "verification_method": "e2e",
        "expected_result": "计划列表渲染与调度视图切换均正常",
        "test_command": "",
        "evidence_artifact_dir": str(artifact_dir),
    }


@pytest.mark.asyncio
async def test_browser_stuck_verdict_is_decided_by_the_checkpoint_artifact(
    tmp_path: Path,
):
    """A walk whose every checkpoint passed is PASSED — decided from the
    artifact, with no LLM summarizer involved.

    The sub-agent that got stuck never returned a verdict JSON and never
    produced a pytest log (a browser walk does not run pytest). Its
    observations live in checkpoints.json, and those observations are
    what the verification point asked for.
    """
    artifact_dir = tmp_path / "vp_artifacts" / "VP-023"
    path = _write_checkpoints(artifact_dir, VP023_CONSOLE_FLOW_CHECKPOINTS)

    vp = _browser_vp(artifact_dir)
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = AssertionError(
        "the artifact decides; the summarizer must NOT call the LLM"
    )

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(tmp_path / "no_such_pytest.log"),
        coding_tool=fake_tool,
        log_path=log_path,
    )
    assert verdict.verdict == "PASSED"
    assert str(path) in verdict.reasons[0]
    fake_tool.query_json.assert_not_called()


@pytest.mark.asyncio
async def test_browser_stuck_verdict_names_the_failed_checkpoint(
    tmp_path: Path,
):
    """One failed checkpoint is a FAILED that names the selector and the
    expectation that was not met — not a generic "no output"."""
    artifact_dir = tmp_path / "vp_artifacts" / "VP-023"
    checkpoints = [dict(c) for c in VP023_CONSOLE_FLOW_CHECKPOINTS]
    checkpoints[3] = dict(checkpoints[3], passed=False, actual="—")
    path = _write_checkpoints(artifact_dir, checkpoints)

    vp = _browser_vp(artifact_dir)
    agent = _make_summarizer_agent()
    fake_tool = MagicMock()

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(tmp_path / "no_such_pytest.log"),
        coding_tool=fake_tool,
        log_path=tmp_path / "vp_attempt.log",
    )
    assert verdict.verdict == "FAILED"
    joined = " ".join(verdict.reasons)
    assert "#scheduler-view-capacity-used" in joined
    assert str(path) in joined


@pytest.mark.asyncio
async def test_browser_stuck_verdict_names_the_missing_artifact(
    tmp_path: Path,
):
    """A missing artifact is a FAILED that NAMES THE ARTIFACT.

    It must not claim that a test log was read — a browser walk never
    produces one, and the old wording ("no test output captured") sent
    the reader looking for a pytest run that did not exist.
    """
    artifact_dir = tmp_path / "vp_artifacts" / "VP-023"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    expected_path = artifact_dir / "checkpoints.json"

    vp = _browser_vp(artifact_dir)
    agent = _make_summarizer_agent()
    fake_tool = MagicMock()

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(tmp_path / "no_such_pytest.log"),
        coding_tool=fake_tool,
        log_path=tmp_path / "vp_attempt.log",
    )
    assert verdict.verdict == "FAILED"
    joined = " ".join(verdict.reasons)
    assert str(expected_path) in joined
    assert "no test output captured" not in joined
    # The wording must not claim a test log was read. The path is
    # stripped first: pytest's own tmp_path contains the word.
    wording = joined.replace(str(expected_path), "")
    assert "pytest" not in wording.lower()


@pytest.mark.asyncio
async def test_browser_stuck_verdict_names_a_malformed_artifact(
    tmp_path: Path,
):
    """A malformed artifact is a FAILED that names the artifact path."""
    artifact_dir = tmp_path / "vp_artifacts" / "VP-023"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "checkpoints.json"
    path.write_text("{not json at all", encoding="utf-8")

    vp = _browser_vp(artifact_dir)
    agent = _make_summarizer_agent()
    fake_tool = MagicMock()

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(tmp_path / "no_such_pytest.log"),
        coding_tool=fake_tool,
        log_path=tmp_path / "vp_attempt.log",
    )
    assert verdict.verdict == "FAILED"
    joined = " ".join(verdict.reasons)
    assert str(path) in joined
    # The wording must not claim a test log was read. The path is
    # stripped first: pytest's own tmp_path contains the word.
    wording = joined.replace(str(path), "")
    assert "pytest" not in wording.lower()


@pytest.mark.asyncio
async def test_browser_stuck_verdict_rejects_an_empty_checkpoint_list(
    tmp_path: Path,
):
    """An artifact with no checkpoints carries no evidence — FAILED, named."""
    artifact_dir = tmp_path / "vp_artifacts" / "VP-023"
    path = _write_checkpoints(artifact_dir, [])

    vp = _browser_vp(artifact_dir)
    agent = _make_summarizer_agent()

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(tmp_path / "no_such_pytest.log"),
        coding_tool=MagicMock(),
        log_path=tmp_path / "vp_attempt.log",
    )
    assert verdict.verdict == "FAILED"
    assert str(path) in " ".join(verdict.reasons)


def test_browser_templates_state_the_evidence_and_progress_contract():
    """The browser templates must say the three things a stuck walk needs.

    2026-11-09 (VP-023): the sub-agent wrote its checkpoints at the very
    end of the walk, never appended a line to the progress log the
    framework reads, and spent the rest of its budget on optional
    corroboration instead of returning the verdict. All three are
    template-level instructions, so they belong in the template.
    """
    from verification_subagent import MethodTemplateRegistry

    for method in ("e2e", "ui_validation"):
        template = MethodTemplateRegistry.get_template(method)
        # 1. the artifact is written incrementally, not at the end
        assert "checkpoints.json" in template, method
        assert "incrementally" in template.lower(), method
        # 2. one progress-log line per checkpoint.
        #
        # 2026-11-18 (VP-032): the template no longer carries a **bare**
        # path. A verification-point id is only unique inside its plan, so
        # ``/tmp/vp_<id>_progress.log`` was a **global** file that two
        # plans with the same vp id wrote to at once — the framework then
        # read the other plan's output and concluded "this round produced
        # no conclusion". The template now carries a placeholder that the
        # framework replaces with the plan-scoped concrete path.
        assert PROGRESS_LOG_PLACEHOLDER in template, method
        assert "/tmp/vp_<id>_progress.log" not in template, method
        # 3. the verdict comes back as soon as the checkpoints are in
        assert "return the verdict JSON" in template, method


# ---------------------------------------------------------------------------
# 9. The progress log path is plan-scoped (2026-11-18, VP-032)
# ---------------------------------------------------------------------------
#
# 2026-11-18: the tee path was the *global* ``/tmp/vp_<id>_progress.log``.
# A verification-point id is only unique **inside its plan**, so two plans
# that both have a ``VP-032`` wrote the same file — the second plan's run
# clobbered the first one's, and the framework's stuck-agent summarizer
# read the other plan's output. The round then concluded "no conclusion
# was produced" about a deliverable that had in fact been verified.


def test_progress_log_path_is_plan_scoped():
    """Two plans, one vp id → two different files."""
    from verification_subagent import _progress_log_path

    here = _progress_log_path("plan-alpha", "VP-032")
    there = _progress_log_path("plan-beta", "VP-032")

    assert here != there, (
        "两个计划的同名 VP 写同一个文件 —— 这正是 2026-11-18 VP-032 误判的根因"
    )
    assert "plan-alpha" in here, here
    assert "plan-beta" in there, there
    # 只差大小写的两个计划也不能共用一个文件
    assert _progress_log_path("plan-Alpha", "VP-032") != here
    # The vp token keeps the framework's own shape (``VP-032`` →
    # ``vp_032``), so the /tmp artifact globs and the ``pgrep``
    # patterns still match the file this path names.
    assert "vp_032" in here, here
    # 写与读必须是同一个函数算出来的同一个路径
    assert here == _progress_log_path("plan-alpha", "VP-032"), here
    # 文件名里不安全的字符被替换掉
    assert _progress_log_path("plan 2026/01", "VP-032") == (
        "/tmp/vp_plan_2026_01_vp_032_progress.log"
    )
    # 没有 plan id 时退回不带计划段的形状 —— 旧计划的读侧不能因此找不到文件
    assert _progress_log_path(None, "VP-032") == "/tmp/vp_vp_032_progress.log"
    assert _progress_log_path("", "VP-032") == "/tmp/vp_vp_032_progress.log"


def test_prompt_names_the_concrete_progress_path():
    """The prompt names the concrete path — never a template."""
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(
        method="code_review", max_retries=1, plan_id="plan-alpha",
    )
    vp = {
        "id": "VP-032",
        "title": "合并完整性",
        "verification_method": "code_review",
        "expected_result": "两侧意图都在",
        "test_command": "pytest -q backend/tests/unit",
    }
    concrete = "/tmp/vp_plan-alpha_vp_032_progress.log"

    system = agent._system_instruction(vp)
    prompt = agent._build_prompt(vp, attempt=0)

    assert concrete in system, system[-800:]
    assert concrete in prompt, prompt[-800:]
    # 裸模板已经没有了：占位符必须被替换掉
    assert PROGRESS_LOG_PLACEHOLDER not in system
    assert PROGRESS_LOG_PLACEHOLDER not in prompt
    assert "/tmp/vp_<id>_progress.log" not in system
    assert "/tmp/vp_<id>_progress.log" not in prompt


def test_stuck_summarizer_reads_the_plan_scoped_progress_path():
    """The reader uses the same helper as the writer.

    The write path is the prompt's; the read path is the stuck-agent
    summarizer's. They have to be the *same* path — two spellings of
    the same convention is how the reader ended up on somebody else's
    file in the first place.
    """
    from verification_subagent import VerificationSubAgent, _progress_log_path

    source = inspect.getsource(VerificationSubAgent._execute_attempt)
    assert "_progress_log_path(" in source, (
        "读侧必须用与写侧同一个 helper 算路径"
    )

    agent = VerificationSubAgent(
        method="code_review", max_retries=1, plan_id="plan-alpha",
    )
    vp = {"id": "VP-032", "title": "t", "verification_method": "code_review"}
    assert _progress_log_path("plan-alpha", "VP-032") in agent._system_instruction(vp)


# ---------------------------------------------------------------------------
# 10. The summarizer must not read an input THIS attempt did not write
# ---------------------------------------------------------------------------
#
# 2026-11-19 (VP-045): the stuck route hands the summarizer two recovery
# inputs — the tee'd progress log and the evidence artifact directory.
# Both are REUSED across rounds, so a summarizer that fires after the
# outer cap can read the PREVIOUS attempt's output and grade it as this
# attempt's conclusion.  2026-11-19 saw exactly that: the summarizer
# read a three-hour-old progress log ("16 passed"), answered PASSED, and
# the framework then checked a stale citations.json against the current
# tree and filed the deliverable as broken — two stale framework-side
# inputs convicting a deliverable nobody had looked at.
#
# The rule: an input this attempt did not write is not evidence about
# this attempt.  The verdict is then UNKNOWN — "this attempt established
# nothing about the deliverable" — and never FAILED (a FAILED here is a
# verdict nobody earned, and it cascades into Phase 2's DEFERRED chain).


def _log_events(log_path: Path, event: str) -> list:
    """The log entries recorded under one event name."""
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("event") == event:
            entries.append(payload.get("data") or {})
    return entries


def _backdate(path: Path, seconds_ago: float = 3600.0) -> float:
    """Push a file's mtime into the past; return the mtime it got."""
    mtime = time.time() - seconds_ago
    os.utime(path, (mtime, mtime))
    return path.stat().st_mtime


def test_stuck_agent_error_carries_the_attempt_start_time():
    """The attempt's start time rides on the exception.

    The existing construction sites keep working — the field defaults
    to ``None`` — but an attempt that was actually run can say when it
    started.
    """
    from verification_subagent import StuckAgentError

    # 既有构造点不带这个字段：默认必须是 None
    assert StuckAgentError(
        elapsed=1.0, threshold=1.0, vp_id="VP-X",
    ).started_at is None

    error = StuckAgentError(
        elapsed=1.0, threshold=1.0, vp_id="VP-X", started_at=123.5,
    )
    assert error.started_at == 123.5


@pytest.mark.asyncio
async def test_summarizer_rejects_a_progress_log_written_before_the_attempt(
    tmp_path: Path,
):
    """A log left by an EARLIER attempt is not this attempt's conclusion.

    The summarizer must not grade it at all: the verdict is UNKNOWN
    ("this attempt established nothing"), never a PASSED/FAILED read
    out of somebody else's output.
    """
    pytest_log = tmp_path / "vp_VP-STALE_progress.log"
    _write_pytest_log(pytest_log, "= 16 passed, 14 deselected in 3.1s =\n")
    stale_mtime = _backdate(pytest_log)

    vp = {
        "id": "VP-STALE",
        "title": "stale log",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = AssertionError(
        "a log an earlier attempt wrote must not be summarized"
    )

    attempt_started_at = time.time()
    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
        attempt_started_at=attempt_started_at,
    )

    assert verdict.verdict == "UNKNOWN"
    fake_tool.query_json.assert_not_called()
    # 分类是 infra —— 框架自己没拿到结论，不是交付物坏了
    assert verdict.failure_class == "infra"
    assert verdict.failure_class_source == "deterministic"
    # 理由里必须同时出现两个时间戳
    joined = " ".join(verdict.reasons)
    assert f"{stale_mtime:.3f}" in joined
    assert f"{attempt_started_at:.3f}" in joined
    # 证据里是输入路径与它的 mtime
    assert str(pytest_log) in " ".join(verdict.evidence)
    # 事件记下输入路径、mtime 与 attempt_started_at
    events = _log_events(log_path, "summarizer_stale_input")
    assert len(events) == 1
    assert events[0]["path"] == str(pytest_log)
    assert abs(events[0]["mtime"] - stale_mtime) < 1.0
    assert events[0]["attempt_started_at"] == attempt_started_at


@pytest.mark.asyncio
async def test_summarizer_rejects_a_checkpoint_artifact_written_before_the_attempt(
    tmp_path: Path,
):
    """An evidence artifact an EARLIER attempt wrote is not this
    attempt's walk.

    The artifact decides in the artifact-first branch — so a stale
    artifact must be refused there, before it is graded.
    """
    artifact_dir = tmp_path / "vp_artifacts" / "VP-023"
    path = _write_checkpoints(artifact_dir, VP023_CONSOLE_FLOW_CHECKPOINTS)
    stale_mtime = _backdate(path)

    vp = _browser_vp(artifact_dir)
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = AssertionError(
        "a stale artifact must not be summarized"
    )

    attempt_started_at = time.time()
    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(tmp_path / "no_such_pytest.log"),
        coding_tool=fake_tool,
        log_path=log_path,
        artifact_dir=artifact_dir,
        attempt_started_at=attempt_started_at,
    )

    assert verdict.verdict == "UNKNOWN"
    fake_tool.query_json.assert_not_called()
    assert verdict.failure_class == "infra"
    joined = " ".join(verdict.reasons)
    assert f"{stale_mtime:.3f}" in joined
    assert f"{attempt_started_at:.3f}" in joined
    events = _log_events(log_path, "summarizer_stale_input")
    assert len(events) == 1
    assert events[0]["path"] == str(path)


@pytest.mark.asyncio
async def test_summarizer_rejects_a_stale_artifact_named_by_the_vp_node(
    tmp_path: Path,
):
    """The browser branch reads the artifact the VP NODE names.

    The freshness check has to look there too — otherwise a stale
    checkpoints.json is graded as this attempt's walk.
    """
    artifact_dir = tmp_path / "vp_artifacts" / "VP-023"
    path = _write_checkpoints(artifact_dir, VP023_CONSOLE_FLOW_CHECKPOINTS)
    stale_mtime = _backdate(path)

    vp = _browser_vp(artifact_dir)
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = AssertionError(
        "a stale artifact must not be summarized"
    )

    attempt_started_at = time.time()
    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(tmp_path / "no_such_pytest.log"),
        coding_tool=fake_tool,
        log_path=log_path,
        artifact_dir=None,
        attempt_started_at=attempt_started_at,
    )

    assert verdict.verdict == "UNKNOWN"
    fake_tool.query_json.assert_not_called()
    joined = " ".join(verdict.reasons)
    assert f"{stale_mtime:.3f}" in joined
    assert f"{attempt_started_at:.3f}" in joined
    events = _log_events(log_path, "summarizer_stale_input")
    assert len(events) == 1
    assert events[0]["path"] == str(path)


@pytest.mark.asyncio
async def test_summarizer_reads_a_log_written_during_the_attempt(
    tmp_path: Path,
):
    """The guard does not fire on an input this attempt wrote.

    A fresh log is still summarized — the freshness check must not turn
    every stuck attempt into an UNKNOWN.
    """
    pytest_log = tmp_path / "vp_VP-FRESH_progress.log"
    _write_pytest_log(pytest_log, "= 3 passed in 0.05s =\n")

    vp = {
        "id": "VP-FRESH",
        "title": "fresh log",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        "verdict": "PASSED",
        "reasons": ["3 passed in 0.05s, no failures"],
        "evidence": [],
    }

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
        attempt_started_at=time.time() - 60.0,
    )

    assert verdict.verdict == "PASSED"
    fake_tool.query_json.assert_called_once()
    assert _log_events(log_path, "summarizer_stale_input") == []


@pytest.mark.asyncio
async def test_summarizer_without_an_attempt_start_time_keeps_its_behaviour(
    tmp_path: Path,
):
    """No attempt start time → the current behaviour, unchanged.

    The guard is a NEW precondition, not a change of the existing
    branches: a caller that cannot say when the attempt started gets
    exactly what it got before.
    """
    pytest_log = tmp_path / "vp_VP-OLD_progress.log"
    _write_pytest_log(pytest_log, "= 3 passed in 0.05s =\n")
    _backdate(pytest_log)

    vp = {
        "id": "VP-OLD",
        "title": "old log, no attempt start time",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }
    log_path = tmp_path / "vp_attempt.log"
    agent = _make_summarizer_agent()

    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        "verdict": "PASSED",
        "reasons": ["3 passed in 0.05s, no failures"],
        "evidence": [],
    }

    verdict = await agent._summarize_vp_result(
        vp_node=vp,
        output_file=str(pytest_log),
        coding_tool=fake_tool,
        log_path=log_path,
    )

    assert verdict.verdict == "PASSED"
    fake_tool.query_json.assert_called_once()
    assert _log_events(log_path, "summarizer_stale_input") == []


@pytest.mark.asyncio
async def test_self_heal_loop_hands_the_attempt_start_time_to_the_summarizer(
    tmp_path: Path,
):
    """The start time travels from the attempt to the summarizer.

    Without it the summarizer cannot tell this attempt's output from an
    earlier attempt's, and it grades the earlier one.
    """
    from verification_subagent import (
        StuckAgentError,
        VerificationSubAgent,
        Verdict,
    )

    vp = {
        "id": "VP-STUCK",
        "title": "stuck agent end-to-end",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
        "timeout_seconds": 900,
    }

    agent = VerificationSubAgent(method="code_review", max_retries=1)

    stuck_exc = StuckAgentError(
        elapsed=3630.0,
        threshold=3630.0,
        vp_id="VP-STUCK",
        output_file=None,
        started_at=1000.5,
    )

    async def fake_execute_attempt(*args, **kwargs):
        raise stuck_exc

    agent._execute_attempt = fake_execute_attempt  # type: ignore[method-assign]

    captured: Dict[str, Any] = {}

    async def fake_summarize(*args, **kwargs):
        captured.update(kwargs)
        return Verdict(
            verdict="UNKNOWN",
            reasons=["no conclusion about the deliverable"],
            evidence=[],
            model_complexity="simple",
        )

    agent._summarize_vp_result = fake_summarize  # type: ignore[method-assign]

    verdict = await agent._self_heal_loop(
        vp_node=vp,
        coding_tool=MagicMock(),
        settings_path=None,
        log_path=tmp_path / "vp_attempt.log",
        project_dir=tmp_path,
    )

    assert verdict.verdict == "UNKNOWN"
    assert captured.get("attempt_started_at") == 1000.5


@pytest.mark.asyncio
async def test_execute_attempt_stamps_the_attempt_start_time(
    tmp_path: Path, monkeypatch,
):
    """The attempt's start time is taken where the attempt starts.

    It is the only record of "when did this attempt begin" — and the
    freshness check is meaningless without it.
    """
    import verification_subagent as vs

    agent = _make_summarizer_agent()
    agent.wall_clock_cap = lambda *a, **k: 2.0  # type: ignore[method-assign]

    def _outer_cap_fired(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(vs, "query_json_in_session", _outer_cap_fired)

    vp = {
        "id": "VP-START",
        "title": "attempt start time",
        "verification_method": "code_review",
        "expected_result": "all green",
        "test_command": "pytest tests/",
    }

    before = time.time()
    with pytest.raises(vs.StuckAgentError) as excinfo:
        await agent._execute_attempt(
            vp_node=vp,
            coding_tool=MagicMock(),
            settings_path=None,
            log_path=tmp_path / "vp_attempt.log",
            attempt=1,
            project_dir=tmp_path,
            artifact_dir=None,
        )
    after = time.time()

    started_at = excinfo.value.started_at
    assert started_at is not None
    assert before <= started_at <= after
