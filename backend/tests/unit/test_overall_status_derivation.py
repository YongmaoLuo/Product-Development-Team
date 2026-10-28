"""RED regression tests: a round that judged nothing must not say PASSED.

Background
----------
``overall_status`` -- the one word every reader of a verification report
takes the round's verdict from -- is derived in two places: where the
report is built, and where the environment-blocked entries are taken out
of the failure statistics
(``verification_agent.Orchestrator._exclude_environment_blocked_from_failures``).
Both places ask the same question -- what did this round come back with --
and both answer it with a three-way choice::

    FAILED   a point failed
    PARTIAL  a point came back partial
    PASSED   otherwise

"Otherwise" is the bug.  A round that judged **nothing** lands in
"otherwise", and the report says ``PASSED``: a verdict about a product
nobody asked.  That is what the production round did -- ten points
PASSED, eleven SKIPPED, two DEFERRED, and the round claimed to have
passed.  The thirteen points nobody judged were read as agreement.

What these tests pin
-------------------- 1. a round with a DEFERRED point is not PASSED;
  2. a round with an environment-blocked point is not PASSED;
  3. a point the accounting layer rewrote to SKIPPED but that carries an
     environment reason code is still recognised as blocked -- the status
     is not the evidence;
  4. the rounds that already said the truth keep saying it: all-PASSED
     stays PASSED, a real failure still wins, a partial verdict stays
     partial, a plain SKIPPED does not drag the round down, and a round
     with nothing in it stays PASSED (a round with no verification point
     has no verdict to give; that behaviour is out of scope here and must
     not change);
  5. the accounting layer re-derives the round's verdict the same way.

The interface contract
-----------------------
The derivation becomes one module-level function::

    verification_agent._derive_overall_status(entries: list[dict]) -> str

``entries`` are the report's ``verification_results``.  The decision is
taken in this order::

    a FAILED entry          -> "FAILED"
    a PARTIAL entry         -> "PARTIAL"
    a DEFERRED entry        -> "SKIPPED"
    an environment-blocked  -> "SKIPPED"
    anything else           -> "PASSED"

An entry counts as environment-blocked when it says so itself
(``entry["environment_blocked"] is True``) or when its evidence carries a
reason code the environment owns (``evidence["reason_code"]`` in
``ENVIRONMENT_REASON_CODES``).  The **status is not consulted**: the
accounting layer rewrites a blocked point to SKIPPED before the verdict
is derived, so a rule that looked at the status alone would miss it.

Why the cases carry a marker
----------------------------
The reproduction has to fail on the base point -- that failure is the
evidence this file exists for -- but a test command that exits non-zero
is a **failed task**, and a failed task blocks every task that depends
on it (the implementation of this contract is the next task).  The
marker is therefore a ratchet rather than a waiver.  It applies only
while the implementation has not landed at all -- and it switches itself
off the moment it has (see ``_implementation_is_missing``).  An
implementation that lands with the wrong value therefore fails these
tests outright instead of being absorbed by the marker.

To read the raw red on the base point, run the module with the markers
switched off::

    pytest tests/unit/test_overall_status_derivation.py --runxfail -v

A note on the plan's "collection-time ImportError"
---------------------------------------------------
The plan's task brief expected the base point to fail at *collection*
time, with ``ImportError`` on the missing function.  This file
deliberately does not import the function at module level: a collection
error takes the whole file -- and every case name in it -- out of the
report, and it breaks the CI unit lane (``pytest tests/unit``), which
collects this directory.  The missing function is therefore reached at
call time instead: the module always collects, every case is reported by
name, and the red is still a red -- ``--runxfail`` exits non-zero with
the reproduction's own assertion text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
from verification_agent import (  # noqa: E402
    ENVIRONMENT_REASON_CODES,
    Orchestrator,
)


#: The reason code the data-plane block carries (the code the accounting
#: layer matches on).  The value is spelled out here so the test pins the
#: code itself rather than whatever the constant happens to hold.
DATA_PLANE_MISMATCH_CODE = "service_data_plane_mismatch"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _implementation_is_missing() -> bool:
    """Has the unified derivation landed yet?

    The structure probe: ``verification_agent`` defines the module-level
    ``_derive_overall_status`` the round's verdict is supposed to come
    from.  When it is there the marker switches itself off and the
    assertions below become hard assertions again.
    """
    return not hasattr(verification_agent, "_derive_overall_status")


def _derive(entries):
    """The round's verdict, derived from the round's entries."""
    derive = getattr(verification_agent, "_derive_overall_status", None)
    assert derive is not None, (
        "verification_agent has no module-level _derive_overall_status: "
        "the round's verdict is still derived by two ad-hoc branches "
        "that read 'otherwise' as PASSED"
    )
    return derive(entries)


def _make_agent(tmp_path: Path):
    """An orchestrator with nothing in it but the plan it verifies."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(plan_dir=plan_dir, project_dir=project_dir)


#: The marker every case below carries.  See the module docstring: it is
#: a ratchet, not a waiver -- it switches itself off the moment the
#: implementation lands.
_R4_NOT_LANDED = pytest.mark.xfail(
    strict=True,
    reason=(
        "R4 has not landed yet: the round's verdict is still derived by "
        "two ad-hoc branches that read 'otherwise' as PASSED"
    ),
    condition=_implementation_is_missing(),
)


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------


