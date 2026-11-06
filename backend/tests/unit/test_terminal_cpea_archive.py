"""The terminal round's CPEA analysis must land on disk — every round.

Background
----------
A terminal round computes an **analysis**: the judge reads the failure
scene and says what it means (背景 / 问题 / 证据 / 建议 / 不确定).  It is
a model call that already happened, so it cannot be reproduced later —
if it is not written down, it is gone.

Two things used to lose it:

* the branch that found the plan **already asked** computed the analysis
  and then wrote it nowhere at all — the bytes were produced and
  dropped on the floor;
* the sidecar next to the plan is **one file per plan**: the next round
  overwrote the previous round's analysis, so the history of how the
  plan went was gone too.

What these tests pin
--------------------
1. **The already-asked branch archives too.**  The analysis is a fact
   about the round, not about the question: nobody was asked, the round
   still read its own failure, and the reading is kept.
2. **A round that asked still archives**, with the decision id in the
   archive.
3. **An ``unroutable`` decision archives too.**  A question nobody was
   ever asked is still a question the round put, and the round's
   analysis is still worth having.
4. **A round without an analysis archives the facts** — the question
   verbatim — and says ``analysis: null`` in the machine-readable half.
5. **Two writes in the same round do not overwrite each other.**
6. **An archive that cannot be written is not a failed round.**
7. The **module** writes an ``.md`` (for a person) and a ``.json`` (for
   a machine) per write, under ``<plan_dir>/reports/``.

The rounds below are driven through ``check_cycle_conditions`` — the
production entry — with the root-cause chain's terminal verdict
supplied, because the terminal branch is reached only through it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification import terminal_decision_session as tds  # noqa: E402
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


def _analysis():
    """A well-formed analysis — the six fields the contract asks for."""
    return {
        "context": "第三轮验证停在登录接口",
        "problem": "登录接口返回 500，不是预期结果",
        "evidence": ["VP-3 失败"],
        "recommendation": "accept_as_failed",
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
    path = tmp_path / "plans" / "cpea-archive"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def project_dir(tmp_path):
    path = tmp_path / "projects" / "cpea-archive"
    path.mkdir(parents=True)
    return path


def _make_orchestrator(plan_dir, project_dir, judge=None):
    """An orchestrator whose heavy collaborators are stubbed out."""
    orchestrator = VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=Mock(),
        terminal_judge=judge if judge is not None else _FakeJudge(_analysis()),
    )
    orchestrator.verification_agent = Mock()
    orchestrator.repair_generator = Mock()
    return orchestrator


def _terminal_chain_decision(root_cause_vp_ids, round_number):
    """The chain's verdict: the machine's road on the point is walked out."""
    return {
        "terminal": True,
        "active": False,
        "vp_ids": [],
        "actions": [],
        "blocked": [],
        "scope_missing": [],
        "payload": {
            "stage": "BLOCKED_ON_USER",
            "round": int(round_number),
            "points": [],
        },
    }


def _run_terminal_round(orchestrator, report=None, round_number=3):
    """Drive the orchestrator into the terminal branch and return its verdict."""
    orchestrator._root_cause_chain_decision = _terminal_chain_decision
    return orchestrator.check_cycle_conditions(
        report if report is not None else _report(), round_number,
    )


def _archives(plan_dir):
    """Every archive file the plan directory holds, sorted by name."""
    reports = plan_dir / "reports"
    if not reports.is_dir():
        return []
    return sorted(reports.iterdir())


def _archives_of(plan_dir, suffix):
    """The archive files with the given suffix."""
    return [p for p in _archives(plan_dir) if p.suffix == suffix]


def _read_json(plan_dir):
    """The single archive's JSON payload."""
    files = _archives_of(plan_dir, ".json")
    assert len(files) == 1, f"expected exactly one archive, got {files!r}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The module itself
# ---------------------------------------------------------------------------


def test_archive_writes_a_markdown_and_a_json(tmp_path):
    """One write leaves two files: one for a person, one for a machine."""
    written = tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="verification round 3 stopped with repair_execution_failed",
        options=["rerun_this_round", "accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
    )

    assert written is not None
    assert written.name.endswith(".md")
    assert written.parent == tmp_path / "reports"

    payload = _read_json(tmp_path)
    assert payload["round"] == 3
    assert payload["stop_reason"] == "repair_execution_failed"
    assert payload["decision_id"] == "dec-1"
    assert payload["failed_points"] == ["VP-3"]
    assert payload["options"] == ["rerun_this_round", "accept_as_failed"]
    assert payload["default_option"] == "accept_as_failed"
    assert payload["analysis"] == _analysis()
    assert payload["written_at"]


