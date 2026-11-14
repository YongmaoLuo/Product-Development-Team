"""The scheduler's plans must be drawn as cards that start folded, and
a plan's detail must be asked for only when its card is opened.

Why this gate exists
--------------------
The scheduler panel polls its read model every five seconds.  A panel
that drew a plan's progress lines as part of that poll would spend one
request per plan per poll on information the operator may never look
at; a panel that drew them as a table row would have nowhere to put
them once the operator *did* want them.

So a plan is a **card**: a header the operator reads at a glance, and
a detail that is folded away until it is asked for.  The fold is a
front-end state and nothing else — it is the ``data-expanded``
attribute on the card, and the stylesheet keys on it.  Four things
follow from that, and each of them is pinned here:

* a card is written **folded** (``data-expanded="false"``), so the
  only way a detail is shown is to open the card;
* the **detail request** happens in the toggle — the function that
  opens a card — and in no renderer, so a folded card costs nothing;
* opening and folding is **not a write**: neither of the two fold
  functions may call a POST endpoint.  Folding a card is the
  operator's business, not the plan's;
* the cards are drawn into the **card containers**
  (``#scheduler-running-cards`` / ``#scheduler-queue-cards``), not
  into the tables' list containers: a card that is drawn as a table
  row cannot be folded at all.

Two more promises the fold makes are pinned as well: a card that was
opened before is **not asked for again** (the detail is cached by the
plan's id), and a plan whose detail cannot be read says so **in its
own card** rather than leaving the list blank.

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


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_STYLE_CSS = _REPO_ROOT / "frontend" / "style.css"

#: The function that draws one plan's card.
_CARD_FUNCTION = "schedulerCard"

#: The function that opens a card, or folds it again.
_TOGGLE_FUNCTION = "toggleSchedulerCard"

#: The function that puts the cards the operator has opened back the
#: way they left them.
_RESTORE_FUNCTION = "schedulerRestoreOpenCards"

#: The function that decides whether a card's detail has gone stale.
_STALE_FUNCTION = "schedulerCardDetailStale"

#: The function that finds the card of one plan on the page.
_CARD_ELEMENT_FUNCTION = "schedulerCardElement"

#: The two functions that write into a card's detail: the lines a plan
#: is showing, and the word a failed request leaves behind.
_DETAIL_HTML_FUNCTION = "schedulerCardDetailHtml"
_ERROR_HTML_FUNCTION = "schedulerCardErrorHtml"

#: The fields the running side owns.  They are the plan's own clock,
#: the slot clock it falls back to, and the backend's eviction flag.
_RUNNING_FIELDS = ("running_sec", "elapsed_sec", "evictable")

#: The fields the queue side owns.
_QUEUE_FIELDS = ("promote_order",)

#: The two renderers, and the container each of them draws into.
_RUNNING_RENDERER = "renderSchedulerRunning"
_QUEUE_RENDERER = "renderSchedulerQueue"
_RUNNING_CONTAINER = "#scheduler-running-cards"
_QUEUE_CONTAINER = "#scheduler-queue-cards"

#: The list containers the two tables are drawn into.  The cards
#: replaced them as the rendering surface, so a renderer that still
#: wrote into them would draw the plan twice — once as a card and
#: once as a row that cannot be folded.
_RUNNING_LIST = "#scheduler-running-list"
_QUEUE_LIST = "#scheduler-queue-list"

#: The attribute that carries the fold state, and the value that means
#: "collapsed" — the state a card is written in, and the one it stays
#: in until the operator opens it.
_COLLAPSED = 'data-expanded="false"'

#: The detail endpoint the card opens.  The path is matched loosely
#: because the plan id is interpolated into it.
_DETAIL_PATH = re.compile(r"/scheduler/plan/[^\s`'\"]*?/detail")

#: A write: the way this codebase asks for a POST.
_POST = re.compile(r"""method\s*:\s*["']POST["']""")

#: The cache the opened cards' details are kept in.
_DETAIL_CACHE = "_schedulerCardDetails"

#: The lifetime of one cached detail.  The panel polls its read model
#: every five seconds, and an open card that never catches up with the
#: plan it is about is a card that answers a question nobody asked.
_TTL_CONSTANT = "SCHEDULER_CARD_DETAIL_TTL_MS"

#: The field the cache keeps the time a detail was fetched under.
_FETCHED_AT = "at"


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_card_defaults_to_collapsed() -> None:
    """A card must be written folded.

    Collapsed is the state a card starts in, and the stylesheet reads
    it: a card that says nothing about its fold state is collapsed
    anyway, but a card that says it is *open* before the operator
    opened it would show a detail nobody asked for — and a detail
    request that had already been made by the time anyone looked at
    it.

    So the card is written with ``data-expanded="false"``, and no
    renderer writes it any other way.
    """
    source = _source(_APP_JS)
    card = _function_body(source, _CARD_FUNCTION)

    assert _COLLAPSED in card, (
        f"{_CARD_FUNCTION}() does not write {_COLLAPSED!r}. A card must be "
        "written folded: the only way a detail is shown is to open the "
        "card."
    )

    for renderer in (_RUNNING_RENDERER, _QUEUE_RENDERER):
        body = _function_body(source, renderer)
        assert 'data-expanded="true"' not in body, (
            f"{renderer}() writes a card that is open before it was "
            "opened. The fold is the operator's to make."
        )


def test_detail_request_only_on_expand() -> None:
    """The detail must be asked for in the toggle, and nowhere else.

    The request is what an open card costs.  A request made by a
    renderer would be made on **every poll** — one per plan, every
    five seconds, for a detail the operator may never look at — and a
    folded card would no longer be free.

    So the request lives in the toggle: the one function that opens a
    card.  No renderer may ask for it.
    """
    source = _source(_APP_JS)
    toggle = _function_body(source, _TOGGLE_FUNCTION)

    assert _DETAIL_PATH.search(toggle), (
        f"{_TOGGLE_FUNCTION}() never asks for the plan's detail, so "
        "opening a card opens an empty one."
    )

    for renderer in (_CARD_FUNCTION, _RUNNING_RENDERER, _QUEUE_RENDERER):
        body = _function_body(source, renderer)
        assert _DETAIL_PATH.search(body) is None, (
            f"{renderer}() asks for the plan's detail. The detail is "
            "asked for when the operator opens the card — asking for it "
            "while the card is folded spends a request per plan on every "
            "poll."
        )


def test_toggle_does_not_post() -> None:
    """Folding and opening must not write anything.

    The fold is a front-end state: the card says which it is with
    ``data-expanded`` and the panel changes nothing else.  A POST in
    either of the two fold functions would make opening a card a
    change to the plan — a write the operator never asked for, made
    by a click that was only meant to look.
    """
    source = _source(_APP_JS)

    for name in (_CARD_FUNCTION, _TOGGLE_FUNCTION):
        body = _function_body(source, name)
        assert _POST.search(body) is None, (
            f"{name}() calls a POST endpoint. Folding and opening a card "
            "is a front-end state; it must not change the plan."
        )


def test_renders_into_cards_container() -> None:
    """The two renderers must draw into the card containers.

    A card that is drawn as a table row cannot be folded: the table is
    either there or it is not.  So the renderers write into
    ``#scheduler-running-cards`` and ``#scheduler-queue-cards``, and
    they no longer write into the tables' list containers.
    """
    source = _source(_APP_JS)

    running = _function_body(source, _RUNNING_RENDERER)
    queue = _function_body(source, _QUEUE_RENDERER)

    assert _RUNNING_CONTAINER in running, (
        f"{_RUNNING_RENDERER}() does not draw into {_RUNNING_CONTAINER!r}. "
        "The running plans must be drawn as cards, or they cannot be "
        "folded."
    )

    assert _QUEUE_CONTAINER in queue, (
        f"{_QUEUE_RENDERER}() does not draw into {_QUEUE_CONTAINER!r}. The "
        "queued plans must be drawn as cards, or they cannot be folded."
    )

    assert _RUNNING_LIST not in running, (
        f"{_RUNNING_RENDERER}() still writes into {_RUNNING_LIST!r}. The "
        "plan would be drawn twice — once as a card and once as a row "
        "that cannot be folded."
    )

    assert _QUEUE_LIST not in queue, (
        f"{_QUEUE_RENDERER}() still writes into {_QUEUE_LIST!r}. The plan "
        "would be drawn twice — once as a card and once as a row that "
        "cannot be folded."
    )


def test_expand_reuses_the_cached_detail() -> None:
    """A card that was opened before must not be asked for again.

    The panel repaints every five seconds, and a card that was opened
    is re-opened by the repaint.  Without a cache the operator's card
    would ask for its detail on every repaint — one request per five
    seconds for a plan whose answer has not changed.

    The cache is keyed by the plan's id, and it is consulted **before**
    the request: a card whose detail is already known must never reach
    the request at all.
    """
    source = _source(_APP_JS)
    toggle = _function_body(source, _TOGGLE_FUNCTION)

    request = _DETAIL_PATH.search(toggle)
    assert request is not None, (
        f"{_TOGGLE_FUNCTION}() never asks for the plan's detail."
    )

    assert _DETAIL_CACHE in toggle, (
        f"{_TOGGLE_FUNCTION}() keeps no cache of the details it has "
        "already fetched. Opening a card twice is two requests for one "
        "answer."
    )

    consulted = [
        match.start()
        for match in re.finditer(re.escape(_DETAIL_CACHE), toggle)
        if match.start() < request.start()
    ]
    assert consulted, (
        f"{_TOGGLE_FUNCTION}() asks for the detail before it looks at the "
        "cache. The cache has to be consulted first, or it is a cache "
        "nobody reads."
    )


def test_detail_failure_stays_in_the_card() -> None:
    """A detail that cannot be read must be reported in the card.

    The failure belongs to the plan whose detail failed, and it is said
    in that card: a list that went blank because one plan could not be
    read is a worse answer than a card that says what went wrong — and
    a card whose detail was cleared is a card that says nothing at all.
    """
    source = _source(_APP_JS)
    toggle = _function_body(source, _TOGGLE_FUNCTION)

    assert "catch" in toggle, (
        f"{_TOGGLE_FUNCTION}() does not catch a failed detail request. An "
        "uncaught failure empties the card and leaves the operator with "
        "a card that says nothing."
    )

    assert "innerHTML" in toggle, (
        f"{_TOGGLE_FUNCTION}() never writes into the card. A failed "
        "request has to leave a word in the card it failed for."
    )

    assert 'innerHTML = ""' not in toggle, (
        f"{_TOGGLE_FUNCTION}() empties the card when the detail cannot be "
        "read. The other plans in the list are fine, and they must stay "
        "on screen."
    )


def test_detail_request_goes_through_the_api_wrapper() -> None:
    """The detail request must go through ``api()``.

    Every request the page makes carries the guard header
    ``X-PDT-Request: 1``, and exactly two functions attach it.  A bare
    ``fetch(`` in the toggle would answer 403 at runtime — the request
    would go out and come back refused, which reads on screen as a
    plan whose detail cannot be read.
    """
    source = _source(_APP_JS)
    toggle = _function_body(source, _TOGGLE_FUNCTION)

    assert "api(" in toggle, (
        f"{_TOGGLE_FUNCTION}() does not ask through the api() wrapper, so "
        "the request carries no guard header and is refused."
    )

    assert "fetch(" not in toggle, (
        f"{_TOGGLE_FUNCTION}() calls fetch() directly. Every request must "
        "go through api(), which attaches the guard header."
    )


def test_the_card_takes_the_row_and_nothing_else() -> None:
    """The card must be drawn from the row alone.

    The card's signature is part of the contract: ``schedulerCard(row)``
    is what the renderers call.  A second parameter — the action
    buttons, say — puts the card and its callers on a shape that is not
    the row's, and the buttons then have to be threaded through every
    call that draws a card.

    The buttons belong to the row: the renderer puts them on it, and the
    card draws what the row carries.
    """
    source = _source(_APP_JS)

    declaration = re.search(
        r"^function\s+" + re.escape(_CARD_FUNCTION) + r"\s*\(([^)]*)\)",
        source,
        re.MULTILINE,
    )
    assert declaration is not None, (
        f"function {_CARD_FUNCTION}(...) not found in the source"
    )

    arguments = [
        argument.strip()
        for argument in declaration.group(1).split(",")
        if argument.strip()
    ]

    assert len(arguments) == 1, (
        f"{_CARD_FUNCTION}() takes {len(arguments)} arguments "
        f"({arguments!r}); it must take the row and nothing else."
    )


def test_the_actions_travel_on_the_row() -> None:
    """The action buttons must travel on the row the card is drawn from.

    The card takes the row and nothing else, so the buttons have to be
    part of the row.  A renderer that drew the buttons itself and
    handed them over separately would be the two-argument shape this
    gate forbids.
    """
    source = _source(_APP_JS)

    for renderer in (_RUNNING_RENDERER, _QUEUE_RENDERER):
        body = _function_body(source, renderer)
        assert "actions_html" in body, (
            f"{renderer}() does not put the action buttons on the row it "
            "draws the card from. The card takes the row and nothing "
            "else, so the buttons have to travel on it."
        )
        assert "schedulerActionButtons(" in body, (
            f"{renderer}() draws no action buttons. A plan the operator "
            "cannot act on is a plan the panel has drawn for nothing."
        )


def test_the_detail_cache_expires() -> None:
    """A card's detail must not be kept for ever.

    The panel polls its read model every five seconds, and the point of
    an open card is to show what the plan is doing **now**.  A cache
    that never expires shows the operator the answer they saw when they
    opened the card, for as long as the page is open — which is the
    opposite of what an open card is for.

    So the cache records when the detail was fetched, and the panel
    asks again once the answer is older than the poll interval.
    """
    source = _source(_APP_JS)

    assert _TTL_CONSTANT in source, (
        f"the card's detail cache has no lifetime ({_TTL_CONSTANT} is "
        "nowhere in the source). A cache that never expires shows the "
        "operator the answer they saw when they opened the card, for as "
        "long as the page is open."
    )

    toggle = _function_body(source, _TOGGLE_FUNCTION)
    assert re.search(r"\b" + _FETCHED_AT + r"\s*:", toggle), (
        f"{_TOGGLE_FUNCTION}() does not record when the detail was "
        "fetched, so nothing can tell a fresh answer from a stale one."
    )

    stale = _function_body(source, _STALE_FUNCTION)
    assert _TTL_CONSTANT in stale, (
        f"{_STALE_FUNCTION}() does not compare the age of the cached "
        "detail against the lifetime it is given."
    )
    assert "Date.now()" in stale, (
        f"{_STALE_FUNCTION}() never reads the clock, so it cannot tell "
        "an old answer from a fresh one."
    )

    restore = _function_body(source, _RESTORE_FUNCTION)
    assert _STALE_FUNCTION in restore, (
        f"{_RESTORE_FUNCTION}() puts the open cards back without asking "
        "whether what they show has gone stale, so an open card never "
        "catches up with the plan it is about."
    )
    assert _TOGGLE_FUNCTION in restore, (
        f"{_RESTORE_FUNCTION}() never asks again. A card that was opened "
        "shows what it showed when it was opened, for ever."
    )


def test_the_answer_goes_to_the_card_that_is_on_the_page() -> None:
    """The detail that comes back must go to the card that is on the page.

    The panel redraws its cards every five seconds, and a redraw
    replaces the card the request was made from.  An answer written
    into the card that was there when the request went out lands in a
    node that is no longer on the page: the card the operator is
    looking at stays empty, and nothing says why.

    So the card is looked up **again** after the request, and the answer
    is written into the card that is there now.
    """
    source = _source(_APP_JS)
    toggle = _function_body(source, _TOGGLE_FUNCTION)

    request = _DETAIL_PATH.search(toggle)
    assert request is not None, (
        f"{_TOGGLE_FUNCTION}() never asks for the plan's detail."
    )

    lookups = [
        match.start()
        for match in re.finditer(re.escape(_CARD_ELEMENT_FUNCTION) + r"\(", toggle)
    ]
    assert lookups, (
        f"{_TOGGLE_FUNCTION}() never looks the card up, so it has nowhere "
        "to put the answer."
    )

    assert any(position > request.start() for position in lookups), (
        f"{_TOGGLE_FUNCTION}() looks the card up only before the request. "
        "The panel redraws its cards every five seconds, and the card the "
        "request was made from may be gone by the time the answer "
        "arrives — the answer has to go to the card that is on the page "
        "now."
    )


def test_every_class_the_card_uses_has_a_rule() -> None:
    """Every class the card's markup uses must be one the stylesheet knows.

    A class name nothing styles is a name that says nothing: the
    element it is on is drawn exactly as the browser would draw it,
    and a style put on it later has to start from scratch.  An inline
    style is the same thing wearing a disguise — and one a stylesheet
    cannot override.

    So the card may only use classes ``frontend/style.css`` has a rule
    for — and that goes for the whole of the card's markup, the header
    and the detail alike: the detail's lines and the word a failed
    request leaves behind are drawn by the card too.
    """
    source = _source(_APP_JS)
    css = _source(_STYLE_CSS)

    classes = {}
    for function in (
        _CARD_FUNCTION,
        _DETAIL_HTML_FUNCTION,
        _ERROR_HTML_FUNCTION,
    ):
        body = _function_body(source, function)
        for match in re.finditer(r"class=\"([^\"]*)\"", body):
            for name in match.group(1).split():
                classes.setdefault(name, function)

    assert classes, (
        f"{_CARD_FUNCTION}() names no class at all, so the card is drawn "
        "as the browser draws a plain box."
    )

    for name in sorted(classes):
        assert re.search(r"\." + re.escape(name) + r"\b", css), (
            "{0}() uses the class {1!r}, and frontend/style.css has no "
            "rule for it. A class nothing styles is a name that says "
            "nothing.".format(classes[name], name)
        )


def test_each_renderer_reads_the_fields_it_draws() -> None:
    """Each side must read the fields it draws, and the card must not.

    The card is a pure renderer: it draws what the row carries.  The
    row is put together by the renderer that owns the side — the
    running side reads the plan's own clock and the backend's eviction
    flag, the queue side reads the promotion — and the card is drawn
    from two different rows, one of which has neither.

    So the fields live in the renderer that owns them, and the card
    draws the facts it is handed.  A field that moved into the card
    would make the card depend on a row shape it is only half of.
    """
    source = _source(_APP_JS)
    running = _function_body(source, _RUNNING_RENDERER)
    queue = _function_body(source, _QUEUE_RENDERER)
    card = _function_body(source, _CARD_FUNCTION)

    for field in _RUNNING_FIELDS:
        assert field in running, (
            f"{_RUNNING_RENDERER}() does not read {field!r}, so the running "
            "card cannot show what the backend said about the plan."
        )

    fallback = re.search(r"running_sec\s*\?\?\s*([^;\n]+)", running)
    assert fallback is not None, (
        f"{_RUNNING_RENDERER}() does not fall back from the plan's own "
        "clock to the slot clock with `??`. The plan clock of `0` is a "
        "real answer, and `||` would throw it away."
    )
    assert "elapsed_sec" in fallback.group(1), (
        f"{_RUNNING_RENDERER}() falls back to something other than the slot "
        "clock, so a listing that carries no plan clock has nothing to "
        "show."
    )

    assert "item.evictable" in running, (
        f"{_RUNNING_RENDERER}() does not read the backend's eviction flag. "
        "The rule is the backend's — it is measured against the slot "
        "clock — and the panel does not recompute it."
    )

    for field in _QUEUE_FIELDS:
        assert field in queue, (
            f"{_QUEUE_RENDERER}() does not read {field!r}, so the queue card "
            "cannot show the queue's own number."
        )

    assert '"—"' in queue, (
        f"{_QUEUE_RENDERER}() no longer falls back to 「—」 for a plan "
        "without a promotion. The dash says \"there is nothing here\"; an "
        "empty value says \"not filled in yet\"."
    )

    for field in _RUNNING_FIELDS + _QUEUE_FIELDS:
        assert field not in card, (
            f"{_CARD_FUNCTION}() reads {field!r} itself. The card draws "
            "what the row carries; the fields belong to the renderer that "
            "owns the side."
        )


def test_the_plan_id_reaches_the_card_and_the_buttons() -> None:
    """The queue must name the plan it acts on.

    One id, three jobs: read off the queue item, shown on the card,
    handed to the buttons that promote and start it.  Showing one id
    and acting on another names a plan the operator cannot act on, and
    reading an id no cell renders names no plan at all.
    """
    source = _source(_APP_JS)
    queue = _function_body(source, _QUEUE_RENDERER)
    card = _function_body(source, _CARD_FUNCTION)

    assert "item.plan_id" in queue, (
        f"{_QUEUE_RENDERER}() never reads the queue item's plan id, so the "
        "queue card has no plan to name."
    )

    binding = re.search(
        r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[^;]*?item\.plan_id",
        queue,
    )
    assert binding is not None, (
        f"{_QUEUE_RENDERER}() reads the plan id but binds it to no name, so "
        "the id never reaches the card or the buttons."
    )
    variable = binding.group(1)

    assert re.search(
        r"schedulerActionButtons\s*\(\s*" + re.escape(variable) + r"\b", queue
    ), (
        f"{_QUEUE_RENDERER}() does not hand the plan id it reads to the "
        "buttons, so the buttons act on something else."
    )

    for action in ("promote", "start"):
        assert re.search(r"[\"']" + action + r"[\"']", queue), (
            f"{_QUEUE_RENDERER}() does not offer the {action!r} action on "
            "the plan id it shows."
        )

    assert "item.plan_id" in card, (
        f"{_CARD_FUNCTION}() does not read the plan id, so the card names "
        "no plan."
    )

    shown = re.search(
        r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[^;]*?item\.plan_id",
        card,
    )
    assert shown is not None, (
        f"{_CARD_FUNCTION}() reads the plan id but binds it to no name."
    )
    assert re.search(
        r"escapeHtml\(\s*" + re.escape(shown.group(1)) + r"\b", card
    ), (
        f"{_CARD_FUNCTION}() reads the plan id but shows it without "
        "escaping it, so a plan whose id carries a quote breaks the card's "
        "markup."
    )


def test_a_fact_is_written_with_the_name_it_is_known_by() -> None:
    """A fact must carry the name it is known by.

    The card's facts are read **by name** — a reader asks for
    ``running_sec`` and is answered with the duration, not with a
    paragraph to take apart — so a fact is written with the name it
    goes by and the value it carries.
    """
    source = _source(_APP_JS)
    card = _function_body(source, _CARD_FUNCTION)

    assert "data-fact=" in card, (
        f"{_CARD_FUNCTION}() writes no fact names, so a fact cannot be "
        "asked for by name."
    )
    assert "data-value=" in card, (
        f"{_CARD_FUNCTION}() writes no fact values, so a reader has to "
        "take the value out of the text."
    )
