"""Integration tests for the criterion-replacement branch of the triage.

Why this file exists
====================
The semantic triage (see :mod:`verification.triage_agent`) can name a
failure as a **criterion defect** — the check itself is wrong, so no
amount of rework can satisfy it. Until now the loop's only answer was
to stop and wait for a human, which is right when nobody can say what
the criterion *should* be, and useless when the triage already carries
the answer: the judge is asked for a ``replacement`` criterion
alongside the verdict.

This file pins the branch that turns that answer into a change on
disk. Three contracts are pinned, and they are the ones that decide
whether the loop may act at all:

1. **The replacement lands as a pair.** The broken criterion is marked
   ``obsolete`` (kept, not deleted — the audit trail matters) and a new
   point is added that points back at it through ``replacement_for``.
   The OBSOLETE verdict is recorded **after** the plan file landed, so
   the record can never claim something the plan does not say. And the
   obsoleted point gets **no repair task**: the criterion it failed
   against is gone, so reworking the deliverable for it is work nobody
   asked for.
2. **A weaker replacement is refused.** Replacing a real check with a
   rubber stamp is the mistake this guard exists to prevent, so the
   plan is not touched **at all** (zero writes) and the failure is
   downgraded to a deliverable defect: the loop keeps repairing.
3. **A failed write records nothing.** If the delta cannot be
   persisted, the disk and the verdict store must both still say "the
   criterion is unchanged" — a verdict that outlives the change it
   describes is worse than no verdict.
4. **A refused replacement is audited.** "Nothing was written" is not
   the same as "nothing happened": the operator has to be able to read
   *why* the criterion was not replaced — the write failed, or the
   proposal checked less than the criterion it replaces. So the refusal
   leaves an audit event on disk and a reason in the round's own
   record, while the plan itself stays untouched.

A triage that cannot conclude, and a triage that malfunctioned, are
pinned by ``tests/integration/test_same_failure_triage.py``; what is
pinned here is what happens **after** a conclusive "criterion defect".
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


#: The point whose criterion is broken.
VP_ID = "VP-007"

#: A second point with a genuine deliverable defect. It exists so the
#: "no repair task for the obsoleted point" assertion is not vacuous:
#: the round must still repair this one.
OTHER_VP_ID = "VP-008"

#: The class the report records for the broken criterion.
FAILURE_CLASS = "criterion_defect"

#: The recorded failure text — quotable verbatim by the judge.
ACTUAL_RESULT = "AssertionError: the response schema was rejected"


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
    """The verdict store — the OBSOLETE verdict lands in ``plan_verification``.

    A real repository over the hermetic SQLite file, so the assertion
    reads the same column the production path writes.
    """
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
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as mock_generator, \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {"id": VP_ID, "title": "the broken criterion", "priority": "high"},
            {
                "id": OTHER_VP_ID,
                "title": "a genuine deliverable defect",
                "priority": "medium",
            },
        ]
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
        yield mock_extract


@pytest.fixture
def plan_write_spy(monkeypatch):
    """Record every atomic JSON write, and let it through.

    The plan file is the one write this branch has to get right, so the
    spy names it. That turns "the plan was not touched" from a claim
    about the file's *content* into a claim about the *attempt* — and it
    turns "the write failed" into something provable rather than
    something that merely happened to fail somewhere.
    """
    from utils import atomic_io

    original = atomic_io.atomic_write_json
    written = []

    def spy(target, *args, **kwargs):
        written.append(str(target))
        return original(target, *args, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_json", spy)
    return written


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


def _write_plan(plan_dir):
    """The verification plan: the broken criterion and a healthy point."""
    payload = {
        "plan_id": plan_dir.name,
        "verification_points": [
            {
                "id": VP_ID,
                "title": "the broken criterion",
                "verification_method": "api_test",
                "related_prd_criteria": "AC-1",
                "expected_result": "the response matches the schema",
                "verification_phase": 1,
            },
            {
                "id": OTHER_VP_ID,
                "title": "a genuine deliverable defect",
                "verification_method": "api_test",
                "related_prd_criteria": "AC-2",
                "expected_result": "the endpoint returns 200",
                "verification_phase": 1,
            },
        ],
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _read_plan(plan_dir):
    return json.loads(
        (plan_dir / "verification_plan.json").read_text(encoding="utf-8"),
    )


def _by_id(plan):
    return {
        str(vp.get("id")): vp
        for vp in plan.get("verification_points") or []
        if isinstance(vp, dict)
    }


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class _FakeJudge:
    """A triage judge that answers with a fixed conclusion.

    ``raises`` makes the judge malfunction — the path where the
    provider blew up, so there is no verdict at all.
    """

    def __init__(self, conclusion=None, raises=None):
        self._conclusion = conclusion
        self._raises = raises
        self.calls = []

    def judge(self, vp, failure_history, round_number=0):
        self.calls.append({
            "vp": dict(vp) if isinstance(vp, dict) else vp,
            "failure_history": list(failure_history or []),
            "round_number": round_number,
        })
        if self._raises is not None:
            raise self._raises
        return self._conclusion


def _criterion_conclusion(method="api_test", title="the fixed criterion"):
    """A conclusive "criterion defect" that proposes a replacement."""
    from verification.triage_agent import TriageConclusion

    return TriageConclusion(
        vp_id=VP_ID,
        failure_class=FAILURE_CLASS,
        conclusive=True,
        evidence_quote=ACTUAL_RESULT,
        rationale="the criterion asserts a schema the API never had",
        replacement={
            "title": title,
            "verification_method": method,
            "expected_result": "the endpoint returns 200",
            "related_prd_criteria": "AC-1",
        },
    )


def _report():
    """A round report: the broken criterion and a genuine defect."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "failure_class": FAILURE_CLASS,
                "actual_result": ACTUAL_RESULT,
                "evidence": ACTUAL_RESULT,
            },
            {
                "id": OTHER_VP_ID,
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "actual_result": "AssertionError: expected 200, got 500",
                "evidence": "AssertionError: expected 200, got 500",
            },
        ],
    }


