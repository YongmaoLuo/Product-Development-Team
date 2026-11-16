"""Unit tests for the four context sections of ``build_progress_sections``.

The scheduler page's plan-detail panel has to answer the same questions
the Feishu card answers — *what is running right now*, *what is waiting
on what*, *what did the verification round say* and *what failed and
why* — and it must not grow a second phrasing of them. The wording
lives in the card's section builders, and the panel renders those
builders' output.

These tests pin the four sections that complete the structured progress
area:

  * ``in_progress`` — the running tasks, as ``id`` / ``title`` rows;
  * ``blocked`` — the tasks an upstream failure holds up, as ``id`` /
    ``title`` / ``dep`` / ``state`` rows;
  * ``verification`` — the round's status, number, bound and stop
    reason, and the VPs that failed it;
  * ``failure`` — the failed tasks as ``id`` / ``title`` / ``reason`` /
    ``attempt`` / ``max_attempts`` rows;

and the rules around them: the sections come out in a fixed order, every
section's wording is the card builder's own wording, a section whose
data is absent does not appear at all, and the verification section's
terminal judgement is the one :func:`notifications.cards.build_card`
makes — the plan's *phase* is never a source for it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from notifications import cards
from notifications.cards import build_progress_sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(**overrides: Any) -> Dict[str, Any]:
    """A plan-status-shaped payload carrying every kind of context."""
    payload: Dict[str, Any] = {
        "plan_id": "p1",
        "current_phase": "verification_failed",
        "summary": {
            "state": {"current_phase": "verification_failed"},
            "tasks": {"total": 4, "completed": 1, "failed": 1},
        },
        "execution": {
            "tasks": [
                {"id": "1-1", "title": "写门禁", "status": "in_progress"},
                {
                    "id": "1-2", "title": "跑测试", "status": "failed",
                    "failure_reason": "断言失败",
                },
                {
                    "id": "1-3", "title": "发布", "status": "pending",
                    "depends_on": ["1-2"],
                },
                {
                    "id": "1-4", "title": "收尾", "status": "pending",
                    "depends_on": ["1-3"],
                },
            ],
        },
        "verification": {
            "verification_status": "failed",
            "verification_round": 2,
            "max_rounds": 4,
            "stop_reason": "same_failure_repeated_after_max_attempts",
            "failed_vps": ["VP-2"],
            "vps": [{"id": "VP-2", "title": "端口"}],
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


def _plan_status(verification_status: str) -> Any:
    """A snapshot carrying one verification status — the card's input.

    The phase is the payload's own, so the card and the panel are
    looking at the same snapshot: the card renders its body from the
    snapshot it is handed, and the panel renders its body from the
    snapshot its own context carries.
    """
    from plan_status import PlanStatus

    return PlanStatus(
        plan_id="p1",
        phase="verification_failed",
        verification_status=verification_status,
        verification_round=2,
        verification_max_rounds=4,
        execution_in_flight=False,
        verification_in_flight=False,
        tasks={},
    )


def _card_text(card: Dict[str, Any]) -> str:
    """Everything the card rendered, as one string."""
    return "\n".join(cards._section_text_lines(card["elements"]))


# ---------------------------------------------------------------------------
# The four context sections
# ---------------------------------------------------------------------------


def test_in_progress_section_lists_running_tasks():
    """The running tasks are listed with their id and their title."""
    sections = build_progress_sections(_payload())

    in_progress = _section(sections, "in_progress")
    assert in_progress is not None, "a payload with a running task has one"
    assert in_progress["items"] == [{"id": "1-1", "title": "写门禁"}]

    # The wording is the card's own running-tasks block.
    assert "写门禁" in in_progress["text"]
    assert "🔄 **正在执行：**" in in_progress["text"]


def test_blocked_section_lists_waiting_and_failed_deps():
    """A blocked task says which dep holds it — and whether it failed."""
    sections = build_progress_sections(_payload())

    blocked = _section(sections, "blocked")
    assert blocked is not None, "a payload with blocked tasks has one"
    assert blocked["items"] == [
        {"id": "1-3", "title": "发布", "dep": "1-2", "state": "failed"},
        {"id": "1-4", "title": "收尾", "dep": "1-3", "state": "waiting"},
    ]

    assert "**🚧 被阻塞的待执行任务：**" in blocked["text"]
    assert "• `1-3` 发布　↳ 被 [`1-2`] 阻塞（失败）" in blocked["text"]
    assert "• `1-4` 收尾　↳ 等待 [`1-3`]" in blocked["text"]


def test_verification_section_carries_round_and_failed_vps():
    """The verification section carries the round's facts and the VPs."""
    sections = build_progress_sections(_payload())

    verification = _section(sections, "verification")
    assert verification is not None, "a payload with verification has one"
    assert verification["status"] == "failed"
    assert verification["round"] == 2
    assert verification["max_rounds"] == 4
    assert verification["stop_reason"] == (
        "same_failure_repeated_after_max_attempts"
    )
    assert verification["failed_vps"] == [{"id": "VP-2", "title": "端口"}]
    assert verification["text"], "the section carries the card's wording"


