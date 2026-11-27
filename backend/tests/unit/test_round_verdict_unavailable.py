"""An UNKNOWN round stops with a reason of its own — not a deferral, not a block.

Background
----------
``UNKNOWN`` is the verdict a gate writes when its answer could not be
parsed and its retry budget ran out (``verification_subagent.
VERDICT_UNKNOWN``; the same word ``verification_buckets`` keeps as
``STATUS_UNKNOWN``).  It says nothing at all about the deliverable —
which is why it is not folded into ``FAILED`` (that would manufacture a
repair task for a defect nobody found) and not folded into ``PASSED``
(that would record an unverified thing as verified).  arch decision
point 1 gave it its own status; arch decision point 3 gives the round
that carries it a stop reason of its own.

The orchestrator had no word for it.  ``_round_judged_nothing`` knew
``DEFERRED``, ``INCONCLUSIVE`` and the environment-blocked signal, so an
UNKNOWN round did not even reach the "judged nothing" exit — it fell
through to the failure path.  And had it reached that exit, the exit
would have dressed it up as an environment block: ``environment_blocked
= True`` and ``stop_reason = None``, which claims the environment took
the round out when in fact the gate ran and its answer could not be
read.

What these tests pin
--------------------
 1. an UNKNOWN round stops with ``stop_reason == "verdict_unavailable"``
    and ``environment_blocked is False`` — the round says what actually
    happened instead of borrowing another round's vocabulary;
 2. a round that was **only** environment-blocked keeps the old shape
    (``environment_blocked is True``, ``stop_reason is None``);
 3. a round with a real ``FAILED`` never enters this branch — it keeps
    its repair path (the negative control);
 4. a ``DEFERRED`` point next to an ``UNKNOWN`` one does not turn the
    round back into a deferral;
 5. only ``INCONCLUSIVE`` asks the operator — an UNKNOWN round asks
    nobody;
 6. ``check_cycle_conditions`` returns that exit verbatim, and the plan
    is not recorded as a failed verification.

The interface contract
-----------------------
``_round_judged_nothing(report, failed_count)`` learns the word
``UNKNOWN``; ``_judged_nothing_cycle_result(report)`` — the single
source of the judged-nothing exit — carries the new stop reason.  The
orchestrator module grows the constant ``STATUS_UNKNOWN`` so the
verdict vocabulary has a name on this side too.

Pure rule tests: no model, no subprocess — the only side effects are
pytest's own ``tmp_path`` and the hermetic per-test SQLite file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.orchestrator import (  # noqa: E402
    _judged_nothing_cycle_result,
    _round_judged_nothing,
)


#: The verdict a gate writes when its answer could not be parsed and its
#: retry budget ran out.  Spelled out here so the test pins the wire
#: value itself rather than whatever the constant holds.
UNKNOWN = "UNKNOWN"

#: The stop reason the judged-nothing exit carries when a point's
#: outcome was UNKNOWN.  Spelled out here so the test pins the word
#: itself rather than whatever the constant holds — downstream
#: classification reads this spelling.
VERDICT_UNAVAILABLE = "verdict_unavailable"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    Without the override the orchestrator would write its row into the
    repo's real ``state.db`` and read back whatever an earlier test left
    behind.  Autouse, so no test in this module can leak into — or
    inherit from — another.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


def _unknown_report(*vp_ids: str) -> dict:
    """A round whose points all came back UNKNOWN.

    The gate ran; its answer could not be parsed and the retry budget
    ran out.  The round therefore has no verdict about the deliverable
    — and it did not fail either.
    """
    return {
        "overall_status": "SKIPPED",
        "verification_results": [
            {"vp_id": vp_id, "status": UNKNOWN}
            for vp_id in (vp_ids or ("VP-017",))
        ],
    }


def _environment_blocked_report() -> dict:
    """A round the environment took a point out of — and nothing else.

    ``VP-1`` carries the reason code the environment owns; ``VP-2`` was
    deferred.  Neither point was judged, and neither came back UNKNOWN.
    """
    return {
        "overall_status": "SKIPPED",
        "environment_blocked": True,
        "verification_results": [
            {
                "vp_id": "VP-1",
                "status": "BLOCKED",
                "evidence": {"reason_code": "service_data_plane_mismatch"},
            },
            {"vp_id": "VP-2", "status": "DEFERRED"},
        ],
    }


def _failed_report() -> dict:
    """A round that found a real defect — plus an UNKNOWN point.

    The failure wins: the round judged something, and it judged it
    badly.  It must keep its repair path.
    """
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {"vp_id": "VP-9", "status": "FAILED"},
            {"vp_id": "VP-017", "status": UNKNOWN},
        ],
    }


def _write_plan_state(plan_dir: Path) -> None:
    """Bootstrap a plan state the orchestrator can start a cycle on."""
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "current_phase": "executing",
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. The UNKNOWN round stops with a reason of its own
# ---------------------------------------------------------------------------


def test_unknown_round_stops_with_verdict_unavailable():
    """The round says what happened: the verdict was unavailable.

    Not "the environment took this round out" — the environment was
    fine, the gate ran and its answer could not be read.
    """
    report = _unknown_report()

    assert _round_judged_nothing(report, 0) is True, report

    result = _judged_nothing_cycle_result(report)

    assert result["should_continue"] is False, result
    assert result["should_stop"] is True, result
    assert result["stop_reason"] == VERDICT_UNAVAILABLE, result
    assert result["environment_blocked"] is False, result
    assert result["status"] == "loop_stopped", result
    assert result["repair_tasks"] == [], result


def test_unknown_round_does_not_ask_the_operator():
    """Only ``INCONCLUSIVE`` asks the operator — an UNKNOWN round asks nobody.

    An UNKNOWN point says its answer could not be parsed; it does not
    say a gate ran out of budget.  The operator's turn belongs to the
    budget question alone.
    """
    result = _judged_nothing_cycle_result(_unknown_report())

    assert result["needs_operator_decision"] is False, result
    assert result["budget_exhausted_in"] is None, result


def test_deferred_and_unknown_round_reports_verdict_unavailable():
    """A deferred point next to an UNKNOWN one does not hide the UNKNOWN.

    The round's own verdict is the more specific statement: the point
    was deferred on purpose *and* another point's outcome could not be
    read.
    """
    report = {
        "overall_status": "SKIPPED",
        "verification_results": [
            {"vp_id": "VP-2", "status": "DEFERRED"},
            {"vp_id": "VP-017", "status": UNKNOWN},
        ],
    }

    assert _round_judged_nothing(report, 0) is True, report

    result = _judged_nothing_cycle_result(report)

    assert result["stop_reason"] == VERDICT_UNAVAILABLE, result
    assert result["environment_blocked"] is False, result


# ---------------------------------------------------------------------------
# 2. The environment-blocked round keeps the old shape
# ---------------------------------------------------------------------------


def test_environment_blocked_round_keeps_old_shape():
    """A round the environment took out of says exactly that.

    ``environment_blocked`` stays true and the stop reason stays empty:
    the round's status is the verdict, and this stop carries no new
    word.
    """
    report = _environment_blocked_report()

    assert _round_judged_nothing(report, 0) is True, report

    result = _judged_nothing_cycle_result(report)

    assert result["environment_blocked"] is True, result
    assert result["stop_reason"] is None, result
    assert result["status"] == "loop_stopped", result
    assert result["needs_operator_decision"] is False, result


# ---------------------------------------------------------------------------
# 3. The negative control — a real failure keeps its repair path
# ---------------------------------------------------------------------------


def test_failed_round_skips_judged_nothing_branch():
    """A round that found a defect never enters the judged-nothing exit.

    The failure is checked first, so the UNKNOWN point sitting next to
    it cannot swallow the round.
    """
    report = _failed_report()

    assert _round_judged_nothing(report, 1) is False, report


# ---------------------------------------------------------------------------
# 4. The orchestrator returns that exit
# ---------------------------------------------------------------------------


def test_the_orchestrator_names_the_unknown_verdict():
    """The orchestrator's own vocabulary has a name for the verdict.

    The word is spelled in three places (``verification_subagent``,
    ``verification_buckets``, and here) — the orchestrator must not be
    the one that calls it something else.
    """
    import verification.orchestrator as orchestrator_module

    assert orchestrator_module.STATUS_UNKNOWN == UNKNOWN


def test_the_orchestrator_returns_the_same_exit(tmp_path):
    """``check_cycle_conditions`` is the judged-nothing exit's only source.

    The round must not be recorded as a failed verification: nothing was
    broken, so there is nothing to repair.
    """
    from plan_state import PlanState
    from verification.orchestrator import VerificationOrchestrator

    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_plan_state(plan_dir)

    report = _unknown_report()

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(plan_dir, project_dir, Mock())
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    # 2026-11-27: an unjudged round now asks the operator what happens
    # next (the question the terminal branch used to be the only source
    # of), so the exit says a question is waiting — the rest of the
    # judged-nothing exit is unchanged.
    expected = _judged_nothing_cycle_result(report)
    expected["waiting_for_user"] = True
    expected["needs_operator_decision"] = True
    assert result == expected, result
    assert result["stop_reason"] == VERDICT_UNAVAILABLE, result
    assert result["environment_blocked"] is False, result
    assert result["repair_tasks"] == [], result

    ps = PlanState(plan_dir)
    assert ps.get_current_phase() != "verification_failed", (
        f"没判出裁决的一轮不得被记成验证失败，实际相位是 "
        f"{ps.get_current_phase()!r}"
    )
    assert ps.get_verification_status() != "failed", (
        f"没判出裁决的一轮不得被记成一次失败的验证，实际状态是 "
        f"{ps.get_verification_status()!r}"
    )
