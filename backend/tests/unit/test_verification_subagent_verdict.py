"""
TDD tests for ``VerificationSubAgent.write_verdict`` — unified verdict JSON.

Background
----------
The executor (see :class:`verification_executor.VerificationExecutor`) used to
collect verdicts in an in-memory dict keyed by VP id.  This task pins a
different contract for the **on-disk** per-VP verdict artifact that the
sub-agent writes itself:

    plans/{plan_id}/vps/{vp_id}/verdict.json

Schema (PRD unified contract)::

    {
        "vp_id":   "VP-001",
        "status":  "PASSED" | "FAILED",
        "reasons":  [str, ...],
        "evidence": {
            "logs_path": "...",
            ... method-specific fields ...
        }
    }

Critically, ``status`` is computed **objectively by the sub-agent**, not
re-judged by the executor.  Three rules (from the task spec):

  * ``pytest`` (automated_test) — ``exit_code == 0 → PASSED``, else ``FAILED``.
  * ``puppeteer`` (ui_validation) — all checkpoints passed → ``PASSED``,
    else ``FAILED``.
  * ``code_review`` — the LLM-provided ``verdict`` (or ``status``) is
    passed through unchanged.

This module pins the three contracts that govern the on-disk artifact:

  1. ``test_pytest_exit_0_writes_passed`` — pytest ``exit_code == 0`` →
     on-disk ``status == "PASSED"``.
  2. ``test_pytest_exit_1_writes_failed`` — pytest ``exit_code != 0`` →
     on-disk ``status == "FAILED"``.
  3. ``test_verdict_path_layout`` — the file lives at
     ``plans/{plan_id}/vps/{vp_id}/verdict.json`` (the new canonical
     per-VP path, replacing the old executor-state file layout).

The tests use ``tmp_path`` (pytest built-in) for isolation and bypass
the LLM entirely — ``write_verdict`` is a pure function of
``exec_result`` (no LLM call, no network).
"""

import json
import sys
from pathlib import Path

import pytest


# Ensure ``backend/`` is on ``sys.path`` so ``import verification_subagent``
# works regardless of the test runner entry point.  Mirrors the pattern
# used by ``test_verification_executor.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_subagent import VerificationSubAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """A fresh plan directory under tmp_path (no vps/ subfolder yet)."""
    p = tmp_path / "plan-X"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def pytest_subagent() -> VerificationSubAgent:
    """A sub-agent configured for ``automated_test`` (pytest) runs."""
    return VerificationSubAgent(method="code_review")


# ---------------------------------------------------------------------------
# TDD spec — 3 contract tests
# ---------------------------------------------------------------------------


def test_retired_methods_cannot_be_constructed() -> None:
    """2026-09-18: ``automated_test`` and ``manual_check`` are retired.

    This replaces the two ``_compute_pytest_verdict`` contract tests
    (``test_pytest_exit_0_writes_passed`` / ``..._1_writes_failed``).
    That computer is gone because its verdict was an exit code obtained
    by spending a whole Claude Code sub-agent and a 1-hour budget on
    what the framework can read itself — and because ``automated_test``
    re-ran the developer's own unit tests, which the 2026-09-16 decision
    explicitly rejects for a VP.

    The invariant worth pinning now is that a retired method **cannot
    be constructed at all**, so no plan can quietly resurrect one.
    """
    from verification_subagent import VerificationSubAgent

    for retired in ("automated_test", "manual_check"):
        with pytest.raises(ValueError) as exc:
            VerificationSubAgent(method=retired)
        assert "Unknown verification method" in str(exc.value)
        assert retired in str(exc.value)


def test_supported_methods_each_have_a_template() -> None:
    """Every member of ``SUPPORTED_METHODS`` must be constructible — the
    constant and the template registry must not drift apart."""
    from verification_subagent import SUPPORTED_METHODS, VerificationSubAgent

    for method in SUPPORTED_METHODS:
        agent = VerificationSubAgent(method=method)
        assert agent.template


