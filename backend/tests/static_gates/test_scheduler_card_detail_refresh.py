"""An open card's detail must be refreshed in the background, and the
card must never go blank while it waits for the new answer.

Why this gate exists
--------------------
The scheduler panel polls its read model every five seconds, and a
card's cached detail was given the same five seconds to live.  When the
two are the same number, the cache expires on every poll — and the
old code answered an expiry by **throwing the cached detail away** and
re-opening the card from scratch.  That did two visible things at once,
both of them wrong:

* the card was **folded and opened again** (the repaint folded it, and
  the re-open put it back), so an open card twitched every five
  seconds;
* the card went **blank** in the meantime, because the answer that was
  thrown away was the only thing it had to show, and the new answer
  had not arrived yet.

So the expiry must not be a *reset*.  It must be a **refresh**: the old
answer stays on the screen, the new one is asked for in the
background, and the card is written in place when the new answer
arrives.  Four promises follow from that, and each is pinned here:

* a **refresh asks only for a card that is open** — a folded card is
  fetched when the operator opens it, not behind their back;
* a **refresh never blanks the card** — the old answer is the card's
  answer until the new one replaces it;
* a **stale cache is refreshed, not deleted** — deleting it is what
  made the card go blank in the first place;
* the repaint **does not toggle** the card — the fold is the
  operator's, and the panel has no business folding a card the
  operator opened.

And one more: a **failed refresh keeps what the card already had**.  A
refresh that fails is a refresh that learned nothing; the card that
was showing the plan's progress must go on showing it.  Only a card
with nothing to show says what went wrong.

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

#: The function that refreshes one card's detail in the background.
_REFRESH_FUNCTION = "schedulerRefreshCardDetail"

#: The function that puts the cards the operator has opened back the
#: way they left them.
_RESTORE_FUNCTION = "schedulerRestoreOpenCards"

#: The function that opens a card, or folds it again.  The operator's.
_TOGGLE_FUNCTION = "toggleSchedulerCard"

#: The function that finds the card of one plan on the page.
_CARD_ELEMENT_FUNCTION = "schedulerCardElement"

#: The function that decides whether a card's detail has gone stale.
_STALE_FUNCTION = "schedulerCardDetailStale"

#: The detail endpoint.  The path is matched loosely because the plan
#: id is interpolated into it.
_DETAIL_PATH = re.compile(r"/scheduler/plan/[^\s`'\"]*?/detail")

#: The element a card's detail is written into.
_DETAIL_SELECTOR = ".scheduler-plan-detail"

#: The cache the opened cards' details are kept in.
_DETAIL_CACHE = "_schedulerCardDetails"

#: The plans whose detail is on its way.
_PENDING = "_schedulerCardPending"


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


def _catch_branch(body: str) -> str:
    """Return the part of a function body that runs when it fails.

    The branch starts at the first ``catch`` and runs to the end of the
    body — which is where the ``finally`` that follows it begins.
    """
    index = body.find("catch")
    assert index != -1, "the function has no catch branch"
    return body[index:]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_refresh_only_for_expanded_card() -> None:
    """A refresh must ask only for a card the operator has open.

    A folded card is worth exactly what the panel already knows, and
    the panel already has that: the plan's progress is asked for when
    the operator opens the card, and only then.  A refresh that asked
    for folded cards would spend one request per plan per poll on
    details nobody is looking at — the very cost the fold exists to
    avoid.

    So the refresh looks the card up, asks whether it is open, and only
    then asks for the detail.
    """
    source = _source(_APP_JS)
    refresh = _function_body(source, _REFRESH_FUNCTION)

    request = _DETAIL_PATH.search(refresh)
    assert request is not None, (
        f"{_REFRESH_FUNCTION}() never asks for the plan's detail, so a "
        "card whose answer has grown old never catches up with the plan "
        "it is about."
    )

    opened = re.search(r"""dataset\.expanded\s*!==\s*["']true["']""", refresh)
    assert opened is not None, (
        f"{_REFRESH_FUNCTION}() does not ask whether the card is open. A "
        "folded card must not be fetched behind the operator's back — the "
        "fold is what keeps a folded card free."
    )

    assert opened.start() < request.start(), (
        f"{_REFRESH_FUNCTION}() asks for the detail before it checks "
        "whether the card is open. The check has to come first, or the "
        "check is a comment."
    )

    assert _CARD_ELEMENT_FUNCTION + "(" in refresh, (
        f"{_REFRESH_FUNCTION}() never looks the card up, so it cannot "
        "know whether the card is open."
    )


def test_refresh_never_blanks_detail() -> None:
    """A refresh must never leave the card empty.

    The card's answer is worth reading until the new one is here.  A
    refresh that emptied the card first — and put the new answer in
    when it arrived — would show the operator a blank card for the
    length of a request, every five seconds, for as long as the card
    was open.  That is the same blank window the old code had, only
    moved.

    So the refresh writes the new answer **into** the card it already
    has, and it never writes an empty string.  And when the new answer
    says exactly what the card already says, the card is left alone:
    the panel updates what changed, not everything.
    """
    source = _source(_APP_JS)
    refresh = _function_body(source, _REFRESH_FUNCTION)

    assert 'innerHTML = ""' not in refresh, (
        f"{_REFRESH_FUNCTION}() empties the card. The answer the card "
        "already has stays on the screen until the new one replaces it — "
        "an open card must not go blank while it waits."
    )

    assert _DETAIL_SELECTOR in refresh, (
        f"{_REFRESH_FUNCTION}() does not write into the card's detail. A "
        "refresh that writes nowhere leaves the card showing the past "
        "for ever."
    )

    assert "innerHTML =" in refresh, (
        f"{_REFRESH_FUNCTION}() never writes the new answer into the "
        "card. The refresh has to put the answer where the operator is "
        "looking."
    )

    # The answer is compared with what the card already shows, and the
    # card is only written when they differ.
    assert re.search(r"innerHTML\s*===|innerHTML\s*!==", refresh), (
        f"{_REFRESH_FUNCTION}() writes the answer over the card without "
        "looking at what the card already says. A refresh that changes "
        "nothing must change nothing on the screen."
    )


def test_stale_cache_is_refreshed_not_deleted() -> None:
    """A detail that has grown old must be refreshed, not thrown away.

    The repaint knows that the cached answer is old.  Two answers are
    open to it, and only one of them is right: throw the answer away
    and fetch a new one (which is what the old code did — the card
    went blank until the new answer arrived), or **keep** the answer
    and fetch a new one to replace it.

    So the cache is never dropped on an expiry, and the repaint asks
    the refresh for a new answer.
    """
    source = _source(_APP_JS)
    restore = _function_body(source, _RESTORE_FUNCTION)

    assert f"{_DETAIL_CACHE}.delete" not in restore, (
        f"{_RESTORE_FUNCTION}() throws the cached detail away when it has "
        "grown old. Throwing it away is what made the card go blank: the "
        "old answer must stay until the new one is here."
    )

    assert _REFRESH_FUNCTION + "(" in restore, (
        f"{_RESTORE_FUNCTION}() never refreshes a card whose detail has "
        "grown old. A card that was opened shows what it showed when it "
        "was opened, for ever."
    )

    assert _STALE_FUNCTION + "(" in restore, (
        f"{_RESTORE_FUNCTION}() refreshes without asking whether what the "
        "card shows has gone stale. A card that is fresh does not need a "
        "request."
    )

    stale = restore.index(_STALE_FUNCTION + "(")
    refresh = restore.index(_REFRESH_FUNCTION + "(")
    assert stale < refresh, (
        f"{_RESTORE_FUNCTION}() refreshes before it asks whether what the "
        "card shows has gone stale. Every repaint would then spend a "
        "request, whether the answer had changed or not."
    )


def test_restore_does_not_toggle() -> None:
    """The repaint must not fold a card and open it again.

    The fold is the **operator's**: they opened the card, and nothing
    else moves it.  A repaint that re-opened an open card through the
    toggle would fold it first — the toggle is what folds a card — and
    the operator would watch their card close and open again every
    five seconds.

    So the repaint puts the open cards back the way they were: it sets
    the fold state to "open", and it never calls the toggle.
    """
    source = _source(_APP_JS)
    restore = _function_body(source, _RESTORE_FUNCTION)

    assert _TOGGLE_FUNCTION + "(" not in restore, (
        f"{_RESTORE_FUNCTION}() calls {_TOGGLE_FUNCTION}(). The toggle is "
        "the operator's: it folds and opens the card. The repaint must "
        "not fold a card and open it again — that is the twitch the "
        "operator sees every five seconds."
    )

    assert 'dataset.expanded = "false"' not in restore, (
        f"{_RESTORE_FUNCTION}() folds a card. A card the operator opened "
        "stays open."
    )

    assert 'dataset.expanded = "true"' in restore, (
        f"{_RESTORE_FUNCTION}() does not put the open cards back. A "
        "repaint would close them under the operator's eyes."
    )


def test_failure_keeps_cached_content() -> None:
    """A failed refresh must keep what the card already shows.

    A refresh that fails learned nothing new about the plan.  The card
    that was showing the plan's progress must go on showing it — the
    failure is worth saying only where there is nothing else to say.

    So the error is written into the card only when the card has no
    answer of its own, and the cached answer is never dropped because a
    refresh failed.
    """
    source = _source(_APP_JS)
    refresh = _function_body(source, _REFRESH_FUNCTION)

    assert "catch" in refresh, (
        f"{_REFRESH_FUNCTION}() does not catch a failed request. An "
        "uncaught failure escapes the poll and the card keeps showing an "
        "answer that is no longer being kept fresh."
    )

    branch = _catch_branch(refresh)

    assert f"{_DETAIL_CACHE}.delete" not in branch, (
        f"{_REFRESH_FUNCTION}() drops the cached answer when the refresh "
        "fails. A failed refresh is a refresh that learned nothing; the "
        "answer the card has is still the best answer there is."
    )

    guard = f"{_DETAIL_CACHE}.has"
    assert guard in branch, (
        f"{_REFRESH_FUNCTION}() does not look at the cache when the "
        "refresh fails. A card that still holds an answer must keep it."
    )

    assert branch.index(guard) < branch.index("schedulerCardErrorHtml("), (
        f"{_REFRESH_FUNCTION}() writes the failure into the card before "
        "it has looked at the cache. A card that still holds an answer "
        "must keep it — the failure belongs in a card with nothing to "
        "show."
    )


def test_identical_answer_is_not_written_back() -> None:
    """A refresh whose answer says the same thing must change nothing.

    The panel repaints every five seconds, and a plan that has not
    moved answers with the same words every time.  Writing those same
    words over the card anyway would tear down and rebuild the very
    element the operator may be reading — for no change at all.

    So the new answer is compared with what the card already shows, and
    the card is left alone when they are the same.
    """
    source = _source(_APP_JS)
    refresh = _function_body(source, _REFRESH_FUNCTION)

    writes = re.findall(r"\.innerHTML\s*=", refresh)
    assert writes, (
        f"{_REFRESH_FUNCTION}() never writes the new answer into the "
        "card. The refresh has to put the answer where the operator is "
        "looking."
    )

    assert re.search(r"innerHTML\s*===", refresh), (
        f"{_REFRESH_FUNCTION}() writes the answer over the card without "
        "comparing it with what the card already shows."
    )

    assert _PENDING in refresh, (
        f"{_REFRESH_FUNCTION}() does not know which requests are already "
        "on their way. A card that is being fetched must not be fetched "
        "again."
    )