def test_the_archive_is_named_after_the_round_and_the_utc_stamp(tmp_path):
    """The name says which round and when — in UTC, to the second."""
    moment = datetime(2026, 11, 6, 17, 32, 5, tzinfo=timezone.utc)
    written = tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="a question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
        now=moment,
    )

    assert written is not None
    assert written.name == "terminal-decision-round-3-20261106T173205Z.md"
    assert written.with_suffix(".json").name == (
        "terminal-decision-round-3-20261106T173205Z.json"
    )
    # The stamp is UTC: a file named in one zone and read in another is
    # a file nobody can find.
    payload = _read_json(tmp_path)
    assert payload["written_at"].startswith("2026-11-06T17:32:05")
    assert "+00:00" in payload["written_at"]


def test_the_json_carries_exactly_the_keys_the_spec_names(tmp_path):
    """The machine half's shape is fixed — a reader can rely on it."""
    tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="a question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
    )

    payload = _read_json(tmp_path)
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


def test_archive_markdown_renders_the_five_sections(tmp_path):
    """The ``.md`` half carries the analysis as a person reads it."""
    tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="the factual question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
    )

    text = _archives_of(tmp_path, ".md")[0].read_text(encoding="utf-8")
    assert "# 终局决策分析 · round 3" in text
    assert "- stop_reason: repair_execution_failed" in text
    assert "- decision_id: dec-1" in text
    assert "- failed points: VP-3" in text
    assert "背景" in text and "证据" in text and "建议" in text


def test_archive_without_an_analysis_keeps_the_facts(tmp_path):
    """No analysis: the archive keeps the question verbatim and says so."""
    tds.save_round_archive(
        tmp_path,
        round_number=2,
        stop_reason="repair_execution_failed",
        question="verification round 2 stopped with repair_execution_failed",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=None,
        decision_id="",
        failed_vp_ids=[],
    )

    payload = _read_json(tmp_path)
    assert payload["analysis"] is None
    assert payload["decision_id"] == ""

    text = _archives_of(tmp_path, ".md")[0].read_text(encoding="utf-8")
    assert (
        "verification round 2 stopped with repair_execution_failed" in text
    )
    assert "- decision_id: " in text


def test_two_writes_in_the_same_second_do_not_overwrite(tmp_path):
    """Same round, same second: two pairs of files, the first one intact."""
    moment = datetime(2026, 11, 6, 17, 32, 5, tzinfo=timezone.utc)
    first = tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="first question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
        now=moment,
    )
    second = tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="second question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-2",
        failed_vp_ids=["VP-3"],
        now=moment,
    )

    assert first is not None and second is not None
    assert first != second
    assert len(_archives_of(tmp_path, ".md")) == 2
    assert len(_archives_of(tmp_path, ".json")) == 2

    first_text = first.read_text(encoding="utf-8")
    assert "dec-1" in first_text
    second_text = second.read_text(encoding="utf-8")
    assert "dec-2" in second_text


def test_archive_write_failure_returns_none(tmp_path):
    """A ``reports`` that cannot be a directory: nothing is written, no raise."""
    (tmp_path / "reports").write_text("not a directory", encoding="utf-8")

    written = tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="a question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
    )

    assert written is None


# ---------------------------------------------------------------------------
# The rounds — through the production entry
# ---------------------------------------------------------------------------


def test_the_round_that_asked_archives_its_analysis(plan_dir, project_dir):
    """A round that asked a human archives the analysis it produced."""
    orchestrator = _make_orchestrator(plan_dir, project_dir)
    orchestrator._ask_a_human_about = lambda **kwargs: "dec-asked"

    verdict = _run_terminal_round(orchestrator)

    assert verdict["should_stop"] is True
    payload = _read_json(plan_dir)
    assert payload["decision_id"] == "dec-asked"
    assert payload["analysis"] == _analysis()
    assert payload["round"] == 3
    assert payload["failed_points"] == ["VP-3"]
    assert len(_archives_of(plan_dir, ".md")) == 1
    assert len(_archives_of(plan_dir, ".json")) == 1