def _orchestrator(plan_dir, project_dir, coding_tool, judge, verif_repo):
    """The orchestrator under test, with the judge and the store injected."""
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
# 1. The replacement lands as a pair
# ---------------------------------------------------------------------------


def test_confirm_obsoletes_and_replaces(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo,
):
    """A criterion defect with a usable replacement is replaced on disk.

    Three things must agree when the round ends: the **plan file** (the
    old criterion is obsolete, the new one points back at it), the
    **verdict store** (the OBSOLETE verdict names both), and the
    **repair tasks** (the obsoleted point gets none — the criterion it
    failed against is gone).
    """
    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir)
    judge = _FakeJudge(conclusion=_criterion_conclusion())
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge, verif_repo,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    assert len(judge.calls) == 1, "the threshold buys exactly one triage call"

    # (1) The plan on disk: the old criterion is obsolete (kept, not
    # deleted) and the new point points back at it.
    by_id = _by_id(_read_plan(temp_plan_dir))
    assert by_id[VP_ID].get("obsolete") is True, (
        "the broken criterion must be marked obsolete on disk"
    )
    replacements = [
        vp for vp in by_id.values() if vp.get("replacement_for") == VP_ID
    ]
    assert len(replacements) == 1, (
        "exactly one replacement point must point back at the obsoleted one"
    )
    new_id = replacements[0]["id"]
    assert new_id != VP_ID

    # (2) The verdict store: the OBSOLETE verdict is the last one, and it
    # names the point, the class and the replacement.
    verdicts = verif_repo.current("test-plan")["verdicts"]
    assert len(verdicts) == 1, (
        "the round's only verdict is the OBSOLETE one — nothing else "
        "claimed anything about this point"
    )
    assert verdicts[-1] == {
        "vp_id": VP_ID,
        "failure_class": FAILURE_CLASS,
        "replaced_by": new_id,
    }, "the OBSOLETE verdict must name the point, the class and its replacement"

    # (3) The repair tasks: the obsoleted point gets none, the genuine
    # defect still does (so the assertion above is not vacuous).
    repaired = _repair_vp_ids(result)
    assert VP_ID not in repaired, (
        "a criterion that was replaced must not produce a repair task"
    )
    assert OTHER_VP_ID in repaired, (
        "the round's other failure must still be repaired"
    )


# ---------------------------------------------------------------------------
# 2. A weaker replacement is refused
# ---------------------------------------------------------------------------


