"""The whole terminal decision chain, one round, end to end.

Background
----------
The parts of the terminal decision live in three places, and each of them
has its own test:

* the **judge** reads the failure scene and answers with a six-field
  analysis (``test_terminal_analysis_judge.py``);
* the **decision card** is drawn from that analysis and is bounded
  (``test_terminal_analysis_brief_render.py``);
* the **archive** keeps the whole analysis on disk
  (``test_terminal_cpea_archive.py``, ``test_terminal_archive_path.py``).

What none of them does is walk a single round through **all** of them
and check that the four documents a human actually sees agree with each
other.  That is what this module is for: the judge answers, the card is
drawn, the payload carries the card and the path of the report, the
report is written under that path, and the host message quotes the card
without losing a line.

The chain is only as good as its weakest joint, and the joints are
where this plan's parts meet:

1. **The card is short, the report is whole.**  One analysis of about
   3KB — the size the judge really writes — is asked as a decision card
   of at most 600 characters, and is kept on disk as the full CPEA
   report.  A card that grew a section of the report would be a report
   nobody can answer; a report that had been cut down to a card would
   be a summary where the evidence was supposed to be.
2. **The path on the card is the file on disk.**  The card is built
   before the archive is written, so the path is computed; the archive
   is written after, so the name is stamped.  One clock reading for
   both, and the card's link resolves.
3. **The host message quotes every line of the card.**  A quote in
   Markdown is a block: a line without the marker ends the block, and
   the rest of the card arrives as body text of the message rather than
   as part of what is being asked.

2026-11-08: added for the decision card (arch decision points 1, 2 and
4; test decision points 2 and 4).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The host message: the card is quoted into it, line by line.
from hooks.session_send import format_message  # noqa: E402

# The orchestrator fixture is borrowed from the decision-question test,
# the same way ``tests/unit/test_terminal_analysis_render.py`` borrows
# it.  The fixture's judge answers with a fixed analysis and its
# ``result`` attribute can be swapped for a longer one.
from tests.unit.test_terminal_decision_question import (  # noqa: F401
    analysis_orchestrator,
)


# ---------------------------------------------------------------------------
# The sample: an analysis of the size the judge really writes
# ---------------------------------------------------------------------------

#: One round's worth of detail — the same shape the judge's answers
#: have: what the repair task covered, what the chain read next, and
#: why the round stopped with no repair task at all.
_DETAIL_TEMPLATE = (
    "第 {round} 轮生成的修复任务只覆盖了 VP-3 的失败分支，其余断言没有被"
    "触达；验证链路在下一轮仍然读到同一处 AssertionError: expected 200, "
    "got 500，于是被判定为实现缺陷而不是环境偶发，repair_generator 也不再"
    "为同一判据生成新的修复任务，第 {round} 轮的 repair 任务数因此为 0。"
)


def _details(count: int) -> str:
    return "\n".join(
        _DETAIL_TEMPLATE.format(round=index)
        for index in range(1, count + 1)
    )


def _long_analysis():
    """A six-field analysis of about 3KB — the size of a real one.

    The judge writes the report in full: the context names the rounds it
    read, the problem states what the chain could not fix, the evidence
    quotes the verdicts.  The card has to be shorter than any of that,
    which is why the sample is long.

    The four fields that carry the argument — the context, the problem,
    the first piece of evidence and the reason — are what make up the
    3KB; the rest is what the judge always adds around them.
    """
    return {
        "context": (
            "第 4 轮验证终止：实现已存在，但契约测试仍失败。\n" + _details(6)
        ),
        "problem": (
            "VP-3「登录接口返回 401」连续三轮失败，判定为实现缺陷而非环境偶发："
            "第 2 轮修的是 handler 的入参校验，第 3 轮修的是 client 的重试退避，"
            "两处改动都没有触达断言失败的那一行，"
            "因此第 4 轮读到的仍是同一个 AssertionError。"
            "链路无法自愈的根因是判据本身覆盖不到实现分支，"
            "而不是修复任务没有生成。"
        ),
        "evidence": [
            "VP-3 连续 3 轮失败：failure_class=deliverable_defect，"
            "verdict 为 AssertionError: expected 200, got 500，"
            "未见环境偶发特征（无超时、无 5xx 抖动、无依赖不可用）。",
            "第 4 轮 repair 任务数为 0：repair_generator 在同一判据连续失败"
            "达到阈值后不再生成新的修复任务。",
            "verification_report.json 的 generated_at 为 2026-11-08T10:10:10Z，"
            "state.db 的 plan_verification 行同步写入本轮结果。",
        ],
        "recommendation": "revise_criterion",
        "recommendation_reason": (
            "同一失败连续 3 轮，重跑不会再产生新信息："
            "判据覆盖不到实现分支，重跑只会再失败一次。"
        ),
        "uncertainty": (
            "未读 vp_artifacts/VP-3/ci_output.json 的第一条错误，"
            "因此不能断言断言失败的那一行属于哪一次改动引入的。"
        ),
    }


#: The analysis the judge answers with in the rounds below.
LONG_ANALYSIS = _long_analysis()

#: The clock reading both halves of the round are stamped with — the
#: card quotes the path of the archive, and the archive is written
#: under that path.
MOMENT = datetime(2026, 11, 8, 10, 10, 10, tzinfo=timezone.utc)

#: The five headings a report carries — and a card does not.
_CPEA_HEADINGS = (
    "## 背景（C）",
    "## 问题（P）",
    "## 证据（E）",
    "## 建议（A）",
    "## 不确定（U）",
)


def _report(*vp_ids):
    """A report whose points all failed, as the terminal branch sees it."""
    return {
        "verification_results": [
            {"id": vp_id, "status": "FAILED"} for vp_id in vp_ids
        ],
    }


def _run_the_round(orchestrator):
    """Ask the question of one terminal round, then archive it.

    The two halves are handed **one** clock reading: the card quotes
    the path of the archive, and the archive is written under that path.
    """
    orchestrator._terminal_judge.result = LONG_ANALYSIS

    payload = orchestrator._terminal_decision_question(
        _report("VP-3"), 4, ["VP-3"], 0, archive_moment=MOMENT,
    )
    orchestrator._save_terminal_cpea_archive(
        round_number=4,
        stop_reason="repair_execution_failed",
        question=payload["question"],
        options=payload["options"],
        default_option=payload["default_option"],
        analysis=LONG_ANALYSIS,
        decision_id="d1",
        failed_vp_ids=["VP-3"],
        now=MOMENT,
    )
    return payload


# ---------------------------------------------------------------------------
# 1. The card is short, the report is whole
# ---------------------------------------------------------------------------


def test_the_card_is_short_and_the_report_is_whole(analysis_orchestrator):
    """One analysis, two documents: a card to answer, a report to keep.

    The judge answered with about 3KB of argument.  The **card** is what
    the human answers: at most 600 characters, at most eight lines,
    naming the point that failed, the two options on the table, the
    recommendation and the path of the whole analysis — and nothing of
    the report's sections.

    The **report** is what a reader finds on disk afterwards: all five
    CPEA sections, with the evidence the judge quoted, carried whole.
    """
    payload = _run_the_round(analysis_orchestrator)
    card = payload["question"]

    # The sample has to be long, or the shortness below is vacuous: the
    # four fields that carry the argument are what the judge really
    # writes, and they are about 3KB.
    argument = (
        LONG_ANALYSIS["context"]
        + LONG_ANALYSIS["problem"]
        + LONG_ANALYSIS["evidence"][0]
        + LONG_ANALYSIS["recommendation_reason"]
    )
    sample_bytes = len(argument.encode("utf-8"))
    assert sample_bytes >= 2500, (
        f"the sample's argument is only {sample_bytes} bytes; the "
        "shortness assertion below would be vacuous"
    )

    # 1. The card: short, and carrying everything a decision needs.
    assert len(card) <= 600, (
        f"the card is {len(card)} characters, over the cap:\n{card}"
    )
    assert len(card.splitlines()) <= 8, (
        f"the card is {len(card.splitlines())} lines, over the cap:\n{card}"
    )
    assert "VP-3" in card, f"the failed point is not on the card:\n{card}"
    assert "rerun_this_round" in card, f"the first option is missing:\n{card}"
    assert "revise_criterion" in card, (
        f"the recommendation is missing:\n{card}"
    )
    assert payload["report_path"] in card, (
        f"the card does not point at the report:\n{card}"
    )

    # 2. The card is a card: it carries no section of the report.
    for heading in _CPEA_HEADINGS:
        assert heading not in card, (
            f"the card carries the report's {heading!r} section — a card "
            f"is a summary, not the report:\n{card}"
        )

    # 3. The path on the card is the file on disk.
    report_path = Path(payload["report_path"])
    assert report_path.exists(), (
        f"the card points at {report_path}, which was never written"
    )

    # 4. The report: the whole analysis, section for section.
    body = report_path.read_text(encoding="utf-8")
    for heading in _CPEA_HEADINGS:
        assert heading in body, (
            f"the report does not keep its {heading!r} section:\n{body}"
        )
    assert LONG_ANALYSIS["evidence"][0] in body, (
        "the report does not keep the judge's first piece of evidence:\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# 2. The host message quotes every line of the card
# ---------------------------------------------------------------------------


def test_the_host_message_quotes_every_card_line(analysis_orchestrator):
    """The message the host sends quotes the card, line by line.

    A quote in Markdown is a **block**: the marker goes on every line,
    and a line without the marker ends the block.  A card of eight
    lines quoted on its first line alone would arrive as a heading plus
    seven lines of body text — the reader would see the question and
    then the rest of it as something else.

    So every line of the card arrives inside the quote: the non-empty
    lines carry the marker and the line, and the blank lines carry the
    bare marker (a blank line with no marker at all would end the
    block).  No line of the card is dropped.
    """
    payload = _run_the_round(analysis_orchestrator)
    card = payload["question"]

    message = format_message({
        "plan_id": "plan",
        "question": card,
        "options": payload["options"],
        "default_option": payload["default_option"],
    })

    quoted = [
        line for line in message.splitlines() if line.startswith(">")
    ]
    card_lines = card.splitlines()

    assert len(quoted) == len(card_lines), (
        f"the message quotes {len(quoted)} lines, the card has "
        f"{len(card_lines)}:\n{message}"
    )
    for line, quoted_line in zip(card_lines, quoted):
        if line.strip():
            assert quoted_line == f"> {line.rstrip()}", (
                f"the card's line {line!r} did not arrive inside the "
                f"quote:\n{message}"
            )
        else:
            assert quoted_line == ">", (
                f"the card's blank line did not arrive inside the quote:\n"
                f"{message}"
            )