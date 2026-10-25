"""2026-10-22 (P0-1): a verdict-parse failure is a MALFUNCTION of the
attempt, and the L2 retry it earns must be reachable.

The bug this pins down
----------------------
``VerificationSubAgent._execute_attempt`` used to catch
:class:`VerdictParseError` and answer with a locally built
``Verdict(verdict="FAILED", reasons=["verdict parse error: ..."])``.
That construction swallowed the failure: the caller
(``_self_heal_loop``) saw a well-formed verdict, so its ``except
VerdictParseError`` branch — the L2 retry, the one that exists exactly
for a transient protocol garble (truncated stream, stray prose around
the JSON object) — was unreachable. One garbled response failed the
verification point for good, and the verdict it produced claimed the
deliverable was broken when nothing was known about the deliverable at
all.

The contract pinned here
------------------------
  * a payload that cannot be parsed raises out of ``_execute_attempt``
    instead of becoming a verdict (no inline ``FAILED`` verdict);
  * the raise reaches ``_self_heal_loop``, which retries the attempt
    (L2) and logs a ``verdict_parse_retry`` event per retry;
  * a retry that succeeds produces that attempt's verdict — a garble
    followed by a good payload is a PASSED, not a FAILED;
  * a budget that ran out on malfunctions ends as a hard FAILED
    classified ``infra`` (the attempt malfunctioned, the deliverable is
    not implicated) with the source ``deterministic`` (the class was
    derived by rule, not read off a claim).

The negative control (``# NEGCTL-P0-1-BEGIN`` / ``# NEGCTL-P0-1-END``)
lives in the production source: the block between the two markers is
the except arm, and it must not build a ``Verdict`` of its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_subagent as _verification_subagent  # noqa: E402
from verification_subagent import (  # noqa: E402
    VerificationSubAgent,
    VerdictParseError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedCodingTool:
    """A coding tool that answers with a scripted list of payloads.

    ``query_json`` is called on a worker thread (the real coding tool
    blocks), but only one call is ever in flight, so a plain list is
    enough to script the responses.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def query_json(self, prompt=None, system_instruction=None, timeout=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError(
                "the scripted coding tool ran out of responses"
            )
        return self._responses.pop(0)


def _read_events(log_path: Path) -> List[Dict[str, Any]]:
    """The JSON-lines entries the attempt log holds."""
    events: List[Dict[str, Any]] = []
    if not log_path.exists():
        return events
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _count_event(log_path: Path, event: str) -> int:
    return len([e for e in _read_events(log_path) if e.get("event") == event])


def _vp_node() -> Dict[str, Any]:
    return {
        "id": "VP-001",
        "title": "the module exposes its public API",
        "verification_method": "code_review",
        "expected_result": "the public API is reachable",
    }


#: A payload whose ``verdict`` is a list, not a string — rejected by
#: ``parse_verdict`` (the field must be a str).
MALFORMED_PAYLOAD: Dict[str, Any] = {"verdict": ["FAILED"]}

#: A well-formed payload.
VALID_PAYLOAD: Dict[str, Any] = {
    "verdict": "PASSED",
    "reasons": [],
    "evidence": [],
}


# ---------------------------------------------------------------------------
# 1. A malformed payload is retried, and the retry's verdict counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_error_retries_then_passes(tmp_path):
    """Garbled response → L2 retry → the second attempt's verdict.

    The final verdict must be the second attempt's PASSED, and the
    attempt log must show exactly one ``verdict_parse_retry``.
    """
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    tool = _ScriptedCodingTool([MALFORMED_PAYLOAD, VALID_PAYLOAD])

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=None,
    )

    assert tool.calls == 2, (
        f"the garbled attempt must be retried exactly once; the tool was "
        f"called {tool.calls} times"
    )
    assert verdict.verdict == "PASSED", (
        f"the retry's verdict is the one that counts; got "
        f"{verdict.verdict!r}"
    )
    assert verdict.failure_class == "", (
        f"a passed verdict has no failure to classify; got "
        f"{verdict.failure_class!r}"
    )
    assert _count_event(log_path, "verdict_parse_retry") == 1, (
        "the L2 retry must be observable in the attempt log"
    )


