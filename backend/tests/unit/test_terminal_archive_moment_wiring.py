"""The card's path and the archive's file must be the same file.

Background
----------
A terminal round puts a **decision card** to a human.  The card quotes
the path of the round's full analysis — the ``.md`` half of the archive
the round writes under ``<plan_dir>/reports/``.

The card is put to the human **before** the archive is written, so the
path cannot be read back from the write: it is computed, from the round
number and a clock reading.  The write then takes **its own** clock
reading.  Two readings taken at two different moments can straddle a
second boundary — and then the card points at a file that was never
written, because the archive landed on the next second's name.

The fix is one moment, read once: the branch computes it, hands it to
the question (which computes the path from it) and to the archive (which
writes the file under it).  The card's path is then the file's path.

What these tests pin
--------------------
1. **The card's path is the file that is written.**  One moment, one
   name: the path the card quotes exists after the archive is written.
2. **An archive without a moment still writes.**  ``now=None`` keeps the
   old behaviour — the current UTC second names the files.
3. **The branch reads the clock once.**  The question and the archive
   are handed the *same* moment, so the two cannot drift apart.

2026-11-08: added for the decision card's "完整分析" path (arch
decision point 4: the path is determinable at question time).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


#: The clock reading the first test stamps both halves with.
_MOMENT = datetime(2026, 11, 8, 10, 10, 10, tzinfo=timezone.utc)

#: The name that moment gives the round-3 archive.
_MOMENT_NAME = "terminal-decision-round-3-20261108T101010Z"


class _FakeJudge:
    """A judge that answers a fixed analysis."""

    def __init__(self, analysis) -> None:
        self.analysis = analysis
        self.last_session_id = "sess-abc"
        self.kwargs = None

    def analyze(self, **kwargs):
        self.kwargs = kwargs
        return self.analysis


class _FakeTriageJudge:
    """A triage judge that answers "the deliverable is wrong".

    The triage is what decides whether a stuck round stops or repairs
    itself again.  Its answer ``deliverable_defect`` means the repair
    budget is spent, so the loop stops and asks a human.
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