def test_weaker_method_rejected(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo, plan_write_spy,
):
    """A replacement that checks less than the original is refused.

    The plan must not be touched **at all** — not the obsolete mark, not
    the new point — and the failure is downgraded to a deliverable
    defect: the loop keeps working on the deliverable instead of
    stopping on a criterion nobody can repair.
    """
    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir)
    plan_file = temp_plan_dir / "verification_plan.json"
    before = plan_file.read_bytes()
    judge = _FakeJudge(
        conclusion=_criterion_conclusion(method="ui_validation"),
    )
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge, verif_repo,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    # Zero writes: the plan file was never even attempted, and what is
    # on disk is byte-for-byte what it was.
    assert str(plan_file) not in plan_write_spy, (
        "a refused replacement must not write the plan file at all"
    )
    after = plan_file.read_bytes()
    assert after == before, (
        "a refused replacement must not touch the plan file at all"
    )
    by_id = _by_id(_read_plan(temp_plan_dir))
    assert "obsolete" not in by_id[VP_ID]
    assert not [vp for vp in by_id.values() if vp.get("replacement_for")]

    # No OBSOLETE verdict either — the plan says nothing, so the verdict
    # store must say nothing.
    verdicts = verif_repo.current("test-plan")["verdicts"]
    assert not [v for v in verdicts if v.get("vp_id") == VP_ID], (
        "a refused replacement must not record an OBSOLETE verdict"
    )

    # Downgraded to a deliverable defect: the loop keeps repairing.
    assert result["should_stop"] is False, (
        "a refused replacement downgrades the failure — it must not stop "
        "the loop"
    )
    assert VP_ID in _repair_vp_ids(result), (
        "the downgraded failure must be repaired like any other "
        "deliverable defect"
    )


# ---------------------------------------------------------------------------
# 3. A failed write records nothing
# ---------------------------------------------------------------------------


def test_persist_failure_no_obsolete_verdict(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo, monkeypatch,
):
    """A delta that cannot be written leaves no trace.

    The verdict is written **after** the plan file, so a failed write
    means there is no OBSOLETE verdict to write: the disk and the
    verdict store must both still say "the criterion is unchanged".
    """
    from utils import atomic_io

    attempts = []

    def boom(target, *args, **kwargs):
        attempts.append(str(target))
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io, "atomic_write_json", boom)

    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir)
    plan_file = temp_plan_dir / "verification_plan.json"
    judge = _FakeJudge(conclusion=_criterion_conclusion())
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge, verif_repo,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    # The write was attempted — otherwise this test would be proving
    # that something *else* refused the replacement, not that a failed
    # write leaves nothing behind.
    assert str(plan_file) in attempts, (
        "the plan write must have been attempted — otherwise the persist "
        "failure is not what this test exercises"
    )

    verdicts = verif_repo.current("test-plan")["verdicts"]
    assert not [v for v in verdicts if v.get("vp_id") == VP_ID], (
        "a replacement that was not persisted must not record an OBSOLETE "
        "verdict"
    )

    by_id = _by_id(_read_plan(temp_plan_dir))
    assert "obsolete" not in by_id[VP_ID], (
        "the criterion must not be marked obsolete when the write failed"
    )
    assert not [vp for vp in by_id.values() if vp.get("replacement_for")]

    # Downgraded to a deliverable defect — the loop keeps working.
    assert result["should_stop"] is False
    assert VP_ID in _repair_vp_ids(result)


# ---------------------------------------------------------------------------
# 4. A triage that malfunctioned never obsoletes anything
# ---------------------------------------------------------------------------


def test_triage_exception_never_obsoletes(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo, plan_write_spy,
):
    """A broken judge must not cost the plan a criterion.

    The judge is the only thing that may retire a criterion, so a judge
    that raises cannot retire one: the plan is not touched, no verdict
    is recorded, and the failure is repaired like any other. This is
    the edge the whole branch turns on — the **only** path that may
    obsolete a point is a conclusive triage carrying a usable
    replacement, and a malfunctioning judge produces no conclusion at
    all.
    """
    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir)
    plan_file = temp_plan_dir / "verification_plan.json"
    before = plan_file.read_bytes()
    judge = _FakeJudge(raises=RuntimeError("provider exploded"))
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge, verif_repo,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    assert len(judge.calls) == 1, "the threshold was reached — the judge was asked"

    # The plan is untouched — not even an attempted write.
    assert str(plan_file) not in plan_write_spy, (
        "a malfunctioning triage must not write the plan file at all"
    )
    assert plan_file.read_bytes() == before
    by_id = _by_id(_read_plan(temp_plan_dir))
    assert "obsolete" not in by_id[VP_ID], (
        "a malfunctioning triage must never obsolete a criterion"
    )
    assert not [vp for vp in by_id.values() if vp.get("replacement_for")]

    # And nothing is claimed about it either.
    verdicts = verif_repo.current("test-plan")["verdicts"]
    assert not [v for v in verdicts if v.get("vp_id") == VP_ID], (
        "a malfunctioning triage must not record an OBSOLETE verdict"
    )

    # The failure is repaired like any other — a broken triage is not
    # evidence about the failure.
    assert result["should_stop"] is False
    assert VP_ID in _repair_vp_ids(result), (
        "a malfunctioning triage must send the failure into the repair path"
    )


