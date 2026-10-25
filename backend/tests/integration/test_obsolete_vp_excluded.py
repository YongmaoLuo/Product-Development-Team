"""Integration tests: a point the plan retired gets no repair task.

Why this file exists
====================

The plan can retire a verification point. ``verification_plan_delta``
writes ``obsolete`` / ``obsolete_reason`` onto it — the point is
**marked**, never deleted, because the audit trail matters — and the
executor skips it (``verification_executor._build_items_from_plan``):
running a check the plan no longer requires would manufacture a
failure for something nobody asked for.

The repair path did not know that. It read the **report**, and the
report still lists the retired point as FAILED — the round that
retired it is the round that saw it fail. So the retired point came
back as a repair task: work for a judgement nobody can satisfy.

Worse, the retired point's failure kept feeding the same-cause
counter. A criterion that no longer exists ended the chain with "the
same failure, again" — a verdict about a failure that is not a
failure any more, bought with a triage call to decide what to do about
it.

Two contracts are pinned here:

1. **A retired point produces no repair task.** The round still repairs
   the points that are alive, so the assertion is not vacuous.
2. **A round whose every failure is retired does not stop.** The
   same-cause counter must not be moved by a failure against a
   criterion that is gone: no triage call, no chain-ending verdict.

The predicate reads the **plan file's** points (``verification_points``
— the agent schema), not the executor's converted list: the conversion
(``verification_agent._convert_plan_to_executor_schema``) forwards
``obsolete`` but drops ``obsolete_reason``, and a reason alone is
enough to retire a point.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Make ``backend/`` importable regardless of the test runner's entry
# point (``verification`` is a package under ``backend/``).
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification import VerificationOrchestrator  # noqa: E402


#: The point the plan retired.
OBSOLETE_VP_ID = "VP-A"

#: A point that is still alive. It exists so the "no repair task for the
#: retired point" assertion is not vacuous: the round must still repair
#: this one.
LIVE_VP_ID = "VP-B"

#: The reason the plan retired the point.
OBSOLETE_REASON = "the feature this criterion checked was dropped"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point the state machine at a hermetic per-test SQLite file.

    Without the override the orchestrator would write the ``test-plan``
    row into the repo's real ``state.db``.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def verif_repo(state_db):
    """The verdict store — the round's verdicts land here."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(state_db)
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert("test-plan", verification_status="running", verdicts=[])
    try:
        yield repo
    finally:
        conn.close()


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the round's repair tasks are observable.

    The generator **echoes** the candidates it was handed (a candidate
    the orchestrator dropped must produce no task) and the assembler
    turns each content row into a task.

    The report reader is **not** mocked: the round reads the report
    from disk, so the test exercises the real path from "the report
    lists the point as FAILED" to "the round decided what to repair".
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as mock_generator, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_generator.return_value.generate_repair_contents.side_effect = (
            lambda candidates, *args, **kwargs: [
                {
                    "failed_vp_id": str(candidate.get("id")),
                    "title": f"fix {candidate.get('id')}",
                    "description": f"fix {candidate.get('id')}",
                    "files_to_modify": ["src/app.py"],
                    "test_command": "pytest -q",
                }
                for candidate in candidates
            ]
        )
        mock_assembler.return_value.assemble.side_effect = (
            lambda contents: [
                {
                    "id": f"RP-{index}",
                    "failed_vp_id": str(content.get("failed_vp_id")),
                    "title": str(content.get("title") or ""),
                    "files_to_modify": ["src/app.py"],
                    "test_command": "pytest -q",
                }
                for index, content in enumerate(contents, start=1)
            ]
        )
        yield mock_generator


# ---------------------------------------------------------------------------
# The scenario on disk
# ---------------------------------------------------------------------------


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3):
    """Bootstrap the plan state the way a real plan carries it."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase not in ("interview",) else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


def _live_point(vp_id):
    """A point the plan still requires."""
    return {
        "id": vp_id,
        "title": f"the {vp_id} criterion",
        "verification_method": "api_test",
        "related_prd_criteria": "AC-1",
        "expected_result": "the endpoint returns 200",
        "verification_phase": 1,
    }


def _retired_point(vp_id, reason=OBSOLETE_REASON):
    """A point the plan retired: marked ``obsolete``, never deleted."""
    point = _live_point(vp_id)
    point["obsolete"] = True
    point["obsolete_reason"] = reason
    return point


def _write_plan(plan_dir, points):
    """The verification plan — the agent schema, with its marks."""
    payload = {
        "plan_id": plan_dir.name,
        "verification_points": points,
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _report(vp_ids=(OBSOLETE_VP_ID, LIVE_VP_ID)):
    """A round report: every point failed."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": vp_id,
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "actual_result": "AssertionError: expected 200, got 500",
                "evidence": "AssertionError: expected 200, got 500",
            }
            for vp_id in vp_ids
        ],
    }


