"""2026-10-23: an unparseable verdict must reach the retry branch —
it must NOT be reported as a verification failure.

Defect 2 (measured): the sub-agent returned ``{}``;
``parse_verdict_to_dataclass`` raised
``VerdictParseError: missing required field: 'verdict'``, but
``_execute_attempt`` caught that error and returned a *clean* FAILED
verdict. ``_self_heal_loop`` treats a clean FAILED as a finding
(2026-09-15: the verifier is an observer, a finding is not a
malfunction) and returns it immediately — so the ``except
VerdictParseError`` retry branch in ``_self_heal_loop`` was dead code.
The attempt log showed only ``attempt_started`` +
``attempt_completed FAILED``: never ``verdict_parse_error``, never
``verdict_parse_retry``, and the advertised ``max_attempts: 3`` never
took effect.

Contract pinned here:
  * a parse failure re-raises out of ``_execute_attempt`` and is
    retried until the malfunction budget is exhausted (3 attempts with
    the default ``max_retries=2``);
  * every attempt writes one ``verdict_parse_error``; every retry
    writes one ``verdict_parse_retry``;
  * every ``attempt_started`` carries ``max_attempts == 3``;
  * the synthesized FAILED verdict says the verdict was *unparseable*
    — it is not dressed up as "max retries exhausted", because an
    unparseable verdict and a transport failure are different
    malfunctions and the operator needs to tell them apart.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from verification_subagent import VerificationSubAgent


class _EmptyTool:
    """Stand-in for ``ClaudeCodingTool`` whose ``query_json`` always
    returns ``{}`` — the payload measured in defect 2.

    It is deliberately *not* a ``ClaudeCodingTool`` instance, so
    ``_execute_attempt`` uses it as its own ``scoped_tool``; the only
    method it has to provide is ``query_json``.
    """

    def __init__(self) -> None:
        self.calls = 0

    def query_json(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        return {}


async def _no_sleep(_delay: float) -> None:
    """Backoff stand-in — the retry *pacing* is not what these tests
    pin, and a real 1s + 2s wait would only slow the suite down."""
    return None


def _read_events(log_path: Path) -> List[Dict[str, Any]]:
    """Parse the JSON-lines attempt log into a list of entries."""
    entries: List[Dict[str, Any]] = []
    if not log_path.exists():
        return entries
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _events(log_path: Path, name: str) -> List[Dict[str, Any]]:
    return [e for e in _read_events(log_path) if e.get("event") == name]


async def _run_until_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_EmptyTool, Path]:
    """Drive the real ``_self_heal_loop`` with a tool that always
    returns an unparseable payload. Returns the tool (so the caller can
    count ``query_json`` calls) and the attempt-log path."""
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    tool = _EmptyTool()
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    log_path = tmp_path / "vp.log"

    verdict = await sub._self_heal_loop(
        vp_node={"id": "VP-001", "title": "parse failure probe"},
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=None,
    )

    # The run must end as a FAILED verdict — a parse failure is never
    # allowed to escape as anything else.
    assert verdict.verdict == "FAILED", (
        f"an unparseable verdict must synthesize FAILED; got "
        f"{verdict.verdict!r}"
    )
    return tool, log_path


@pytest.mark.asyncio
async def test_parse_error_retries_until_budget_exhausted(
    tmp_path, monkeypatch,
):
    """The parse failure must consume the whole malfunction budget:
    3 attempts (initial + 2 retries), each one calling the tool
    again. Before the fix the loop returned a clean FAILED after the
    FIRST attempt, so ``query_json`` was called exactly once and the
    retry branch was dead code."""
    tool, _log_path = await _run_until_budget_exhausted(tmp_path, monkeypatch)

    assert tool.calls == 3, (
        f"the parse failure must be retried until the budget is "
        f"exhausted (3 attempts); query_json was called {tool.calls} times"
    )


@pytest.mark.asyncio
async def test_parse_events_written(tmp_path, monkeypatch):
    """The attempt log must show the parse failure and the retries:
    one ``verdict_parse_error`` per attempt, one ``verdict_parse_retry``
    per retry (3 errors, 2 retries)."""
    _tool, log_path = await _run_until_budget_exhausted(tmp_path, monkeypatch)

    errors = _events(log_path, "verdict_parse_error")
    retries = _events(log_path, "verdict_parse_retry")

    assert len(errors) == 3, (
        f"expected 3 verdict_parse_error events (one per attempt); got "
        f"{len(errors)}"
    )
    assert len(retries) == 2, (
        f"expected 2 verdict_parse_retry events (one per retry); got "
        f"{len(retries)}"
    )


@pytest.mark.asyncio
async def test_attempt_started_uses_max_attempts_3(tmp_path, monkeypatch):
    """Every ``attempt_started`` must advertise the budget it is
    spending — ``max_attempts == 3`` — so an operator can grep the
    attempt log and see the malfunction budget held."""
    _tool, log_path = await _run_until_budget_exhausted(tmp_path, monkeypatch)

    started = _events(log_path, "attempt_started")

    assert len(started) == 3, (
        f"expected 3 attempt_started events; got {len(started)}"
    )
    assert all(
        e.get("data", {}).get("max_attempts") == 3 for e in started
    ), (
        f"every attempt must advertise max_attempts=3; got "
        f"{[e.get('data', {}).get('max_attempts') for e in started]}"
    )


@pytest.mark.asyncio
async def test_final_reason_says_unparseable(tmp_path, monkeypatch):
    """The synthesized FAILED verdict must name the malfunction it was
    built from: the verdict was unparseable. It must NOT reuse the
    transport-failure wording ("max retries exhausted"), because the
    two are different failures and the operator has to be able to tell
    them apart in the report."""
    tool = _EmptyTool()
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    sub = VerificationSubAgent(method="code_review", max_retries=2)
    log_path = tmp_path / "vp.log"

    verdict = await sub._self_heal_loop(
        vp_node={"id": "VP-001", "title": "parse failure probe"},
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=None,
    )

    reasons = list(verdict.reasons)
    assert reasons, "the synthesized FAILED verdict must carry a reason"

    joined = " | ".join(reasons)
    assert "verdict unparseable after 3 attempts" in joined, (
        f"the reason must say the verdict was unparseable after 3 "
        f"attempts; got {reasons}"
    )
    assert "max retries exhausted" not in joined, (
        f"an unparseable verdict must not be reported as an exhausted "
        f"transport budget; got {reasons}"
    )
