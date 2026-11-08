"""What the panel shows after a write must be the server's answer.

Why this gate exists
--------------------
``schedulerApplyCapacity(total)`` wrote the operator's pick and then
read the state again.  The read was the only thing that put a number
on the control, so two things went wrong.

The server's answer was never read.  The server may regularize the
pick — the override may be refused, the number may be capped — and
the operator never learned: the panel showed whatever the read
brought, and a number the server did not take was passed off as the
number in force.

And an **old** read could win.  The panel polls every five seconds,
and a poll that was already in flight when the write went out carries
the snapshot from *before* that write.  When it came back it painted
over the number that had just been applied, so the control jumped back
to a number the scheduler was not running with.

The rule
--------
* ``schedulerApplyCapacity()`` reads ``capacity.total`` out of the
  answer and records it in ``select.dataset.serverValue``: the number
  in force is the server's, and it is recorded from the answer — not
  from the pick that was sent.
* When the answer's number is not the number that was asked for, the
  panel says so: the server regularized the pick and the operator is
  told what it decided.  When the answer agrees with the pick, the
  notice is cleared, so a message from an earlier write does not
  outlive it.
* ``loadSchedulerState()`` numbers its reads and only lets the newest
  one paint.  A read that a newer read has superseded returns without
  painting, so a snapshot from before a write cannot undo that write.
* A failed write goes through the existing error path and leaves the
  recorded number alone.

Scan target
-----------
``frontend/app.js`` and ``frontend/index.html``, read as source.  This
is a shape gate, like the rest of ``static_gates/``: it runs on a
plain Python environment with no JS toolchain, so it pins the contract
in the source rather than executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"

#: The name of the module-level counter that numbers the state reads.
_SEQ_NAME = "schedulerStateSeq"

#: The id of the element that says what the server did with the pick.
_NOTICE_ID = "scheduler-capacity-notice"

#: The id of the element that says where the number in force came from.
_SOURCE_ID = "scheduler-capacity-source"

#: The words the notice carries when the server regularized the pick.
_NOTICE_WORDS = "服务端规整为"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError`` — a gate that dies on a
    malformed byte reports nothing useful about the contract it was
    written to protect.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    The body ends at the first line that is exactly ``}`` — the file's
    top-level functions close at column zero, so that line ends the
    function no matter what the body contains.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)",
        re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, f"function {name}() not found in the source"
    lines = source[match.end() :].splitlines()
    collected: list[str] = []
    for line in lines:
        if line.rstrip() == "}":
            return "\n".join(collected)
        collected.append(line)
    raise AssertionError(f"unterminated function {name}()")


def _declaration(body: str, name: str) -> str:
    """Return the text of the declaration that gives ``name`` its value.

    The declaration is the ``const``/``let``/``var`` statement that
    names the variable; only the text up to the closing semicolon is
    returned, so the chain that produced the value can be walked.
    """
    match = re.search(
        r"(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*([^;]+);", body
    )
    assert match is not None, f"no declaration found for {name!r}"
    return match.group(1)


def _server_number(body: str) -> str:
    """Return the name of the variable that holds the server's number.

    The number the control records is written as
    ``select.dataset.serverValue = String(<name>)``, so the name in that
    assignment is the one the rest of the gate walks back from.
    """
    match = re.search(
        r"select\.dataset\.serverValue\s*=\s*String\(\s*(\w+)\s*\)", body
    )
    assert match is not None, (
        "schedulerApplyCapacity() never records the server's number in "
        "select.dataset.serverValue, so the control has no second copy "
        "of the number in force to fall back on."
    )
    return match.group(1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_apply_reads_the_server_total() -> None:
    """The number in force must come from the server's answer.

    The pick is what the operator *asked* for; the answer is what the
    server *took*.  The record on the control exists so the control
    falls back to the number in force once the draft is dropped, and a
    record of the pick would put a number the server never took back on
    the control.

    So the number recorded has to travel the whole way from the
    answer: the answer's ``capacity.total``, and not the pick that was
    sent.
    """
    body = _function_body(_source(_APP_JS), "schedulerApplyCapacity")

    server_number = _server_number(body)
    assert server_number != "total", (
        "schedulerApplyCapacity() records the number the operator picked "
        "as the server's number. The record exists so the control falls "
        "back to the number in force, and the pick is not that number "
        "when the server regularizes it."
    )

    # The number must be read out of a `.total`.
    total_read = re.search(
        r"(?:const|let|var)\s+"
        + re.escape(server_number)
        + r"\s*=\s*Number\(\s*(\w+)\.total\s*\)",
        body,
    )
    assert total_read is not None, (
        f"schedulerApplyCapacity() never reads the answer's total into "
        f"{server_number!r}. The number in force comes from the answer, "
        "not from the pick that was sent."
    )
    holder = total_read.group(1)

    # ... and that holder must be the answer's capacity.
    answered = re.search(
        r"(?:const|let|var)\s+(\w+)\s*=\s*await\s+api\(", body
    )
    assert answered is not None, (
        "schedulerApplyCapacity() never keeps the answer of the write, "
        "so there is nothing to read the server's number from."
    )
    answer = answered.group(1)

    holder_declaration = _declaration(body, holder)
    assert ".capacity" in holder_declaration, (
        f"{holder!r} is not the answer's capacity, so the number "
        f"recorded in select.dataset.serverValue does not come from the "
        "server's answer."
    )
    assert answer in holder_declaration, (
        f"{holder!r} is not built from the answer of the write "
        f"({answer!r}), so the number recorded in "
        "select.dataset.serverValue is not the server's number."
    )


def test_apply_announces_a_different_total() -> None:
    """A server that regularizes the pick must say so.

    The number the operator picked and the number the server took can
    differ, and when they do the operator has to be told: a panel that
    shows a number the server decided without a word is a panel that
    makes the server's decision look like the operator's.

    The notice carries the server's number, and it is cleared when the
    answer agrees with the pick — a message from an earlier write must
    not outlive it.
    """
    body = _function_body(_source(_APP_JS), "schedulerApplyCapacity")

    assert f"#{_NOTICE_ID}" in body, (
        f"schedulerApplyCapacity() never mentions #{_NOTICE_ID}, so a "
        "server that regularizes the pick says nothing: the operator "
        "cannot tell the number they asked for from the number the "
        "server took."
    )

    notice = re.search(
        r"(?:const|let|var)\s+(\w+)\s*=\s*\$\(\s*[\"']#" + _NOTICE_ID + r"[\"']\s*\)",
        body,
    )
    assert notice is not None, (
        f"schedulerApplyCapacity() never looks up #{_NOTICE_ID}, so the "
        "panel has nowhere to say what the server did with the pick."
    )
    notice_name = notice.group(1)

    message = re.search(
        re.escape(notice_name) + r"\.textContent\s*=\s*`([^`]*)`", body
    )
    assert message is not None, (
        f"schedulerApplyCapacity() never writes {notice_name}.textContent, "
        f"so #{_NOTICE_ID} stays empty whatever the server answered."
    )
    assert _NOTICE_WORDS in message.group(1), (
        f"the notice does not say {_NOTICE_WORDS!r}, so the operator is "
        "not told that the server changed the number."
    )

    server_number = _server_number(body)
    assert re.search(
        r"\$\{\s*" + re.escape(server_number) + r"\s*\}", message.group(1)
    ), (
        f"the notice does not carry the server's number "
        f"({server_number!r}), so it says that something happened without "
        "saying what the server decided."
    )

    assert re.search(
        re.escape(notice_name) + r"\.textContent\s*=\s*\"\"", body
    ), (
        f"schedulerApplyCapacity() never clears {notice_name}.textContent, "
        "so the message from an earlier write stays on the panel after a "
        "write that the server took as it was."
    )

    # The notice is news only when the answer is not the pick.
    assert re.search(
        re.escape(server_number) + r"\s*!==\s*Number\(\s*total\s*\)", body
    ), (
        f"schedulerApplyCapacity() never compares the server's number "
        f"({server_number!r}) with the number it sent, so the notice "
        "cannot tell a server that regularized the pick from one that "
        "took it as it was."
    )


def test_state_reads_are_latest_wins() -> None:
    """Only the newest state read may paint.

    The panel polls every five seconds and the write reads the state
    again, so two reads are in flight at once and they do not come back
    in the order they were started.  A read that was started before a
    write carries the snapshot from before that write, and painting it
    puts back the number the write just moved past.

    So the reads are numbered, and a read that a newer one has
    superseded returns without painting.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "loadSchedulerState")

    declaration = re.search(
        r"^let\s+" + _SEQ_NAME + r"\s*=\s*0\s*;", source, re.MULTILINE
    )
    assert declaration is not None, (
        f"the module has no module-level {_SEQ_NAME} counter. Without one "
        "the reads cannot be told apart, and an old read paints over a "
        "new one."
    )

    taken = re.search(
        r"(?:const|let|var)\s+(\w+)\s*=\s*\+\+" + _SEQ_NAME + r"\s*;", body
    )
    assert taken is not None, (
        f"loadSchedulerState() never takes a number from {_SEQ_NAME}, so "
        "no read can know that a newer read has been started."
    )
    seq = taken.group(1)

    request = body.index("await api(")
    assert taken.start() < request, (
        f"the number is taken after the read goes out, so a read that "
        f"was started later cannot be recognised as newer. {seq!r} must "
        f"be taken from {_SEQ_NAME} before the request."
    )

    comparison = re.search(
        r"if\s*\(\s*" + re.escape(seq) + r"\s*!==\s*" + _SEQ_NAME + r"\s*\)"
        r"\s*return\b",
        body,
    )
    assert comparison is not None, (
        f"loadSchedulerState() never compares {seq!r} with {_SEQ_NAME} "
        "after the read comes back, so a read that a newer one has "
        "superseded still paints its snapshot."
    )
    assert request < comparison.start(), (
        "the comparison happens before the read comes back, so a read "
        "that was started earlier is not caught by it."
    )
    assert comparison.start() < body.index("renderScheduler("), (
        "the superseded read is dropped after the render, so the old "
        "snapshot still reaches the panel."
    )


def test_notice_element_exists() -> None:
    """The notice must be on the panel, next to the source word.

    The words beside the control say where the number in force came
    from; the notice says what the server did with the pick the
    operator just sent.  They belong together: a notice in another row
    is a notice the operator does not read.
    """
    html = _source(_INDEX_HTML)

    assert f'id="{_NOTICE_ID}"' in html, (
        f"frontend/index.html has no `{_NOTICE_ID}` element, so there is "
        "nowhere on the panel to say what the server did with the pick."
    )

    row = html[
        html.index('class="scheduler-capacity-control"') : html.index(
            'class="scheduler-capacity-stamp"'
        )
    ]
    assert f'id="{_NOTICE_ID}"' in row, (
        f"`{_NOTICE_ID}` is not in the capacity control row, next to "
        f"`{_SOURCE_ID}`. The notice belongs beside the control it "
        "talks about."
    )
