"""Tests for ``hooks.session_send`` — the HITL delivery message format.

What is being pinned
--------------------
A decision that is asked of a human reaches them as a **message**, and
the message is built from the event JSON by one pure function:
:func:`hooks.session_send.format_message`.  Two properties carry the
weight, and each of them is a way the "the message says what happened"
rule can be broken by accident:

* **The message says everything the human needs.**  The decision's
  identity, the question itself, the answers on the table, the answer
  that will be used if nobody replies and the deadline are all in it.
  A message that dropped the question would say *that* a decision was
  asked without saying what was being asked about.
* **A missing field is a placeholder, not a crash.**  The event is
  assembled by another module and read by a third, and a field that is
  absent — or an event that is not a mapping at all — must produce a
  message with a visible placeholder in it rather than an exception on
  the delivery path.  A delivery that raises is a delivery that never
  happens, and the decision it was carrying becomes ``unroutable``.

The format itself is pinned exactly: the wording, the labels and the
enumeration are the contract with the reader on the other end, so a
reformat is a change to what a human is shown, not a refactor.

The module is deliberately pure — no I/O, no clock, no environment.
That is what lets the later delivery CLI reuse it without dragging a
side effect along.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks.session_send import PLACEHOLDER, format_message  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(**overrides):
    """A complete decision event, the way the outlet builds one."""
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


# ---------------------------------------------------------------------------
# The complete message
# ---------------------------------------------------------------------------


def test_message_contains_all_fields():
    """A complete event produces a message that carries every field.

    Six fields, six places the reader can look: which plan, which
    decision, what is being asked, what the answers are, which answer
    happens by default and how long there is to reply.
    """
    message = format_message(_event())

    assert "p1" in message
    assert "d1" in message
    assert "是否合入？" in message
    assert "accept" in message
    assert "revise" in message
    assert "2026-10-26T12:00:00Z" in message


def test_message_has_the_agreed_shape():
    """The wording and the labels are the contract with the reader."""
    message = format_message(_event())

    assert message == (
        "[HITL] 计划 p1 需要你的决定\n"
        "决策 ID：d1\n"
        "问题：是否合入？\n"
        "选项：1) accept 2) revise\n"
        "默认答案：accept\n"
        "截止时间：2026-10-26T12:00:00Z"
    )


def test_options_are_enumerated():
    """The answers are numbered, so a reply can name one."""
    message = format_message(_event())

    assert "1) accept" in message
    assert "2) revise" in message


# ---------------------------------------------------------------------------
# The degraded inputs
# ---------------------------------------------------------------------------


def test_missing_fields_do_not_raise():
    """An empty event produces a message of placeholders.

    The event is somebody else's data.  A field that is not there is
    shown as not there — visibly, in the message a human reads — and
    the delivery path does not raise on the way out.
    """
    message = format_message({})

    assert isinstance(message, str)
    assert PLACEHOLDER in message


def test_missing_fields_are_named_per_line():
    """Each absent field shows a placeholder on its own line.

    One placeholder for the whole message would leave the reader
    guessing *which* fact is missing, and a message that says
    "（未提供）" five times is not more informative than one that says
    it once.
    """
    message = format_message({})

    assert message == (
        f"[HITL] 计划 {PLACEHOLDER} 需要你的决定\n"
        f"决策 ID：{PLACEHOLDER}\n"
        f"问题：{PLACEHOLDER}\n"
        f"选项：{PLACEHOLDER}\n"
        f"默认答案：{PLACEHOLDER}\n"
        f"截止时间：{PLACEHOLDER}"
    )


def test_empty_field_is_treated_as_missing():
    """An empty value is a missing value."""
    message = format_message(_event(question="", default_option=None))

    assert message.count(PLACEHOLDER) == 2


def test_non_mapping_input_returns_string():
    """An event that is not a mapping still produces a message.

    ``None`` and a list are the two shapes that reach this function
    when something upstream is wrong.  Neither is a reason to raise on
    the delivery path: the reader gets a message with placeholders,
    and the failure shows up where it can be seen.
    """
    for event in (None, ["not", "a", "mapping"], "text", 42):
        message = format_message(event)

        assert isinstance(message, str)
        assert PLACEHOLDER in message


def test_options_that_are_not_a_list_are_treated_as_missing():
    """``options`` that is not a list is not a set of answers.

    A string is not a list of answers — it is one answer written out —
    and enumerating it would produce one numbered line per character.
    """
    message = format_message(_event(options="accept"))

    assert f"选项：{PLACEHOLDER}" in message


def test_empty_options_are_treated_as_missing():
    """An empty option list is no options at all."""
    message = format_message(_event(options=[]))

    assert f"选项：{PLACEHOLDER}" in message