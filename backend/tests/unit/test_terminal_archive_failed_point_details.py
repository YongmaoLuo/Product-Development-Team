"""The terminal round's archive keeps every failed point's detail.

Background
----------
A terminal round that produced **no analysis** puts a **fallback card**
to the human (arch decision point 2): the failed points are written out
by hand, and the card says how many more there are — 「另有 K 个失败点，
见完整报告」.

The card is a chat message and the chat is gone, and the card is
bounded (:data:`verification.terminal_analysis.TERMINAL_FALLBACK_MAX_POINTS`
points at most).  So the "完整报告" the card sends the human to has to
carry the detail of **every** failed point: its id, its status, its
title and the actual result that came back.

It did not.  The archive wrote ``failed_points`` as a list of **ids**
(:func:`verification.terminal_decision_session.save_round_archive`),
and the ``.md`` header listed those ids and nothing else — so the file
the card pointed the human at did not hold the detail the card had
promised them.  The report said "VP-3, VP-4" and nothing about what
those two points were.

What these tests pin
--------------------
1. The archive's JSON carries a ``failed_point_details`` key — one
   record per failed point, with its four fields, in the order they
   were handed over.
2. The key is **absent** when there is nothing to say: no details
   passed, and an empty list, are the same thing.
3. The ``.md`` header writes one line per failed point, after the
   failed-points line, with the actual result **whole** — not the
   first sentence, not a cut, not a summary.
4. A terminal round that failed four points archives four details.

2026-11-08: arch decision point 2 (the card gives the path, the report
keeps the detail); test decision point 5 (the existing tests do not
regress).
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
from tests.unit.test_terminal_cpea_archive import (  # noqa: E402
    _FakeJudge,
    _make_orchestrator,
    _run_terminal_round,
)


#: The four fields one failed point's detail carries.
_DETAIL_FIELDS = ("id", "status", "title", "actual_result")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def plan_dir(tmp_path):
    path = tmp_path / "plans" / "archive-failed-point-details"
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
    path = tmp_path / "projects" / "archive-failed-point-details"
    path.mkdir(parents=True)
    return path


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
            {"id": "VP-1", "title": "修 VP-1", "priority": "high"},
        ]
        generator.return_value.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-1", "title": "修 VP-1"},
        ]
        assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "failed_vp_id": "VP-1", "title": "修 VP-1"},
        ]
        yield generator


def _details():
    """Two failed points, as the detail records the archive keeps."""
    return [
        {
            "id": "VP-3",
            "status": "FAILED",
            "title": "登录接口",
            "actual_result": "HTTP 500",
        },
        {
            "id": "VP-4",
            "status": "FAILED",
            "title": "订单接口",
            "actual_result": "expected 200, got 502",
        },
    ]


def _detail_line(detail):
    """The line the ``.md`` header writes for one failed point."""
    return (
        f"- {detail['id']} {detail['status']} — {detail['title']}："
        f"{detail['actual_result']}"
    )


def _header_lines(text):
    """The archive's header block — the facts, and the detail lines.

    The header is the title, a blank line, the fact lines and the blank
    line that separates them from the body.
    """
    return text.split("\n\n", 2)[1].splitlines()


def _detail_lines(text):
    """The header's detail lines — one per failed point, in order."""
    return [
        line for line in _header_lines(text)
        if line.startswith("- ")
        and not line.startswith("- stop_reason")
        and not line.startswith("- decision_id")
        and not line.startswith("- failed points")
    ]


def _write_archive(directory, *, details=None, failed_vp_ids=None):
    """Write one archive into ``directory``; return its ``.md`` path."""
    return tds.save_round_archive(
        directory,
        round_number=3,
        stop_reason="repair_execution_failed",
        question="a question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=None,
        decision_id="dec-1",
        failed_vp_ids=list(
            failed_vp_ids if failed_vp_ids is not None else ["VP-3", "VP-4"]
        ),
        failed_point_details=details,
    )


def _read_pair(written):
    """The archive's two halves, read back from disk."""
    md_text = written.read_text(encoding="utf-8")
    json_path = written.with_suffix(".json")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    return md_text, payload


