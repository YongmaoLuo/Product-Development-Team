"""Which points the terminal branch calls failed.

The terminal branch asks a human what happens next.  The question it
asks carries **the points that failed** — and a point that is not in
that list is a point the human is not told about.  So the list has to
be exactly the points that did not pass this round.

One source feeds that list — **the round's failed ids** —
``_extract_failed_ids`` unions the top-level results with the last
round's snapshot, and the snapshot is the *older* reading.  A point
that failed last round and passed this round is still in the union.

(2026-11-06: the root-cause chain's blocked points used to be a
second source.  The chain is deleted, so there is no second source:
the round's own failed set is the whole list of what a human has to
decide about.)

The 2026-11-05 terminal decision is what this is about: its question
read ``failed checks: VP-002=FAILED, VP-004=FAILED, VP-017=DEFERRED,
VP-020=PASSED, VP-021=PASSED, VP-022=FAILED, VP-023=FAILED`` — two
points that had **passed** were listed as failed.

The rule this module pins down:

* **The latest reading wins.**  A point whose newest entry says it
  passed is not a failure, whoever named it.
* **Three statuses are not failures**: ``PASSED``, ``SKIPPED`` and
  ``DEFERRED``.  Everything else is one — including ``SPLIT``, which
  the executor calls a terminal success but the failed-id collection
  has always counted as a failure.
* **A point the report says nothing about is kept.**  No evidence of
  a pass is no reason to drop it from the list of what a human has to
  decide about.
* **The comparison ignores case and padding.**  ``"passed"`` and
  ``"PASSED "`` are the same reading.

The orchestrator is built with its heavy collaborators replaced and its
judge faked: the selection reads the report it is handed and nothing
else, so a real agent or a real model call would only add noise.
"""

from unittest.mock import Mock

import pytest

from verification.orchestrator import VerificationOrchestrator


class _FakeJudge:
    """A judge with no analysis to offer — the factual question stands."""

    def analyze(self, **kwargs):
        return None


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """An orchestrator whose heavy collaborators and judge are replaced."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=tmp_path,
        coding_tool=Mock(),
        terminal_judge=_FakeJudge(),
    )


def test_only_failed_points_survive_a_mixed_report(orchestrator):
    """A passed point and a deferred point are not failures."""
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "FAILED"},
            {"id": "VP-002", "status": "PASSED"},
            {"id": "VP-003", "status": "DEFERRED"},
        ],
    }

    ids = orchestrator._terminal_failed_vp_ids(
        report,
        {"VP-001", "VP-002", "VP-003"},
    )

    assert ids == ["VP-001"]

    question = orchestrator._terminal_decision_question(report, 3, ids)[
        "question"
    ]
    assert "VP-001=FAILED" in question
    assert "VP-002" not in question, "a passed point was listed as failed"
    assert "VP-003" not in question, "a deferred point was listed as failed"

    assert orchestrator._terminal_deferred_count(report) == 1, (
        "the deferred point is counted as deferred, not as failed"
    )


def test_passed_point_is_excluded_from_question_and_failed_points(
    orchestrator,
):
    """A point that has since passed is not in the failure list."""
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "FAILED"},
            {"id": "VP-009", "status": "PASSED"},
        ],
    }

    ids = orchestrator._terminal_failed_vp_ids(
        report, {"VP-001", "VP-009"},
    )

    assert "VP-009" not in ids

    question = orchestrator._terminal_decision_question(report, 1, ids)[
        "question"
    ]
    assert "VP-009" not in question
    assert "VP-001=FAILED" in question

    points = orchestrator._terminal_failed_points(report, ids)
    assert [point["id"] for point in points] == ["VP-001"]


def test_stale_snapshot_failure_is_excluded(orchestrator):
    """A point that failed last round and passed this round is not failed.

    ``_extract_failed_ids`` unions the top-level results with the last
    round's snapshot, so the older FAILED reading survives in the
    failed-id set even though the point has since passed.
    """
    report = {
        "verification_results": [
            {"id": "VP-002", "status": "PASSED"},
        ],
        "rounds": [
            {"results": [{"id": "VP-002", "status": "FAILED"}]},
        ],
    }
    current_failed_ids = orchestrator._extract_failed_ids(report)
    assert "VP-002" in current_failed_ids, (
        "the fixture must reproduce the stale reading the failed-id set keeps"
    )

    ids = orchestrator._terminal_failed_vp_ids(
        report, current_failed_ids,
    )

    assert ids == []

    question = orchestrator._terminal_decision_question(report, 1, ids)[
        "question"
    ]
    assert "VP-002" not in question


def test_real_terminal_decision_regression(orchestrator):
    """The 2026-11-05 decision, as it was asked.

    Seven points: four failed, one deferred, two passed — and the
    failed-id set named all seven.  Only the four failures belong in
    the question.
    """
    report = {
        "verification_results": [
            {"id": "VP-002", "status": "FAILED"},
            {"id": "VP-004", "status": "FAILED"},
            {"id": "VP-017", "status": "DEFERRED"},
            {"id": "VP-020", "status": "PASSED"},
            {"id": "VP-021", "status": "PASSED"},
            {"id": "VP-022", "status": "FAILED"},
            {"id": "VP-023", "status": "FAILED"},
        ],
    }
    ids = orchestrator._terminal_failed_vp_ids(
        report,
        {"VP-002", "VP-004", "VP-017",
         "VP-020", "VP-021", "VP-022", "VP-023"},
    )

    assert ids == ["VP-002", "VP-004", "VP-022", "VP-023"]
    assert orchestrator._terminal_deferred_count(report) == 1

    question = orchestrator._terminal_decision_question(report, 3, ids)[
        "question"
    ]
    assert "VP-002=FAILED" in question
    assert "VP-004=FAILED" in question
    assert "VP-022=FAILED" in question
    assert "VP-023=FAILED" in question
    assert "VP-017" not in question, "a deferred point was listed as failed"
    assert "VP-020" not in question, "a passed point was listed as failed"
    assert "VP-021" not in question, "a passed point was listed as failed"


def test_point_missing_from_report_is_kept(orchestrator):
    """A point the report says nothing about is kept."""
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "FAILED"},
        ],
    }

    ids = orchestrator._terminal_failed_vp_ids(
        report, {"VP-001", "VP-777"},
    )

    assert ids == ["VP-001", "VP-777"], (
        "no reading means no evidence of a pass — the point stays"
    )


def test_verdict_field_counts_as_status(orchestrator):
    """A verdict is a status when the entry carries no status of its own."""
    report = {
        "verification_results": [
            {"id": "VP-9", "verdict": "passed"},
            {"id": "VP-10", "status": "PASSED "},
        ],
    }

    ids = orchestrator._terminal_failed_vp_ids(
        report, {"VP-9", "VP-10"},
    )

    assert ids == [], (
        "the reading is case- and padding-insensitive, and a verdict "
        "counts as a status"
    )


def test_skipped_point_is_not_a_failure(orchestrator):
    """A skipped point is done, not failed."""
    report = {
        "verification_results": [
            {"id": "VP-005", "status": "SKIPPED"},
        ],
    }

    ids = orchestrator._terminal_failed_vp_ids(
        report, {"VP-005"},
    )

    assert ids == []
