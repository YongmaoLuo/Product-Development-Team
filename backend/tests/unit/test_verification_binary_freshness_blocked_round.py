"""The round the binary-freshness preflight blocked is recognised.

Background
----------
The verification agent stops a round **before** it dispatches
anything when the working tree is not fresh — a stale artefact means
every verification point would have graded yesterday's code.  Such a
round carries::

    stop_reason = "binary_freshness_preflight_failed"
    binary_freshness_preflight_failed = {<the freshness evidence>}

The verification loop only knows two kinds of blocked round so far:
``service_preflight_failed`` (the round's services were not usable) and
``base_drift_conflicts`` (the branch had drifted away from its base).
Any other ``stop_reason`` reads as "this round simply produced no
result", so the loop keeps the empty verdict and hands it to the
judgement — and an empty verdict is read as "everything failed", which
sends the repair chain after a product nobody ever looked at.

So the loop must recognise this third kind: the round dispatched
nothing because the **environment** took it out, and the evidence of
that (which files were stale, which surface was scanned, what the
rebuild attempt returned) has to travel with the report.

The word has to be the **same** word on both sides.  The agent writes
``binary_freshness_preflight_failed``; the loop must recognise exactly
that — a near-miss spelling would put the loop back where it started,
silently.

TDD contract — 4 test cases
---------------------------
1. ``test_binary_freshness_blocked_report_is_recognised`` — the round
   is recognised and its payload is handed back untouched.
2. ``test_other_stop_reasons_are_ignored`` — a round that ran, a round
   blocked by its services, a round blocked by a base-drift conflict
   and something that is not a report at all: not this function's
   business.
3. ``test_missing_payload_yields_empty_failure`` — a report whose
   payload is missing or malformed still yields a failure, so the
   blocked round is never silently dropped.
4. ``test_stop_reason_matches_the_agent_constant`` — the loop and the
   agent speak the same word.

Boundary conditions
-------------------
* A report that is not a dict: ``None``.
* A ``stop_reason`` that differs from the constant (including an empty
  one): ``None``.
* A payload that is missing or is not a dict: a fallback dict carrying
  ``detail`` — never ``None``, because a blocked round that is not
  recognised is a blocked round that is lost.
* Read-only — pure function calls over report dicts; no database row is
  written and no thread is started.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``backend/`` on sys.path so ``import server`` works no matter which
# entry point runs the suite, and the repository root so the
# ``backend.framework`` package resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import server  # noqa: E402,F401  — seeds sys.modules["server"] for the loop
import verification_agent  # noqa: E402
import verification_loop as vl  # noqa: E402


#: The payload the agent writes for a blocked round.
PAYLOAD = {
    "kind": "stale_pyc",
    "detail": "3 stale .pyc files",
    "evidence": ["pyc older than source"],
    "rebuild_command": "python -m compileall backend",
    "scan_scope": "backend",
    "missing_pyc_files": [],
    "stale_pyc_files": ["backend/a.pyc", "backend/b.pyc", "backend/c.pyc"],
    "rebuild_attempt": None,
}


def _report(**over):
    """The report a round blocked by the freshness preflight carries."""
    report = {
        "overall_status": "BLOCKED",
        "verification_results": [],
        "summary": "binary freshness preflight failed: 3 stale .pyc files",
        "stop_reason": vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND,
        "binary_freshness_preflight_failed": dict(PAYLOAD),
        "execution_profile": {"mode": "binary_freshness_preflight_blocked"},
    }
    report.update(over)
    return report


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_binary_freshness_blocked_report_is_recognised():
    """A freshness-blocked round is recognised and its payload kept."""
    blocked = vl._binary_freshness_blocked_report(_report())

    assert blocked is not None
    assert blocked == PAYLOAD


def test_other_stop_reasons_are_ignored():
    """Only this one stop reason is ours."""
    assert vl._binary_freshness_blocked_report(
        {"stop_reason": "service_preflight_failed"}
    ) is None
    assert vl._binary_freshness_blocked_report(
        {"stop_reason": "base_drift_conflicts"}
    ) is None
    assert vl._binary_freshness_blocked_report({"stop_reason": None}) is None
    assert vl._binary_freshness_blocked_report(None) is None
    assert vl._binary_freshness_blocked_report("not a report") is None


def test_missing_payload_yields_empty_failure():
    """A malformed payload still yields a failure — never ``None``.

    Returning ``None`` here would drop the blocked round on the floor:
    the loop would read the empty verdict as "everything failed".
    """
    blocked = vl._binary_freshness_blocked_report(
        _report(binary_freshness_preflight_failed=None)
    )

    assert blocked is not None
    assert blocked["detail"] == (
        "binary freshness preflight failed: 3 stale .pyc files"
    )

    missing = vl._binary_freshness_blocked_report(
        {
            "stop_reason": vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND,
            "summary": "nothing but a summary",
        }
    )

    assert missing is not None
    assert missing["detail"] == "nothing but a summary"


def test_stop_reason_matches_the_agent_constant():
    """The loop and the agent must speak the same word."""
    assert vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND == (
        verification_agent.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND
    )
