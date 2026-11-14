"""The scheduler panel must show the running plans before the queue, and
must no longer carry the third block.

Why this gate exists
--------------------
The scheduler view used to stack three blocks: 队列 (the queue), 在跑
(the running plans), and 「可操作的计划」 (the plans an operator can act
on).  That third block was built from ``GET /api/plans`` filtered by
phase, which makes it a *superset* of the two blocks above it: every
plan the operator could act on was already named in the queue or in the
running list.  It cost a second request, a second phase filter and a
third copy of the same plan ids, and it sat at the bottom — so the
operator had to scroll past the answer to find the plans.

The order was backwards as well.  The queue was drawn *above* the
running plans, although the question an operator opens the panel with
is "what is running right now, and what is it doing".  The panel now
reads capacity → 在跑 → 队列.

These are properties of the *markup*, invisible in a browser and loud
in a source scan, so they are pinned here rather than at runtime.

The rule
--------
* ``frontend/index.html``: the running block (its ``<h3>``, its empty
  state and its **cards**) comes before the queue block, and nothing
  else is wedged between the two.  Each block draws its plans as
  cards, so the cards are what the block is made of.
* The 「可操作的计划」 block is gone — its heading and its three element
  ids.
* ``frontend/app.js``: no call to the deleted renderer or loader is
  left, and no selector points at a deleted element id.

The last one is the failure this gate is really about: a block removed
from the markup but still called from the script leaves
``$("#scheduler-plans-list")`` returning ``null``, and the panel
throws — so the blocks that are still wanted go blank too.

Scan target
-----------
``frontend/index.html`` and ``frontend/app.js``, read as source.  These
are shape gates, like the rest of ``static_gates/``: they run on a
plain Python environment with no JS toolchain, so they pin the
contract in the source rather than executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The block headings of the scheduler view, in the order the panel is
#: expected to draw them.  The running plans come first: they are what
#: the operator opened the panel to see.
_EXPECTED_SUBTITLES = ("在跑", "队列")

#: The ids of the deleted 「可操作的计划」 block.  All of them must be
#: gone from the markup, and none of them may be named by the script.
_REMOVED_ELEMENT_IDS = (
    "scheduler-plans-list",
    "scheduler-plans-table",
    "scheduler-plans-empty",
)

#: The words of the deleted block.  The heading is what the operator
#: read; the empty-state text is the only other place the same idea
#: was written down.
_REMOVED_WORDS = ("可操作的计划", "没有可操作的计划")

#: The heading of each block, as the markup writes it.
_SUBTITLE_RE = re.compile(
    r"<h3[^>]*class=\"scheduler-subtitle\"[^>]*>(.*?)</h3>", re.DOTALL
)

#: The card container of each block.  The panel draws a block's plans
#: as cards, so the cards are what the block is made of.
_RUNNING_CARDS_ID = "scheduler-running-cards"
_QUEUE_CARDS_ID = "scheduler-queue-cards"

#: One fact of a card: the object that carries the name it is known by
#: and the label it is drawn under.  A fact holds no object of its own,
#: so it can be matched without counting braces.
_FACT_RE = re.compile(r"\{[^{}]*?name:\s*\"(?P<name>[^\"]+)\"[^{}]*?\}", re.DOTALL)


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


def _fact(body: str, name: str) -> str | None:
    """Return the fact of *body* that is known by *name*.

    A fact is the object a renderer hands to the card: it carries the
    name it is known by, the label it is drawn under and the value it
    shows.  It holds no object of its own, so it is matched without
    counting braces.

    Returns ``None`` when the body carries no such fact, so the caller
    can fail with a message about the fact rather than about a regex.
    """
    for match in _FACT_RE.finditer(body):
        if match.group("name") == name:
            return match.group(0)
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_running_cards_before_queue_cards() -> None:
    """The running block must be drawn before the queue block.

    The panel answers "what is running right now" before "what is
    waiting" — the queue is the remainder, and it used to sit above the
    answer.  The running block is a triple (heading, empty state,
    cards), and all three of them have to move together: a heading
    above cards that live further down is not a moved block, it is a
    mismatched pair.

    Nothing may be wedged between the two blocks either: a third block
    in the middle would put the answer back out of reach.
    """
    view = _section_html(_source(_INDEX_HTML), "scheduler-view")

    running_at = _subtitle_offset(view, "在跑")
    queue_at = _subtitle_offset(view, "队列")

    assert running_at < queue_at, (
        "the scheduler view draws 队列 above 在跑. The operator opens the "
        "panel to see what is running; the queue is what is left over."
    )

    running_empty_at = view.index('id="scheduler-running-empty"')
    running_cards_at = view.index(f'id="{_RUNNING_CARDS_ID}"')
    queue_empty_at = view.index('id="scheduler-queue-empty"')
    queue_cards_at = view.index(f'id="{_QUEUE_CARDS_ID}"')

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

    titles = [title for title, _ in _subtitles(view)]
    assert titles == list(_EXPECTED_SUBTITLES), (
        "the scheduler view must draw exactly the 在跑 block and the 队列 "
        f"block, in that order; found {titles}."
    )


def test_plans_block_is_gone() -> None:
    """The 「可操作的计划」 block must no longer exist in the markup.

    It was a superset of the two blocks that remain: it listed every
    plan of every actionable phase, while the queue lists the waiting
    ones and the running list the ones holding a slot.  Every plan it
    showed was already shown twice, and the operator had to scroll to
    the bottom of the panel to see it.
    """
    html = _source(_INDEX_HTML)

    for word in _REMOVED_WORDS:
        assert word not in html, (
            f"index.html still carries {word!r}. The 「可操作的计划」 block "
            "is a superset of the queue and the running list, and it is "
            "gone."
        )

    for element_id in _REMOVED_ELEMENT_IDS:
        assert element_id not in html, (
            f"index.html still carries the id {element_id!r}. The block it "
            "belongs to is gone; the element goes with it."
        )


def test_app_js_has_no_dangling_plans_call() -> None:
    """The script must not call what the markup no longer has.

    ``$("#scheduler-plans-list")`` returns ``null`` when the element is
    gone, and the renderer throws on the next line — so the panel that
    is still wanted goes blank with it.  The load of ``/api/plans`` is
    a second cost for a block that no longer exists: one request fewer
    on every open of the scheduler view.
    """
    source = _source(_APP_JS)

    for name in ("loadSchedulerPlans", "renderSchedulerPlans"):
        assert name not in source, (
            f"app.js still names {name}(). The 「可操作的计划」 block is "
            "gone; the call that fed it has to go with it."
        )

    for element_id in _REMOVED_ELEMENT_IDS:
        assert element_id not in source, (
            f"app.js still selects #{element_id}. The element is gone, so "
            "the selector resolves to null and the panel throws."
        )


def test_queue_cards_keep_the_promotion_fact() -> None:
    """The queue must keep its 提升 fact, and an absent promotion
    must still read as 「—」.

    The promotion is the one number the queue adds to the plan list —
    how far up the queue the plan was pushed — so removing a *block*
    must not take a *fact* with it.  The empty value stays a dash
    rather than an empty cell: a blank cell reads as "not filled in
    yet", while 「—」 reads as "there is nothing here", which is what an
    unpromoted plan means.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerQueue")

    assert "promote_order" in body, (
        "renderSchedulerQueue() no longer reads promote_order; the 提升 "
        "fact would be empty for every card."
    )

    fact = _fact(body, "promote_order")
    assert fact is not None, (
        "renderSchedulerQueue() has no promote_order fact any more; the "
        "queue's own number — how far up the plan was pushed — would be "
        "missing from every card."
    )
    assert 'label: "提升"' in fact, (
        "the promote_order fact is not labelled 提升 any more; the card "
        "would show a number nobody can read."
    )

    assert '"—"' in body, (
        "renderSchedulerQueue() no longer falls back to 「—」 for a plan "
        "without a promotion. The dash says \"there is nothing here\"; an "
        "empty cell says \"not filled in yet\"."
    )
