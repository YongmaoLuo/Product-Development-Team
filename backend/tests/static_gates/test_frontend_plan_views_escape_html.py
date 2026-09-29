"""Plan/task view render functions in ``frontend/app.js`` must HTML-escape
every ``${...}`` interpolation.

Why this gate exists
--------------------
``frontend/app.js`` rendered the sidebar / plans / tasks views with template
strings piped straight into ``innerHTML`` and no escaping helper in sight.
A field like ``plan.id`` whose value contains a stray ``<`` or ``"`` was
delivered to the DOM verbatim — a textbook injection surface that turns
into a blocker the moment anything other than a dev typing into a fixture
is on the sending end.

The fix is the ``escapeHtml()`` helper that the rest of the frontend uses
for ``esc(...)``-style outputs, applied at every interpolation site inside
the five views that render plan / task data. This gate pins that contract
in source so a regression breaks the suite rather than the browser.

The rule
--------
Every ``${...}`` interpolation inside
``renderSidebar`` / ``renderSteps`` / ``loadPlans`` / ``loadTasks`` /
``renderTasks`` must be wrapped in ``escapeHtml(...)``. The single
allowance is ``${ something.map(...).join(...) }`` — that pattern returns
already-escaped HTML from a helper (``renderTaskCard`` etc.) and wrapping
the joined result would double-escape. A pure static string literal with
no ``${`` is not an interpolation and is not gated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The five views whose ``innerHTML``-feeding template strings this gate
#: covers. Kept in one place so the sibling browser-E2E gate (task 20) and
#: the audit-coverage map (arch decision point 2) can reference the same
#: set by import.
PLAN_VIEW_FUNCTIONS: tuple[str, ...] = (
    "renderSidebar",
    "renderSteps",
    "loadPlans",
    "loadTasks",
    "renderTasks",
)


def _function_body(source: str, name: str) -> tuple[int, str] | None:
    """Return ``(1-based start line, body text)`` of the named top-level
    function, or ``None`` if it is not declared in ``source``.

    The match is by declaration line — either ``function NAME`` or
    ``async function NAME`` at column zero — followed by an opening ``{``
    and a brace-matched close. The function name list is small and the
    indentation convention is uniform, so a brace counter is enough.

    Missing is *not* an error: callers may pass a synthetic source that
    only contains a subset of the plan-view functions (the
    ``test_raw_interpolation_is_detected`` guard does exactly this), and
    we want them to scan what is there without raising.
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
                return match.start(0) + 1, source[body_start + 1 : i]
        i += 1
    raise AssertionError(f"Function {name!r} has no matching closing brace")


_INTERPOLATION = re.compile(r"\$\{([^{}]*)\}")


def _is_pre_escaped_html_interpolation(expr: str) -> bool:
    """A ``.map(...).join(...)`` returning HTML from a helper is exempt —
    the helper already escaped its fields, and wrapping the joined result
    would double-escape. Everything else must be wrapped in
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


def unescaped_interpolations(
    source: str, functions: tuple[str, ...]
) -> list[tuple[str, int, str]]:
    """Return one row per ``${...}`` interpolation that is not wrapped.

    Each row is ``(function_name, 1-based line in source, raw expression
    inside ${...})``. Used by the assertion in
    ``test_plan_view_interpolations_are_escaped``.
    """
    rows: list[tuple[str, int, str]] = []
    for name in functions:
        located = _function_body(source, name)
        if located is None:
            continue
        start_line, body = located
        for match in _INTERPOLATION.finditer(body):
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

    Catches the silent-regression class where the file is renamed, moved,
    or emptied: a gate that reads zero bytes vacuously passes.
    """
    assert _APP_JS.exists(), (
        f"{_APP_JS} is missing — the gate targets the plan view renderer "
        f"in frontend/app.js; a missing file is a configuration drift, not "
        f"a clean state."
    )
    text = _APP_JS.read_text(encoding="utf-8")
    assert len(text.splitlines()) > 0, (
        f"{_APP_JS} is empty. The gate cannot assert anything about a "
        f"zero-line renderer."
    )


def test_escape_html_helper_defined() -> None:
    """``escapeHtml(value)`` must be declared somewhere in app.js.

    A wrapper that no-one defined protects nothing — this test pins the
    definition so a rename of the helper breaks the suite before it
    breaks the browser.
    """
    text = _APP_JS.read_text(encoding="utf-8")
    assert re.search(r"function\s+escapeHtml\s*\(", text), (
        "frontend/app.js does not declare ``function escapeHtml(``. "
        "The plan-view gates and every other escaping call site depend "
        "on this helper being present; rename it across the file or "
        "update the gate if the signature changes on purpose."
    )


def test_plan_view_interpolations_are_escaped() -> None:
    """No ``${...}`` in the five plan-view functions escapes the helper.

    Whitelisted: ``${ something.map(...) }`` / ``.join(...)`` chains that
    hand back already-escaped HTML from a helper. Everything else —
    primitives, ternary results, attribute values — must be wrapped in
    ``escapeHtml(...)`` so a stray server-supplied ``<`` cannot reach
    ``innerHTML``.
    """
    text = _APP_JS.read_text(encoding="utf-8")
    issues = unescaped_interpolations(text, PLAN_VIEW_FUNCTIONS)
    assert not issues, (
        "Plan/task view renderers contain ${...} interpolations that "
        "are not wrapped in escapeHtml(...). Each row is "
        "(function, line, expression); wrap the expression as "
        "${escapeHtml(expr)} so server-supplied values cannot reach "
        "innerHTML verbatim.\n"
        + "\n".join(f"  {name} L{line}: ${{{expr}}}" for name, line, expr in issues)
    )


def test_raw_interpolation_is_detected() -> None:
    """A synthetic unwrapped interpolation must be flagged.

    This is a guard for the *gate itself*: it pins that
    ``unescaped_interpolations`` actually inspects the function bodies
    and does not silently no-op. Without this test, a refactor that
    breaks the regex (e.g. forgetting ``MULTILINE``) would let every
    real regression through unchallenged.
    """
    synthetic = (
        "function renderTasks(data) {\n"
        "    return `${p.title}`;\n"
        "}\n"
    )
    issues = unescaped_interpolations(synthetic, PLAN_VIEW_FUNCTIONS)
    assert issues, (
        "unescaped_interpolations() returned no rows for a synthetic "
        "function body that contains a raw `${p.title}`. The gate is "
        "no longer scanning function bodies — fix the helper before "
        "trusting any PASS."
    )
    fn_name, _, expr = issues[0]
    assert fn_name == "renderTasks", issues
    assert expr == "p.title", issues