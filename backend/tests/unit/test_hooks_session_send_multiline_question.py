"""Tests for ``hooks.session_send`` — a question of more than one line.

What is being pinned
--------------------
A decision reaches a human as a **message**, and the question is the
one part of it that is prose.  Prose is often more than one line: a
decision that has to explain itself arrives carrying a heading, a
paragraph and a second heading.

The message quotes the question, and a quote in Markdown is a *block*
— a run of lines that all carry the marker.  So the question is quoted
line by line.  A marker on the first line alone leaves the remaining
lines outside the quote, and a reader sees the second half of the
question as body text of the message rather than as part of what is
being asked.  That is the failure this module exists to prevent.

What is pinned here:

* **The quote stays one block.**  Every line of the question carries
  the quote marker, so the whole question renders as one quoted block
  and nothing that belongs to the question leaks into the body of the
  message.
* **A question that is not a string is its text first.**  The event is
  JSON, and a field can carry a number; the question is turned into
  text before it is quoted, so a non-string question is quoted the same
  way a string one is.

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
# The quote is one block
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
    question = "## 背景（C）\n第 3 轮停止\n\n## 问题（P）\n无法自愈"

    message = format_message(_event(question=question))

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


def test_non_string_question_is_text_before_it_is_quoted():
    """A question that is not a string is its text form, quoted.

    The event is JSON, and a field can carry a number.  The question
    is turned into text first — ``str()`` and stripped — and the text
    is what gets quoted, so the number arrives as a quoted number
    rather than as a crash or as a missing field.
    """
    message = format_message(_event(question=42))

    assert _quote_block(message) == "> 42"
