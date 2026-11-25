"""The waiting panel must show the plans that are stopping.

Why this gate exists
--------------------
The scheduler's read model carries a bucket beside "queued" and
"running": ``stopping``.  It holds the plans for which a stop command
has been **accepted but not yet carried out** — the plan is not
running any more, it is not waiting for anybody either, and it is not
in the queue.  An operator who cannot see that gap reads the panel as
"the plan vanished" (arch decision R3: a plan must never be in neither
bucket).

The waiting panel is where the transition has to be visible, because
that is the one list an operator watches while a plan is on its way
out: the row says 停止中, so the plan is not gone, it is on its way.

Nothing here fails loudly.  A missing row is a row that is simply not
drawn; the panel keeps working, the operator just loses the plan in
the middle of its stop.

The rule
--------
* ``renderSchedulerWaiting`` must read the ``stopping`` list out of the
  snapshot and draw one row per entry.  Each row carries
  ``data-stopping-plan`` so that a test — and the operator's eye — can
  find it again.
* The row's second cell says 停止中, the third carries the action that
  was asked for, the fourth the age in whole seconds.
* The panel must clear the previous paint **before** it draws the new
  one: the poll runs every five seconds, and a row that outlived its
  paint would stack under itself.
* The waiting block must carry a hint that says 停止中, hidden when
  nothing is stopping.

Scan target
-----------
``frontend/app.js`` and ``frontend/index.html``, read as source.  These
are shape gates, like the rest of ``static_gates/``: they run on a
plain Python environment with no JS toolchain, so they pin the contract
in the source rather than executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"


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

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.

    Brace counting is naive — it does not know about strings or
    comments — which is adequate for the handful of short, uniformly
    indented functions this file inspects.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, f"function {name}() not found in the source"
    body_start = match.end() - 1  # index of the opening '{'
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1 : index]
    raise AssertionError(f"unbalanced braces in function {name}()")


#: A statement that empties the panel — either by assigning the empty
#: string to ``innerHTML`` or by removing the rows one by one.  Both are
#: ways of saying "the previous paint is gone"; what the gate cares
#: about is that one of them happens.
_CLEAR = re.compile(r"""\.innerHTML\s*=\s*(?:""|'')|\.remove\(\)|removeChild""")

#: A statement that puts rows into the panel.  ``innerHTML = "..."`` is
#: deliberately *not* in this list: an assignment replaces the panel's
#: content, so it cannot be stacked on top of a previous paint.
_APPEND = re.compile(r"insertAdjacentHTML|appendChild|insertBefore|\.append\(")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_app_renders_stopping_rows() -> None:
    """The panel must draw a row per stopping plan, and say 停止中 on it.

    The row is what makes the transition visible, so the three things it
    carries are checked separately: the plan it is about (so a test can
    find it again), the word that says what is happening to it, and the
    two numbers that say what was asked for and how long ago.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "renderSchedulerWaiting")

    assert "data-stopping-plan" in source, (
        "no row carries `data-stopping-plan`, so the plans that are "
        "stopping are not drawn at all. A plan that is stopping is in "
        "neither the queue nor the running list, so without this row "
        "it simply disappears from the panel."
    )

    assert "停止中" in source, (
        "the stopping row never says 停止中. The operator cannot tell "
        "a plan that is on its way out from a plan that is gone."
    )

    assert re.search(r"snapshot\s*\.\s*stopping", body), (
        "renderSchedulerWaiting() does not read the `stopping` list out "
        "of the snapshot, so the rows it draws cannot come from it."
    )

    assert re.search(r"\.action\b", body), (
        "the stopping row does not carry the action that was asked for "
        "(pause, stop, ...), so the operator cannot tell what the "
        "command was."
    )

    assert "age_seconds" in body, (
        "the stopping row does not carry `age_seconds`, so the operator "
        "cannot see how long the plan has been on its way out."
    )

    assert "<td>停止中</td>" in body, (
        "停止中 is not the row's own second cell. The word has to be on "
        "the row itself — a hint somewhere else does not travel with "
        "the plan."
    )


def test_html_has_stopping_hint() -> None:
    """The waiting block must carry a 停止中 hint, hidden by default.

    The hint is what tells the operator that the block below it is
    showing something other than the waits.  It starts hidden: the
    panel shows it only while a plan is actually stopping.
    """
    html = _source(_INDEX_HTML)

    assert 'id="scheduler-stopping-hint"' in html, (
        "the waiting block has no #scheduler-stopping-hint, so nothing "
        "tells the operator that the block also shows the plans that "
        "are stopping."
    )

    match = re.search(
        r"<span[^>]*id=\"scheduler-stopping-hint\"[^>]*>(.*?)</span>", html, re.DOTALL
    )
    assert match is not None, (
        "#scheduler-stopping-hint is not a <span>: the hint cannot be "
        "read as one."
    )

    assert "停止中" in match.group(1), (
        "the hint does not say 停止中, so it names nothing."
    )

    assert "hidden" in match.group(0).split(">", 1)[0], (
        "the hint does not start hidden. The panel shows it while a "
        "plan is stopping; until then it must not be on the page."
    )


def test_stopping_rows_cleared_before_render() -> None:
    """The panel must be cleared before the new rows go in.

    The poll runs every five seconds and the panel is repainted from
    scratch every time.  A row that survived its paint would stack
    under itself: the operator would see the same plan twice, then
    three times, and the age beside it would be the age of the last
    paint rather than of the plan.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerWaiting")

    assert "data-stopping-plan" in body, (
        "renderSchedulerWaiting() does not draw the stopping rows "
        "itself, so there is nothing here whose clearing could be "
        "checked."
    )

    clear = _CLEAR.search(body)
    append = _APPEND.search(body)

    assert clear is not None, (
        "renderSchedulerWaiting() never empties the panel. The rows of "
        "the last paint would survive into this one and stack under "
        "themselves."
    )

    assert append is not None, (
        "renderSchedulerWaiting() never puts rows into the panel."
    )

    assert clear.start() < append.start(), (
        "the panel is filled before it is emptied: the rows of this "
        "paint are drawn first and the clear then takes them away "
        "again, so the panel ends up empty."
    )