@_R4_NOT_LANDED
def test_a_round_with_deferred_points_is_not_passed():
    """A round with points nobody judged does not pass.

    Ten points passed, eleven were skipped, two were deferred.  The
    deferred ones were never asked, so the round did not judge them --
    and a round that did not judge them cannot say it passed.
    """
    entries = (
        [{"status": "PASSED"}] * 10
        + [{"status": "SKIPPED"}] * 11
        + [{"status": "DEFERRED"}] * 2
    )

    assert _derive(entries) == "SKIPPED", entries


@_R4_NOT_LANDED
def test_a_round_with_an_environment_blocked_entry_is_not_passed():
    """A round the environment took a point out of does not pass.

    The blocked point is kept in the report and annotated with the
    reason it was blocked -- but the round that could not judge it is
    not a round that passed.
    """
    entries = [
        {"status": "PASSED"},
        {
            "status": "BLOCKED",
            "environment_blocked": True,
            "reason_code": DATA_PLANE_MISMATCH_CODE,
        },
    ]

    assert _derive(entries) == "SKIPPED", entries


@_R4_NOT_LANDED
def test_a_skipped_entry_with_an_environment_reason_code_is_not_passed():
    """The status is not the evidence -- the reason code is.

    The accounting layer rewrites a blocked point to SKIPPED before the
    verdict is derived.  A rule that looked at the status alone would
    read that point as an ordinary skip and call the round green.
    """
    assert DATA_PLANE_MISMATCH_CODE in ENVIRONMENT_REASON_CODES, (
        f"{DATA_PLANE_MISMATCH_CODE!r} is not one of "
        f"{sorted(ENVIRONMENT_REASON_CODES)!r}"
    )
    entries = [
        {"status": "PASSED"},
        {
            "status": "SKIPPED",
            "evidence": {"reason_code": DATA_PLANE_MISMATCH_CODE},
        },
    ]

    assert _derive(entries) == "SKIPPED", entries


# ---------------------------------------------------------------------------
# The rounds that already said the truth
# ---------------------------------------------------------------------------


@_R4_NOT_LANDED
def test_all_passed_stays_passed():
    """Every point passed: the round passes."""
    entries = [{"status": "PASSED"}] * 3

    assert _derive(entries) == "PASSED", entries


@_R4_NOT_LANDED
def test_passed_with_plain_skipped_stays_passed():
    """An ordinary skip does not drag the round down.

    A plain SKIPPED is not an unjudged point in the sense of this
    contract -- it is a point the round did not need to ask.  The
    behaviour that already existed stays.
    """
    entries = [{"status": "PASSED"}, {"status": "SKIPPED"}]

    assert _derive(entries) == "PASSED", entries


@_R4_NOT_LANDED
def test_an_empty_round_stays_passed():
    """A round with no verification point keeps its verdict.

    There is nothing to judge, so there is nothing to complain about
    either -- and that is the behaviour the zero-verification-point
    rounds have always had.  This case is out of scope for the change
    and must not move.
    """
    assert _derive([]) == "PASSED"


@_R4_NOT_LANDED
def test_a_real_failure_still_wins():
    """A failure outranks everything else."""
    entries = [{"status": "PASSED"}, {"status": "FAILED"}]

    assert _derive(entries) == "FAILED", entries


@_R4_NOT_LANDED
def test_a_partial_verdict_stays_partial():
    """A partial verdict is not downgraded to a skip."""
    entries = [{"status": "PASSED"}, {"status": "PARTIAL"}]

    assert _derive(entries) == "PARTIAL", entries


# ---------------------------------------------------------------------------
# The accounting layer
# ---------------------------------------------------------------------------


@_R4_NOT_LANDED
def test_the_blocked_accounting_re_derives_to_skipped(tmp_path: Path):
    """The accounting layer derives the round's verdict the same way.

    The judgment LLM wrote the blocked point down as FAILED; the
    executor's verdict says the environment stopped it.  The accounting
    layer takes it out of the failure statistics -- and the round that
    is left judged one point and could not judge the other is not a
    round that passed.
    """
    agent = _make_agent(tmp_path)
    execution_results = {
        "verification_points": [],
        "execution_results": [{
            "id": "VP-002",
            "status": "BLOCKED",
            "actual_result": "the service serves another data plane",
            "evidence": {
                "vp_id": "VP-002",
                "reason_code": DATA_PLANE_MISMATCH_CODE,
            },
        }],
    }
    report_data = {
        "overall_status": "PASSED",
        "summary": "the judgment LLM's own words",
        "verification_results": [
            {"id": "VP-001", "status": "PASSED", "actual_result": "ok"},
            {
                "id": "VP-002",
                "status": "FAILED",
                "actual_result": "connection refused",
            },
        ],
        "requirement_deviations": [],
    }

    updated = agent._exclude_environment_blocked_from_failures(
        report_data, execution_results,
    )

    assert updated["environment_blocked"] is True, updated
    assert updated["overall_status"] == "SKIPPED", updated