def test_the_already_asked_round_archives_its_analysis(plan_dir, project_dir):
    """The branch that asks nobody still archives what it read.

    The first round asks and leaves a decision behind; the second round
    finds the plan already asked — and its analysis, which was computed
    all the same, used to be dropped on the floor.
    """
    first = _make_orchestrator(plan_dir, project_dir)
    _run_terminal_round(first, round_number=3)

    second = _make_orchestrator(plan_dir, project_dir)
    verdict = _run_terminal_round(second, round_number=3)

    assert verdict["should_stop"] is True
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in _archives_of(plan_dir, ".json")
    ]
    assert len(payloads) == 2, "both rounds must leave an archive"
    # The second round asked nobody: its decision is empty, but its
    # analysis is kept all the same.
    asked_nobody = [p for p in payloads if p["decision_id"] == ""]
    assert len(asked_nobody) == 1, (
        f"the already-asked round must archive an empty decision, got "
        f"{[p['decision_id'] for p in payloads]!r}"
    )
    assert asked_nobody[0]["analysis"] == _analysis()
    assert len(_archives_of(plan_dir, ".md")) == 2


def test_an_unroutable_decision_still_archives(plan_dir, project_dir):
    """A question nobody was ever asked is still the round's question."""
    orchestrator = _make_orchestrator(plan_dir, project_dir)

    verdict = _run_terminal_round(orchestrator)

    assert verdict["should_stop"] is True
    payload = _read_json(plan_dir)
    assert payload["decision_id"], "the decision row exists even unroutable"
    assert payload["analysis"] == _analysis()

    from hitl.service import list_decisions

    decisions = list_decisions(plan_dir.name)
    assert [d.status for d in decisions] == ["unroutable"], (
        f"本轮的问题没人接，应当是 unroutable，实际是 "
        f"{[d.status for d in decisions]!r}"
    )


def test_a_round_without_an_analysis_archives_the_facts(
    plan_dir, project_dir,
):
    """No analysis: the archive keeps the question and says ``null``."""
    orchestrator = _make_orchestrator(
        plan_dir, project_dir, judge=_FakeJudge(None),
    )
    orchestrator._ask_a_human_about = lambda **kwargs: "dec-asked"

    _run_terminal_round(orchestrator)

    payload = _read_json(plan_dir)
    assert payload["analysis"] is None
    text = _archives_of(plan_dir, ".md")[0].read_text(encoding="utf-8")
    assert "verification round 3 stopped with repair_execution_failed" in text


def test_two_rounds_in_the_same_plan_keep_both_archives(
    plan_dir, project_dir,
):
    """Two rounds, two archives — the second does not overwrite the first."""
    first = _make_orchestrator(plan_dir, project_dir)
    first._ask_a_human_about = lambda **kwargs: "dec-1"
    _run_terminal_round(first, round_number=3)

    second = _make_orchestrator(plan_dir, project_dir)
    second._ask_a_human_about = lambda **kwargs: "dec-2"
    _run_terminal_round(second, round_number=4)

    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in _archives_of(plan_dir, ".json")
    ]
    assert len(payloads) == 2
    rounds = sorted(payload["round"] for payload in payloads)
    assert rounds == [3, 4]


def test_an_archive_that_cannot_be_written_does_not_fail_the_round(
    plan_dir, project_dir,
):
    """The round stands: a failed archive is a fact, not a failed round."""
    (plan_dir / "reports").write_text("not a directory", encoding="utf-8")

    orchestrator = _make_orchestrator(plan_dir, project_dir)
    orchestrator._ask_a_human_about = lambda **kwargs: "dec-asked"
    audits = []
    orchestrator.logger = Mock()
    orchestrator.logger.warning.side_effect = (
        lambda event, message, data=None: audits.append(event)
    )

    verdict = _run_terminal_round(orchestrator)

    assert verdict["should_stop"] is True
    assert verdict["stop_reason"] == "repair_execution_failed"
    assert verdict["repair_tasks"] == []
    assert verdict["needs_operator_decision"] is True
    # The round tried to archive and the failure was reported, not raised.
    assert "verification_terminal_cpea_archive_failed" in audits, (
        f"归档写失败必须被记进 audit，实际记的是 {audits!r}"
    )
    # The sidecar is a separate file and is still written.
    assert (plan_dir / tds.SESSION_FILENAME).exists()


