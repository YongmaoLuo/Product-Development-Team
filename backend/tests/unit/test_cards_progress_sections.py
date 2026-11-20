"""Unit tests for ``cards.build_progress_sections`` — the structured progress area.

The Feishu progress card answers three questions in its body: where the
plan is right now, how many tasks are done, and how the plan is
labelled. The scheduler page's plan-detail panel answers the same
three questions, and it must not grow a second phrasing of them —
the wording lives in the card's section builders, and the web view
reuses those builders instead of writing its own sentences.

``build_progress_lines`` answered with flattened text lines, which is
all a line-by-line renderer can use. The detail panel needs the same
facts as **structure**: a status section (plan id, label, colour,
elapsed time), a phase section (the card's unified phase banner) and a
task-progress section (the counts, the percentage and the bar).

These tests pin:

  * the four sections come out in a fixed order — status, phase, task
    progress, running tasks — and the status section is always first;
  * the wording of every section is the card's own wording: the phase
    section says what the ``phase`` line says, and the task-progress
    section says what the ``summary`` line says;
  * the structured facts (counts, percentage, bar) are the card's own
    structured facts (``_task_summary_data``);
  * a payload with nothing in it produces no sections at all, and one
    with a plan but no phase produces the status section alone.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from notifications import cards
from notifications.cards import build_progress_lines, build_progress_sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(**overrides: Any) -> Dict[str, Any]:
    """A plan-status-shaped payload: a plan, a phase and some tasks."""
    payload: Dict[str, Any] = {
        "plan_id": "p1",
        "current_phase": "executing",
        "summary": {
            "state": {"current_phase": "executing"},
            "tasks": {"total": 4, "completed": 3},
        },
        "execution": {
            "tasks": [{"id": "1-3", "title": "写门禁", "status": "in_progress"}],
            "started_at": "2026-11-14T10:00:00",
        },
    }
    payload.update(overrides)
    return payload


def _kinds(sections: List[Dict[str, Any]]) -> List[str]:
    return [section["kind"] for section in sections]


def _section(
    sections: List[Dict[str, Any]], kind: str,
) -> Optional[Dict[str, Any]]:
    for section in sections:
        if section["kind"] == kind:
            return section
    return None


def _lines_of(payload: Dict[str, Any], kind: str) -> List[str]:
    return [
        line["text"]
        for line in build_progress_lines(payload)["lines"]
        if line["kind"] == kind
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_status_section_first_with_plan_and_phase():
    """The status section is the first one, always."""
    sections = build_progress_sections(_payload())

    assert sections, "a payload with a plan and a phase must produce sections"
    assert sections[0]["kind"] == "status", sections
    status = sections[0]
    assert status["plan_id"] == "p1"
    assert status["phase"] == "executing"
    assert status["title"], "the status section carries the card's title"
    assert status["color"], "the status section carries the card's colour"


def test_status_phase_label_override_wins():
    """An explicit label beats the card's own phase table."""
    sections = build_progress_sections(_payload(), phase_label="自定义阶段")

    assert sections[0]["phase_label"] == "自定义阶段"

    # Without the override the label is the card's own — the one table
    # that knows how a phase is called.
    default = build_progress_sections(_payload())[0]
    assert default["phase_label"] == cards._PHASE_LABEL["executing"]


def test_phase_section_matches_progress_lines():
    """The phase section says what the card's ``phase`` line says."""
    payload = _payload()

    phase = _section(build_progress_sections(payload), "phase")
    assert phase is not None, "a payload with a phase has a phase section"

    phase_lines = _lines_of(payload, "phase")
    assert phase_lines
    assert phase["text"] == "\n".join(phase_lines)


def test_task_progress_section_matches_summary_line():
    """The task-progress section says what the card's ``summary`` line says."""
    payload = _payload()

    progress = _section(build_progress_sections(payload), "task_progress")
    assert progress is not None, "a payload with tasks has a task-progress section"

    summary_lines = _lines_of(payload, "summary")
    assert summary_lines
    assert progress["text"] == "\n".join(summary_lines)

    # The structured facts are the card's own structured facts.
    data = cards._task_summary_data({"total": 4, "completed": 3})
    assert data is not None
    assert progress["counts"] == data["counts"]
    assert progress["percent"] == data["percent"]
    assert progress["bar"] == data["bar"]
    assert progress["percent"] == 75
    assert progress["bar"] == "███████░░░"


def test_empty_payload_returns_no_sections():
    """A payload with nothing in it has nothing to show."""
    assert build_progress_sections({}) == []
    assert build_progress_sections(None) == []
    assert build_progress_sections("not a payload") == []