def _write_report(plan_dir, report):
    """The round's report on disk — what the repair path reads."""
    (plan_dir / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class _FakeJudge:
    """A triage judge that answers with a fixed conclusion.

    The answer is deliberately **inconclusive**: a judge that cannot
    name the failure stops the chain. That is what makes the judge a
    negative control — if the round asks it about a point the plan has
    already retired, the round ends on a verdict about a failure that
    is not a failure any more.
    """

    def __init__(self, conclusion=None):
        self._conclusion = conclusion
        self.calls = []

    def judge(self, vp, failure_history, round_number=0):
        self.calls.append({
            "vp": dict(vp) if isinstance(vp, dict) else vp,
            "failure_history": list(failure_history or []),
            "round_number": round_number,
        })
        return self._conclusion


def _inconclusive_conclusion(vp_id):
    """A judge that could not conclude what the failure is."""
    return {
        "vp_id": vp_id,
        "failure_class": "deliverable_defect",
        "conclusive": False,
        "evidence_quote": "AssertionError: expected 200, got 500",
        "rationale": "the recorded output does not say what went wrong",
    }


def _orchestrator(plan_dir, project_dir, coding_tool, verif_repo, judge=None):
    """The orchestrator under test, with the judge injected."""
    return VerificationOrchestrator(
        plan_dir, project_dir, coding_tool,
        verif_repo=verif_repo, triage_judge=judge,
    )


def _run_round(orchestrator, report, round_number):
    """One verification round: start the cycle, then classify it."""
    orchestrator.start_verification_cycle(round_number=round_number)
    return orchestrator.check_cycle_conditions(report, round_number=round_number)


def _repair_vp_ids(result):
    return [task.get("failed_vp_id") for task in result.get("repair_tasks") or []]


# ---------------------------------------------------------------------------
# 1. A retired point produces no repair task
# ---------------------------------------------------------------------------


def test_obsolete_excluded(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo,
):
    """A point the plan retired is not repaired — the live one still is.

    The report lists both points as FAILED (the round that retired the
    point is the round that saw it fail), but the retired point's
    criterion no longer exists: repairing it would be work nobody asked
    for. The live point's failure must still be repaired, so the
    assertion is not vacuous.
    """
    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir, [
        _retired_point(OBSOLETE_VP_ID),
        _live_point(LIVE_VP_ID),
    ])
    report = _report()
    _write_report(temp_plan_dir, report)

    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, verif_repo,
    )

    result = _run_round(orchestrator, report, round_number=1)

    repaired = _repair_vp_ids(result)
    assert OBSOLETE_VP_ID not in repaired, (
        "a point the plan retired must not produce a repair task"
    )
    assert LIVE_VP_ID in repaired, (
        "the round's live failure must still be repaired"
    )


# ---------------------------------------------------------------------------
# 2. A round whose every failure is retired does not stop
# ---------------------------------------------------------------------------


def test_all_obsolete_no_false_stop(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo,
):
    """A failure against a retired criterion is not a same-cause failure.

    The same-cause counter fires on the second round with the same
    failure. A retired point keeps failing in the report forever (the
    report still lists it), so without the filter the counter reaches
    its cap on a failure against a criterion that no longer exists —
    and the round ends the chain on a verdict about a failure that is
    not a failure any more.

    So: no repair task, no triage call, no stop.
    """
    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir, [_retired_point(OBSOLETE_VP_ID)])
    report = _report(vp_ids=(OBSOLETE_VP_ID,))
    _write_report(temp_plan_dir, report)

    judge = _FakeJudge(conclusion=_inconclusive_conclusion(OBSOLETE_VP_ID))
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, verif_repo, judge,
    )

    first = _run_round(orchestrator, report, round_number=1)
    second = _run_round(orchestrator, report, round_number=2)

    assert first["repair_tasks"] == [], (
        "a retired point must not produce a repair task"
    )
    assert second["repair_tasks"] == [], (
        "a retired point must not produce a repair task"
    )
    assert second["should_stop"] is False, (
        "a failure against a retired criterion must not stop the chain"
    )
    assert second["stop_reason"] is None, (
        "a failure against a retired criterion must not be reported as a "
        "same-cause termination"
    )
    assert judge.calls == [], (
        "the round must not buy a triage call for a failure against a "
        "criterion that no longer exists"
    )


# ---------------------------------------------------------------------------
# 3. A reason alone retires the point
# ---------------------------------------------------------------------------


def test_obsolete_reason_alone_excluded(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo,
):
    """``obsolete_reason`` without the flag still retires the point.

    The executor's converted schema forwards ``obsolete`` but drops
    ``obsolete_reason``, so a predicate that only looked at the
    executor's list would miss a point the plan retired with a reason
    only. The predicate reads the plan file's own points, so both
    marks count.
    """
    _write_plan_state(temp_plan_dir)
    reason_only = _live_point(OBSOLETE_VP_ID)
    reason_only["obsolete_reason"] = OBSOLETE_REASON
    _write_plan(temp_plan_dir, [reason_only, _live_point(LIVE_VP_ID)])
    report = _report()
    _write_report(temp_plan_dir, report)

    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, verif_repo,
    )

    result = _run_round(orchestrator, report, round_number=1)

    repaired = _repair_vp_ids(result)
    assert OBSOLETE_VP_ID not in repaired, (
        "a point the plan retired with a reason must not produce a "
        "repair task"
    )
    assert LIVE_VP_ID in repaired, (
        "the round's live failure must still be repaired"
    )