# ---------------------------------------------------------------------------
# 2. A budget that ran out on malfunctions ends as infra / deterministic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_marks_infra(tmp_path):
    """Every attempt garbled → hard FAILED, classified ``infra``.

    The attempt malfunctioned; the deliverable is not implicated, and
    the class is marked ``deterministic`` because it was derived by rule
    rather than read off a claim.
    """
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    tool = _ScriptedCodingTool(
        [MALFORMED_PAYLOAD, {"verdict": {"wrong": "shape"}}]
    )

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=None,
    )

    assert tool.calls == 2, (
        f"the budget is max_retries + 1 attempts; the tool was called "
        f"{tool.calls} times"
    )
    assert verdict.verdict == "FAILED", (
        f"an exhausted budget is a hard FAILED, never a SKIPPED; got "
        f"{verdict.verdict!r}"
    )
    assert verdict.failure_class == "infra", (
        f"the attempt malfunctioned, so the class is infra; got "
        f"{verdict.failure_class!r}"
    )
    assert verdict.failure_class_source == "deterministic", (
        f"the class was derived by rule, not claimed; got "
        f"{verdict.failure_class_source!r}"
    )
    assert any(
        "verdict unparseable after 2 attempts" in reason
        for reason in verdict.reasons
    ), f"the budget-exhausted marker is missing: {verdict.reasons}"


# ---------------------------------------------------------------------------
# 3. The except arm does not build a verdict of its own
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_inline_failed_verdict(tmp_path):
    """``_execute_attempt`` must raise, not answer with a verdict.

    Two checks, one behavioural and one structural:

      * calling ``_execute_attempt`` directly with a malformed payload
        raises ``VerdictParseError`` — the caller decides what a
        malformed payload means, and the only caller that may answer
        with a verdict is the retry loop;
      * the source block between the two negative-control markers
        (``# NEGCTL-P0-1-BEGIN`` / ``# NEGCTL-P0-1-END``) constructs no
        ``Verdict`` — the marker pair is what the negative-control
        script locates, so the guarantee has to live inside it.
    """
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    tool = _ScriptedCodingTool([MALFORMED_PAYLOAD])

    with pytest.raises(VerdictParseError):
        await sub._execute_attempt(
            vp_node=_vp_node(),
            coding_tool=tool,
            settings_path=str(tmp_path / "settings.json"),
            log_path=tmp_path / "vp.log",
            attempt=0,
            project_dir=None,
        )

    source = Path(_verification_subagent.__file__).read_text(encoding="utf-8")
    begin_marker = "# NEGCTL-P0-1-BEGIN"
    end_marker = "# NEGCTL-P0-1-END"
    assert begin_marker in source, (
        f"the negative-control begin marker {begin_marker!r} is missing "
        f"from {Path(_verification_subagent.__file__).name}"
    )
    assert end_marker in source, (
        f"the negative-control end marker {end_marker!r} is missing "
        f"from {Path(_verification_subagent.__file__).name}"
    )
    block = source.split(begin_marker, 1)[1].split(end_marker, 1)[0]
    assert "Verdict(" not in block, (
        "the parse-error arm must not build a verdict of its own:\n"
        f"{block}"
    )
    assert "raise" in block, (
        "the parse-error arm must re-raise so the L2 retry is "
        f"reachable:\n{block}"
    )
    assert "VerdictParseError" in block, (
        "the marked block must be the VerdictParseError handler itself "
        "— that is what the negative-control script locates:\n"
        f"{block}"
    )


# ---------------------------------------------------------------------------
# 4. The classification literals match the failure-triage vocabulary
# ---------------------------------------------------------------------------


def test_the_infra_literals_match_the_failure_triage_vocabulary():
    """The drift guard.

    ``verification_subagent`` cannot import ``verification.failure_triage``
    at module level (the import is circular), so the two values are
    spelled as literals here. This test is what keeps them the same
    values — the same pattern ``tests/unit/test_verdict_fields.py`` uses
    for the default class.
    """
    from verification import failure_triage

    assert (
        _verification_subagent.INFRA_FAILURE_CLASS
        == failure_triage.FailureClass.INFRA
    )
    assert (
        _verification_subagent.DETERMINISTIC_FAILURE_CLASS_SOURCE
        == failure_triage.SOURCE_DETERMINISTIC
    )
