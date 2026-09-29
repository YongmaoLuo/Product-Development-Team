"""2026-09-15: verification-phase retries are for
MALFUNCTIONS only — a clean FAILED verdict returns immediately.

History:
  * 2026-09-14: retries were capped (5 attempts incl. a watchdog
    bonus slot; a single VP could burn ~100 min of pytest per round
    and still verdict FAILED on every attempt).
  * 2026-09-15: the retry is removed for the dominant case. The
    verifier is an observer, not
    a repairer: a well-formed FAILED verdict with real evidence is a
    FINDING. It goes straight to the repair/split judge; re-running
    the same pytest cannot change the code under test, and self-heal
    has nothing to heal without corrupting verification independence.

New contract pinned here:
  * default construction -> max_retries == 2 (the budget still
    exists, but only for attempt malfunctions);
  * clean FAILED verdict -> exactly ONE ``_execute_attempt`` call, NO
    ``_self_heal``, verdict returned unchanged (no "max retries
    exhausted" marker);
  * watchdog kill still consumes the normal budget, but a subsequent
    FAILED verdict short-circuits the loop (kill + FAILED = 2 calls);
  * verdict-parse garble / transport errors retry with the budget and
    carry ``max_attempts == 3`` in the attempt_started audit trail;
  * budget exhaustion by malfunctions synthesizes a FAILED verdict
    from the last error (never SKIPPED).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from verification_subagent import (
    VerificationSubAgent,
    Verdict,
    VerdictParseError,
    WatchdogKilledError,
)


def _failed_verdict() -> Verdict:
    return Verdict(
        verdict="FAILED",
        reasons=["suite has real failures"],
        evidence=["117 failed, 33 errors"],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="simple",
    )


@pytest.mark.asyncio
async def test_default_retry_budget_is_initial_plus_two(tmp_path):
    """Default construction: max_retries == 2 → up to 3 total attempts
    for ATTEMPT MALFUNCTIONS (the budget itself is unchanged)."""
    sub = VerificationSubAgent(method="code_review")
    assert sub.max_retries == 2, (
        f"malfunction retry budget: initial + 2 retries; got "
        f"max_retries={sub.max_retries}"
    )


@pytest.mark.asyncio
async def test_failed_verdict_returns_immediately_no_retry_no_heal(tmp_path):
    """2026-09-15 core contract: a clean FAILED verdict is a finding.
    The loop must call _execute_attempt exactly ONCE, never invoke
    _self_heal, and return the verdict unchanged (no budget-exhausted
    marker) so the round can close into repair/split."""
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    log_path = tmp_path / "vp.log"

    exec_mock = AsyncMock(return_value=_failed_verdict())
    heal_mock = AsyncMock(return_value="healed")
    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=heal_mock), \
         patch.object(sub, "_write_log") as log_mock:
        verdict = await sub._self_heal_loop(
            vp_node={"id": "VP-023"},
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=log_path,
            project_dir=None,
        )

    assert exec_mock.await_count == 1, (
        f"a clean FAILED verdict must NOT be retried; _execute_attempt "
        f"was called {exec_mock.await_count} times"
    )
    assert heal_mock.await_count == 0, (
        "self-heal on a clean FAILED verdict is meaningless for an "
        "observer agent and must not run"
    )
    assert verdict.verdict == "FAILED"
    assert verdict.reasons == ["suite has real failures"], (
        f"verdict must be returned unchanged, got reasons={verdict.reasons}"
    )
    assert not any("max retries exhausted" in r for r in verdict.reasons)


@pytest.mark.asyncio
async def test_watchdog_kill_consumes_budget_then_failed_short_circuits(tmp_path):
    """Watchdog kill (a malfunction) consumes one budget slot, but the
    retry that follows produces a FAILED verdict — which now returns
    immediately. Total: 2 calls, not 3 (and never the old 4th bonus
    slot)."""
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    log_path = tmp_path / "vp.log"

    kill = WatchdogKilledError(
        kill_count=1, original_exc=RuntimeError("idle 1300s > 1200s"),
    )
    side_effects = [kill, _failed_verdict()]
    exec_mock = AsyncMock(side_effect=side_effects)
    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value="healed")), \
         patch.object(sub, "_write_log"):
        verdict = await sub._self_heal_loop(
            vp_node={"id": "VP-023"},
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=log_path,
            project_dir=None,
        )

    assert exec_mock.await_count == 2, (
        f"kill + FAILED short-circuit must stop at 2 attempts; got "
        f"{exec_mock.await_count}"
    )
    assert verdict.verdict == "FAILED"


@pytest.mark.asyncio
async def test_attempt_log_carries_max_attempts_three_on_malfunction_path(tmp_path):
    """The attempt_started audit trail must show max_attempts == 3 so
    operators can grep vp_attempts logs and verify the malfunction
    budget held. Driven here by two verdict-parse garbles followed by
    a FAILED verdict (3rd call returns immediately)."""
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    log_path = tmp_path / "vp.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    parse_err = VerdictParseError("truncated stream")
    exec_mock = AsyncMock(side_effect=[parse_err, parse_err, _failed_verdict()])
    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value="healed")):
        verdict = await sub._self_heal_loop(
            vp_node={"id": "VP-023"},
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=log_path,
            project_dir=None,
        )

    started = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event") == "attempt_started":
                started.append(entry)
    assert len(started) == 3, (
        f"expected 3 attempt_started events (2 garbles + 1 FAILED), "
        f"got {len(started)}"
    )
    assert all(e.get("data", {}).get("max_attempts") == 3 for e in started), (
        f"every attempt must advertise max_attempts=3: "
        f"{[e.get('data', {}).get('max_attempts') for e in started]}"
    )
    assert verdict.verdict == "FAILED"
    assert exec_mock.await_count == 3


@pytest.mark.asyncio
async def test_transport_exception_retries_with_heal_then_failed_returns(tmp_path):
    """A coding-tool transport error is a malfunction: retry with
    self-heal. But once an attempt produces a clean FAILED verdict,
    the loop returns it — no further retries."""
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    log_path = tmp_path / "vp.log"

    exec_mock = AsyncMock(side_effect=[RuntimeError("api 502"), _failed_verdict()])
    heal_mock = AsyncMock(return_value="healed")
    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=heal_mock), \
         patch.object(sub, "_write_log"):
        verdict = await sub._self_heal_loop(
            vp_node={"id": "VP-023"},
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=log_path,
            project_dir=None,
        )

    assert exec_mock.await_count == 2, exec_mock.await_count
    assert heal_mock.await_count == 1, (
        "self-heal applies to the transport malfunction, not to the "
        f"clean FAILED verdict; got {heal_mock.await_count} calls"
    )
    assert verdict.verdict == "FAILED"
    assert verdict.reasons == ["suite has real failures"]


@pytest.mark.asyncio
async def test_malfunction_budget_exhaustion_synthesizes_failed(tmp_path):
    """Every attempt malfunctions (transport errors) and the budget
    runs out → synthesize a hard FAILED from the last error (never
    SKIPPED)."""
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    log_path = tmp_path / "vp.log"

    exec_mock = AsyncMock(side_effect=RuntimeError("api 502"))
    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value="healed")), \
         patch.object(sub, "_write_log"):
        verdict = await sub._self_heal_loop(
            vp_node={"id": "VP-023"},
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=log_path,
            project_dir=None,
        )

    assert exec_mock.await_count == 3, (
        f"malfunction budget is exactly 3 attempts; got "
        f"{exec_mock.await_count}"
    )
    assert verdict.verdict == "FAILED"
    assert any("max retries exhausted" in r for r in verdict.reasons), (
        f"budget-exhausted marker missing: {verdict.reasons}"
    )
