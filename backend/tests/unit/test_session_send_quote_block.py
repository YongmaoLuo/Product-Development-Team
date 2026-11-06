"""Tests for ``hooks.session_send`` — the quoted question.

What is being pinned
--------------------
A decision reaches a human as a **message**, and the question is the
one part of it that is prose.  Prose is often more than one line: a
decision that has to explain itself arrives carrying a heading, a
paragraph and a second heading.

The message quotes the question, and a quote in Markdown is a *block*
— a run of lines that all carry the marker.  So the question has to be
quoted line by line.  A marker on the first line alone leaves the
remaining lines outside the quote, and a reader sees the second half
of the question as body text of the message rather than as part of
what is being asked.  That is the failure this module exists to
prevent.

The four branches, and why each one is its own case:

* **the quote stays one block** — every line of the question carries
  the marker, so nothing that belongs to the question leaks into the
  body of the message;
* **every line is quoted** — the marker goes on *every* line, not just
  the first;
* **a blank line is a bare marker** — a line with nothing on it still
  carries the marker, so it cannot end the block;
* **a one-line question is untouched** — the change is only about
  questions of more than one line, so a one-line question comes out
  byte for byte what it came out before.

The module under test is deliberately pure — no I/O, no clock, no
environment — so everything here is about the text.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks.session_send import format_message  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: A question that carries a whole report: a heading, a line of body, a
#: blank line, and a second heading with a line of body.  It is the
#: shape a decision that has to explain itself arrives in.
_MULTILINE_QUESTION = (
    "## 背景（C）\n"
    "第 3 轮停止\n"
    "\n"
    "## 问题（P）\n"
    "无法自愈"
)


def _event(**overrides):
    """A decision event, the way the outlet builds one."""
    event = {
        "plan_id": "p1",
        "decision_id": "d1",
        "question": "是否合入？",
        "options": ["accept", "revise"],
        "default_option": "accept",
        "deadline_at": "2026-10-26T12:00:00Z",
    }
    event.update(overrides)
    return event


def _quote_block(message: str) -> str:
    """The quoted question, as it sits between the title and the fields.

    The message is three blocks separated by blank lines: the title,
    the question as a quote, and the list of fields.  A question of
    several lines is still one block — the lines inside it are quoted,
    not blank — so the split finds exactly three parts.
    """
    parts = message.split("\n\n")
    assert len(parts) == 3, "the message is not three blocks: {0!r}".format(message)
    return parts[1]


# ---------------------------------------------------------------------------
# The four branches
# ---------------------------------------------------------------------------


def test_quote_block_is_one_block_for_multiline_question():
    """The whole question is one quoted block.

    The question carries a heading, a paragraph, a blank line and a
    second heading with a paragraph.  Every one of those lines has to
    carry the quote marker: a line without it ends the quote, and the
    rest of the question — the part that says what is actually being
    asked — arrives as body text of the message instead of as the
    thing being asked.
    """
    message = format_message(_event(question=_MULTILINE_QUESTION))

    quote = _quote_block(message)
    lines = quote.split("\n")

    for line in lines:
        assert line.startswith(">"), (
            "the line {0!r} of the question carries no quote marker, so it "
            "leaves the quote block".format(line)
        )

    assert quote == (
        "> ## 背景（C）\n"
        "> 第 3 轮停止\n"
        ">\n"
        "> ## 问题（P）\n"
        "> 无法自愈"
    )

    # The quote is one block: nothing inside it is a blank line, and so
    # nothing inside it can end the block early.
    assert "\n\n" not in quote


def test_multiline_question_every_line_is_quoted():
    """Every line of a multi-line question carries the quote marker.

    The question is the one part of the message that is prose, and
    prose of more than one line has to be quoted line by line: a
    marker on the first line alone leaves the rest of the question
    outside the quote, where it reads as the body of the message
    rather than as the thing being asked.

    The question here has no blank line in it, so every one of its
    lines has something on it and every one of them is quoted with
    the marker *and a space*.  A line with nothing on it is the other
    branch — the bare marker — and it has its own case below.
    """
    question = "## 背景（C）\n第 3 轮停止\n## 问题（P）\n无法自愈"

    message = format_message(_event(question=question))

    quote = _quote_block(message)

    for line in quote.split("\n"):
        assert line.startswith("> "), (
            "the line {0!r} of the question does not start with the quote "
            "marker and a space".format(line)
        )

    assert quote == (
        "> ## 背景（C）\n"
        "> 第 3 轮停止\n"
        "> ## 问题（P）\n"
        "> 无法自愈"
    )

    # A blank line in the question is the one line that is not quoted
    # with a space — it carries the bare marker so that the quote
    # stays one block.
    with_a_blank_line = format_message(_event(question=_MULTILINE_QUESTION))

    assert _quote_block(with_a_blank_line) == (
        "> ## 背景（C）\n"
        "> 第 3 轮停止\n"
        ">\n"
        "> ## 问题（P）\n"
        "> 无法自愈"
    )


def test_blank_line_inside_question_is_a_bare_quote():
    """A blank line inside the question is a bare quote marker.

    A blank line has nothing to quote, so it carries the marker and
    nothing else.  A line with no marker at all would end the quote
    and let the rest of the question out into the body of the
    message; a line with a trailing space after the marker would be
    the same quote, only harder to read.
    """
    message = format_message(_event(question="第一段\n\n第二段"))

    assert _quote_block(message).split("\n") == [
        "> 第一段",
        ">",
        "> 第二段",
    ]

    whitespace_only = format_message(_event(question="第一段\n   \n第二段"))

    assert _quote_block(whitespace_only).split("\n") == [
        "> 第一段",
        ">",
        "> 第二段",
    ]


def test_single_line_question_golden_unchanged():
    """A one-line question produces the message it always produced.

    Quoting a question line by line is only allowed to change what
    happens to a question of more than one line.  A one-line question
    has to come out byte for byte what it came out before, because
    that wording is what a reader has been looking at.
    """
    message = format_message(_event())

    assert message == (
        "**⏳ HITL 决策待处理** · `p1`\n"
        "\n"
        "> 是否合入？\n"
        "\n"
        "- **决策 ID**：`d1`\n"
        "- **选项**：1) accept 2) revise\n"
        "- **默认答案**：accept（超时未答时采用）\n"
        "- **截止时间**：2026-10-26T12:00:00Z"
    )
