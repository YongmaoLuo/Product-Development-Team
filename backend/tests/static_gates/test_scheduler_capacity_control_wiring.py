"""The capacity must be a control the operator can turn, not a number.

Why this gate exists
--------------------
The scheduler panel carried the capacity as a **read-out**: 「已用 6
共 6 队列 7」, three numbers that say what the scheduler is doing and
nothing about what it may do.  Changing the capacity meant leaving the
page — writing the override through the API by hand — and the panel
could not tell the operator whether the number in force was the one
they wrote or the one the configuration happens to carry.  A capacity
that is only visible is a capacity that is only changeable by someone
who knows the endpoint.

The endpoint exists (``POST /api/scheduler/capacity``, written in an
earlier task) and the read model says where the number came from
(``capacity.source``).  What was missing was the control: a select, an
apply button, a way back to the configured capacity, and a visible
error when the write fails.

The rule
--------
* ``frontend/index.html``: the scheduler view's capacity row carries
  the four controls — ``#scheduler-capacity-select`` (every number
  from 1 to 12), ``#scheduler-capacity-apply`` (「应用」),
  ``#scheduler-capacity-reset`` (「跟随配置」) and
  ``#scheduler-capacity-source`` (the word that says the number in
  force is the operator's own).
* ``frontend/app.js``: a named ``schedulerApplyCapacity(total)``
  writes through the ``api()`` wrapper — never a bare ``fetch()`` —
  and a ``total`` of ``null`` asks for the configured capacity back
  (``{"reset": true}``).
* The panel follows the **server**, not the operator: after a write
  the state is read again, and the select is filled from the server's
  ``capacity.total``.  A failed write says so — the error's own text,
  in the action-result box — and never leaves the operator's rejected
  pick on screen as if it were the capacity in force.
* ``renderSchedulerCapacity()`` reads ``capacity.source`` and writes
  「(覆盖)」 when the number in force is the override; otherwise it
  writes nothing, so a configured capacity is never passed off as an
  override.

Scan target
-----------
``frontend/app.js`` and ``frontend/index.html``, read as source.  These
are shape gates, like the rest of ``static_gates/``: they run on a
plain Python environment with no JS toolchain, so they pin the
contract in the source rather than executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"

#: The call the panel must make to write the capacity.  It goes
#: through the ``api()`` wrapper — see the request-guard note below.
_CAPACITY_CALL = 'api("/scheduler/capacity", {method: "POST"'

#: The endpoint, without the ``/api`` prefix the wrapper adds.
_CAPACITY_PATH = "/scheduler/capacity"

#: The ids of the four controls.
_SELECT_ID = "scheduler-capacity-select"
_APPLY_ID = "scheduler-capacity-apply"
_RESET_ID = "scheduler-capacity-reset"
_SOURCE_ID = "scheduler-capacity-source"

#: The row the controls live in.
_CAPACITY_ROW_ID = "scheduler-view-capacity"

#: The numbers the operator may pick from the select.
_SELECT_VALUES = tuple(range(1, 13))

#: The label of the apply button.
_APPLY_LABEL = "应用"

#: The label of the reset button — it puts the **configured** capacity
#: back, so the button says which capacity it means.
_RESET_LABEL = "跟随配置"

#: The word that says the number in force is the operator's own.
_OVERRIDE_TEXT = "(覆盖)"

#: The body a reset carries.
_RESET_BODY = '{"reset": true}'


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


def _element_markup(html: str, element_id: str) -> str:
    """Return the markup of the element with ``element_id``.

    The element must exist: a control the panel never draws is a
    control that does not exist, and the gate has to say so.
    """
    match = re.search(
        r"<[a-zA-Z][\w-]*[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>",
        html,
    )
    assert match is not None, f"no element with id={element_id!r} in the markup"
    return match.group(0)


def _element_text(html: str, element_id: str) -> str:
    """Return the text inside the element with ``element_id``.

    The markup is stripped of tags so that a button whose label is
    wrapped in a ``<span>`` still reads as its label.
    """
    match = re.search(
        r"<([a-zA-Z][\w-]*)[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>(.*?)</\1>",
        html,
        re.DOTALL,
    )
    assert match is not None, f"no element with id={element_id!r} in the markup"
    return re.sub(r"<[^>]+>", "", match.group(2)).strip()


def _element_children(html: str, element_id: str) -> str:
    """Return the markup *inside* the element with ``element_id``.

    Used to prove that a control lives in the row it is supposed to
    live in, and not somewhere else on the page.
    """
    match = re.search(
        r"<([a-zA-Z][\w-]*)[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>",
        html,
    )
    assert match is not None, f"no element with id={element_id!r} in the markup"
    tag = match.group(1)
    rest = html[match.end() :]
    close = f"</{tag}>"
    end = rest.find(close)
    assert end != -1, f"element {element_id!r} is never closed"
    return rest[:end]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_capacity_control_posts_via_api() -> None:
    """The write must go through ``api()``, and it must carry the reset.

    ``backend/request_guard.py`` answers only requests carrying
    ``X-PDT-Request: 1``.  A bare ``fetch()`` sends no such header and
    the API answers 403 — a failure that looks exactly like "the
    endpoint is down" and would send the operator looking in the wrong
    place.  The wrapper is what carries the header.

    A reset is the other half of the contract: ``null`` asks for the
    configured capacity back, and the body the wire carries says so
    (``{"reset": true}``) instead of carrying a number.
    """
    source = _source(_APP_JS)

    assert _CAPACITY_CALL in source, (
        f"frontend/app.js does not write the capacity through "
        f"`{_CAPACITY_CALL}`. The capacity knob posts to "
        f"`{_CAPACITY_PATH}` through the api() wrapper, which carries "
        "the X-PDT-Request guard header; without it the API answers 403."
    )

    body = _function_body(source, "schedulerApplyCapacity")

    assert "fetch(" not in body, (
        "schedulerApplyCapacity() issues a bare fetch(). Every call must "
        "go through api(), which attaches the X-PDT-Request guard "
        "header; without it the request is answered 403."
    )

    assert _RESET_BODY in source, (
        f"the source never writes {_RESET_BODY}. Without it the reset "
        "cannot put the configured capacity back — the server would "
        "read the request as a request that says nothing."
    )

    assert re.search(r"\bnull\b", body), (
        "schedulerApplyCapacity() never mentions null, so it cannot "
        "tell 'apply this number' from 'put the configured capacity "
        "back'."
    )


def test_capacity_control_elements() -> None:
    """The four controls must exist, in the capacity row of the view.

    The select must offer every number from 1 to 12: a select that
    offers only some of them is a select that cannot express the
    capacity the operator wants.  The reset button must say which
    capacity it puts back — 「跟随配置」, the configured one — because
    "reset" is a word that means nothing on a panel.
    """
    html = _source(_INDEX_HTML)

    for element_id in (_SELECT_ID, _APPLY_ID, _RESET_ID, _SOURCE_ID):
        assert f'id="{element_id}"' in html, (
            f"frontend/index.html has no `{element_id}` element, so the "
            "capacity cannot be changed from the panel at all."
        )

    row = _element_children(html, _CAPACITY_ROW_ID)
    for element_id in (_SELECT_ID, _APPLY_ID, _RESET_ID, _SOURCE_ID):
        assert f'id="{element_id}"' in row, (
            f"`{element_id}` is not inside `{_CAPACITY_ROW_ID}`. The "
            "control belongs next to the number it changes."
        )

    options = re.findall(
        r"<option[^>]*value=\"(\d+)\"",
        _element_children(html, _SELECT_ID),
    )
    missing = [
        str(value)
        for value in _SELECT_VALUES
        if str(value) not in options
    ]
    assert not missing, (
        "the capacity select does not offer "
        + ", ".join(missing)
        + ". Every number from 1 to 12 must be pickable."
    )

    assert _APPLY_LABEL in _element_text(html, _APPLY_ID), (
        f"the apply button does not say 「{_APPLY_LABEL}」; a button "
        "whose label does not say what it does is a button nobody "
        "presses on purpose."
    )

    assert _RESET_LABEL in _element_text(html, _RESET_ID), (
        f"the reset button does not say 「{_RESET_LABEL}」. The button "
        "puts the *configured* capacity back, and the label is what "
        "says so."
    )


def test_capacity_source_rendered() -> None:
    """The source of the capacity must be read and shown.

    ``capacity.source`` says where the number in force came from:
    ``override`` for the operator's own number, ``config`` for the
    configured one.  Without it the panel cannot tell the operator
    that the number they see is not the configured one — the whole
    point of writing an override.

    The word is 「(覆盖)」 for an override and **nothing** otherwise: a
    configured capacity is the normal case and must not be marked as
    if it were an override.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerCapacity")

    assert "capacity.source" in body, (
        "renderSchedulerCapacity() never reads `capacity.source`, so "
        "the panel cannot say whether the number in force is the "
        "operator's own."
    )

    assert "override" in body, (
        "renderSchedulerCapacity() never names the `override` source, "
        "so it cannot tell an override from the configured capacity."
    )

    assert _OVERRIDE_TEXT in body, (
        f"renderSchedulerCapacity() never writes {_OVERRIDE_TEXT!r}, so "
        "an overridden capacity is drawn exactly like a configured one."
    )


def test_capacity_control_buttons_are_bound() -> None:
    """Both buttons must be bound to the write they stand for.

    A button that is on the page and does nothing is worse than no
    button: the operator presses it, the panel does not change, and
    the conclusion is that the capacity cannot be changed.
    """
    body = _function_body(_source(_APP_JS), "init")

    assert _APPLY_ID in body, (
        f"init() never binds `{_APPLY_ID}`, so the apply button does "
        "nothing."
    )

    assert _RESET_ID in body, (
        f"init() never binds `{_RESET_ID}`, so the reset button does "
        "nothing."
    )

    assert "schedulerApplyCapacity(" in body, (
        "init() never calls schedulerApplyCapacity(), so neither button "
        "can write anything."
    )