def test_the_archive_renders_what_the_question_renders(tmp_path):
    """The archive's five sections are the question's five sections.

    The archive and the question say the same thing about the same
    round, so a reader who has seen one recognises the other.
    """
    from verification.terminal_analysis import render_analysis

    tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="the factual question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
    )

    text = _archives_of(tmp_path, ".md")[0].read_text(encoding="utf-8")
    assert render_analysis(_analysis()) in text


def test_the_copy_the_module_keeps_agrees_with_the_question_renderer(
    tmp_path,
):
    """The module's own copy of the renderer does not drift.

    :mod:`verification.terminal_decision_session` imports the standard
    library only — a pinned property — so it cannot call the question's
    renderer itself and keeps a copy for the callers that hand no
    rendering over.  A copy that drifted would put two different
    renderings of the same analysis on disk, so the two are pinned
    together here: on a contract-valid analysis they say the same
    thing, section for section.
    """
    from verification.terminal_analysis import render_analysis

    tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="the factual question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
    )

    text = _archives_of(tmp_path, ".md")[0].read_text(encoding="utf-8")
    # The header is the title, the three facts, and the blank line that
    # separates them from the body.
    body = text.split("\n\n", 2)[2].rstrip("\n")
    assert body == render_analysis(_analysis()), (
        "the module's copy of the renderer has drifted from the "
        "question's renderer"
    )


def test_a_rendering_handed_over_is_the_body(tmp_path):
    """What the caller drew is what the archive writes.

    The caller (the orchestrator) draws the body with the question's
    renderer and hands it over; the archive writes it as it is.  That
    is what makes an analysis the renderer **refuses** fall back to
    the question's factual sentence instead of five sections the human
    was never shown.
    """
    tds.save_round_archive(
        tmp_path,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="the factual question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
        rendered="the body the caller drew",
    )

    text = _archives_of(tmp_path, ".md")[0].read_text(encoding="utf-8")
    assert "the body the caller drew" in text
    assert "背景（C）" not in text


def test_an_analysis_the_renderer_refuses_falls_back_to_the_question(
    plan_dir, project_dir,
):
    """The archive draws its sections with the **question's** renderer.

    The renderer refuses an analysis that is not one — here a
    recommendation outside the three the question offers — and renders
    nothing.  An archive that drew the sections with its own copy of
    the renderer would show five sections the human was never shown,
    which is a claim about the round that nobody made.

    So the refusal is honoured: the ``.md`` falls back to the factual
    question, exactly as the question itself did.  The raw analysis is
    still kept verbatim in the ``.json`` — nothing is thrown away.

    Driven through the production entry, because that is where the
    renderer is: the archive module cannot reach it itself.
    """
    from verification.terminal_analysis import render_analysis

    unusable = dict(_analysis(), recommendation="invent_something")
    assert render_analysis(unusable) == "", (
        "this test needs an analysis the renderer refuses"
    )

    orchestrator = _make_orchestrator(
        plan_dir, project_dir, judge=_FakeJudge(unusable),
    )
    orchestrator._ask_a_human_about = lambda **kwargs: "dec-asked"

    _run_terminal_round(orchestrator)

    text = _archives_of(plan_dir, ".md")[0].read_text(encoding="utf-8")
    assert "verification round 3 stopped with repair_execution_failed" in text
    assert "建议（A）" not in text
    # The raw analysis is still there — verbatim — in the machine half.
    assert _read_json(plan_dir)["analysis"] == unusable


def test_the_archive_body_is_exactly_what_the_question_reads(
    plan_dir, project_dir,
):
    """Through the production entry, one rendering serves both readers.

    The question a human reads and the archive a reader finds later are
    one rendering of one analysis — not two renderings that happen to
    look alike.  Driven through the production entry, because that is
    where the renderer is.
    """
    from verification.terminal_analysis import render_analysis

    asked = {}

    def _capture(**kwargs):
        asked["question"] = kwargs["question"]
        return "dec-asked"

    orchestrator = _make_orchestrator(plan_dir, project_dir)
    orchestrator._ask_a_human_about = _capture

    _run_terminal_round(orchestrator)

    text = _archives_of(plan_dir, ".md")[0].read_text(encoding="utf-8")
    # The header is the title, the three facts, and the blank line that
    # separates them from the body.
    body = text.split("\n\n", 2)[2].rstrip("\n")
    assert body == render_analysis(_analysis())
    assert body == asked["question"]