def test_failure_section_lists_failed_task_reason():
    """A failed task is listed with the reason it failed."""
    sections = build_progress_sections(_payload())

    failure = _section(sections, "failure")
    assert failure is not None, "a payload with a failed task has one"
    assert len(failure["items"]) == 1

    item = failure["items"][0]
    assert item["id"] == "1-2"
    assert item["title"] == "跑测试"
    assert item["reason"] == "断言失败"
    assert item["attempt"] == 1
    assert item["max_attempts"] >= 1

    # The wording is the card's own failure detail.
    assert "**❌ 失败任务详情：**" in failure["text"]
    assert "• `[1-2]` 跑测试" in failure["text"]
    assert "└─ 断言失败" in failure["text"]


def test_absent_data_emits_no_section():
    """A section whose data is absent does not appear at all."""
    # Neither an execution nor a verification.
    sections = build_progress_sections({
        "plan_id": "p1",
        "current_phase": "executing",
        "summary": {"tasks": {"total": 1, "completed": 1}},
    })
    kinds = _kinds(sections)
    assert "in_progress" not in kinds, kinds
    assert "blocked" not in kinds, kinds
    assert "verification" not in kinds, kinds
    assert "failure" not in kinds, kinds

    # An execution with nothing running, nothing blocked, nothing failed.
    sections = build_progress_sections(_payload(execution={
        "tasks": [{"id": "1-1", "title": "已完成", "status": "completed"}],
    }))
    kinds = _kinds(sections)
    assert "blocked" not in kinds, kinds
    assert "failure" not in kinds, kinds
    # The running-tasks section is the execution part's own section: it is
    # there, and it says that nothing is running.
    in_progress = _section(sections, "in_progress")
    assert in_progress is not None, kinds
    assert in_progress["items"] == []
    # The verification section is a different kind of data — it stays.
    assert "verification" in kinds, kinds


def test_section_text_comes_from_builders():
    """Every section's wording is the card builder's own wording."""
    payload = _payload()
    sections = build_progress_sections(payload)
    context = cards._progress_context(payload)

    in_progress = _section(sections, "in_progress")
    assert in_progress["text"] == "\n".join(cards._section_text_lines(
        cards._in_progress_tasks_section(
            context["task_progress"], context["activity"],
        )
    ))

    blocked = _section(sections, "blocked")
    assert blocked["text"] == "\n".join(cards._section_text_lines(
        cards._blocked_tasks_section(context["execution"])
    ))

    failure = _section(sections, "failure")
    assert failure["text"] == "\n".join(cards._section_text_lines(
        cards._failed_tasks_section(context["execution"])
    ))

    verification = _section(sections, "verification")
    # The payload's round failed, so the wording is the terminal one —
    # the judgement the section itself makes, spelled out here so the
    # comparison below is against a known value and not against
    # whatever the implementation happens to do.
    is_terminal = "failed" in cards._VERIFICATION_TERMINAL_STATUSES
    assert verification["text"] == "\n".join(cards._section_text_lines(
        cards._verification_sections(
            context["verification"], is_terminal, context["status"],
        )
    ))


def test_verification_is_terminal_matches_build_card():
    """The terminal judgement is the one ``build_card`` makes."""
    for verification_status, terminal in (
        ("passed", True),
        ("failed", True),
        ("loop_stopped", True),
        ("running", False),
    ):
        payload = _payload(verification_status=verification_status)
        sections = build_progress_sections(payload)
        verification = _section(sections, "verification")
        assert verification is not None, verification_status
        assert verification["status"] == verification_status.lower()

        # The card decides the same thing from the same snapshot field.
        card = cards.build_card(
            "p1",
            _plan_status(verification_status),
            verification_progress=payload["verification"],
        )
        card_text = _card_text(card)
        assert ("📋 历史轮次" in card_text) is terminal, verification_status

        # And the panel says what the card says about the round.
        assert ("📋 历史轮次" in verification["text"]) is terminal, (
            verification_status
        )
        assert verification["text"] in card_text, verification_status


def test_verification_status_falls_back_to_the_progress_payload():
    """A payload with no snapshot status reads the round's own status."""
    payload = _payload()
    assert "verification_status" not in payload, payload

    sections = build_progress_sections(payload)
    verification = _section(sections, "verification")
    assert verification is not None
    assert verification["status"] == "failed"
    assert verification["round"] == 2
    assert verification["max_rounds"] == 4
    assert verification["stop_reason"] == (
        "same_failure_repeated_after_max_attempts"
    )

    # The plan's PHASE is not a source for any of it: the phase here
    # says the plan failed its verification, and the round's status is
    # what the section reports.
    card = cards.build_card(
        "p1",
        _plan_status("failed"),
        verification_progress=payload["verification"],
    )
    assert "📋 历史轮次" in _card_text(card)


def test_section_order_is_fixed():
    """The sections come out in the card's order."""
    sections = build_progress_sections(_payload())

    assert _kinds(sections) == [
        "status",
        "phase",
        "task_progress",
        "in_progress",
        "blocked",
        "verification",
        "failure",
    ]
