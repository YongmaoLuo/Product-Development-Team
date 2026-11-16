"""The scheduler's cards must be **reused**, not drawn again on every poll.

Why this gate exists
--------------------
The scheduler panel polls its read model every five seconds.  The two
renderers used to answer that poll by writing the container's whole
``innerHTML``: every card was thrown away and drawn again, five seconds
apart, for ever.

That is not only wasteful — it is **wrong in a way the operator sees**.
A card is a thing the operator *holds*: they open it, and the fold
they opened and the answer they are reading live on that card's node.
A card that is drawn again every five seconds is a new card, so the
fold is closed under their hands and the answer they were reading goes
with it.  The panel has to keep the card and change what is on it.

So the cards are **keyed by the plan they are about**, and the panel
syncs the container with the rows it is given:

* a plan that is still there keeps the card it has, and only what
  changed is written into it;
* a plan that is new is parsed out of its markup and put on the page;
* a plan that has gone takes its own card off the page — and only its
  own card;
* a plan that has moved is **moved**, not drawn again, so the card the
  operator is looking at stays the card they are looking at.

And the update writes only into the card's **header** and its
**actions**: the fold (``data-expanded``) and the detail area belong to
the operator and to the plan, and a poll has no business rewriting
them.

The keying is pinned as a **chain of variables** and not as a set of
words that happen to be in the file: the id read off the card has to
be the key the card is filed under, and the id read off the row has to
be what goes into the set of wanted plans.  A panel that reads the id
under another name, or files every card under one key, has broken the
contract even though the words are all still there — so the gate has
to be able to say so.

And the card has to **carry** that id, because the key the card is
filed under is the id the card carries: a card whose markup is filled
from another field of the row is filed under a key the rows are never
read under, so it is not found again — and a card that is not found
again is a card that is drawn again.

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

#: The function that brings a container's cards in line with its rows.
_SYNC_FUNCTION = "schedulerSyncCards"

#: The function that turns one card's markup into a node.
_PARSE_FUNCTION = "schedulerCardFromHtml"

#: The function that writes a plan's new facts into the card on the page.
_UPDATE_FUNCTION = "schedulerUpdateCard"

#: The function that draws one plan's card.
_CARD_FUNCTION = "schedulerCard"

#: The two renderers, and the container each of them draws into.
_RUNNING_RENDERER = "renderSchedulerRunning"
_QUEUE_RENDERER = "renderSchedulerQueue"
_RUNNING_CONTAINER = "#scheduler-running-cards"
_QUEUE_CONTAINER = "#scheduler-queue-cards"

#: The four parts of a card, and the class each one carries.
_CARD_CLASS = "scheduler-plan-card"
_HEADER_CLASS = "scheduler-plan-header"
_ACTIONS_CLASS = "scheduler-row-actions"
_DETAIL_CLASS = "scheduler-plan-detail"

#: The attribute a card carries the plan's id in.
_PLAN_ID_ATTRIBUTE = "data-plan-id="

#: The word the fold state is called by, in the markup and in the DOM.
_EXPANDED = "expanded"

#: A variable name in JavaScript.
_JS_NAME = r"[A-Za-z_$][\w$]*"

#: The plan id, read off the card that is on the page.  The guard at
#: the end keeps ``planIdX`` — a different attribute, and so a
#: different plan — from being read as ``planId``.
_CARD_ID_SOURCE = r"card\.dataset\.planId(?![\w$])"

#: The plan id, read off the row the server sent.  The same guard
#: keeps ``plan_id_x`` from being read as ``plan_id``.
_ROW_ID_SOURCE = r"\bplan_id(?![\w$])"


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


def _removal_position(body: str) -> int | None:
    """Return where *body* takes a card off the page, if it does.

    Two ways to do it are accepted — ``removeChild`` and ``remove()`` —
    and the position of the first of them is what the caller wants: the
    removal has to come **after** the check that says which plans are
    still wanted.
    """
    positions = [
        match.start()
        for match in re.finditer(r"removeChild\s*\(|\.remove\s*\(", body)
    ]
    return min(positions) if positions else None


def _check_renderer_syncs(body: str, renderer: str, container: str) -> None:
    """The rule the two renderers have to follow, as one check.

    It is a function of its own so that the negative control can run
    it against a fixture that breaks it.
    """
    assert _SYNC_FUNCTION + "(" in body, (
        f"{renderer}() does not sync the cards through {_SYNC_FUNCTION}(). "
        "A renderer that draws the cards itself draws them again on "
        "every poll, and the card the operator opened is a new card five "
        "seconds later."
    )

    assert container in body, (
        f"{renderer}() does not name the container it draws into "
        f"({container!r})."
    )

    assert "innerHTML" not in body, (
        f"{renderer}() writes the container's whole innerHTML. The cards "
        "are synced, not rebuilt: a card that is drawn again is a new "
        "node, and the operator's fold and the answer they are reading "
        "are lost with the old one."
    )


def _assigned_names(body: str, source_pattern: str) -> list[str]:
    """The names *body* binds to a read of ``source_pattern``.

    An assignment is read as ``name = ...`` up to the end of its
    statement, and the source has to be read **inside** that statement.
    So ``const id = card.dataset.planId ? ...`` binds ``id`` to the
    card's plan id — and nothing else in the body binds anything to it.

    The name has to be a name of its own: the dot in ``card.dataset``
    keeps ``dataset`` from being read as the thing being assigned.
    """
    names: list[str] = []
    for match in re.finditer(
        r"(?<![.\w$])(" + _JS_NAME + r")\s*=\s*([^;\n]*)", body
    ):
        if re.search(source_pattern, match.group(2)):
            names.append(match.group(1))
    return names


def _wanted_region(body: str) -> str:
    """The part of *body* that works out which plans are wanted.

    It starts where the set is made and ends where a plan is put into
    it: what lies between those two points is where the plans that are
    still wanted are read off the rows.
    """
    start = re.search(r"wanted\s*=\s*new Set\(", body)
    assert start is not None, (
        f"{_SYNC_FUNCTION}() does not make a set of the plans it wants, so "
        "there is nowhere for the plans it has been given to be read."
    )

    end = re.search(r"wanted\.add\(", body[start.end() :])
    assert end is not None, (
        f"{_SYNC_FUNCTION}() makes a set of the plans it wants but never "
        "puts a plan into it, so the set stays empty and every card is "
        "taken off the page."
    )

    return body[start.end() : start.end() + end.start()]


def _check_cards_are_keyed_by_plan(body: str) -> None:
    """The rule that a card is keyed by the plan it is about.

    It is a function of its own so that the negative control can run
    it against a fixture that breaks it.

    The rule is a **chain of variables**, not a set of words that
    happen to be in the body:

    * the card side — the plan id is read off the card that is on the
      page, and that very variable is the key the card is filed under
      in the table of cards; and **every** card in the table is filed
      under a plan id, so no card can be filed under where it happens
      to sit in the list;
    * the row side — the plan id is read off the row the server sent,
      and that very variable is what goes into the set of the plans
      that are wanted;
    * the lookups — the table and the set are consulted with variables
      that were read off a plan id, so the card that is found is the
      card of **that** plan, and the card that is taken off is the
      card of **that** plan.

    A card filed under anything else is a card that changes when the
    list changes — and the list changes on every poll.
    """
    # -- the card side -------------------------------------------------
    card_keys = _assigned_names(body, _CARD_ID_SOURCE)
    assert card_keys, (
        f"{_SYNC_FUNCTION}() does not read the plan id off the card that "
        "is on the page into a variable of its own, so it cannot tell "
        "which card belongs to which plan."
    )

    row_ids = set(_assigned_names(body, _ROW_ID_SOURCE))
    plan_ids = set(card_keys) | row_ids

    set_calls = re.findall(r"cards\.set\(", body)
    set_keys = re.findall(r"cards\.set\(\s*(" + _JS_NAME + r")\s*,", body)
    assert set_keys, (
        f"{_SYNC_FUNCTION}() does not file the cards in its table under a "
        "variable of their own, so the table has no key to speak of."
    )

    assert len(set_keys) == len(set_calls), (
        f"{_SYNC_FUNCTION}() files at least one card under something that "
        "is not a variable of its own. The key has to be the plan id the "
        "card was read under — a key that is the same for every card is "
        "a key that finds no card at all."
    )

    assert set(set_keys) & set(card_keys), (
        f"{_SYNC_FUNCTION}() files the cards in its table under "
        f"{sorted(set(set_keys))!r} rather than under the plan id it read "
        "off the card. A card filed under anything else is a card that "
        "changes when the list changes — and the list changes on every "
        "poll."
    )

    assert set(set_keys) <= plan_ids, (
        f"{_SYNC_FUNCTION}() files a card in its table under "
        f"{sorted(set(set_keys) - plan_ids)!r}, which is not a plan id. A "
        "card filed under anything else — under the row's place in the "
        "list, say — is a card that is not found again on the next poll, "
        "and a card that is not found again is a card that is drawn again."
    )

    # -- the row side --------------------------------------------------
    row_keys = _assigned_names(_wanted_region(body), _ROW_ID_SOURCE)
    assert row_keys, (
        f"{_SYNC_FUNCTION}() does not read the plan id off the rows it has "
        "been given into a variable of its own, so it cannot tell which "
        "plan a card is about."
    )

    added = re.findall(r"wanted\.add\(\s*(" + _JS_NAME + r")\s*\)", body)
    assert added, (
        f"{_SYNC_FUNCTION}() never puts a plan into the set of the plans it "
        "wants, so the set stays empty and every card is taken off the "
        "page."
    )

    assert set(added) & set(row_keys), (
        f"{_SYNC_FUNCTION}() puts {sorted(set(added))!r} into the set of the "
        "plans it wants rather than the plan id it read off the rows. A "
        "set of anything else never matches a card, so every card is "
        "taken off the page and drawn again."
    )

    # -- the lookups ---------------------------------------------------
    for call, what, allowed in (
        (r"wanted\.has\(", "wanted.has(...)", plan_ids),
        (r"cards\.get\(", "cards.get(...)", row_ids),
        (r"cards\.delete\(", "cards.delete(...)", plan_ids),
    ):
        args = re.findall(call + r"\s*(" + _JS_NAME + r")\s*\)", body)
        assert args, (
            f"{_SYNC_FUNCTION}() never asks {what} about a plan id, so it "
            "cannot tell a card that has gone from a card that is still "
            "there."
        )

        assert set(args) <= allowed, (
            f"{_SYNC_FUNCTION}() asks {what} about "
            f"{sorted(set(args) - allowed)!r}, which is not a plan id. The "
            "card that is found, and the card that is taken off, have to "
            "be found and taken off by the plan they are about."
        )


def _check_card_markup_carries_the_plan_id(card_body: str) -> None:
    """The rule that a card **carries** the plan's id in its markup.

    It is a function of its own so that the negative control can run
    it against a fixture that breaks it.

    The card is keyed by the id it carries: the sync reads that id off
    the card with ``card.dataset.planId`` and files the card under it.
    So the id in the markup has to be **the plan's id** — the one read
    off the row the card is drawn for — and not some other field of
    that row that happens to be lying around.

    A card that carries another id is filed under a key the rows are
    never read under, so it is not found again on the next poll — and
    a card that is not found again is a card that is drawn again.  That
    is the rebuild this whole gate exists to forbid, and it is
    invisible to a gate that only asks whether the word
    ``data-plan-id=`` is in the file.
    """
    filled = re.search(
        re.escape(_PLAN_ID_ATTRIBUTE) + r"\"\$\{([^{}]*)\}\"", card_body
    )
    assert filled is not None, (
        f"{_CARD_FUNCTION}() does not fill the plan's id into the card's "
        f"markup ({_PLAN_ID_ATTRIBUTE!r}). A card that carries no id "
        "cannot be keyed by the plan it is about, so it is drawn again "
        "on every poll."
    )
    expression = filled.group(1)

    plan_ids = _assigned_names(card_body, _ROW_ID_SOURCE)
    assert plan_ids, (
        f"{_CARD_FUNCTION}() does not read the plan's id off the row it "
        "draws the card for, so there is no plan id to put into the card."
    )

    used = [
        name
        for name in plan_ids
        if re.search(
            r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", expression
        )
    ]
    assert used, (
        f"{_CARD_FUNCTION}() fills the card's plan id from {expression!r} "
        f"rather than from the plan id it read off the row "
        f"({sorted(plan_ids)!r}). A card that carries another id is a card "
        "the sync files under a key the rows are never read under, so it "
        "is taken off the page and drawn again on every poll."
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_renderers_sync_cards_instead_of_rebuilding() -> None:
    """The two renderers must sync their cards, not draw them again.

    A renderer that writes the container's whole ``innerHTML`` throws
    away every card on the page and draws new ones — so the card the
    operator opened is a **different node** five seconds later, and the
    fold and the answer they were reading go with it.

    So the renderers hand their rows to the sync, and the sync does the
    drawing.  What the renderers must not do is write the container's
    markup themselves: a whole-container ``innerHTML`` assignment in a
    renderer is exactly the rebuild this gate exists to forbid.
    """
    source = _source(_APP_JS)

    for renderer, container in (
        (_RUNNING_RENDERER, _RUNNING_CONTAINER),
        (_QUEUE_RENDERER, _QUEUE_CONTAINER),
    ):
        _check_renderer_syncs(
            _function_body(source, renderer), renderer, container
        )


def test_card_is_keyed_by_plan_id() -> None:
    """A card must be keyed by the plan it is about.

    The key is what makes a card reusable.  Without it the panel can
    only find a card by its **position**, and a card that is found by
    its position is a card that changes when the list changes — which
    is every poll.

    So the card says which plan it is about (``data-plan-id``), the
    sync reads that key off the cards that are on the page, and it
    reads the plan's id off the rows it is given.  The cards are kept
    in a table keyed by it: a plan's card is then the same card on the
    next poll.

    The key is pinned as a **chain of variables** and not as a word
    that happens to be in the body: the id read off the card is the
    key the card is filed under, and the id read off the row is what
    goes into the set of wanted plans.  A panel that reads the id
    under another name, or files every card under one key, has broken
    the contract even though the words are all still there.
    """
    source = _source(_APP_JS)

    card = _function_body(source, _CARD_FUNCTION)
    assert _PLAN_ID_ATTRIBUTE in card, (
        f"{_CARD_FUNCTION}() does not write the plan's id into the card's "
        "markup, so a card on the page cannot be traced back to the plan "
        "it is about."
    )

    _check_card_markup_carries_the_plan_id(card)

    sync = _function_body(source, _SYNC_FUNCTION)

    assert "new Map(" in sync, (
        f"{_SYNC_FUNCTION}() does not keep the cards in a table keyed by "
        "the plan. Cards looked up by their position are cards that "
        "change with the list — and the list changes on every poll."
    )

    _check_cards_are_keyed_by_plan(sync)


def test_update_card_leaves_detail_and_expanded_alone() -> None:
    """The update must not touch the fold or the detail.

    The fold is the **operator's**: they opened the card, and nothing
    else moves it.  The detail is the **plan's**: it is what the
    operator is reading, and the panel's poll has no newer answer than
    the one already on the screen — a request for it is made by the
    card's own refresh, in the background.

    So an update writes the header and the actions and nothing else.  A
    card that is open keeps what it shows; a card that is folded keeps
    its detail empty.  An update that rewrote either of them would
    undo the operator's own work on every poll.
    """
    source = _source(_APP_JS)
    update = _function_body(source, _UPDATE_FUNCTION)

    assert _DETAIL_CLASS not in update, (
        f"{_UPDATE_FUNCTION}() reaches into the card's detail. The detail "
        "is the plan's answer and the operator is reading it — a poll "
        "that rewrites it throws away what is on the screen."
    )

    assert _EXPANDED not in update, (
        f"{_UPDATE_FUNCTION}() touches the fold state. The fold is the "
        "operator's: they opened the card, and a poll must not close it "
        "under their hands."
    )


def test_update_keeps_header_node() -> None:
    """The update must write into the header it has, not replace it.

    The header is the button the operator clicks.  A header that is
    **replaced** is a new button, and the click that lands on it is a
    click on something else — the card's own node, and everything the
    page knows about it, has to stay the node it was.

    So the update writes the header's **contents** (``innerHTML``) and
    leaves the header itself in place.  The same goes for the actions:
    they are written into, not swapped out.
    """
    source = _source(_APP_JS)
    update = _function_body(source, _UPDATE_FUNCTION)

    assert _HEADER_CLASS in update, (
        f"{_UPDATE_FUNCTION}() never looks at the card's header, so the "
        "header keeps saying what it said when the card was drawn."
    )

    assert re.search(r"header\.innerHTML\s*=(?!=)", update), (
        f"{_UPDATE_FUNCTION}() does not write into the header it already "
        "has. The header's contents are what changed; the header itself "
        "is the button the operator clicks, and it must stay the node it "
        "was."
    )

    assert re.search(r"actions\.innerHTML\s*=(?!=)", update), (
        f"{_UPDATE_FUNCTION}() does not write into the actions it "
        "already has, so the card keeps offering the actions of the plan "
        "it used to be about."
    )

    for forbidden in ("replaceWith", "replaceChild", "outerHTML", "removeChild"):
        assert forbidden not in update, (
            f"{_UPDATE_FUNCTION}() uses {forbidden!r}. The card's nodes are "
            "written into, not swapped out — a node that is replaced is a "
            "node the page no longer knows about."
        )

    assert not re.search(r"card\.innerHTML\s*=(?!=)", update), (
        f"{_UPDATE_FUNCTION}() rewrites the whole card. Only the header "
        "and the actions change; the card's own state does not."
    )


def test_removed_plan_drops_only_its_card() -> None:
    """A plan that has gone must take **its own** card with it.

    A card that is left behind is a card for a plan that is not there:
    the operator sees a plan that has finished, or been taken off the
    queue, as if it were still waiting.  And the way the card is taken
    off matters just as much: emptying the container would draw every
    card again, which is the rebuild this whole change is about.

    So the plans that are still wanted are worked out from the rows, and
    the cards of the plans that are not wanted are taken off the page
    one by one.
    """
    source = _source(_APP_JS)
    sync = _function_body(source, _SYNC_FUNCTION)

    assert "new Set(" in sync, (
        f"{_SYNC_FUNCTION}() does not work out which plans are still "
        "wanted, so it cannot tell a card that has gone from a card that "
        "is still there."
    )

    assert "wanted" in sync, (
        f"{_SYNC_FUNCTION}() does not name the plans it wants. The cards "
        "that are not wanted are the ones that have to go."
    )

    removal = _removal_position(sync)
    assert removal is not None, (
        f"{_SYNC_FUNCTION}() never takes a card off the page, so a plan "
        "that has gone leaves its card behind."
    )

    assert "innerHTML" not in sync, (
        f"{_SYNC_FUNCTION}() writes the container's markup. A card that is "
        "taken off has to be taken off **as a node**: emptying the "
        "container draws every card again, and the card the operator is "
        "looking at becomes a new one."
    )

    assert sync.index("wanted") < removal, (
        f"{_SYNC_FUNCTION}() takes a card off the page before it has "
        "worked out which plans are still wanted. The check has to come "
        "first, or every card is taken off and drawn again."
    )


def test_new_card_parsed_from_single_card_html() -> None:
    """A new card must be parsed out of **one** card's markup.

    The card is drawn as markup by the card renderer, and a plan that
    is new has to become a node the page can hold.  The markup is
    parsed in a ``template`` element: a template parses without putting
    anything on the page, so nothing in the markup can fire an event or
    fetch a resource by accident — the card goes on the page only when
    the panel has decided to show it.

    What comes back is **one** card — the one the markup is about —
    and markup that carries no card gives back nothing at all.
    """
    source = _source(_APP_JS)
    parse = _function_body(source, _PARSE_FUNCTION)

    assert 'createElement("template")' in parse, (
        f"{_PARSE_FUNCTION}() does not parse the markup in a template. A "
        "template parses without putting anything on the page, so the "
        "markup cannot act before the panel has decided to show it."
    )

    assert re.search(
        r"querySelector\(\s*[\"']\." + re.escape(_CARD_CLASS) + r"[\"']\s*\)",
        parse,
    ), (
        f"{_PARSE_FUNCTION}() does not look the card up by its own class, "
        "so what it hands back is not the card but whatever surrounds it."
    )

    assert "querySelectorAll" not in parse, (
        f"{_PARSE_FUNCTION}() hands back a list of cards. One card's "
        "markup is one card, and a list would put every card of the "
        "panel on the page once per plan."
    )

    assert re.search(r"return\s+null", parse), (
        f"{_PARSE_FUNCTION}() never gives back nothing. Markup that "
        "carries no card has no card to show, and handing back something "
        "else would put the wrong thing on the page."
    )


def test_the_gate_has_teeth() -> None:
    """Negative control: a panel that rebuilds its cards must fail.

    Two fixtures are checked against the rules of this gate:

    * a panel whose renderers do not call the sync — the rebuild this
      change replaced;
    * a renderer that writes the container's whole ``innerHTML`` — the
      rebuild the sync was written to end.

    Both must be caught.  A gate that passes them pins nothing.
    """
    source = _source(_APP_JS)

    # The fixture: the renderers no longer call the sync.  Only the
    # calls are renamed — the declaration stays, so the gate's other
    # rules still have something to read.
    broken = re.sub(
        r"(?<!function )\b" + re.escape(_SYNC_FUNCTION) + r"\(",
        "schedulerRebuildCards(",
        source,
    )
    assert broken.count(f"{_SYNC_FUNCTION}(") < source.count(
        f"{_SYNC_FUNCTION}("
    ), (
        "the negative control did not take: the fixture did not change "
        "the number of calls."
    )

    assert f"function {_SYNC_FUNCTION}(" in broken, (
        "the negative control took the declaration with it; the gate's "
        "other rules have nothing left to read."
    )

    for renderer, container in (
        (_RUNNING_RENDERER, _RUNNING_CONTAINER),
        (_QUEUE_RENDERER, _QUEUE_CONTAINER),
    ):
        body = _function_body(broken, renderer)
        with pytest.raises(AssertionError):
            _check_renderer_syncs(body, renderer, container)

    # And the rule that forbids the rebuild itself: a renderer that
    # writes the container's markup must be caught too.
    rebuilt = (
        'const body = $("#scheduler-queue-cards");\n'
        '    body.innerHTML = rows.map((row) => row).join("");\n'
    )
    with pytest.raises(AssertionError):
        _check_renderer_syncs(rebuilt, _QUEUE_RENDERER, _QUEUE_CONTAINER)


def test_the_keying_gate_has_teeth() -> None:
    """Negative control: a panel that does not key its cards must fail.

    Four fixtures are cut out of the real body of the sync:

    * a panel that reads the plan id off the card under another name —
      so the card it is holding is not the card it thinks it is;
    * a panel that files **every** card under one key — so the cards
      overwrite one another and none of them is found again;
    * a panel that reads the plan id off the row under another name —
      so the set of wanted plans is empty and every card is taken off
      the page and drawn again;
    * a panel that files a **new** card under the row's place in the
      list — so that card is not found again on the next poll, which is
      the same rebuild the whole change is about.

    And two more are cut out of the real body of the card:

    * a card whose plan id is filled from another field of the row —
      so the card carries an id the rows are never read under;
    * a card that reads another id off the row — so it carries that one
      instead of the plan's.

    All six must be caught.  A gate that passes them pins nothing.
    """
    source = _source(_APP_JS)
    body = _function_body(source, _SYNC_FUNCTION)

    # The rule holds on the real body — so the fixtures below fail
    # because of what was changed, not because the rule was already
    # broken.
    _check_cards_are_keyed_by_plan(body)

    fixtures = (
        (
            "the card's plan id is read under another name",
            "card.dataset.planId",
            "card.dataset.planIdX",
        ),
        (
            "every card is filed under one key",
            "cards.set(id, card);",
            'cards.set("one-key-for-every-plan", card);',
        ),
        (
            "the row's plan id is read under another name",
            "item.plan_id",
            "item.plan_id_x",
        ),
        (
            "a new card is filed under the row's place in the list",
            "            cards.set(id, card);",
            "            cards.set(index, card);",
        ),
    )

    for what, old, new in fixtures:
        broken = body.replace(old, new)
        assert broken != body, (
            f"the negative control did not take: {what} — {old!r} is not "
            "in the body of the sync."
        )

        with pytest.raises(AssertionError):
            _check_cards_are_keyed_by_plan(broken)

    # And the card's own side: the card has to **carry** the plan's id,
    # because the key the sync files the card under is the id the card
    # carries.  A card that carries another id is filed under a key the
    # rows are never read under, so it is not found again — and a card
    # that is not found again is a card that is drawn again.
    card = _function_body(source, _CARD_FUNCTION)

    # The rule holds on the real card — so the fixtures below fail
    # because of what was changed, not because the rule was already
    # broken.
    _check_card_markup_carries_the_plan_id(card)

    card_fixtures = (
        (
            "the card's plan id is filled from another field",
            'data-plan-id="${escapeHtml(planId)}"',
            'data-plan-id="${escapeHtml(item.run_id)}"',
        ),
        (
            "the card reads another id off the row",
            'const planId = item.plan_id ? String(item.plan_id) : "";',
            'const planId = item.run_id ? String(item.run_id) : "";',
        ),
    )

    for what, old, new in card_fixtures:
        broken = card.replace(old, new)
        assert broken != card, (
            f"the negative control did not take: {what} — {old!r} is not "
            "in the body of the card."
        )

        with pytest.raises(AssertionError):
            _check_card_markup_carries_the_plan_id(broken)
