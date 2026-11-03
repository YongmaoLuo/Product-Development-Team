"""TDD tests for the report-side infra-unresolved terminal (2026-11-03).

Background
----------
``verification.failure_triage`` gives an environment failure the
disposition ``retry_not_counted``: it may be retried, and it does not
count against the deliverable.  ``MAX_INFRA_CROSS_ROUND_RETRIES``
(2) puts a bound on that: after the first try plus two cross-round
retries, the point has said "the environment is broken" three times and
the environment is not going to fix itself.

What the report said about such a point was the bug.  The judgment
LLM writes ``verification_report.json`` from a schema with no room for
an infra terminal, so the point landed as ``FAILED`` — a verdict about
the deliverable — and the round's verdict was derived from that
(``verification_agent._derive_overall_status``).  A point that was
never judged (the product was never asked) was counted as a broken
product, and the round it belonged to was red for the wrong reason.

The report side therefore needs a terminal that says **"the
environment, not the code"**, and it has to be distinguishable from a
code failure by a reader who only has the report:

    status                    BLOCKED  (not FAILED: no verdict about the deliverable)
    infrastructure_unresolved  True    (the marker itself)
    failure_terminal          infrastructure_unresolved
    reason_code               infrastructure_unresolved
    failure_class             infra
    failure_class_source      deterministic

The round's verdict follows: an entry the round could not judge makes
the round **unjudged**, and an unjudged round says ``SKIPPED``
(:func:`_derive_overall_status`).  A passing point elsewhere in the
same round does not turn it back into ``PASSED`` — the round judged
something it did not manage to judge.

What these tests pin
--------------------
  1. the listed entry carries the terminal, field for field;
  2. the round's verdict becomes ``SKIPPED`` — not ``PASSED``, not
     ``FAILED``;
  3. the negative control: an entry that was **not** listed keeps
     every field it had, and a real failure still keeps the round red;
  4. the no-ops: an empty ``vp_ids`` changes nothing at all (not even
     ``overall_status``), a report without a usable
     ``verification_results`` comes back untouched;
  5. an entry that is not a dict is skipped, not crashed on;
  6. the boundary: the terminal is a **report-entry** marker only.  It
     is not a stop reason — it must not appear in the state machine's
     vocabulary (``plan_state.PlanState.stop_verification_loop``'s
     ``valid_reasons``) nor in
     ``server.VERIFICATION_TERMINAL_STOP_REASONS``, because the
     stopping decision is a different task's decision (arch decision
     point 3) and a new word in that vocabulary would silently change
     the chain-ending contract.

The interface contract
-----------------------
::

    verification.failure_triage.INFRA_UNRESOLVED_TERMINAL
        == "infrastructure_unresolved"

    verification_agent._mark_infrastructure_unresolved(
        report_data: dict, vp_ids: Iterable[str],
    ) -> dict

The marker mutates the report in place (the way its sibling
``Orchestrator._backfill_failure_classes`` does) and returns it, so a
caller can use either the return value or the report it passed in.

A note on the imports
---------------------
Nothing is imported **by name** from the modules under test: the
missing constant and the missing function are reached at call time, so
this module always collects and every case is reported by name.  A
collection-time ``ImportError`` takes the whole file — and every case
name in it — out of the report, and it breaks the CI unit lane
(``pytest tests/unit``), which collects this directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
import verification_agent  # noqa: E402
from plan_state import PlanState  # noqa: E402
from verification import failure_triage  # noqa: E402


#: The terminal the contract names, spelled out here so the tests pin
#: the value itself rather than whatever the constant happens to hold.
INFRA_UNRESOLVED_TERMINAL = "infrastructure_unresolved"

#: The failure class the terminal carries: the environment, not the
#: deliverable.
INFRA = "infra"

#: The source tag that says the class was decided by a rule and not
#: assumed.
DETERMINISTIC = "deterministic"


def _mark(report, vp_ids):
    """Call the marker the way the report path calls it."""
    return verification_agent._mark_infrastructure_unresolved(report, vp_ids)


# ---------------------------------------------------------------------------
# 0. The constant.
# ---------------------------------------------------------------------------


def test_terminal_constant_is_the_documented_value():
    """``INFRA_UNRESOLVED_TERMINAL`` is the word the contract names."""
    assert (
        failure_triage.INFRA_UNRESOLVED_TERMINAL == INFRA_UNRESOLVED_TERMINAL
    ), failure_triage.INFRA_UNRESOLVED_TERMINAL


# ---------------------------------------------------------------------------
# 1. The terminal itself.
# ---------------------------------------------------------------------------


def test_marks_listed_entry_as_infra_terminal():
    """The listed entry carries the infra terminal, field for field.

    The entry is not a FAILED verdict any more: it is BLOCKED, it says
    the environment is what stopped it, and it says so in the same
    vocabulary the failure-class layer reads (``failure_class`` /
    ``failure_class_source``), so a reader of the report alone can tell
    an infra terminal from a broken deliverable.
    """
    report = {
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
            {"id": "VP-001", "status": "PASSED"},
        ],
    }

    out = _mark(report, ["VP-006"])

    assert out["verification_results"][0] == {
        "id": "VP-006",
        "status": "BLOCKED",
        "infrastructure_unresolved": True,
        "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
        "reason_code": INFRA_UNRESOLVED_TERMINAL,
        "failure_class": INFRA,
        "failure_class_source": DETERMINISTIC,
    }, out["verification_results"][0]


# ---------------------------------------------------------------------------
# 2. The round's verdict.
# ---------------------------------------------------------------------------


def test_overall_status_becomes_skipped():
    """A point the round could not judge makes the round unjudged.

    The round had a passing point, so the old derivation said
    ``PASSED``: a verdict about a product nobody asked.  The terminal
    says the round could not judge that point, so the round says
    ``SKIPPED`` — and the passing point is left exactly as it was.
    """
    report = {
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
            {"id": "VP-001", "status": "PASSED"},
        ],
    }

    out = _mark(report, ["VP-006"])

    assert out["overall_status"] == "SKIPPED", out
    assert out["verification_results"][1] == {"id": "VP-001", "status": "PASSED"}


# ---------------------------------------------------------------------------
# 3. The negative control.
# ---------------------------------------------------------------------------


def test_unlisted_failed_entry_still_fails_round():
    """An entry that was not listed keeps every field — and the round stays red.

    This is the control that keeps the marker from swallowing real
    defects: the marker only touches the points it was told about.  A
    point that genuinely failed stays FAILED, and the round it belongs
    to stays FAILED.
    """
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-002", "status": "FAILED"},
            {"id": "VP-001", "status": "PASSED"},
        ],
    }

    out = _mark(report, ["VP-006"])

    assert out["verification_results"][0] == {"id": "VP-002", "status": "FAILED"}, out
    assert out["verification_results"][1] == {"id": "VP-001", "status": "PASSED"}, out
    assert out["overall_status"] == "FAILED", out


# ---------------------------------------------------------------------------
# 4. The no-ops.
# ---------------------------------------------------------------------------


def test_empty_vp_ids_is_noop():
    """Nothing to mark → the report comes back untouched.

    Not even ``overall_status`` is written: a caller that had nothing
    to say must not leave a verdict behind, and a report that never
    carried one must not acquire one.
    """
    report = {
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
        ],
    }

    out = _mark(report, [])

    assert out == {
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
        ],
    }, out
    assert "overall_status" not in out, out


def test_missing_verification_results_is_noop():
    """A report without a usable ``verification_results`` comes back as it was."""
    report = {"overall_status": "FAILED"}

    out = _mark(report, ["VP-006"])

    assert out == {"overall_status": "FAILED"}, out


def test_non_list_verification_results_is_noop():
    """``verification_results`` that is not a list is not a report to mark."""
    report = {"verification_results": {"id": "VP-006"}, "overall_status": "PASSED"}

    out = _mark(report, ["VP-006"])

    assert out == {
        "verification_results": {"id": "VP-006"},
        "overall_status": "PASSED",
    }, out


# ---------------------------------------------------------------------------
# 5. The odd entry.
# ---------------------------------------------------------------------------


def test_non_dict_entry_is_skipped():
    """An entry that is not a dict is skipped, not crashed on.

    The report is written by a model, so its ``verification_results`` is
    not guaranteed to be a list of dicts.  One malformed entry must not
    take the whole marking pass — or the whole round — down.
    """
    report = {
        "verification_results": [
            "VP-006 is not a dict",
            {"id": "VP-006", "status": "FAILED"},
        ],
    }

    out = _mark(report, ["VP-006"])

    assert out["verification_results"][0] == "VP-006 is not a dict", out
    assert out["verification_results"][1]["status"] == "BLOCKED", out
    assert out["verification_results"][1]["failure_terminal"] == (
        INFRA_UNRESOLVED_TERMINAL
    ), out


# ---------------------------------------------------------------------------
# 6. The boundary: a report-entry marker, not a stop reason.
# ---------------------------------------------------------------------------


def test_terminal_is_not_a_stop_reason():
    """The terminal never enters the state machine's stop-reason vocabulary.

    Stopping the verification chain is a **different decision** (arch
    decision point 3), and it has its own closed vocabulary.  A new
    word in that vocabulary would change what ends a chain, so the
    terminal stays a report-entry field: the state machine refuses it
    as a stop reason, and the server's chain-ending set does not carry
    it.
    """
    assert INFRA_UNRESOLVED_TERMINAL not in server.VERIFICATION_TERMINAL_STOP_REASONS

    plan = PlanState.__new__(PlanState)
    plan._state = {"verification": {}}
    try:
        plan.stop_verification_loop(INFRA_UNRESOLVED_TERMINAL)
    except ValueError:
        pass
    else:
        raise AssertionError(
            "the state machine accepted "
            f"{INFRA_UNRESOLVED_TERMINAL!r} as a stop reason"
        )


# ---------------------------------------------------------------------------
# 7. The contract's shape: the same function on the orchestrator.
# ---------------------------------------------------------------------------


def test_orchestrator_exposes_the_marker_as_a_static_method():
    """``Orchestrator._mark_infrastructure_unresolved`` is the same function.

    The report path reaches its report helpers through the orchestrator,
    and the helper is a pure function of the report — it needs neither
    the plan nor the project — so it is a static method there.
    """
    report = {
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
        ],
    }

    out = verification_agent.Orchestrator._mark_infrastructure_unresolved(
        report, ["VP-006"],
    )

    assert out["verification_results"][0]["failure_terminal"] == (
        INFRA_UNRESOLVED_TERMINAL
    ), out
    assert out["overall_status"] == "SKIPPED", out
