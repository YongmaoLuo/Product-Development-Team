"""The question and the archive are two documents — each keeps its half.

Background
----------
A terminal round computes an **analysis** (背景 / 问题 / 证据 / 建议 /
不确定) and puts the round to a human.  Two readers want two different
things out of that one round:

* the **human answering now** wants a *card*: what stopped, what they
  may choose, which choice is recommended, and where the whole analysis
  lives.  That is what :func:`render_brief` draws — it is short on
  purpose, because a question nobody scrolls through is a question
  nobody answers.
* the **reader of the archive** — a colleague three weeks later, or the
  agent that picks the plan up again — wants the *argument*: all five
  CPEA sections, verbatim.  That is what :func:`render_analysis` draws.

So the split is: **the card gives the path, the report keeps the whole
analysis.**  A round that put the card in the archive would keep a
summary where the evidence was supposed to be; a round that put the
report in the card would ask a question nobody can read.

What these tests pin
--------------------
1. **The card is brief, the archive is complete.**  One round, two
   documents: the question starts with 「【需要你决定】」 and carries no
   CPEA heading at all, while the archive's body carries all five.
2. **A round without an analysis keeps the card.**  With no analysis
   there is nothing to summarise, so the question is the **fallback
   card** — the same decision shape as the analysis card, with the
   failed points written out by hand — and the archive keeps that same
   card, so the archive is never empty and never claims an analysis
   nobody made.

The rounds below are driven through ``check_cycle_conditions`` — the
production entry — because that is where the two renderings are chosen.

2026-11-08: added for the decision card (arch decision point 2: the
report file still uses ``render_analysis()``; test decision point 4:
the report output does not regress).
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

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    path = tmp_path / "plans" / "cpea-separation"
    path.mkdir(parents=True)
    # The plan's state: the rounds below run the **ordinary** loop, so
    # the plan has to be in a state where the loop's phases are legal
    # (the execution phase is behind it).
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
    path = tmp_path / "projects" / "cpea-separation"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the rounds before the last one run.

    The two repair rounds that come before the terminal one build
    repair tasks; their collaborators are replaced so the rounds are
    about the two documents, not about how a repair task is assembled.
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


def _make_orchestrator(plan_dir, project_dir, judge=None):
    """An orchestrator whose heavy collaborators are stubbed out."""
    orchestrator = VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=Mock(),
        terminal_judge=judge if judge is not None else _FakeJudge(_analysis()),
        triage_judge=_FakeTriageJudge(),
    )
    orchestrator.verification_agent = Mock()
    orchestrator.repair_generator = Mock()
    return orchestrator


#: How many rounds the loop gives the same failure before it stops.
#: The third one is the terminal one.
_BUDGET = 3


def _run_terminal_round(orchestrator, report=None, round_number=3):
    """Drive the orchestrator into the terminal branch and return its verdict.

    The terminal branch is reached the way production reaches it: the
    same failure, round after round, until the budget is spent.  The
    first two rounds buy repair tasks; the third one stops and asks.
    """
    body = report if report is not None else _report()
    verdict = {}
    for _ in range(_BUDGET):
        verdict = orchestrator.check_cycle_conditions(body, round_number)
        if verdict.get("should_stop"):
            break
    return verdict


def _asked_questions(orchestrator):
    """Replace the human-ask with a recorder; return the recorder."""
    asked = []

    def _capture(**kwargs):
        asked.append(kwargs.get("question") or "")
        return "dec-asked"

    orchestrator._ask_a_human_about = _capture
    return asked


def _archive_body(plan_dir):
    """The body of the plan's single archive — the report, not the header."""
    reports = plan_dir / "reports"
    archives = sorted(reports.glob("*.md"))
    assert len(archives) == 1, f"expected exactly one archive, got {archives!r}"
    text = archives[0].read_text(encoding="utf-8")
    # The header is the title, the three facts, and the blank line that
    # separates them from the body.
    return text.split("\n\n", 2)[2].rstrip("\n")


#: The five headings a report carries and a card does not.
_CPEA_HEADINGS = (
    "## 背景（C）",
    "## 问题（P）",
    "## 证据（E）",
    "## 建议（A）",
    "## 不确定（U）",
)


# ---------------------------------------------------------------------------
# The two documents
# ---------------------------------------------------------------------------


def test_question_is_brief_while_the_archive_keeps_cpea(
    plan_dir, project_dir, repair_mocks,
):
    """One round, two documents: a card for the human, a report on disk.

    The question a human reads is the **card** — short, headed
    「【需要你决定】」, carrying what stopped, what may be chosen, which
    choice is recommended and the path of the report.  It carries no
    CPEA section: a card that carried the whole analysis would be a
    report, and a report nobody can answer.

    The archive a reader finds later is the **report** — all five CPEA
    sections, drawn by the report renderer, so the argument survives
    the round that produced it.
    """
    orchestrator = _make_orchestrator(plan_dir, project_dir)
    asked = _asked_questions(orchestrator)

    _run_terminal_round(orchestrator)

    assert asked, "the terminal round asked nobody"
    question = asked[-1]

    # 1. The card: headed as a decision, and carrying no section of the
    #    report.
    assert question.startswith("【需要你决定】"), (
        f"the question is not a decision card:\n{question}"
    )
    for heading in _CPEA_HEADINGS:
        assert heading not in question, (
            f"the card carries the report's {heading!r} section — a card "
            f"is a summary, not the report:\n{question}"
        )

    # 2. The card points at the report.
    assert "完整分析：" in question, (
        f"the card does not say where the whole analysis lives:\n{question}"
    )
    report_path = question.split("完整分析：", 1)[1].splitlines()[0].strip()
    assert report_path.endswith(".md"), (
        f"the card's link does not name the report file:\n{question}"
    )
    assert Path(report_path).parent.name == "reports", (
        f"the card's link does not point into the plan's reports "
        f"directory: {report_path!r}"
    )

    # 3. The report: all five sections, whole.
    body = _archive_body(plan_dir)
    for heading in _CPEA_HEADINGS:
        assert heading in body, (
            f"the archive does not keep the report's {heading!r} section:\n"
            f"{body}"
        )


def test_a_round_without_an_analysis_keeps_the_card(
    plan_dir, project_dir, repair_mocks,
):
    """No analysis: the question is the card, and the archive keeps it.

    There is nothing to summarise and nothing to report, so the question
    is the **fallback card** — the same decision shape as the analysis
    card, with the failed points written out by hand (arch decision
    point 2).  The archive keeps that same card, so the two documents
    say the same thing about the same round: the archive is never
    empty, and it never claims an analysis nobody made.
    """
    orchestrator = _make_orchestrator(
        plan_dir, project_dir, judge=_FakeJudge(None),
    )
    asked = _asked_questions(orchestrator)

    _run_terminal_round(orchestrator)

    assert asked, "the terminal round asked nobody"
    question = asked[-1]
    assert question.startswith("【需要你决定】"), (
        f"a round without an analysis did not draw the fallback card:\n"
        f"{question}"
    )
    assert "VP-3 FAILED" in question, (
        f"the card does not name the failed point:\n{question}"
    )

    body = _archive_body(plan_dir)
    assert body == question, (
        "the archive's body is not the card the human was shown:\n"
        f"{body}\n\n---- the card ----\n{question}"
    )
    for heading in _CPEA_HEADINGS:
        assert heading not in body, (
            f"the archive claims a report nobody made ({heading!r}):\n{body}"
        )