def _report_with_four_failed_points():
    """A round whose four verification points failed."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": f"VP-{index}",
                "status": "FAILED",
                "title": f"接口 {index}",
                "expected_result": "返回 200",
                "actual_result": f"HTTP {500 + index}",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
            }
            for index in range(1, 5)
        ],
    }


# ---------------------------------------------------------------------------
# The module itself
# ---------------------------------------------------------------------------


def test_archive_records_each_failed_point_detail(tmp_path):
    """Every failed point's four fields land in the archive's JSON."""
    details = _details()

    written = _write_archive(tmp_path, details=details)

    assert written is not None
    _, payload = _read_pair(written)
    assert "failed_point_details" in payload, (
        "the archive does not carry the failed points' detail:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    recorded = payload["failed_point_details"]
    assert len(recorded) == 2, (
        f"expected one record per failed point, got {recorded!r}"
    )
    for got, want in zip(recorded, details):
        assert set(got) == set(_DETAIL_FIELDS), (
            f"the record does not carry exactly the four fields: {got!r}"
        )
        for field in _DETAIL_FIELDS:
            assert got[field] == want[field], (
                f"the record's {field} is not what was handed over: "
                f"{got[field]!r} != {want[field]!r}"
            )


def test_archive_omits_the_key_without_details(tmp_path):
    """No details: the key is absent and the header gains no line."""
    written = _write_archive(tmp_path / "none", details=None)

    assert written is not None
    md_text, payload = _read_pair(written)
    assert "failed_point_details" not in payload, (
        f"a round with no detail must not carry the key:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    assert _detail_lines(md_text) == [], (
        f"the header grew a line it had no detail for:\n{md_text}"
    )
    assert _header_lines(md_text) == [
        "- stop_reason: repair_execution_failed",
        "- decision_id: dec-1",
        "- failed points: VP-3, VP-4",
    ]

    # An empty list is the same thing as no list at all.
    empty = _write_archive(tmp_path / "empty", details=[])

    assert empty is not None
    empty_md, empty_payload = _read_pair(empty)
    assert "failed_point_details" not in empty_payload
    assert _detail_lines(empty_md) == []


def test_markdown_lists_every_detail_line(tmp_path):
    """The ``.md`` header writes one line per failed point, in order."""
    details = _details()

    written = _write_archive(tmp_path, details=details)

    assert written is not None
    md_text, _ = _read_pair(written)
    for detail in details:
        assert _detail_line(detail) in md_text, (
            f"the archive does not list {detail['id']}'s detail:\n{md_text}"
        )
    header = _header_lines(md_text)
    assert header[2].startswith("- failed points:"), (
        f"the detail lines are not in the header:\n{md_text}"
    )
    assert header[3:] == [_detail_line(detail) for detail in details], (
        f"the detail lines are not one per failed point, in order:\n"
        f"{md_text}"
    )


def test_an_empty_actual_result_still_writes_the_line(tmp_path):
    """A point with nothing to say is still a point that failed."""
    details = [{
        "id": "VP-3",
        "status": "FAILED",
        "title": "登录接口",
        "actual_result": "",
    }]

    written = _write_archive(tmp_path, details=details)

    assert written is not None
    md_text, payload = _read_pair(written)
    assert payload["failed_point_details"][0]["actual_result"] == ""
    lines = _detail_lines(md_text)
    assert len(lines) == 1, f"the point was dropped:\n{md_text}"
    assert lines[0].startswith("- VP-3 FAILED — 登录接口："), (
        f"the line does not name the point and its status:\n{md_text}"
    )


def test_the_actual_result_is_written_whole(tmp_path):
    """The actual result is evidence: it is written whole, not cut.

    The card cuts it — a card is a chat message a person reads — but
    the report is where the evidence lives, so the report keeps every
    character of it, line breaks included.
    """
    details = [{
        "id": "VP-3",
        "status": "FAILED",
        "title": "登录接口",
        "actual_result": (
            "HTTP 500\nTraceback (most recent call last):\n"
            "  File \"app.py\", line 42, in login\n    raise RuntimeError"
        ),
    }]

    written = _write_archive(tmp_path, details=details)

    assert written is not None
    md_text, payload = _read_pair(written)
    assert payload["failed_point_details"][0]["actual_result"] == (
        details[0]["actual_result"]
    ), "the JSON kept only part of the actual result"
    assert details[0]["actual_result"] in md_text, (
        f"the report did not keep the actual result whole:\n{md_text}"
    )


# ---------------------------------------------------------------------------
# The round — through the production entry
# ---------------------------------------------------------------------------


def test_terminal_round_archives_details_for_every_failed_point(
    plan_dir, project_dir, repair_mocks,
):
    """A terminal round that failed four points archives four details.

    The card the human is shown is bounded — it names a few points and
    sends the reader to the report for the rest — so the report is the
    only place the **whole** failure scene can live.  This round failed
    four points; the archive has to carry all four.
    """
    asked = []

    def _capture(**kwargs):
        asked.append(kwargs.get("question") or "")
        return "dec-asked"

    orchestrator = _make_orchestrator(
        plan_dir, project_dir, judge=_FakeJudge(None),
    )
    orchestrator._ask_a_human_about = _capture

    verdict = _run_terminal_round(
        orchestrator, report=_report_with_four_failed_points(),
    )

    assert verdict["should_stop"] is True
    assert asked, "the terminal round asked nobody"

    payload = json.loads(
        sorted((plan_dir / "reports").glob("*.json"))[0].read_text(
            encoding="utf-8"
        )
    )
    assert "failed_point_details" in payload, (
        "the round's archive does not carry the failed points' detail:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    details = payload["failed_point_details"]
    assert len(details) == 4, (
        f"expected one record per failed point, got {details!r}"
    )
    assert [detail["id"] for detail in details] == [
        "VP-1", "VP-2", "VP-3", "VP-4",
    ]
    for detail in details:
        assert set(detail) == set(_DETAIL_FIELDS)
        assert detail["status"] == "FAILED"
        assert detail["title"], f"the record names no title: {detail!r}"
        assert detail["actual_result"], (
            f"the record names no actual result: {detail!r}"
        )

    md_text = sorted((plan_dir / "reports").glob("*.md"))[0].read_text(
        encoding="utf-8"
    )
    lines = _detail_lines(md_text)
    assert len(lines) == 4, (
        f"the report does not list every failed point:\n{md_text}"
    )
    for detail in details:
        assert _detail_line(detail) in md_text, (
            f"the report does not list {detail['id']}'s detail:\n{md_text}"
        )

    # The card is the cut one; the report is the whole one.
    assert "见完整报告" in asked[-1], (
        f"the card does not send the reader to the report for the rest:\n"
        f"{asked[-1]}"
    )