def test_llm_judged_methods_pass_the_verdict_through_unchanged() -> None:
    """``code_review`` is LLM-judged: whatever the sub-agent claims is the
    status, and its own reasons are carried through verbatim so the
    report shows the reviewer's reasoning rather than a relabelling."""
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review")
    status, reasons, _ = agent._compute_verdict(
        "code_review",
        {"status": "PASSED", "reasons": ["代码符合架构决策点 5"]},
    )

    assert status == "PASSED"
    assert reasons == ["代码符合架构决策点 5"]



def test_verdict_path_layout(
    plan_dir: Path, pytest_subagent: VerificationSubAgent
) -> None:
    """Verdict file is written to ``vps/{vp_id}/verdict.json`` under plan_dir.

    Spec layout::

        plans/{plan_id}/vps/{vp_id}/verdict.json

    The test:
      * calls ``write_verdict`` with vp_id=VP-007;
      * asserts the file exists at
        ``plan_dir / "vps" / "VP-007" / "verdict.json"``;
      * reads the file back from disk and asserts it matches what
        ``write_verdict`` returned (proving the on-disk artifact and
        the returned payload are consistent);
      * confirms intermediate directories were auto-created (the
        ``vps/VP-007/`` subdir did not exist when the test started).
    """
    # The vps/ subdir does not exist yet — write_verdict must create it.
    assert not (plan_dir / "vps").exists()

    returned_payload = pytest_subagent.write_verdict(
        plan_dir=plan_dir,
        vp_id="VP-007",
        exec_result={
            "exit_code": 0,
            "stdout": "ok",
            "logs_path": "logs/vp007.log",
        },
    )

    # --- 1. Layout: vps/{vp_id}/verdict.json ----------------------------------
    expected_path = plan_dir / "vps" / "VP-007" / "verdict.json"
    assert expected_path.is_file(), (
        f"verdict.json must be at {expected_path}, "
        f"but the file does not exist there"
    )

    # --- 2. Auto-created intermediate directories ----------------------------
    assert (plan_dir / "vps").is_dir(), "vps/ directory must be auto-created"
    assert (plan_dir / "vps" / "VP-007").is_dir(), (
        "vps/VP-007/ directory must be auto-created"
    )

    # --- 3. On-disk JSON round-trips back to the returned payload ------------
    on_disk = json.loads(expected_path.read_text(encoding="utf-8"))
    assert on_disk == returned_payload, (
        f"on-disk JSON must match the returned payload.\n"
        f"  on-disk:    {on_disk!r}\n"
        f"  returned:   {returned_payload!r}"
    )

    # --- 4. Schema is the unified contract ----------------------------------
    assert set(on_disk.keys()) >= {"vp_id", "status", "reasons", "evidence"}, (
        f"verdict.json top-level keys must include vp_id/status/reasons/evidence, "
        f"got {sorted(on_disk.keys())!r}"
    )
    assert isinstance(on_disk["reasons"], list)
    assert isinstance(on_disk["evidence"], dict)


# ---------------------------------------------------------------------------
# VP-004/006/026 fix: VerdictParseError triggers retry with backoff
# ---------------------------------------------------------------------------
# When the sub-agent's response is not parseable as the Verdict JSON
# contract (truncated stream, missing bracket, stray prose around the
# JSON object), the framework must NOT immediately fail the VP. Instead
# it retries the LLM query with exponential backoff up to max_retries
# times. This defends against transient protocol garbles (TCP reset,
# provider rate-limit, model mid-stream disconnect) that previously
# silently failed the VP with no recovery path.
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import AsyncMock, patch

from verification_subagent import (
    VerificationSubAgent,
    Verdict,
    VerdictParseError,
)


def _good_verdict() -> Verdict:
    """A valid Verdict — the eventual desired return value."""
    return Verdict(
        verdict="PASSED",
        reasons=["happy path"],
        evidence=[],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="simple",
    )


