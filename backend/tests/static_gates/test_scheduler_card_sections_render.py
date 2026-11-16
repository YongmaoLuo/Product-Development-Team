"""The scheduler's card detail must be drawn as the backend's sections.

Why this gate exists
--------------------
The detail endpoint answers with a plan's progress in **two** shapes:
the ``lines`` a renderer prints one by one, and the ``sections`` a
panel draws — a status header with a colour and an elapsed time, a
phase banner, a progress bar, the running tasks.

The card used to print the lines and nothing else, so the panel threw
the structure away: a plan's progress bar, its running tasks and its
elapsed time were all there in the answer, and none of them reached
the operator.  The panel is the surface where the plan's state is
read at a glance, so the structure is what it must draw.

Five things are pinned here:

* the detail renderer **reads** the sections, and draws them through
  the section renderer;
* **every** kind the backend produces has a renderer of its own — a
  kind with no renderer is a fact the panel silently drops;
* a kind the panel does **not** know is still drawn, as a generic
  section carrying its own wording — a branch per known kind is not
  enough on its own, because the unknown kind would otherwise leave
  the renderer with nothing said;
* everything the panel interpolates has been through
  :func:`escapeHtml`, because every one of these values comes from the
  server;
* the **lines are still there**: an answer that carries no sections is
  shown as the paragraphs it has always been, so a backend that has
  not been updated yet does not blank the card.

The elapsed time has a renderer of its own: a runtime the panel does
not know is said as 「—」, a runtime under a minute in seconds, and a
longer one in minutes and seconds.

Scan target
-----------
``frontend/app.js``, read as source.  These are shape gates, like the
rest of ``static_gates/``: they run on a plain Python environment with
no JS toolchain, so they pin the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The function that draws one plan's detail.
_DETAIL_HTML_FUNCTION = "schedulerCardDetailHtml"

#: The function that draws one section of that detail.
_SECTION_HTML_FUNCTION = "schedulerSectionHtml"

#: The function that says how long a run has been going.
_DURATION_FUNCTION = "schedulerFormatDuration"

#: The seven kinds the backend's sections carry.  They arrive in this
#: order — status, phase, task progress, in progress, blocked,
#: verification, failure — and each one says something different, so
#: each one needs a renderer of its own.
_SECTION_KINDS = (
    "status",
    "phase",
    "task_progress",
    "in_progress",
    "blocked",
    "verification",
    "failure",
)

#: The comment that introduces the answer for a kind the panel does
#: not know.  It is where the negative control cuts that answer out —
#: the same shape as the mutation probe that removes it.
_UNKNOWN_KIND_COMMENT = "A kind the panel does not know is still a section"

#: The attribute the generic section carries its kind in.
_LABEL_ATTRIBUTE = 'data-section="'

#: The paragraph a card shows when it has nothing to show at all.
_EMPTY_DETAIL = "这个计划还没有进度信息"

#: The answer for a runtime the panel does not know.
_UNKNOWN_DURATION = "—"


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
    function no matter what the body contains.  Brace counting would be
    the alternative, and it miscounts the template literals this file
    is full of.

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


def _interpolations(body: str) -> list[str]:
    """Return every ``${…}`` expression in ``body``.

    Only the expressions without a brace of their own are found — an
    interpolation that carries an object literal is not a thing this
    file writes, and a brace-counting regex would misread it.
    """
    return re.findall(r"\$\{([^{}]*)\}", body)


def _base_indent(body: str) -> int:
    """Return the column the function's own statements are written at.

    The body's first line is the ``{`` that opened the function, so it
    is left out of the count; the statements that follow sit one level
    deeper than it, and every branch inside them is deeper again.  A
    return that belongs to the function is written at this column, and
    a return inside a branch is written deeper.
    """
    indents = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "{":
            continue
        indents.append(len(line) - len(line.lstrip()))
    assert indents, "the function body carries no statement at all"
    return min(indents)


def _body_with_empty_fallback(body: str) -> str:
    """Return the body with its answer for an unknown kind emptied.

    That answer is the tail of the function — everything from the
    comment that introduces it to the end of the function — so it is
    replaced by a return of an empty string.  This is the shape the
    mutation probe uses: the answer is still there, and it draws
    nothing.
    """
    marker = body.find(_UNKNOWN_KIND_COMMENT)
    assert marker >= 0, (
        "the comment that introduces the answer for an unknown kind is "
        "gone, so the negative control cannot cut that answer out."
    )
    line_start = body.rfind("\n", 0, marker) + 1
    return body[:line_start] + " " * _base_indent(body) + 'return "";\n'


def check_sections_are_read(body: str) -> None:
    """The detail renderer must read the sections and draw them."""
    assert "sections" in body, (
        f"{_DETAIL_HTML_FUNCTION}() never mentions the sections. The "
        "answer carries the plan's structure; a renderer that does not "
        "read it prints the flattened lines and throws the structure "
        "away."
    )
    assert re.search(r"Array\.isArray\([^)]*\.sections[^)]*\)", body), (
        f"{_DETAIL_HTML_FUNCTION}() does not check that the sections are "
        "a list. An answer that carries no sections at all must fall "
        "back to the lines, not raise."
    )
    assert _SECTION_HTML_FUNCTION in body, (
        f"{_DETAIL_HTML_FUNCTION}() does not draw the sections through "
        f"{_SECTION_HTML_FUNCTION}(). Each kind has a renderer of its "
        "own, and the detail renderer is what picks them."
    )


def check_every_kind_has_a_renderer(body: str) -> None:
    """Every kind the backend produces must have a branch of its own."""
    for kind in _SECTION_KINDS:
        assert re.search(r'(kind\s*===\s*|case\s+)"' + re.escape(kind) + r'"', body), (
            f"{_SECTION_HTML_FUNCTION}() has no branch for the "
            f"{kind!r} section. A section with no renderer is a fact "
            "the panel drops on the floor."
        )


def _own_return(body: str, start: int) -> str | None:
    """Return the function's own ``return`` at or after ``start``.

    A return that belongs to the function is written at the function's
    own column; a return inside a branch is written deeper, so the
    depth tells the two apart.  The statement is gathered up to the
    line that closes it, so a return that spans several lines — the
    way the generic section is written — is read whole.

    ``None`` when the function has no return of its own at or after
    ``start``.
    """
    prefix = " " * _base_indent(body)
    lines = body[start:].splitlines()
    for index, line in enumerate(lines):
        if not re.match(re.escape(prefix) + r"return\b", line):
            continue
        statement = [line]
        for follow in lines[index + 1 :]:
            statement.append(follow)
            if follow.rstrip().endswith(";"):
                break
        return "\n".join(statement)
    return None


def check_unknown_kind_has_a_fallback(body: str) -> None:
    """A kind the panel does not know must still be drawn.

    The backend sends the sections it has, and a newer backend may
    send a kind this panel has never heard of.  A branch per known
    kind is not enough on its own: the unknown kind reaches the end
    of the function and falls out of it, so the operator is told
    nothing at all.

    The answer is a return of the function's own — one that is not
    behind another branch — and it draws a generic section: the kind
    it does not know as the section's label, and the wording it was
    sent as the section's text.
    """
    guard_ends = []
    for kind in _SECTION_KINDS:
        match = re.search(r'kind\s*===\s*"' + re.escape(kind) + r'"', body)
        assert match is not None, (
            f"{_SECTION_HTML_FUNCTION}() has no branch for the "
            f"{kind!r} section, so where the known branches end cannot "
            "be told."
        )
        guard_ends.append(match.end())
    last_guard_end = max(guard_ends)

    tail = body[last_guard_end:]

    assert not re.search(r'kind\s*===\s*"', tail), (
        f"{_SECTION_HTML_FUNCTION}() asks which kind it is again after "
        "its last known branch. The answer for a kind the panel does "
        "not know comes after every known branch, not in front of one "
        "of them."
    )

    fallback = _own_return(body, last_guard_end)
    assert fallback is not None, (
        f"{_SECTION_HTML_FUNCTION}() has no answer of its own for a "
        "kind it does not know. Every return it has belongs to a "
        "branch of a kind it knows, so an unknown kind leaves the "
        "function with nothing drawn and nothing said."
    )

    assert "scheduler-detail-section" in fallback, (
        f"{_SECTION_HTML_FUNCTION}() answers an unknown kind with "
        "something that is not a section. An unknown kind is drawn as "
        "a generic section, so the operator still sees it."
    )

    assert _LABEL_ATTRIBUTE in fallback, (
        f"{_SECTION_HTML_FUNCTION}() draws the generic section without "
        "a label. The label is what says which kind the block is."
    )
    value_start = fallback.index(_LABEL_ATTRIBUTE) + len(_LABEL_ATTRIBUTE)
    closing = fallback.find('"', value_start)
    assert closing >= 0, (
        f"{_SECTION_HTML_FUNCTION}() draws the generic section with an "
        "unclosed label."
    )
    value = fallback[value_start:closing]
    assert re.search(r"escapeHtml\(\s*(?:String\(\s*)?kind\s*\)?\s*\)", value), (
        f"{_SECTION_HTML_FUNCTION}() does not label the generic section "
        "with the kind it does not know. The label comes from the "
        "server, so it is escaped on the way in."
    )

    assert re.search(r"escapeHtmlTextLines\(\s*data\.text\s*\)", fallback), (
        f"{_SECTION_HTML_FUNCTION}() draws an unknown kind without its "
        "wording. The generic section carries the text it was sent, and "
        "that text comes from the server."
    )


def check_interpolations_are_escaped(body: str) -> None:
    """Every interpolated value must have been through ``escapeHtml``.

    Every value in a section comes from the server — the plan's id,
    the task's title, the failure's reason — so a value that reaches
    ``innerHTML`` verbatim is a script the server (or anything it
    echoes) can run in the operator's browser.
    """
    expressions = _interpolations(body)
    assert expressions, (
        f"{_SECTION_HTML_FUNCTION}() interpolates nothing at all, so the "
        "gate cannot tell whether the values it draws are escaped."
    )
    for expression in expressions:
        assert "escapeHtml(" in expression, (
            f"{_SECTION_HTML_FUNCTION}() interpolates {expression!r} "
            "without escapeHtml(). Every value in a section comes from "
            "the server."
        )


def check_lines_fallback_is_kept(body: str) -> None:
    """The lines must still be drawn when there are no sections."""
    assert "lines" in body, (
        f"{_DETAIL_HTML_FUNCTION}() no longer mentions the lines. An "
        "answer that carries no sections — a backend that has not been "
        "updated yet — would show an empty card."
    )
    assert "<p>" in body, (
        f"{_DETAIL_HTML_FUNCTION}() no longer draws the lines as "
        "paragraphs. The fallback is the paragraphs the card has always "
        "shown."
    )
    assert _EMPTY_DETAIL in body, (
        f"{_DETAIL_HTML_FUNCTION}() no longer says {_EMPTY_DETAIL!r} "
        "when there is nothing at all to show."
    )


def check_duration_is_formatted(body: str) -> None:
    """The runtime must be said the way the card says it."""
    assert _UNKNOWN_DURATION in body, (
        f"{_DURATION_FUNCTION}() does not answer {_UNKNOWN_DURATION!r} "
        "for a runtime it does not know. \"The run has been going for "
        "no time at all\" and \"the panel does not know how long the run "
        "has been going\" are different statements."
    )
    assert "null" in body and "undefined" in body, (
        f"{_DURATION_FUNCTION}() does not treat a missing runtime as an "
        "unknown one. A plan that never started has no runtime, and "
        "that is not the same as a runtime of zero."
    )
    assert "60" in body, (
        f"{_DURATION_FUNCTION}() does not know where the minute is. A "
        "runtime under a minute is counted in seconds and a longer one "
        "in minutes and seconds."
    )
    assert re.search(r'"m\s*"', body), (
        f"{_DURATION_FUNCTION}() has no minutes to answer with."
    )
    assert re.search(r'"s"', body), (
        f"{_DURATION_FUNCTION}() has no seconds to answer with."
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_detail_html_reads_sections() -> None:
    """The detail must be drawn from the sections the answer carries.

    The answer has carried the plan's structure since the detail
    endpoint learned to return it; a renderer that keeps printing the
    flattened lines throws all of it away, and the panel shows a plan's
    progress as a wall of paragraphs.
    """
    body = _function_body(_source(_APP_JS), _DETAIL_HTML_FUNCTION)

    check_sections_are_read(body)


def test_every_section_kind_has_renderer() -> None:
    """Each of the seven kinds must have a renderer of its own.

    The kinds say different things — a status header, a phase banner,
    a progress bar, the running tasks, the blocked tasks, the
    verification round, the failed tasks — and a kind with no renderer
    is a fact the operator never sees.
    """
    body = _function_body(_source(_APP_JS), _SECTION_HTML_FUNCTION)

    check_every_kind_has_a_renderer(body)


def test_section_text_is_escaped() -> None:
    """Everything the panel draws out of a section must be escaped.

    A section carries the plan's id, the task's title, the failure's
    reason — all of them from the server, all of them into
    ``innerHTML``.
    """
    body = _function_body(_source(_APP_JS), _SECTION_HTML_FUNCTION)

    check_interpolations_are_escaped(body)


def test_lines_fallback_kept() -> None:
    """The lines must still be drawn when the answer carries no sections.

    The sections are newer than the lines, and an answer that carries
    only the lines — an older backend, a plan that was never started —
    must still show something.
    """
    body = _function_body(_source(_APP_JS), _DETAIL_HTML_FUNCTION)

    check_lines_fallback_is_kept(body)


def test_duration_formatter() -> None:
    """The runtime must be said the way the card says it.

    A runtime the panel does not know is 「—」, a runtime under a minute
    is counted in seconds, and a longer one in minutes and seconds.
    """
    body = _function_body(_source(_APP_JS), _DURATION_FUNCTION)

    check_duration_is_formatted(body)


def test_unknown_kind_falls_back_to_a_generic_section() -> None:
    """A kind the panel does not know must still be drawn.

    The backend sends the sections it has, and a newer backend may
    send a kind this panel has never heard of.  The panel must not
    drop it on the floor and must not raise: the kind is drawn as a
    generic block carrying its own wording.
    """
    body = _function_body(_source(_APP_JS), _SECTION_HTML_FUNCTION)

    check_unknown_kind_has_a_fallback(body)


def test_the_escaping_gate_has_teeth() -> None:
    """Negative control: an unescaped interpolation must fail.

    The fixture is the section renderer with one escaped value
    unwrapped — everything else is as it is, so a gate that only
    looked for ``escapeHtml`` anywhere in the body would accept it.
    """
    body = _function_body(_source(_APP_JS), _SECTION_HTML_FUNCTION)

    broken = body.replace("escapeHtml(", "rawHtml(", 1)
    assert broken != body, (
        "the fixture no longer unwraps an escaped value, so the negative "
        "control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_interpolations_are_escaped(broken)


def test_the_duration_gate_has_teeth() -> None:
    """Negative control: a formatter that never says 「—」 must fail.

    The fixture is the runtime formatter with the unknown answer
    removed — the minute boundary and the units are all still there,
    so a gate that only looked for those would accept it.
    """
    body = _function_body(_source(_APP_JS), _DURATION_FUNCTION)

    broken = body.replace(_UNKNOWN_DURATION, "?")
    assert broken != body, (
        "the fixture no longer removes the unknown answer, so the "
        "negative control would prove nothing."
    )
    assert _UNKNOWN_DURATION not in broken, (
        "the fixture still answers an unknown runtime with the dash, so "
        "the negative control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_duration_is_formatted(broken)


def test_the_unknown_kind_fallback_gate_has_teeth() -> None:
    """Negative control: a fallback that draws nothing must fail.

    The fixture is the section renderer with its answer for an unknown
    kind reduced to an empty string — every known branch is still
    there, so a gate that only counted the branches would accept it.
    """
    body = _function_body(_source(_APP_JS), _SECTION_HTML_FUNCTION)

    broken = _body_with_empty_fallback(body)
    assert broken != body, (
        "the fixture no longer empties the answer for an unknown kind, so "
        "the negative control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_unknown_kind_has_a_fallback(broken)
