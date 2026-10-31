"""The running column must show how long a plan has really been running.

Why this gate exists
--------------------
The running table has two clocks to choose from, and they answer
different questions:

* ``elapsed_sec`` is the **slot** clock — the time since the slot was
  acquired.  It is measured against the tick, so it restarts at zero
  whenever the scheduler restarts, and it is the number the eviction
  rule is applied to.
* ``running_sec`` is the **plan** clock — the time since the plan
  itself started.  It is what an operator means by 「已运行」.

The panel used to show the slot clock under a header that said
「已运行(秒)」 — a heading that promised a duration and delivered a
bare number of seconds measured from the wrong origin.  After a
scheduler restart a plan that had been running for an hour showed
``0``, and the header still said 「已运行」.  The page was not broken;
it was wrong about the thing it named.

The rule
--------
* ``frontend/app.js``: the running row reads ``running_sec`` and falls
  back to ``elapsed_sec`` when it is missing.  The fallback must be a
  nullish coalescing (``??``), not ``||`` — a plan that started a
  moment ago has ``running_sec`` of ``0``, and ``||`` would throw that
  number away and show the slot clock instead.
* The duration is written by a **named** function
  (``schedulerDurationText``) so the format lives in one place and can
  be pinned here.  It must carry the three units — hours, minutes and
  seconds — and the two thresholds that separate them, and it must
  draw a dash for a missing number.
* ``frontend/index.html``: the column is headed 「已运行」.  The old
  「已运行(秒)」 wording is gone: the column no longer shows seconds,
  it shows a duration.

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

#: The units the duration is written in, from the coarsest to the
#: finest.  All three must appear in the formatter: a duration that
#: never names its unit is a number wearing a duration's label.
_UNITS = ("h", "m", "s")

#: The seconds in an hour and in a minute — the two thresholds that
#: decide which pair of units the formatter writes.
_HOUR_SECONDS = "3600"
_MINUTE_SECONDS = "60"

#: The dash a missing duration is drawn as.
_MISSING = "—"

#: The old header wording, in either parenthesis style.
_OLD_HEADER_RE = re.compile(r"已运行\s*[（(]\s*秒\s*[）)]")


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


def test_running_uses_running_sec() -> None:
    """The running row must show the plan clock, and fall back to the slot clock.

    ``running_sec`` is the time since the plan started — the number the
    「已运行」 column promises.  ``elapsed_sec`` is the time since the
    slot was acquired, which restarts at zero with the scheduler; it is
    the right answer only when the plan clock is missing.

    The fallback must be ``??`` and not ``||``: ``running_sec`` of ``0``
    is a real answer (the plan started a moment ago), and ``||`` would
    discard it in favour of the slot clock.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerRunning")

    assert "running_sec" in body, (
        "the running row never reads `running_sec`, so the 「已运行」 "
        "column shows the slot clock — a number that restarts at zero "
        "when the scheduler restarts."
    )

    assert "elapsed_sec" in body, (
        "the running row never reads `elapsed_sec`, so a listing that "
        "carries no `running_sec` has nothing to fall back to and the "
        "column goes blank."
    )

    match = re.search(r"running_sec\s*\?\?\s*(?P<fallback>[^;\n]+)", body)
    assert match is not None, (
        "the running row does not fall back from `running_sec` to "
        "`elapsed_sec` with `??`. Without the coalescing operator the "
        "column cannot show the plan clock and still survive a listing "
        "that carries only the slot clock."
    )

    assert "elapsed_sec" in match.group("fallback"), (
        "the fallback of `running_sec` does not name `elapsed_sec`; the "
        "row would read the plan clock and then fall back to nothing."
    )


def test_duration_formatter_is_named() -> None:
    """The duration must be written by a named ``schedulerDurationText``.

    The format has to live in one place: an operator reading 「1h 23m」
    and an operator reading 「1h 23m 0s」 are two different panels.  A
    named function is what lets the rest of the system point at the
    format instead of copying it, so the name is part of the contract.

    The function must carry all three units and the two thresholds that
    separate them, and it must draw a dash for a number it was not
    given — a plan whose start time is unknown has not run for zero
    seconds, it has run for an unknown length.
    """
    source = _source(_APP_JS)

    assert re.search(r"function\s+schedulerDurationText\s*\(", source), (
        "frontend/app.js has no `schedulerDurationText()` function. The "
        "duration format must live in a named function, not be spelled "
        "out at the call site."
    )

    body = _function_body(source, "schedulerDurationText")

    for unit in _UNITS:
        assert re.search(r"[\"'`}]\s*" + unit + r"\b", body), (
            f"schedulerDurationText() never writes the {unit!r} unit. A "
            "duration is written in hours, minutes and seconds, and the "
            "format that decides between them must name all three."
        )

    assert _HOUR_SECONDS in body, (
        "schedulerDurationText() does not mention 3600, so it cannot "
        "tell an hour from a minute."
    )

    assert re.search(r"\b" + _MINUTE_SECONDS + r"\b", body), (
        "schedulerDurationText() does not mention 60, so it cannot tell "
        "a minute from a second."
    )

    assert _MISSING in body, (
        f"schedulerDurationText() never draws {_MISSING!r}, so a missing "
        "duration has nowhere to go but a zero — and a plan that has not "
        "started is not a plan that has run for zero seconds."
    )


def test_header_no_longer_says_seconds() -> None:
    """The column must be headed 「已运行」, not 「已运行(秒)」.

    The column shows a duration now — 「1h 23m」 — so a header that
    promises seconds is wrong twice over: it names a unit the cell does
    not use, and it promises the operator a number they cannot read at a
    glance.
    """
    html = _source(_INDEX_HTML)

    assert re.search(r"<th[^>]*>\s*已运行\s*</th>", html), (
        "the running table has no 「已运行」 column header; the column "
        "the operator reads is not labelled."
    )

    assert _OLD_HEADER_RE.search(html) is None, (
        "the running table still heads its column 「已运行(秒)」. The "
        "column shows a duration now, not a number of seconds."
    )


def test_evictable_column_still_reads_evictable() -> None:
    """The 「可驱逐」 column must keep reading ``item.evictable``.

    The eviction rule is applied to the **slot** clock, not to the plan
    clock: a plan is protected for a minimum runtime measured from the
    moment its slot was acquired.  Showing the plan clock next to it
    does not change that — so the flag beside it must still be the
    flag the backend decided, and must not be recomputed from the
    duration the panel happens to draw.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerRunning")

    assert re.search(r"item\.evictable", body), (
        "the 「可驱逐」 column no longer reads `item.evictable`. The "
        "eviction rule is the backend's — measured against the slot "
        "clock — and the panel must not recompute it from the duration "
        "it draws."
    )
