"""The scheduler panel must have a place to draw its plan cards, and a
way to say that a card is collapsed.

Why this gate exists
--------------------
The scheduler view draws two tables: ``#scheduler-running-table`` and
``#scheduler-queue-table``.  A table cannot be collapsed — it is either
there or it is not — so the panel could not show a plan as a card that
starts folded and opens on demand: a plan's details are fetched only
once they are wanted, not before.

Two things have to exist before any script can draw such a card:

* a **container** for the cards, one per block, that the renderer
  writes into;
* a **rule** that says a collapsed card shows no detail.

The collapse is a *front-end state* and nothing else: it is expressed
by a DOM attribute (``data-expanded``) on the card, so folding a card
never changes a plan's state and never reaches the server.  The
attribute is what the stylesheet keys on, so a card that says
``data-expanded="false"`` — the default the renderer writes — must have
its detail hidden, and only a card that says ``"true"`` opens.

The two tables' own list containers (``#scheduler-running-list`` and
``#scheduler-queue-list``) stay: the end-to-end cases and the other
gates read the panel through them, and removing an id is not what this
change is about.

The card's detail is drawn as **sections** — a status header, a phase
banner, a progress bar, the running tasks — and each section is a
class of its own.  A class the stylesheet does not know is a piece of
the detail drawn in the browser's defaults, so the classes the section
renderer writes are scanned here too: the scan follows the renderer
rather than a hand-written list, so a class that is added to the
renderer cannot slip past the stylesheet unnoticed.

Scan target
-----------
``frontend/index.html``, ``frontend/style.css`` and ``frontend/app.js``,
read as source.  These are shape gates, like the rest of
``static_gates/``: they run on a plain Python environment with no JS
toolchain, so they pin the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"
_STYLE_CSS = _REPO_ROOT / "frontend" / "style.css"
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The card containers of the two blocks of the scheduler panel.  Each
#: one is the element a renderer writes the cards into.
_CARD_CONTAINER_IDS = (
    "scheduler-running-cards",
    "scheduler-queue-cards",
)

#: The class the card containers carry.
_CARDS_CLASS = "scheduler-plan-cards"

#: The two list containers the panel's tables are drawn into.  They
#: stay: the end-to-end cases and the other gates read the panel
#: through them.
_LIST_CONTAINER_IDS = (
    "scheduler-running-list",
    "scheduler-queue-list",
)

#: The three parts of a card, and the class each one carries.
_CARD_CLASSES = (
    "scheduler-plan-card",
    "scheduler-plan-header",
    "scheduler-plan-detail",
)

#: The attribute that carries the fold state, and the value that means
#: "collapsed" — the state a card is written in, and the one it stays
#: in until the operator opens it.
_EXPANDED_ATTRIBUTE = "data-expanded"
_COLLAPSED_VALUE = "false"

#: The function that draws one section of a card's detail.  The classes
#: it writes are the ones the stylesheet has to know.
_SECTION_HTML_FUNCTION = "schedulerSectionHtml"


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


def _css_rule_bodies(css: str, selector_fragment: str) -> list[str]:
    """Return the declaration bodies of every rule whose selector
    contains ``selector_fragment``.

    A rule may be written on its own or share its selector with a
    sibling; both are found, and both count as "this is styled".
    """
    bodies: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector_fragment in match.group(1):
            bodies.append(match.group(2))
    return bodies


def _declarations(body: str) -> set[str]:
    """Return the normalised declarations of a CSS rule body."""
    return {
        " ".join(declaration.split())
        for declaration in body.split(";")
        if declaration.strip()
    }


def _css_rule_bodies_for_class(css: str, class_name: str) -> list[str]:
    """Return the bodies of every rule that styles ``class_name``.

    The class must be the whole name: ``.scheduler-detail-status`` does
    not style ``.scheduler-detail-status-dot``.  A rule may be written
    on its own or share its selector with a sibling; both are found, and
    both count as "this is styled".

    Comments are taken out first: a comment is prose, not a selector,
    and one that happens to name a class must not look like a rule that
    styles it.
    """
    pattern = re.compile(r"\." + re.escape(class_name) + r"(?![\w-])")
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    bodies: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", without_comments):
        selectors = [selector.strip() for selector in match.group(1).split(",")]
        if any(pattern.search(selector) for selector in selectors):
            bodies.append(match.group(2))
    return bodies


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


def _section_classes(source: str) -> list[str]:
    """Return the classes the section renderer writes, in order.

    The list is read out of the renderer rather than written down here,
    so a class added to the renderer is picked up by the gate without
    anybody having to remember this file.
    """
    body = _function_body(source, _SECTION_HTML_FUNCTION)
    classes: list[str] = []
    for attribute in re.findall(r'class="([^"]+)"', body):
        for name in attribute.split():
            if name not in classes:
                classes.append(name)
    return classes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_card_container_present() -> None:
    """The panel must have a container for its cards — one per block.

    Without one the renderer has nowhere to draw a plan: it would fall
    back to writing rows into the table, and the panel could never show
    a plan as a card that opens on demand.

    The two containers are named after the blocks they belong to, so a
    card can be traced back to the list it came from.
    """
    html = _source(_INDEX_HTML)

    for container_id in _CARD_CONTAINER_IDS:
        assert f'id="{container_id}"' in html, (
            f"index.html has no #{container_id}. The panel has no place to "
            "draw its plan cards, so a plan can only ever be drawn as a "
            "table row — and a table row cannot be folded."
        )
        assert _CARDS_CLASS in html, (
            f"the card containers must carry the .{_CARDS_CLASS} class; "
            "without it the stylesheet has nothing to hang the card "
            "layout on."
        )


def test_detail_hidden_when_collapsed() -> None:
    """A collapsed card must show no detail, and only an expanded one
    shows it.

    The fold is a DOM attribute, so the stylesheet is the only thing
    that reads it.  A card written with ``data-expanded="false"`` — the
    default the renderer writes — whose detail is still visible is a
    card that looks open before it was opened, and a details request
    that has already been made by the time anyone looks at it.
    """
    css = _source(_STYLE_CSS)

    collapsed_selector = (
        f'.scheduler-plan-card[{_EXPANDED_ATTRIBUTE}="{_COLLAPSED_VALUE}"] '
        ".scheduler-plan-detail"
    )
    bodies = _css_rule_bodies(css, collapsed_selector)
    assert bodies, (
        f"style.css has no rule for {collapsed_selector}. A card that says "
        f'{_EXPANDED_ATTRIBUTE}="{_COLLAPSED_VALUE}" is collapsed, and '
        "the stylesheet is the only thing that reads that attribute."
    )

    declarations = {
        declaration for body in bodies for declaration in _declarations(body)
    }
    assert "display: none" in declarations, (
        "the collapsed card's detail is not hidden: "
        f"{sorted(declarations)}. A collapsed card must show no detail."
    )

    expanded_bodies = _css_rule_bodies(
        css, f'.scheduler-plan-card[{_EXPANDED_ATTRIBUTE}="true"]'
    )
    assert expanded_bodies, (
        'style.css has no rule for a card with '
        f'{_EXPANDED_ATTRIBUTE}="true". Without one the card can never be '
        "opened — the fold would be a one-way door."
    )


def test_legacy_list_containers_are_kept() -> None:
    """The two tables' list containers must stay.

    The cards are a *new* rendering surface, not a replacement for the
    ids: the end-to-end cases and the other gates read the panel
    through ``#scheduler-running-list`` and ``#scheduler-queue-list``,
    and a selector that resolves to nothing waits forever instead of
    failing.
    """
    html = _source(_INDEX_HTML)

    for list_id in _LIST_CONTAINER_IDS:
        assert f'id="{list_id}"' in html, (
            f"index.html no longer carries #{list_id}. The panel's tables "
            "are drawn into it, and the cases that read the panel read "
            "it through that id."
        )


def test_card_parts_styled() -> None:
    """The three parts of a card must each have a rule of their own.

    A card that is not styled is not a card: the header and the detail
    would run together as one block of text, and the operator could
    not tell what is the summary from what is the detail they asked
    for.

    The card's detail is drawn as sections, and each piece of a section
    is a class of its own — the heading, the dot, the badge, the
    elapsed time, the bar, the task rows.  Every class the section
    renderer writes must have a rule as well: a class the stylesheet
    does not know is a piece of the detail drawn in the browser's
    defaults, which is exactly what the card's parts must not be.
    """
    css = _source(_STYLE_CSS)

    for card_class in _CARD_CLASSES:
        assert _css_rule_bodies(css, f".{card_class}"), (
            f"style.css has no .{card_class} rule; the card's parts are "
            "not laid out at all."
        )

    section_classes = _section_classes(_source(_APP_JS))
    assert section_classes, (
        f"{_SECTION_HTML_FUNCTION}() writes no classes at all, so the "
        "gate cannot tell which ones the stylesheet has to know."
    )
    for section_class in section_classes:
        assert _css_rule_bodies_for_class(css, section_class), (
            f"style.css has no .{section_class} rule; the card's detail "
            "is drawn with this class, and without a rule it is drawn in "
            "the browser's defaults."
        )


def test_default_state_is_collapsed() -> None:
    """Collapsed must be the state a card is written in.

    The panel declares the default state in the markup and the
    stylesheet treats a card that says nothing as collapsed too: the
    only way to open a card is to say so, so a card can never be open by
    accident.
    """
    html = _source(_INDEX_HTML)
    css = _source(_STYLE_CSS)

    assert f'{_EXPANDED_ATTRIBUTE}="{_COLLAPSED_VALUE}"' in html, (
        f"index.html never writes {_EXPANDED_ATTRIBUTE}=\"{_COLLAPSED_VALUE}\". "
        "The panel must declare the state a card starts in, and that "
        "state is collapsed."
    )

    default_bodies = _css_rule_bodies(
        css, ".scheduler-plan-card .scheduler-plan-detail"
    )
    assert default_bodies, (
        "style.css has no rule for a card that does not say whether it "
        "is expanded. Such a card must be collapsed: the only way to "
        "open a card is to say so."
    )
