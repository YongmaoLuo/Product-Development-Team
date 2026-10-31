"""The scheduler panel must show how many agents are running in parallel.

Why this gate exists
--------------------
The capacity widget answers 「已用 6 共 6 队列 7」 — that is the
**scheduler's** view: how many plans hold a slot, how many slots there
are, how many plans are queued.  It says nothing about the workflow
itself.

``GET /api/system/active`` is the workflow's own answer: how many
execution subprocesses are running (``execution_running``), how many
verification cycles are running (``verification_running``) and how many
agents are in flight altogether (``total_active``).  The two numbers are
different things — six plans occupying six slots can be driving eight
agents, because one plan may be executing *and* verifying at the same
time — and an operator watching a busy panel cannot tell that from the
capacity widget alone.

The rule
--------
* ``frontend/index.html``: the capacity row carries a 「并行 Agent」
  label and a ``#scheduler-view-agent-count`` element that starts as a
  dash.
* ``frontend/app.js``: a named ``loadSchedulerAgentCount()`` reads
  ``/api/system/active`` through the ``api()`` wrapper and writes
  ``total_active`` into that element.
* The read is **isolated**: a failed request paints a dash and stops.
  It must not call ``renderSchedulerError()`` — that function paints an
  error banner over the whole panel, and a missing agent count is not a
  broken panel.  The panel's own refresh (``loadSchedulerState()``)
  rides along with the read; it must not ``await`` it and it must not
  own a timer of its own.

Why the shape, and not the behaviour
------------------------------------
These are shape gates, like the rest of ``static_gates/``: they run on
a plain Python environment with no JS toolchain, so they pin the
contract in the source rather than executing the page.  The rules are
therefore written against the *shape* of the code — a named function, a
wrapper call, a dash in the failure branch — and each of those shapes
is one an author would plausibly get wrong in a way that only shows up
in the browser.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"

#: The element that carries the parallel-agent count.
_AGENT_COUNT_ID = "scheduler-view-agent-count"

#: The label an operator reads next to the number.
_AGENT_COUNT_LABEL = "并行 Agent"

#: The endpoint that answers 「how many agents are running right now」.
_ACTIVE_PATH = "/system/active"

#: The field of that answer that counts the agents.
_TOTAL_ACTIVE = "total_active"

#: The dash a missing number is drawn as.
_MISSING = "—"


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


def _strip_comments(source: str) -> str:
    """Return ``source`` with its ``//`` and ``/* ... */`` comments removed.

    A gate that reads prose as code is a gate that fails on a well
    written comment and passes on a badly written one.  The docstring
    of the function under test is allowed to *name* the function it
    must not call — that is exactly what a careful author writes, and
    the reason the rule exists.

    String literals are not scanned: the bodies these gates look at
    contain no ``//`` or ``/*`` inside a string, so the simple rule
    holds for the code that is actually under test.
    """
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            index += 2
            while index + 1 < length and not (
                source[index] == "*" and source[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _function_code(source: str, name: str) -> str:
    """Return the body of ``function name(...)`` with comments removed."""
    return _strip_comments(_function_body(source, name))


def _catch_block(body: str) -> str:
    """Return the text inside the first ``catch (...) { ... }`` of ``body``.

    Raises :class:`AssertionError` when there is no catch block — a
    function whose failure branch is missing is not a function whose
    failure branch is isolated.
    """
    match = re.search(r"catch\s*\([^)]*\)\s*\{", body)
    assert match is not None, (
        "the function has no catch block, so a failed request escapes "
        "it instead of being absorbed into a dash."
    )
    start = match.end() - 1  # index of the opening brace
    depth = 0
    for index in range(start, len(body)):
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[start + 1 : index]
    raise AssertionError("unterminated catch block")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_agent_count_element_present() -> None:
    """The capacity row must carry the parallel-agent number.

    Without the element there is nowhere to put the count, and without
    the 「并行 Agent」 label the number is a mystery: the row already
    shows 「已用」「共」「队列」, and a fourth bare number would be read
    as another slot counter.
    """
    html = _source(_INDEX_HTML)

    assert _AGENT_COUNT_ID in html, (
        f"frontend/index.html has no `{_AGENT_COUNT_ID}` element, so the "
        "parallel-agent count has nowhere to be written."
    )

    assert _AGENT_COUNT_LABEL in html, (
        f"frontend/index.html never writes 「{_AGENT_COUNT_LABEL}」, so the "
        "number in the capacity row is unlabelled and reads as another "
        "slot counter."
    )


def test_agent_count_reads_system_active() -> None:
    """The count must come from ``/api/system/active``'s ``total_active``.

    ``total_active`` is the only field of that answer that counts the
    agents in flight: ``execution_running`` and ``verification_running``
    are two halves of it, and one of them alone would under-report a
    plan that is executing and verifying at the same time.
    """
    body = _function_code(_source(_APP_JS), "loadSchedulerAgentCount")

    assert re.search(r"api\(\s*[\"']" + re.escape(_ACTIVE_PATH) + r"[\"']", body), (
        f"loadSchedulerAgentCount() does not read `api(\"{_ACTIVE_PATH}\")`. "
        "The parallel-agent count is the workflow's own answer, not "
        "something the panel may invent from the scheduler's numbers."
    )

    assert _TOTAL_ACTIVE in body, (
        f"loadSchedulerAgentCount() never reads `{_TOTAL_ACTIVE}`, so the "
        "count it shows is not the number of agents in flight."
    )


def test_agent_count_failure_is_isolated() -> None:
    """A failed read must paint a dash and nothing else.

    The count rides along with the panel's own refresh.  If it could
    call ``renderSchedulerError()``, a hiccup in one optional read
    would paint an error banner over a panel that is otherwise fine —
    the operator would be told the scheduler is broken when only the
    agent count is unknown.
    """
    source = _source(_APP_JS)
    body = _function_code(source, "loadSchedulerAgentCount")

    catch = _catch_block(body)

    assert _MISSING in catch, (
        f"the catch branch never draws {_MISSING!r}, so a failed read "
        "leaves the previous number on screen — a stale count is worse "
        "than no count, because it is believed."
    )

    assert "renderSchedulerError" not in body, (
        "loadSchedulerAgentCount() calls renderSchedulerError(). That "
        "function paints an error banner over the whole panel; an "
        "unknown agent count must not make a working panel look broken."
    )


def test_agent_count_uses_the_api_wrapper() -> None:
    """The read must go through ``api()``, never through a bare fetch.

    ``backend/request_guard.py`` answers only requests carrying
    ``X-PDT-Request: 1``.  A bare ``fetch()`` sends no such header and
    the API answers 403 — a failure that looks exactly like "the
    endpoint is down" and would send the reader looking in the wrong
    place.  The wrapper is what carries the header.
    """
    body = _function_code(_source(_APP_JS), "loadSchedulerAgentCount")

    assert "fetch(" not in body, (
        "loadSchedulerAgentCount() issues a bare fetch(). Every call "
        "must go through api(), which attaches the X-PDT-Request guard "
        "header; without it the request is answered 403."
    )

    assert "api(" in body, (
        "loadSchedulerAgentCount() never calls api(); there is no other "
        "way to reach the API from this page."
    )


def test_agent_count_is_called_from_scheduler_state_without_await() -> None:
    """The read must ride along with the panel's refresh, unawaited.

    ``await``ing it would make the panel's own refresh wait on a
    second endpoint that is allowed to fail — the count would then
    delay the queue, and a slow count would delay the panel.
    """
    body = _function_code(_source(_APP_JS), "loadSchedulerState")

    assert "loadSchedulerAgentCount(" in body, (
        "loadSchedulerState() never calls loadSchedulerAgentCount(), so "
        "the parallel-agent count is never read."
    )

    assert not re.search(r"await\s+loadSchedulerAgentCount\s*\(", body), (
        "loadSchedulerState() awaits loadSchedulerAgentCount(). The count "
        "is optional; the panel's refresh must not wait on it."
    )


def test_agent_count_owns_no_timer() -> None:
    """Neither the count nor the refresh may own a timer.

    The panel is refreshed by the plan's own refresh cycle.  A second
    timer for the count would mean two clocks disagreeing about when
    the panel was last read — and it would be a timer nobody scheduled
    and nobody stops.
    """
    source = _source(_APP_JS)

    for name in ("loadSchedulerAgentCount", "loadSchedulerState"):
        body = _function_code(source, name)
        for timer in ("setInterval", "setTimeout"):
            assert timer not in body, (
                f"{name}() calls {timer}(). The parallel-agent count "
                "rides along with the panel's refresh; it must not own a "
                "timer of its own."
            )