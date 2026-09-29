"""Chat / verification / usage view render functions in ``frontend/app.js``
must HTML-escape every ``${...}`` interpolation.

Why this gate exists
--------------------
Task 6 introduced ``escapeHtml()`` and applied it to the plan / task
views, but the chat-message bubble, interview view, and the four
verification / usage renderers still piped LLM-produced strings straight
into ``innerHTML``. Those renderers are precisely where user-typed
interview replies, model replies, requirement deviations, repair
descriptions, and usage notes land — the injection surface that the
plan-view gate deliberately targeted now lives behind these eight
functions.

This gate pins the rule across those eight functions so a future edit
that drops the wrapper breaks the suite rather than the browser.

The rule
--------
Every ``${...}`` interpolation inside
``pollPlanState`` / ``addChatMsg`` / ``showInterview`` /
``renderVerificationPoints`` / ``renderVerificationResult`` /
``renderRepairTasks`` / ``renderVerificationPanel`` /
``renderUsagePanel`` must be wrapped in ``escapeHtml(...)`` — same
helper that task 6 introduced for the plan / task views. The two
existing allowances carry over from the sibling gate:

* ``${ something.map(...).join(...) }`` — the helper the chain feeds
  into (``renderTaskCountsStrip`` etc.) already escapes its own fields,
  so wrapping the joined result would double-escape.
* A pure static string literal with no ``${`` is not an interpolation
  and is not gated.

The ``escapeHtml`` helper itself must be declared exactly once. Two
copies would mean two functions whose coverage drifted silently
(``esc()`` vs. ``escapeHtml()`` is exactly the trap that produced this
task).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The eight renderers whose ``innerHTML``-feeding template strings this
#: gate covers. Kept in one place so the sibling browser-E2E gate (task 20)
#: and the audit-coverage map (arch decision point 2) can reference the
#: same set by import.
MESSAGE_VIEW_FUNCTIONS: tuple[str, ...] = (
    "pollPlanState",
    "addChatMsg",
    "showInterview",
    "renderVerificationPoints",
    "renderVerificationResult",
    "renderRepairTasks",
    "renderVerificationPanel",
    "renderUsagePanel",
)


def _function_body(source: str, name: str) -> tuple[int, str] | None:
    """Return ``(1-based start line, body text)`` of the named top-level
    function, or ``None`` if it is not declared in ``source``.

    The match is by declaration line — either ``function NAME`` or
    ``async function NAME`` at column zero — followed by an opening
    ``{`` and a brace-matched close. The function name list is small
    and the indentation convention is uniform, so a brace counter is
    enough.

    Missing is *not* an error: callers may pass a synthetic source
    that only contains a subset of the message-view functions (the
    ``test_raw_interpolation_is_detected`` guard does exactly this),
    and we want them to scan what is there without raising.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    match = pattern.search(source)
    if match is None:
        return None
    body_start = match.end() - 1  # position of the opening '{'
    depth = 0
    i = body_start
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                start_line = 1 + source[: match.start(0)].count("\n")
                return start_line, source[body_start + 1 : i]
        i += 1
    raise AssertionError(f"Function {name!r} has no matching closing brace")


_INTERPOLATION = re.compile(r"\$\{([^{}]*)\}")


def _is_pre_escaped_html_interpolation(expr: str) -> bool:
    """A ``.map(...).join(...)`` returning HTML from a helper is exempt —
    the helper already escaped its fields, and wrapping the joined
    result would double-escape. Everything else must be wrapped in
    ``escapeHtml(...)``.
    """
    stripped = expr.strip()
    if stripped.startswith("escapeHtml("):
        return True
    # ``.map(`` returning HTML via a helper is the documented exception.
    # ``.join(`` on its own is only meaningful as a tail of a ``.map`` /
    # ``.filter`` chain, so accept it as part of the same pattern.
    if ".map(" in stripped or ".filter(" in stripped or stripped.startswith(".join("):
        return True
    return False


def _is_in_html_template(body: str, match_start: int) -> bool:
    r"""Return True if ``${...}`` at ``match_start`` lives inside an
    HTML template literal.

    ``frontend/app.js`` mixes backtick strings of two kinds:

    * HTML templates — assigned to ``.innerHTML`` and made up of
      ``<div>...</div>`` markup. These are the injection surface.
    * URL templates — e.g. ``\`/plan/${planId}/state\``` — used as
      arguments to ``api()`` and never reach the DOM.

    The two are distinguishable by whether the template contains
    ``<\w`` (an angle bracket followed by a word character — a tag
    start). A URL template contains no such pattern; flagging it
    would force an ``escapeHtml()`` wrap that mangles ``&`` into
    ``&amp;`` and breaks the URL. The scanner therefore limits itself
    to templates that already contain markup.
    """
    # Walk backwards from `${` to the opening backtick.
    i = match_start - 1
    while i >= 0 and body[i] != "`":
        i -= 1
    if i < 0:
        return False
    # Walk forwards to the closing backtick, honouring backslash
    # escapes so ``\``` doesn't end the template.
    j = i + 1
    while j < len(body):
        ch = body[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "`":
            break
        j += 1
    if j >= len(body):
        return False
    template = body[i + 1 : j]
    return bool(re.search(r"<\w", template))


def unescaped_interpolations(
    source: str, functions: tuple[str, ...]
) -> list[tuple[str, int, str]]:
    """Return one row per ``${...}`` interpolation that is not wrapped.

    Each row is ``(function_name, 1-based line in source, raw
    expression inside ${...})``. Used by the assertion in
    ``test_message_view_interpolations_are_escaped``.
    """
    rows: list[tuple[str, int, str]] = []
    for name in functions:
        located = _function_body(source, name)
        if located is None:
            continue
        start_line, body = located
        for match in _INTERPOLATION.finditer(body):
            if not _is_in_html_template(body, match.start()):
                continue
            expr = match.group(1)
            if _is_pre_escaped_html_interpolation(expr):
                continue
            offset = match.start()
            consumed = body.count("\n", 0, offset)
            rows.append((name, start_line + consumed, expr.strip()))
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_app_js_is_scanned() -> None:
    """The gate must operate on a real, non-empty ``frontend/app.js``.

    Catches the silent-regression class where the file is renamed,
    moved, or emptied: a gate that reads zero bytes vacuously passes.
    """
    assert _APP_JS.exists(), (
        f"{_APP_JS} is missing — the gate targets the message / "
        f"verification / usage view renderers in frontend/app.js; a "
        f"missing file is a configuration drift, not a clean state."
    )
    text = _APP_JS.read_text(encoding="utf-8")
    assert len(text.splitlines()) > 0, (
        f"{_APP_JS} is empty. The gate cannot assert anything about a "
        f"zero-line renderer."
    )


def test_message_view_interpolations_are_escaped() -> None:
    """No ``${...}`` in the eight message / verification / usage
    renderers escapes the helper.

    Whitelisted: ``${ something.map(...) }`` / ``.join(...)`` chains
    that hand back already-escaped HTML from a helper. Everything
    else — primitives, ternary results, attribute values, numeric
    locals such as ``${pct}`` — must be wrapped in ``escapeHtml(...)``
    so a stray server-supplied ``<`` cannot reach ``innerHTML``.
    """
    text = _APP_JS.read_text(encoding="utf-8")
    issues = unescaped_interpolations(text, MESSAGE_VIEW_FUNCTIONS)
    assert not issues, (
        "Message / verification / usage view renderers contain "
        "${...} interpolations that are not wrapped in "
        "escapeHtml(...). Each row is (function, line, expression); "
        "wrap the expression as ${escapeHtml(expr)} so server-"
        "supplied values cannot reach innerHTML verbatim.\n"
        + "\n".join(f"  {name} L{line}: ${{{expr}}}" for name, line, expr in issues)
    )


def test_escape_helper_is_reused_not_redefined() -> None:
    """``escapeHtml(value)`` must be declared exactly once in app.js.

    Two definitions would mean two functions whose coverage drifted
    silently — ``esc()`` vs. ``escapeHtml()`` is exactly the trap that
    produced this task. A rename to ``escapeHtml`` keeps the contract;
    a second declaration is a regression.
    """
    text = _APP_JS.read_text(encoding="utf-8")
    definitions = list(
        re.finditer(r"function\s+escapeHtml\s*\([^)]*\)\s*\{", text)
    )
    assert len(definitions) == 1, (
        f"frontend/app.js declares `function escapeHtml(...)` "
        f"{len(definitions)} times — expected exactly 1. Two copies "
        f"drift silently: rename one or remove the duplicate so all "
        f"call sites go through the same helper."
    )


def test_empty_assignment_is_not_a_violation() -> None:
    """``innerHTML = ""`` is an empty assignment, not an interpolation.

    Pins the gate's regex behaviour: a bare ``innerHTML = ""`` clears
    the container and feeds the DOM no interpolated content. The gate
    must not flag it as an unwrapped interpolation — the regex
    requires ``${`` and an empty string carries none.
    """
    synthetic = (
        "function addChatMsg(container, role, content) {\n"
        "    const chat = $(\"#interview-chat\");\n"
        "    chat.innerHTML = \"\";\n"
        "    return chat;\n"
        "}\n"
    )
    issues = unescaped_interpolations(synthetic, MESSAGE_VIEW_FUNCTIONS)
    assert not issues, (
        "unescaped_interpolations() flagged `innerHTML = \"\"` as an "
        "unwrapped interpolation. The gate must only flag ${...} "
        "substitutions; a bare assignment carries no interpolation."
    )


def test_raw_interpolation_is_detected() -> None:
    r"""A synthetic unwrapped HTML interpolation must be flagged.

    Guard for the *gate itself*: pins that
    ``unescaped_interpolations`` actually inspects the function bodies
    and does not silently no-op. Without this test, a refactor that
    breaks the regex (e.g. forgetting ``MULTILINE``) would let every
    real regression through unchallenged.

    The synthetic template must look like real HTML markup
    (``<\w``); a bare ``${report.title}`` in a URL template is a
    valid exclusion and would not exercise the HTML-template branch.
    """
    synthetic = (
        "function renderUsagePanel(report) {\n"
        "    return `<div class=\"usage\">${report.title}</div>`;\n"
        "}\n"
    )
    issues = unescaped_interpolations(synthetic, MESSAGE_VIEW_FUNCTIONS)
    assert issues, (
        "unescaped_interpolations() returned no rows for a synthetic "
        "function body that contains a raw `${report.title}` inside "
        "an HTML template. The gate is no longer scanning function "
        "bodies — fix the helper before trusting any PASS."
    )
    fn_name, _, expr = issues[0]
    assert fn_name == "renderUsagePanel", issues
    assert expr == "report.title", issues