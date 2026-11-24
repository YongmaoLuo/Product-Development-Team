"""Tests for ``hooks.session_send`` — the events that are not decisions.

What is being pinned
--------------------
The dispatcher delivers more than one kind of event to the same
route.  A HITL decision is one of them; a plan whose finishing stage
failed is another, and it travels through the very same
``cc-connect send`` hook.  Both arrive at
:func:`hooks.session_send.format_event` as ``{"event": "...", ...}``.

The bug this file exists for: the finishing-stage failure was rendered
by the *decision* template, so the reader got a message that asked
them to decide something while carrying no decision, no failed item
and no evidence path — every field a placeholder.  A message like that
is worse than no message: it looks like somebody was asked, and the
facts that mattered were dropped.

So three properties carry the weight:

* **The finishing-stage failure names the plan, the failed items and
  the evidence.**  Those are the three facts the sender put in the
  payload, and all three have to survive into the message.
* **It is not the decision template.**  A reader must not be asked to
  answer a decision that does not exist, and a placeholder where a
  fact should be is the symptom of exactly that.
* **A decision still gets the decision template, byte for byte.**  The
  wording of a decision message is pinned by the existing tests, and
  adding a second kind of event must not move a single character of
  it.

The fourth property is about events nobody anticipated: they are
rendered generically — the event's name, the plan, and every field of
the payload — and never fall back to the decision template.

The fifth is that the delivery CLI uses the event-aware formatter, so
the message that reaches the transport is the one these tests describe.
That is checked through the real :func:`hooks.session_send.main`, with
a stub transport standing in for ``cc-connect`` — the same way the CLI
tests drive it.  Nothing here sends a message anywhere.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks.session_send import (  # noqa: E402
    PLACEHOLDER,
    format_event,
    format_message,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _postprocess_event(**overrides):
    """A finishing-stage failure event, the way the dispatcher sends it.

    The payload is the three-part one :mod:`postprocess.notify`
    builds — the plan, the items that failed and the evidence path —
    with the outlet's event name at the top level, which is what
    :func:`hooks.dispatcher.dispatch` puts there.
    """
    event = {
        "event": "postprocess_failed",
        "plan_id": "20261120-796c1bcb",
        "failed_items": ["fetch-origin", "merge-origin-main"],
        "evidence_path": "plans/20261120-796c1bcb/postprocess_progress.json",
    }
    event.update(overrides)
    return event


def _decision_event(**overrides):
    """A complete HITL decision event."""
    event = {
        "event": "hitl_decision_created",
        "plan_id": "p1",
        "decision_id": "d1",
        "question": "是否合入？",
        "options": ["accept", "revise"],
        "default_option": "accept",
        "deadline_at": "2026-10-26T12:00:00Z",
    }
    event.update(overrides)
    return event


def _write_stub(tmp_path, exit_code=0):
    """A stand-in for ``cc-connect`` that records how it was called.

    The same shell stub the CLI tests use: it writes its arguments
    one per line, copies everything it reads on stdin into a file, and
    exits with ``exit_code``.
    """
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    stub = tmp_path / "cc-connect"

    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > '{argv}'\n"
        "cat > '{stdin}'\n"
        "exit {code}\n".format(argv=argv_file, stdin=stdin_file, code=exit_code),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, argv_file, stdin_file


# ---------------------------------------------------------------------------
# The finishing-stage failure
# ---------------------------------------------------------------------------


def test_postprocess_failed_message_names_the_plan_items_and_evidence():
    """The message carries the three facts the payload carried.

    The plan says *which* plan, the failed items say *what* went
    wrong, and the evidence path says *where to look*.  A reader can
    act on none of them without the other two, and a message that
    dropped any one of them would be a failure notice that cannot be
    acted on.
    """
    message = format_event(_postprocess_event())

    assert "20261120-796c1bcb" in message
    assert "`fetch-origin`" in message
    assert "`merge-origin-main`" in message
    assert "plans/20261120-796c1bcb/postprocess_progress.json" in message


def test_postprocess_failed_message_is_not_the_decision_template():
    """The finishing-stage failure is not dressed up as a decision.

    It asks for no decision, so it must not ask for one: no
    "HITL 决策待处理" title, no question to answer, and — the symptom
    that gave the bug away — no placeholder standing where a fact
    should be.
    """
    message = format_event(_postprocess_event())

    assert "HITL 决策待处理" not in message
    assert PLACEHOLDER not in message


# ---------------------------------------------------------------------------
# The decision, unchanged
# ---------------------------------------------------------------------------


def test_decision_event_still_uses_the_decision_template():
    """A decision event renders exactly as :func:`format_message` writes it.

    The wording of a decision message is a contract with the reader
    that the existing tests pin byte for byte.  Adding a second kind
    of event must not move one character of it.
    """
    event = _decision_event()

    assert format_event(event) == format_message(event)


# ---------------------------------------------------------------------------
# An event nobody anticipated
# ---------------------------------------------------------------------------


def test_unknown_event_is_rendered_generically():
    """An event with no template still shows its name and its fields.

    It must not fall back to the decision template: that template asks
    a question the event does not contain, and every field of it shows
    a placeholder — a message that says nothing while pretending to
    be a decision.
    """
    message = format_event(
        {
            "event": "something_new",
            "plan_id": "p9",
            "detail": "the thing that happened",
            "items": ["a", "b"],
        }
    )

    assert "something_new" in message
    assert "p9" in message
    assert "the thing that happened" in message
    assert "a" in message and "b" in message
    assert PLACEHOLDER not in message
    assert "HITL 决策待处理" not in message


# ---------------------------------------------------------------------------
# The delivery CLI
# ---------------------------------------------------------------------------


def test_main_uses_the_event_kind_aware_formatter(tmp_path, monkeypatch):
    """What reaches the transport is the event's own template.

    The formatter is only worth having if the CLI uses it: a delivery
    that still formatted the finishing-stage failure with the decision
    template would send the reader exactly the message these tests say
    must never be sent.  The stub transport records what it was given
    on stdin, so this is the message a real group would receive.
    """
    stub, _, stdin_file = _write_stub(tmp_path)
    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))

    event = _postprocess_event()
    code = main(
        ["--project", "grp-1", "--session", "s-1"],
        io.StringIO(json.dumps(event, ensure_ascii=False)),
    )

    assert code == 0

    delivered = stdin_file.read_text(encoding="utf-8")
    assert delivered == format_event(event)
    assert "收尾失败" in delivered
    assert "`fetch-origin`" in delivered
    assert "HITL 决策待处理" not in delivered
