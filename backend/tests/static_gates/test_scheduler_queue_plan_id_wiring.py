"""The scheduler queue must name the plan it acts on.

Why this gate exists
--------------------
The queue is the list of plans waiting for a slot.  Each plan is drawn
as a **card**: a header the operator reads at a glance, and the two
buttons that act on that plan — 提升 (push it up the queue) and 启动
(start it now).  The id the buttons act on travels in the
``data-plan`` attribute, so a card names one plan twice over, and both
names have to be the same one.

The id has to come from the snapshot field the backend fills in.  The
queue reads it off the queue item, the card shows it, and the buttons
act on that same one.  A card that shows an id the buttons do not act
on names a plan nobody can act on; a card that reads an id and shows
nothing names no plan at all.  Both are invisible in a browser and
loud in a source scan, so the contract is pinned here rather than at
runtime.

The queue used to be drawn as table rows, and this gate used to look
for a ``<tr>`` with a ``<td>`` in it.  The panel draws cards now, so
the gate looks at the card: the row is gone, and a gate that still
looked for it would fail on a panel that is right.

Scan target
-----------
``frontend/app.js``, read as source.  This is a shape gate, like the
rest of ``static_gates/``: it runs on a plain Python environment with
no JS toolchain, so it pins the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The renderer that draws the queue.
_RENDERER = "renderSchedulerQueue"

#: The function that draws one plan's card, from the row it is handed.
_CARD_FUNCTION = "schedulerCard"

#: The snapshot field the queue card reads its plan id from.
_PLAN_ID_FIELD = "item.plan_id"

#: The actions the queue card's buttons offer, in the words the source
#: spells them: 提升 is ``promote`` and 启动 is ``start``.
_QUEUE_ACTIONS = ("promote", "start")

#: A call to the button renderer, with its arguments.
_ACTION_CALL_RE = re.compile(r"schedulerActionButtons\s*\(([^)]*)\)")

#: The binding of a plan id: ``const planId = item.plan_id ...``.
_PLAN_ID_BINDING_RE = re.compile(
    r"(?:const|let|var)\s+(?P<var>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[^;]*?"
    + re.escape(_PLAN_ID_FIELD)
)


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


def _action_call(body: str, variable: str) -> str | None:
    """Return the arguments of the button call that acts on *variable*.

    A card may carry more than one set of buttons, so the call is
    looked up by the id it acts on rather than by position.
    """
    for match in _ACTION_CALL_RE.finditer(body):
        arguments = match.group(1)
        if _first_argument(arguments) == variable:
            return arguments
    return None


def _first_argument(arguments: str) -> str | None:
    """Return the name of the first argument of a call."""
    first = arguments.split(",", 1)[0].strip()
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", first):
        return first
    return None


def _plan_id_variable(body: str, where: str) -> str:
    """Return the name the plan id is bound to in *body*.

    Raises :class:`AssertionError` when the body does not read the
    plan id off the queue item, or reads it without binding it to a
    name — a name is what the card and the buttons can both point at.
    """
    assert _PLAN_ID_FIELD in body, (
        f"{where} never reads `{_PLAN_ID_FIELD}`, so the queue card has "
        "no plan id to show and no id for its buttons to act on."
    )

    binding = _PLAN_ID_BINDING_RE.search(body)
    assert binding is not None, (
        f"{where} reads `{_PLAN_ID_FIELD}` but binds it to no variable, "
        "so the id never reaches the card or the buttons."
    )
    return binding.group("var")


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def check_queue_card_reads_plan_id(source: str) -> None:
    """The queue must read the plan id off the queue item.

    The snapshot the backend hands the panel carries the plan's id in
    ``plan_id``, and the queue is where that id belongs.  A queue that
    reads some other field names the wrong thing; a queue that reads
    nothing names nothing.
    """
    _plan_id_variable(_function_body(source, _RENDERER), f"{_RENDERER}()")


def check_plan_id_reaches_the_card(source: str) -> None:
    """The plan id must be shown on the card, and acted on by the buttons.

    One id, three jobs: read off the queue item, shown on the card,
    handed to the buttons that promote and start it.  Showing one id
    and acting on another names a plan the reader cannot act on, and
    reading an id into a name nothing renders names no plan at all.
    """
    queue = _function_body(source, _RENDERER)
    variable = _plan_id_variable(queue, f"{_RENDERER}()")

    arguments = _action_call(queue, variable)
    assert arguments is not None, (
        f"{_RENDERER}() shows `{variable}` but does not hand that same id "
        "to the action buttons, so the buttons act on something else."
    )

    for action in _QUEUE_ACTIONS:
        assert re.search(r"[\"']" + action + r"[\"']", arguments), (
            f"{_RENDERER}() does not offer the {action!r} action on the "
            "plan id it shows."
        )

    # The card names the same plan: it reads the same field of the row
    # the queue hands it, and shows the id it found there.
    card = _function_body(source, _CARD_FUNCTION)
    shown = _plan_id_variable(card, f"{_CARD_FUNCTION}()")

    assert re.search(r"escapeHtml\(\s*" + re.escape(shown) + r"\b", card), (
        f"{_CARD_FUNCTION}() reads the plan id but shows it without "
        "escaping it, so a plan whose id carries a quote breaks the "
        "card's markup."
    )

    assert "data-plan-id=" in card, (
        f"{_CARD_FUNCTION}() does not write the plan's id into the card's "
        "markup, so a card on the page cannot be traced back to the plan "
        "it is about."
    )


def check_queue_plan_id_wiring(source: str) -> None:
    """Both rules at once — what the gate as a whole asserts."""
    check_queue_card_reads_plan_id(source)
    check_plan_id_reaches_the_card(source)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_queue_card_reads_plan_id() -> None:
    """The queue must read the plan id off the queue item."""
    check_queue_card_reads_plan_id(_source(_APP_JS))


def test_plan_id_reaches_the_card() -> None:
    """The plan id must be shown on the card, and acted on by the buttons.

    The id the card shows and the id the buttons act on have to be one
    id.  Two ids mean the card names a plan the buttons do not touch;
    an id in a name nothing renders means the card names no plan at
    all.
    """
    check_plan_id_reaches_the_card(_source(_APP_JS))


def test_the_gate_has_teeth() -> None:
    """Negative control: a queue that does not read the plan id must fail.

    The fixture is the panel with the queue's plan-id read replaced by
    a different field.  Everything else — the card, the plan id on it,
    the buttons — stays as it is, so a gate that only looked at the
    wiring would accept it.  The gate has to notice the missing read.
    """
    source = _source(_APP_JS)
    body = _function_body(source, _RENDERER)
    mutated = body.replace(_PLAN_ID_FIELD, "item.seq")
    assert mutated != body, (
        "the fixture no longer carries the plan-id read, so the negative "
        "control would prove nothing."
    )

    with pytest.raises(AssertionError) as failure:
        check_queue_plan_id_wiring(source.replace(body, mutated))

    assert _PLAN_ID_FIELD in str(failure.value), (
        "the gate rejected the fixture for a reason other than the "
        f"missing plan-id read: {failure.value}"
    )


def test_the_gate_rejects_a_plan_id_no_card_shows() -> None:
    """Negative control: an id the card does not show must fail.

    The fixture is the panel with the card reading a different field.
    The queue still reads the plan id and still hands it to the buttons
    — a gate that only looked for the read would pass it — but the card
    names no plan, so the gate has to notice that the id it reads never
    reaches the reader.
    """
    source = _source(_APP_JS)
    card = _function_body(source, _CARD_FUNCTION)
    binding = _PLAN_ID_BINDING_RE.search(card)
    assert binding is not None, (
        "the panel no longer binds the plan id, so the negative control "
        "would prove nothing."
    )
    mutated = card.replace(_PLAN_ID_FIELD, "item.seq")
    assert mutated != card, (
        "the fixture no longer reads the plan id, so the negative "
        "control would prove nothing."
    )

    with pytest.raises(AssertionError) as failure:
        check_queue_plan_id_wiring(source.replace(card, mutated))

    assert _PLAN_ID_FIELD in str(failure.value), (
        "the gate rejected the fixture for a reason other than the "
        f"unshown plan id: {failure.value}"
    )
