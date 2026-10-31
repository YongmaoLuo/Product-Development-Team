"""The scheduler queue must name the plan it acts on.

Why this gate exists
--------------------
The queue is the list of plans waiting for a slot.  Each row names the
plan it is about — in the plan column — and carries the two buttons
that act on that plan: 提升 (push it up the queue) and 启动 (start it
now).  The id the buttons act on travels in the ``data-plan``
attribute, so a row names one plan twice over, and both names have to
be the same one.

The id has to come from the snapshot field the backend fills in.  The
row reads it off the queue item, shows it, and hands the same one to
the buttons.  A row that shows an id the buttons do not act on names a
plan nobody can act on; a row that reads an id and shows nothing names
no plan at all.  Both are invisible in a browser and loud in a source
scan, so the contract is pinned here rather than at runtime.

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

#: The snapshot field the queue row reads its plan id from.
_PLAN_ID_FIELD = "item.plan_id"

#: The actions the queue row's buttons offer, in the words the source
#: spells them: 提升 is ``promote`` and 启动 is ``start``.
_QUEUE_ACTIONS = ("promote", "start")

#: A template literal, from its opening backtick to the next one.
_TEMPLATE_RE = re.compile(r"`[^`]*`", re.DOTALL)

#: One cell of a rendered row.
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)

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


def _row_template(body: str) -> str | None:
    """Return the template literal that builds a queue row.

    A template literal ends at the next backtick, so a nested template
    inside the row would split the span.  The row carries none, and the
    caller fails loudly when this helper finds no row at all.
    """
    for match in _TEMPLATE_RE.finditer(body):
        if "<tr" in match.group(0):
            return match.group(0)
    return None


def _action_call(row: str, variable: str) -> str | None:
    """Return the arguments of the button call that acts on *variable*.

    The row may carry more than one set of buttons, so the call is
    looked up by the id it acts on rather than by position.
    """
    for match in _ACTION_CALL_RE.finditer(row):
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


def _renders(variable: str, text: str) -> bool:
    """Whether *text* renders *variable* through ``escapeHtml``."""
    pattern = r"escapeHtml\(\s*" + re.escape(variable) + r"\b"
    return bool(re.search(pattern, text))


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _plan_id_variable(source: str) -> str:
    """Return the name the queue row binds its plan id to.

    Raises :class:`AssertionError` when the renderer does not read the
    plan id off the queue item, or reads it without binding it to a
    name — a name is what the row and the buttons can both point at.
    """
    body = _function_body(source, _RENDERER)

    assert _PLAN_ID_FIELD in body, (
        f"{_RENDERER}() never reads `{_PLAN_ID_FIELD}`, so the queue row "
        "has no plan id to show and no id for its buttons to act on."
    )

    binding = _PLAN_ID_BINDING_RE.search(body)
    assert binding is not None, (
        f"{_RENDERER}() reads `{_PLAN_ID_FIELD}` but binds it to no "
        "variable, so the id never reaches the row."
    )
    return binding.group("var")


def check_queue_row_reads_plan_id(source: str) -> None:
    """The queue row must read the plan id off the queue item.

    The snapshot the backend hands the panel carries the plan's id in
    ``plan_id``, and the queue row is where that id belongs.  A row
    that reads some other field names the wrong thing; a row that
    reads nothing names nothing.
    """
    _plan_id_variable(source)


def check_plan_id_reaches_the_row(source: str) -> None:
    """The plan id must be shown in the row, and acted on by the buttons.

    One id, three jobs: read off the queue item, shown in the plan
    column, handed to the buttons that promote and start it.  Showing
    one id and acting on another names a plan the reader cannot act on,
    and reading an id into a variable no cell uses names no plan at
    all.
    """
    variable = _plan_id_variable(source)
    body = _function_body(source, _RENDERER)

    row = _row_template(body)
    assert row is not None, (
        f"{_RENDERER}() builds no row template, so there is nowhere for "
        "the plan id to be shown."
    )

    cells = [
        cell for cell in _CELL_RE.findall(row) if _renders(variable, cell)
    ]
    assert cells, (
        f"{_RENDERER}() reads the plan id into `{variable}` but no cell of "
        "the row shows it. An id nobody can read is an id the row does "
        "not name."
    )

    arguments = _action_call(row, variable)
    assert arguments is not None, (
        f"{_RENDERER}() shows `{variable}` but does not hand that same id "
        "to the action buttons, so the buttons act on something else."
    )

    for action in _QUEUE_ACTIONS:
        assert re.search(r"[\"']" + action + r"[\"']", arguments), (
            f"{_RENDERER}() does not offer the {action!r} action on the "
            "plan id it shows."
        )


def check_queue_plan_id_wiring(source: str) -> None:
    """Both rules at once — what the gate as a whole asserts."""
    check_queue_row_reads_plan_id(source)
    check_plan_id_reaches_the_row(source)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_queue_row_reads_plan_id() -> None:
    """The queue row must read the plan id off the queue item."""
    check_queue_row_reads_plan_id(_source(_APP_JS))


def test_plan_id_reaches_the_row() -> None:
    """The plan id must be shown in the row, and acted on by the buttons.

    The id the row shows and the id the buttons act on have to be one
    id.  Two ids mean the row names a plan the buttons do not touch;
    an id in a variable no cell renders means the row names no plan at
    all.
    """
    check_plan_id_reaches_the_row(_source(_APP_JS))


def test_the_gate_has_teeth() -> None:
    """Negative control: a row that does not read the plan id must fail.

    The fixture is the panel with the queue's plan-id read replaced by
    a different field.  Everything else — the row, the plan column, the
    buttons — stays as it is, so a gate that only looked at the wiring
    would accept it.  The gate has to notice the missing read.
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


def test_the_gate_rejects_a_plan_id_that_no_cell_shows() -> None:
    """Negative control: an id read into a name nobody uses must fail.

    The fixture is the panel with the plan column and the buttons
    pointed at a different field.  The read is still there — a gate
    that only looked for the read would pass it — but the row names no
    plan, so the gate has to notice that the id it reads never reaches
    the reader.
    """
    source = _source(_APP_JS)
    body = _function_body(source, _RENDERER)
    binding = _PLAN_ID_BINDING_RE.search(body)
    assert binding is not None, (
        "the panel no longer binds the plan id, so the negative control "
        "would prove nothing."
    )
    variable = binding.group("var")

    mutated = body.replace(f"escapeHtml({variable}", "escapeHtml(item.seq")
    mutated = mutated.replace(
        f"schedulerActionButtons({variable}", "schedulerActionButtons(item.seq"
    )
    assert mutated != body, (
        "the fixture no longer reads the plan id, so the negative "
        "control would prove nothing."
    )

    with pytest.raises(AssertionError) as failure:
        check_queue_plan_id_wiring(source.replace(body, mutated))

    assert variable in str(failure.value), (
        "the gate rejected the fixture for a reason other than the "
        f"unshown plan id: {failure.value}"
    )
