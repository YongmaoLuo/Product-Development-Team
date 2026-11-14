"""The scheduler panel's card layout is a contract: the running block's
cards come before the queue block's cards, and the two block headings
are what they have always been.

Why this gate exists
--------------------
The scheduler view used to draw two tables — one for the running
plans and one for the queue — and each block was a triple: a heading,
an empty state and the table itself.  A table cannot be folded, so
each block now draws its plans as **cards**
(``#scheduler-running-cards`` / ``#scheduler-queue-cards``) and the
tables are left behind as a legacy surface.

A card layout breaks in ways a browser never shows: the running block's
cards can end up under the queue's, a block's heading can end up over
the other block's cards, and a card container can end up outside the
block it belongs to.  All three are invisible on the page and loud in
a source scan, so the contract is pinned here rather than at runtime.

The rule
--------
* ``frontend/index.html``: the scheduler view draws the 在跑 block's
  cards before the 队列 block's cards, each block's cards inside that
  block, and nothing is wedged between the two blocks.
* The two block headings are unchanged — 在跑 first, 队列 second.  The
  card is a change of surface, not a change of the panel's reading
  order.
* Both card containers exist and carry the ``scheduler-plan-cards``
  class, so the stylesheet has something to hang the card layout on.
* The two tables' own list containers (``#scheduler-running-list`` and
  ``#scheduler-queue-list``) stay: the end-to-end cases and the other
  gates read the panel through them, and a selector that resolves to
  nothing waits for ever instead of failing.

Scan target
-----------
``frontend/index.html``, read as source.  This is a shape gate, like
the rest of ``static_gates/``: it runs on a plain Python environment
with no JS toolchain, so it pins the contract in the source rather
than executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"

#: The card container of the running block, and of the queue block.
#: Each one is the element a renderer writes that block's cards into.
_RUNNING_CARDS_ID = "scheduler-running-cards"
_QUEUE_CARDS_ID = "scheduler-queue-cards"

#: The class the two card containers carry.
_CARDS_CLASS = "scheduler-plan-cards"

#: The two list containers the panel's tables are drawn into.  The cards
#: are the rendering surface now, but these ids stay: the end-to-end
#: cases and the other gates read the panel through them.
_RUNNING_LIST_ID = "scheduler-running-list"
_QUEUE_LIST_ID = "scheduler-queue-list"

#: The block headings of the scheduler view, in the order the panel is
#: expected to draw them.  The running plans come first: they are what
#: the operator opened the panel to see.
_EXPECTED_SUBTITLES = ("在跑", "队列")

#: The heading of each block, as the markup writes it.
_SUBTITLE_RE = re.compile(
    r"<h3[^>]*class=\"scheduler-subtitle\"[^>]*>(.*?)</h3>", re.DOTALL
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


def _section_html(html: str, section_id: str) -> str:
    """Return the HTML of the ``<section id=section_id>`` element.

    The element ends at the first ``</section>`` after its opening tag:
    the scheduler view holds no nested ``<section>``, so the first close
    is the right one, and a wrong id raises rather than returning the
    whole document.
    """
    match = re.search(
        r"<section[^>]*\bid=\"" + re.escape(section_id) + r"\"[^>]*>", html
    )
    assert match is not None, f'<section id="{section_id}"> not found'
    end = html.find("</section>", match.end())
    assert end != -1, f'<section id="{section_id}"> is never closed'
    return html[match.start() : end]


def _subtitles(view: str) -> list[tuple[str, int]]:
    """Return ``(heading, offset)`` for every block heading in ``view``."""
    return [
        (match.group(1).strip(), match.start())
        for match in _SUBTITLE_RE.finditer(view)
    ]


def _subtitle_offset(view: str, heading: str) -> int:
    """Return the offset of the block heading ``heading`` in ``view``.

    Raises :class:`AssertionError` when the heading is absent or appears
    more than once, so a gate that cannot find what it looks for fails
    loudly instead of matching nothing and passing.
    """
    offsets = [
        offset for title, offset in _subtitles(view) if title == heading
    ]
    assert len(offsets) == 1, (
        f"the scheduler view must carry exactly one 「{heading}」 block, "
        f"found {len(offsets)}"
    )
    return offsets[0]


def _offset_of(view: str, element_id: str) -> int:
    """Return the offset of the element ``element_id`` in ``view``.

    Raises :class:`AssertionError` when the element is absent, so a
    container that was renamed or dropped fails loudly instead of
    matching nothing and passing.
    """
    offset = view.find(f'id="{element_id}"')
    assert offset != -1, f'no element carries the id "{element_id}"'
    return offset


def _tag_of(html: str, element_id: str) -> str:
    """Return the opening tag of the element ``element_id``.

    Raises :class:`AssertionError` when no element carries the id.
    """
    match = re.search(
        r"<[a-zA-Z][^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>", html
    )
    assert match is not None, f'no element carries the id "{element_id}"'
    return match.group(0)


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def check_cards_container_order(view: str) -> None:
    """The running block's cards must be drawn before the queue block's.

    The panel answers "what is running right now" before "what is
    waiting" — the queue is the remainder, and it used to sit above the
    answer.  The running block is a triple (heading, empty state,
    cards), and all three of them have to move together: a heading
    above cards that live further down is not a moved block, it is a
    mismatched pair.

    Nothing may be wedged between the two blocks either: a third block
    in the middle would put the answer back out of reach.
    """
    running_at = _subtitle_offset(view, "在跑")
    queue_at = _subtitle_offset(view, "队列")

    running_empty_at = _offset_of(view, "scheduler-running-empty")
    running_cards_at = _offset_of(view, _RUNNING_CARDS_ID)
    queue_empty_at = _offset_of(view, "scheduler-queue-empty")
    queue_cards_at = _offset_of(view, _QUEUE_CARDS_ID)

    assert running_cards_at < queue_cards_at, (
        f"the scheduler view draws the {_QUEUE_CARDS_ID!r} container "
        f"above the {_RUNNING_CARDS_ID!r} one. The operator opens the "
        "panel to see what is running; the queue is what is left over."
    )

    assert running_at < running_empty_at < running_cards_at < queue_at, (
        "the running block is not whole: its heading, its empty state and "
        "its cards must all sit before the queue block's heading."
    )
    assert queue_at < queue_empty_at < queue_cards_at, (
        "the queue block is not whole: its heading, its empty state and "
        "its cards must stay together."
    )

    between = [
        title for title, offset in _subtitles(view) if running_at < offset < queue_at
    ]
    assert not between, (
        f"the scheduler view has another block between 在跑 and 队列: "
        f"{between}. Nothing may be wedged between the two."
    )


def check_subtitles_unchanged(view: str) -> None:
    """The two block headings must still be 在跑 and 队列, in that order.

    The card is a change of surface, not a change of the panel's
    reading order: the operator still reads what is running before
    what is waiting.  A heading that changed, or a third one that
    appeared between the two, would change what the panel says — and
    the fold would have taken a meaning with it.
    """
    titles = [title for title, _ in _subtitles(view)]
    assert titles == list(_EXPECTED_SUBTITLES), (
        "the scheduler view must draw exactly the 在跑 block and the 队列 "
        f"block, in that order; found {titles}."
    )


def check_card_containers(html: str) -> None:
    """Both card containers must exist and carry the card class.

    Without a container the renderer has nowhere to draw a plan: it
    would fall back to writing rows into the table, and the panel could
    never show a plan as a card that opens on demand.  Without the
    class the stylesheet has nothing to hang the card layout on.
    """
    for container_id in (_RUNNING_CARDS_ID, _QUEUE_CARDS_ID):
        tag = _tag_of(html, container_id)
        assert _CARDS_CLASS in tag, (
            f"the {container_id!r} container does not carry the "
            f".{_CARDS_CLASS} class; without it the stylesheet has nothing "
            "to hang the card layout on."
        )


def check_list_containers(html: str) -> None:
    """The two tables' list containers must stay.

    The cards are a *new* rendering surface, not a replacement for the
    ids: the end-to-end cases and the other gates read the panel through
    ``#scheduler-running-list`` and ``#scheduler-queue-list``, and a
    selector that resolves to nothing waits for ever instead of failing.
    """
    for list_id in (_RUNNING_LIST_ID, _QUEUE_LIST_ID):
        assert f'id="{list_id}"' in html, (
            f"index.html no longer carries #{list_id}. The panel's tables "
            "are drawn into it, and the cases that read the panel read it "
            "through that id."
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cards_container_order() -> None:
    """The running block's cards must be drawn before the queue block's.

    The panel answers "what is running right now" before "what is
    waiting".  Each block is a triple — heading, empty state, cards —
    and all three of them have to move together.
    """
    view = _section_html(_source(_INDEX_HTML), "scheduler-view")

    check_cards_container_order(view)


def test_subtitles_unchanged() -> None:
    """The two block headings must still be 在跑 and 队列, in that order.

    The card is a change of surface, not of the panel's reading order.
    """
    view = _section_html(_source(_INDEX_HTML), "scheduler-view")

    check_subtitles_unchanged(view)


def test_card_containers_carry_the_cards_class() -> None:
    """Both card containers must exist and carry ``scheduler-plan-cards``.

    A container is where the renderer draws a plan; the class is what
    the stylesheet hangs the card layout on.  Either one missing means
    the panel falls back to the table it was meant to leave behind.
    """
    html = _source(_INDEX_HTML)

    check_card_containers(html)


def test_list_containers_are_kept() -> None:
    """The two tables' list containers must stay in the markup.

    The cards are the rendering surface now, but the ids the end-to-end
    cases and the other gates read the panel through are not what this
    change is about.
    """
    html = _source(_INDEX_HTML)

    check_list_containers(html)


def test_the_gate_has_teeth() -> None:
    """Negative control: a panel whose cards are in the wrong order must fail.

    The fixture is the panel with the two card containers swapped — the
    running block's cards land under the queue's.  Everything else is
    as it is, so a gate that only looked for the containers would
    accept it.  The gate has to notice the order.
    """
    view = _section_html(_source(_INDEX_HTML), "scheduler-view")

    swapped = view.replace(
        f'id="{_RUNNING_CARDS_ID}"', 'id="__swapped__"', 1
    )
    swapped = swapped.replace(
        f'id="{_QUEUE_CARDS_ID}"', f'id="{_RUNNING_CARDS_ID}"', 1
    )
    swapped = swapped.replace('id="__swapped__"', f'id="{_QUEUE_CARDS_ID}"', 1)
    assert swapped != view, (
        "the fixture no longer swaps the two card containers, so the "
        "negative control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_cards_container_order(swapped)


def test_the_subtitle_gate_has_teeth() -> None:
    """Negative control: a renamed block heading must fail.

    The fixture is the panel with the 在跑 heading renamed.  The
    containers are all still where they were, so a gate that only
    looked at the containers would accept it — the heading is what
    says which block a card belongs to.
    """
    view = _section_html(_source(_INDEX_HTML), "scheduler-view")

    renamed = view.replace(">在跑<", ">正在跑<", 1)
    assert renamed != view, (
        "the fixture no longer renames the 在跑 heading, so the negative "
        "control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_subtitles_unchanged(renamed)