def _analysis():
    """A well-formed analysis — the six fields the contract asks for."""
    return {
        "context": "第三轮验证停在登录接口",
        "problem": "登录接口返回 500，不是预期结果",
        "evidence": ["VP-3 失败"],
        "recommendation": "revise_criterion",
        "recommendation_reason": "缺陷已定性，重跑不会改变结果",
        "uncertainty": "没有看过服务端日志",
    }


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
    """A plan directory the terminal branch can write its archive into."""
    path = tmp_path / "plans" / "archive-moment"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def project_dir(tmp_path):
    path = tmp_path / "projects" / "archive-moment"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def orchestrator(plan_dir, project_dir, monkeypatch):
    """An orchestrator whose heavy collaborators are stubbed out."""
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
        project_dir=project_dir,
        coding_tool=Mock(),
        terminal_judge=_FakeJudge(_analysis()),
        triage_judge=_FakeTriageJudge(),
    )


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the rounds before the last one run.

    The two repair rounds that come before the terminal one build
    repair tasks; their collaborators are replaced so the rounds are
    about the archive, not about how a repair task is assembled.
    """
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


# ---------------------------------------------------------------------------
# One moment, one name
# ---------------------------------------------------------------------------


def test_the_card_path_is_the_file_that_is_written(orchestrator):
    """The path the card quotes is the file the archive writes.

    The card is built **before** the archive exists, so the path is
    computed; the archive is written **after**, so the name is stamped.
    One moment for both is what makes the two the same file.
    """
    payload = orchestrator._terminal_decision_question(
        _report(), 3, ["VP-3"], 0, archive_moment=_MOMENT,
    )

    orchestrator._save_terminal_cpea_archive(
        round_number=3,
        stop_reason="repair_execution_failed",
        question=payload["question"],
        options=payload["options"],
        default_option=payload["default_option"],
        analysis=_analysis(),
        decision_id="d1",
        failed_vp_ids=["VP-3"],
        now=_MOMENT,
    )

    report_path = Path(payload["report_path"])
    assert report_path.exists(), (
        f"the card points at {report_path}, which was never written"
    )
    assert report_path.name == f"{_MOMENT_NAME}.md"


def test_the_archive_still_writes_without_an_explicit_moment(orchestrator):
    """No moment given: the current UTC second names the files, as before."""
    orchestrator._save_terminal_cpea_archive(
        round_number=3,
        stop_reason="repair_execution_failed",
        question="a question",
        options=["rerun_this_round", "accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="d1",
        failed_vp_ids=["VP-3"],
    )

    reports = orchestrator.plan_dir / "reports"
    markdown = sorted(reports.glob("terminal-decision-round-3-*.md"))
    machine = sorted(reports.glob("terminal-decision-round-3-*.json"))
    assert len(markdown) == 1, f"expected one archive, got {markdown!r}"
    assert len(machine) == 1, f"expected one archive, got {machine!r}"
    assert markdown[0].with_suffix(".json") == machine[0]


# ---------------------------------------------------------------------------
# The branch reads the clock once
# ---------------------------------------------------------------------------


def _make_orchestrator(plan_dir, project_dir):
    """An orchestrator whose heavy collaborators are stubbed out."""
    orchestrator = VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=Mock(),
        terminal_judge=_FakeJudge(_analysis()),
        triage_judge=_FakeTriageJudge(),
    )
    orchestrator.verification_agent = Mock()
    orchestrator.repair_generator = Mock()
    return orchestrator


def _run_terminal_round(orchestrator, report=None, round_number=3):
    """Drive the orchestrator into the terminal branch.

    The terminal branch is reached the way production reaches it: the
    same failure, round after round, until the budget is spent.  The
    first two rounds buy repair tasks; the third one stops and asks.
    """
    body = report if report is not None else _report()
    verdict = {}
    for _ in range(3):
        verdict = orchestrator.check_cycle_conditions(body, round_number)
        if verdict.get("should_stop"):
            break
    return verdict


def test_the_terminal_branch_hands_one_moment_to_both(
    plan_dir, project_dir, repair_mocks,
):
    """The branch reads the clock once and hands the same reading to both.

    Two readings taken at two different moments can straddle a second
    boundary — and then the card's path names a file the archive never
    wrote.  So the branch computes one moment and passes it to the
    question (which computes the path) and to the archive (which writes
    the file).
    """
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
    orchestrator = _make_orchestrator(plan_dir, project_dir)
    orchestrator._ask_a_human_about = lambda **kwargs: "dec-asked"

    seen = {}
    asked = orchestrator._terminal_decision_question
    archived = orchestrator._save_terminal_cpea_archive

    def question(*args, **kwargs):
        seen["archive_moment"] = kwargs.get("archive_moment")
        payload = asked(*args, **kwargs)
        seen["report_path"] = payload["report_path"]
        return payload

    def archive(**kwargs):
        seen["now"] = kwargs.get("now")
        return archived(**kwargs)

    orchestrator._terminal_decision_question = question
    orchestrator._save_terminal_cpea_archive = archive

    verdict = _run_terminal_round(orchestrator)

    assert verdict.get("should_stop") is True, "the round never stopped"
    assert seen.get("archive_moment") is not None, (
        "the branch did not hand the question a moment to compute the path "
        "from — the card's path is the current second's, whatever the "
        "archive lands on"
    )
    assert seen.get("now") == seen.get("archive_moment"), (
        "the question and the archive were handed different moments"
    )
    # And the two agree on the ground: the path the card quotes is the
    # file the archive wrote.
    report_path = Path(seen["report_path"])
    assert report_path.exists(), (
        f"the card points at {report_path}, which was never written"
    )
    # The name carries the very moment the branch read.
    stamp = seen["archive_moment"].astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    assert report_path.name == f"terminal-decision-round-3-{stamp}.md"
