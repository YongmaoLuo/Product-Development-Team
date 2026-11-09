"""The archive's machine half says **why** there is no analysis.

Background
----------
A terminal round that produced no analysis puts the human a **fallback
card** (arch decision point 2): the same decision shape as the analysis
card, with the failed points written out by hand.  The card says why
there is no analysis — ``analysis_empty`` when there was no answer at
all, ``analysis_rejected:<reason>`` when the answer was refused — but
the card is a chat message, and it is gone when the chat is gone.

The round's record is the **archive**: the ``.md`` a person reads later
and the ``.json`` a machine reads.  So the reason has to reach the
archive as well — otherwise the file that outlives the card says
``analysis: null`` and nothing else, and a reader three weeks later
cannot tell "nothing is known" from "the analysis was refused and here
is what refused it".

What these tests pin
--------------------
1. **A non-empty reason is written.**  ``analysis_error`` appears in
   the archive's JSON, with the reason exactly as it was given.
2. **No reason, no key.**  A round that had an analysis writes the
   eight keys it always wrote — the key is *added*, never substituted,
   so a reader that knows the old shape still reads the new one.
3. **A terminal round writes its reason.**  Driven through the
   production entry: a judge whose answer was refused leaves the reason
   in the round's JSON, and the round's ``analysis`` is ``null``.

The judge is a fake: the real one is a model call, and what is pinned
here is the wiring, not the model.  The fake is built **here** rather
than imported, so this module does not depend on the modules whose
assertions it shares.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification import terminal_decision_session as tds  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The reason a judge gives when it refused an answer: the check that
#: refused it, named.
_REJECTION = "analysis_rejected:evidence must be a list of strings"


class _FakeJudge:
    """A judge that answers nothing and says why.

    This is the shape the **real** judge has when it refuses an answer:
    it returns ``None`` and keeps the reason on itself
    (:attr:`last_analysis_error`).  A judge that answers an analysis
    the renderer refuses is a different shape — the answer comes back
    and the *renderer* is the one that refuses it — and it is covered
    by the fallback-card tests.
    """

    def __init__(self, analysis=None, analysis_error: str = "") -> None:
        self.analysis = analysis
        self.last_analysis_error = analysis_error
        self.last_session_id = "sess-abc"
        self.kwargs = None

    def analyze(self, **kwargs):
        self.kwargs = kwargs
        return self.analysis


class _FakeTriageJudge:
    """A triage judge that answers "the deliverable is wrong".

    The triage is what decides whether a stuck round stops or repairs
    itself again.  With the root-cause chain deleted, its answer
    ``deliverable_defect`` means exactly one thing: the repair budget
    is spent, so the loop stops and asks a human.
    """

    def judge(self, vp, failure_history, round_number=0):
        from verification.triage_agent import TriageConclusion

        return TriageConclusion(
            vp_id="VP-3",
            failure_class="deliverable_defect",
            conclusive=True,
            evidence_quote="expected 200, got 500",
            rationale="the deliverable does not do what the point asks",
        )


def _report():
    """A round whose one verification point failed."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-3",
                "status": "FAILED",
                "title": "登录接口",
                "expected_result": "返回 200",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
            },
        ],
    }


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def plan_dir(tmp_path):
    path = tmp_path / "plans" / "analysis-error-archive"
    path.mkdir(parents=True)
    (path / "plan_state.json").write_text(
        json.dumps({
            "plan_id": path.name,
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
    return path


@pytest.fixture
def project_dir(tmp_path):
    path = tmp_path / "projects" / "analysis-error-archive"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the rounds before the last one run."""
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as generator, \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as extract, \
         patch("repair_generator.RepairTaskAssembler") as assembler:
        extract.return_value = [
            {"id": "VP-3", "title": "修 VP-3", "priority": "high"},
        ]
        generator.return_value.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-3", "title": "修 VP-3"},
        ]
        assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "failed_vp_id": "VP-3", "title": "修 VP-3"},
        ]
        yield generator


def _make_orchestrator(plan_dir, project_dir, judge=None):
    """An orchestrator whose heavy collaborators are stubbed out."""
    orchestrator = VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=Mock(),
        terminal_judge=judge if judge is not None else _FakeJudge(),
        triage_judge=_FakeTriageJudge(),
    )
    orchestrator.verification_agent = Mock()
    orchestrator.repair_generator = Mock()
    return orchestrator


def _run_terminal_round(orchestrator, report=None, round_number=3):
    """Drive the orchestrator into the terminal branch and return its verdict."""
    body = report if report is not None else _report()
    verdict = {}
    for _ in range(3):
        verdict = orchestrator.check_cycle_conditions(body, round_number)
        if verdict.get("should_stop"):
            break
    return verdict


def _archives_of(plan_dir, suffix):
    """The archive files with the given suffix."""
    reports = plan_dir / "reports"
    assert reports.is_dir(), f"{reports} holds no archive"
    return sorted(p for p in reports.iterdir() if p.suffix == suffix)


def _read_json(plan_dir):
    """The single archive's JSON payload."""
    files = _archives_of(plan_dir, ".json")
    assert len(files) == 1, f"expected exactly one archive, got {files!r}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The machine half
# ---------------------------------------------------------------------------


def test_archive_json_records_the_analysis_error(tmp_path):
    """A reason given is a reason kept — in the archive's own JSON."""
    tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="same_failure_repeated_after_max_attempts",
        question="【需要你决定】…",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=None,
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
        analysis_error=_REJECTION,
    )

    payload = json.loads(
        _archives_of(tmp_path, ".json")[0].read_text(encoding="utf-8")
    )
    assert payload["analysis_error"] == _REJECTION, (
        f"the archive does not say why there is no analysis: "
        f"{payload.get('analysis_error')!r}"
    )
    assert payload["analysis"] is None, (
        "the archive claims an analysis nobody made"
    )


def test_archive_json_omits_the_key_without_an_error(tmp_path):
    """No reason, no key: the eight keys a reader knows are still eight.

    The key is **added** when there is a reason to record, never
    substituted for one of the eight — so a reader that knows the old
    shape reads the new one without changing a line.
    """
    tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="a question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis={
            "context": "第三轮验证停在登录接口",
            "problem": "登录接口返回 500",
            "evidence": ["VP-3 失败"],
            "recommendation": "revise_criterion",
            "recommendation_reason": "缺陷已定性",
            "uncertainty": "没有看过服务端日志",
        },
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
    )

    payload = json.loads(
        _archives_of(tmp_path, ".json")[0].read_text(encoding="utf-8")
    )
    assert "analysis_error" not in payload, (
        f"a round with an analysis reports an error: {payload!r}"
    )
    assert set(payload) == {
        "round",
        "stop_reason",
        "decision_id",
        "failed_points",
        "options",
        "default_option",
        "analysis",
        "written_at",
    }


# ---------------------------------------------------------------------------
# The round
# ---------------------------------------------------------------------------


def test_terminal_round_writes_the_rejection_reason(
    plan_dir, project_dir, repair_mocks,
):
    """A round whose answer was refused says so in its own record.

    The card told the human *why* there is no analysis; the archive is
    what outlives the card, so it says it too — and the analysis itself
    is ``null``, because there was none.
    """
    orchestrator = _make_orchestrator(
        plan_dir, project_dir,
        judge=_FakeJudge(None, analysis_error=_REJECTION),
    )
    orchestrator._ask_a_human_about = lambda **kwargs: "dec-asked"

    verdict = _run_terminal_round(orchestrator)

    assert verdict.get("should_stop") is True, "the round never stopped"
    payload = _read_json(plan_dir)
    assert payload["analysis_error"] == _REJECTION, (
        f"the round's record does not say why there is no analysis: "
        f"{payload.get('analysis_error')!r}"
    )
    assert payload["analysis"] is None, (
        "the round's record claims an analysis nobody made"
    )