def test_build_progress_lines_unchanged():
    """The lines and the sections say the same thing, word for word."""
    payload = _payload()

    sections = build_progress_sections(payload)
    phase = _section(sections, "phase")
    progress = _section(sections, "task_progress")

    assert phase is not None and progress is not None
    assert phase["text"] == "\n".join(_lines_of(payload, "phase"))
    assert progress["text"] == "\n".join(_lines_of(payload, "summary"))

    # The flattened lines are still there, and still the card's own.
    kinds = [line["kind"] for line in build_progress_lines(payload)["lines"]]
    assert kinds[:2] == ["phase", "summary"]
    assert set(kinds[2:]) <= {"task"}


def test_section_order_is_status_phase_task_progress_then_running():
    """The four sections come out in the card's order."""
    sections = build_progress_sections(_payload())

    assert _kinds(sections) == [
        "status", "phase", "task_progress", "in_progress",
    ]


def test_status_section_without_phase_falls_back_to_plan_id():
    """A plan with no phase yet still shows a status line."""
    sections = build_progress_sections({"plan_id": "p9"})

    assert _kinds(sections) == ["status"]
    status = sections[0]
    assert status["title"] == "p9"
    assert status["phase"] == ""
    assert status["phase_label"] == ""


def test_running_sec_must_be_a_positive_int():
    """A runtime that is not a positive integer is reported as unknown."""
    assert build_progress_sections(_payload())[0]["running_sec"] is None
    assert build_progress_sections(_payload(), running_sec=90)[0]["running_sec"] == 90

    for bad in (0, -5, None, "90", 90.0, True):
        assert build_progress_sections(
            _payload(), running_sec=bad,
        )[0]["running_sec"] is None, bad


def test_status_section_carries_the_started_at():
    """The status section says when the run started."""
    status = build_progress_sections(_payload())[0]

    assert status["started_at"] == "2026-11-14T10:00:00"


# ---------------------------------------------------------------------------
# Header parity with the Feishu card
# ---------------------------------------------------------------------------


def _repair_payload(**overrides: Any) -> Dict[str, Any]:
    """A snapshot of a plan whose repair round is executing.

    The shape is the one ``GET /api/plan/{id}/status`` answers with: the
    top-level fields the server computed (``execution_in_flight`` among
    them) AND the summary section the panel reads its state from. The
    verification verdict is already ``failed`` — the repair round that
    follows it is what is running now.
    """
    payload = _payload(
        current_phase="executing",
        summary={
            "state": {
                "current_phase": "executing",
                "verification": {
                    "status": "failed",
                    "round": 3,
                    "max_rounds": 4,
                },
            },
            "tasks": {"total": 4, "completed": 3},
        },
        verification_status="failed",
        verification_round=3,
        verification_max_rounds=4,
        execution_in_flight=True,
        verification_in_flight=False,
        tasks={"total": 4, "completed": 3},
    )
    payload.update(overrides)
    return payload


def test_sections_header_live_repair_reads_payload_execution_in_flight():
    """A running repair round reads as running, from the snapshot's word.

    The panel rebuilt its status from the summary alone, and that rebuild
    cannot see liveness — so a plan mid-repair said "❌ 验证失败" while
    the Feishu card on the very same plan said "🔧 修复执行中".
    """
    sections = build_progress_sections(_repair_payload())

    assert sections[0]["title"] == "🔧 修复执行中 · p1 round 3/4"
    assert sections[0]["color"] == "blue"


def test_sections_header_terminal_failed_when_execution_not_in_flight():
    """A finished repair round stays a failed verification.

    The liveness flag must only ever turn a terminal verdict into a live
    one — never the other way round.
    """
    sections = build_progress_sections(
        _repair_payload(execution_in_flight=False),
    )

    assert sections[0]["title"] == "❌ 验证失败 · p1"
    assert sections[0]["color"] == "red"


def test_sections_header_matches_feishu_card_header():
    """The panel's header and the Feishu card's header are one answer.

    Both are rendered from the same snapshot, so they must read the same
    — this test is the tie between the two surfaces.
    """
    from plan_status import from_payload

    payload = _repair_payload()
    sections = build_progress_sections(payload)
    status = from_payload(payload)
    card = cards.build_card("p1", status, summary={"tasks": payload["tasks"]})

    assert sections[0]["title"] == card["header"]["title"]["content"]
    assert sections[0]["title"] == "🔧 修复执行中 · p1 round 3/4"