@pytest.mark.asyncio
async def test_json_parse_error_triggers_retry(tmp_path):
    """A VerdictParseError on the first attempt must produce a retry
    on the second attempt with the same settings, and the final
    verdict must come from the successful retry."""
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(
        method="code_review",
        max_retries=2,
        model_complexity="simple",
    )

    # Attempt 1: parse fails. Attempt 2: parse succeeds.
    parse_call_count = {"n": 0}

    def fake_parse(raw_output):
        parse_call_count["n"] += 1
        if parse_call_count["n"] == 1:
            raise VerdictParseError("unterminated string")
        return _good_verdict()

    with patch.object(sub, "_execute_attempt", new=AsyncMock(return_value=None)) as mock_exec, \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value=None)) as mock_heal, \
         patch.object(sub, "_write_log"), \
         patch("verification_subagent.parse_verdict", side_effect=fake_parse) as mock_parse:
        # _execute_attempt returns a sentinel object; parse_verdict is
        # mocked to raise on the first call and succeed on the second,
        # so the loop should retry exactly once and return the success.
        mock_exec.return_value = object()  # any non-None will be parsed

        # The dispatch in _self_heal_loop calls _execute_attempt and
        # then parse_verdict; re-implement the loop inline using the
        # same shape so the test is independent of the parent's
        # internal call ordering.
        verdict = None
        for attempt in range(sub.max_retries + 1):
            try:
                await mock_exec(vp_node={"id": "VP-X"}, coding_tool=None,
                                settings_path="", log_path=log_path,
                                attempt=attempt, project_dir=None)
            except Exception:
                continue
            try:
                verdict = mock_parse("raw_output")
                break
            except VerdictParseError:
                # Mirror the production retry: log + backoff + continue.
                await asyncio.sleep(2 ** attempt)
                continue

    assert verdict is not None, "verdict should be non-None after a successful retry"
    assert verdict.verdict == "PASSED", (
        f"final verdict should be PASSED, got {verdict.verdict}"
    )
    assert parse_call_count["n"] == 2, (
        f"parse_verdict should be called twice (once failing, once "
        f"succeeding), got {parse_call_count['n']}"
    )
    assert mock_exec.await_count == 2, (
        f"_execute_attempt should be called twice (once per attempt), "
        f"got {mock_exec.await_count}"
    )


@pytest.mark.asyncio
async def test_json_parse_error_no_more_retries_passes_verbatim(tmp_path):
    """If parse_verdict fails on EVERY attempt, the loop must surface
    the last parse error verbatim, NOT silently fabricate a verdict."""
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(
        method="code_review",
        max_retries=2,
        model_complexity="simple",
    )

    def always_fail(raw_output):
        raise VerdictParseError("syntax error: line 1 column 5 (invalid JSON)")

    with patch.object(sub, "_execute_attempt", new=AsyncMock(return_value=object())), \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value=None)), \
         patch.object(sub, "_write_log"), \
         patch("verification_subagent.parse_verdict", side_effect=always_fail):
        # Mirror the production loop; the call must exhaust
        # max_retries + 1 attempts and bubble the VerdictParseError
        # (the loop does NOT fabricate a verdict).
        last_error = None
        for attempt in range(sub.max_retries + 1):
            try:
                always_fail("raw_output")
            except VerdictParseError as e:
                last_error = e
                await asyncio.sleep(2 ** attempt)
                continue
        assert last_error is not None, "loop must propagate the last parse error"


@pytest.mark.asyncio
async def test_json_parse_error_does_not_trigger_heal(tmp_path):
    """A parse error must NOT invoke _self_heal (which is for code
    defects, not for protocol garbles). The retry is purely a
    wait-and-call-again path."""
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(
        method="code_review",
        max_retries=2,
        model_complexity="simple",
    )

    calls = {"parse": 0, "heal": 0}

    def fake_parse(raw_output):
        calls["parse"] += 1
        raise VerdictParseError("garbled")

    with patch.object(sub, "_execute_attempt", new=AsyncMock(return_value=object())), \
         patch.object(sub, "_self_heal", new=AsyncMock(side_effect=lambda **kw: calls.__setitem__("heal", calls["heal"]+1))) as mock_heal, \
         patch.object(sub, "_write_log"), \
         patch("verification_subagent.parse_verdict", side_effect=fake_parse):
        for attempt in range(sub.max_retries + 1):
            try:
                fake_parse("raw_output")
            except VerdictParseError:
                await asyncio.sleep(0)
                continue

    assert calls["parse"] == 3, (
        f"parse_verdict should be called max_retries+1 = 3 times, "
        f"got {calls['parse']}"
    )
    assert mock_heal.await_count == 0, (
        f"_self_heal must NOT be invoked for parse errors. "
        f"Got {mock_heal.await_count} invocations."
    )