# ---------------------------------------------------------------------------
# 5. A refused replacement is audited
# ---------------------------------------------------------------------------


def _plan_dir_blob(plan_dir):
    """Everything the plan wrote to disk, as one string."""
    return "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in plan_dir.rglob("*")
        if path.is_file()
    )


def test_persist_failure_is_audited(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo, monkeypatch,
):
    """A write that failed leaves an audit trail — and says why.

    "The plan is unchanged" is not the same as "nothing happened": the
    criterion was supposed to be replaced and was not, and the operator
    has to be able to read *why*. So the failed write leaves an audit
    event on disk and the round's record names the reason
    (``persist_failed``).

    What it must **not** leave behind is a replacement: the plan says
    nothing about the criterion, the verdict store says nothing, and
    the round keeps repairing the failure like any other deliverable
    defect. A refusal that quietly acted as if it had succeeded would
    be the failure mode this decision point exists to kill.
    """
    import verification.orchestrator as orch_mod
    from verification_plan_delta import PlanDeltaPersistError

    def explode(*args, **kwargs):
        raise PlanDeltaPersistError("disk full while persisting the delta")

    monkeypatch.setattr(orch_mod, "apply_delta_persisted", explode)

    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir)
    judge = _FakeJudge(conclusion=_criterion_conclusion())
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge, verif_repo,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    # (1) The audit event reached the disk — it names the failure and
    # the reason the proposal was not used.
    blob = _plan_dir_blob(temp_plan_dir)
    assert "plan_delta_persist_failed" in blob, (
        "a failed write must leave an audit event on disk"
    )
    assert "persist_failed" in blob, (
        "the audit event must name why the proposal was not used"
    )

    # (2) The round's own record says the same thing.
    assert result["replacement_rejected_reason"] == "persist_failed", (
        "the round must record why the replacement was not used"
    )

    # (3) Nothing pretends the replacement happened.
    by_id = _by_id(_read_plan(temp_plan_dir))
    assert "obsolete" not in by_id[VP_ID], (
        "a failed write must not mark the criterion obsolete"
    )
    assert not [vp for vp in by_id.values() if vp.get("replacement_for")]
    verdicts = verif_repo.current("test-plan")["verdicts"]
    assert not [v for v in verdicts if v.get("vp_id") == VP_ID], (
        "a failed write must not record an OBSOLETE verdict"
    )

    # (4) The failure is still repaired — the refusal downgrades it, it
    # does not end the chain.
    assert result["should_stop"] is False
    assert VP_ID in _repair_vp_ids(result)


def test_weaker_method_is_audited(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
    verif_repo,
):
    """A replacement that checks less is refused — and the refusal is audited.

    Same contract as the failed write, one step earlier: the proposal
    never even reached the disk, but the operator still has to be able
    to read that a replacement was proposed and refused
    (``weaker_method``) rather than silently getting no replacement at
    all.
    """
    _write_plan_state(temp_plan_dir)
    _write_plan(temp_plan_dir)
    judge = _FakeJudge(conclusion=_criterion_conclusion(method="ui_validation"))
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge, verif_repo,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    blob = _plan_dir_blob(temp_plan_dir)
    assert "plan_delta_replacement_rejected" in blob, (
        "a refused replacement must leave an audit event on disk"
    )
    assert "weaker_method" in blob, (
        "the audit event must name why the proposal was not used"
    )

    assert result["replacement_rejected_reason"] == "weaker_method", (
        "the round must record why the replacement was not used"
    )

    by_id = _by_id(_read_plan(temp_plan_dir))
    assert "obsolete" not in by_id[VP_ID]
    assert not [vp for vp in by_id.values() if vp.get("replacement_for")]

    assert result["should_stop"] is False
    assert VP_ID in _repair_vp_ids(result)
