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


# ---------------------------------------------------------------------------
# The legacy ``esc()`` helper
# ---------------------------------------------------------------------------
#
# ``escapeHtml()`` coerces its argument before escaping it
# (``String(value == null ? "" : value)``).  The older ``esc()`` helper did
# not: it called ``s.replace(...)`` on whatever it was handed.
#
# That difference is not academic.  A task id in ``tasks.json`` is allowed to
# be a **number** — the shape is legal input, not a malformed file — and
# ``(1).replace`` is a ``TypeError``.  The page caught that exception and
# painted the empty state 「任务尚未生成」 over a plan that *had* tasks: the
# user sees "no tasks", the console stays clean, and the failure is silent.
#
# The gate below pins the contract on the helper itself rather than on the
# call sites: a fix that only coerces the id inside ``renderTaskCard`` would
# leave every other renderer (and every other numeric field) one bad payload
# away from the same silent blank.

_ESC_DECLARATION = re.compile(r"function\s+esc\s*\(\s*([A-Za-z_$][\w$]*)\s*\)")


def _esc_helper(source: str) -> tuple[str, str] | None:
    """Return ``(parameter name, body)`` of the legacy ``esc`` helper.

    ``None`` when the helper is not declared in ``source``.
    """
    match = _ESC_DECLARATION.search(source)
    if match is None:
        return None
    located = _function_body(source, "esc")
    if located is None:
        return None
    _, body = located
    return match.group(1), body


def esc_normalises_its_argument(source: str) -> bool:
    """Does the ``esc`` helper normalise its argument before escaping it?

    Two shapes count as normalising:

    * the helper coerces the parameter itself (``String(param)``), and
    * the helper delegates to ``escapeHtml(param)`` — which coerces.

    What does **not** count is calling a string method on the raw parameter
    (``param.replace(...)``): that is the shape that throws on a number.

    A helper that is not declared at all does not normalise anything.
    """
    located = _esc_helper(source)
    if located is None:
        return False
    param, body = located
    if re.search(rf"(?<![\w$.]){re.escape(param)}\s*\.\s*replace\s*\(", body):
        return False
    coerced = re.search(rf"String\s*\(\s*{re.escape(param)}\b", body)
    delegated = re.search(rf"escapeHtml\s*\(\s*{re.escape(param)}\b", body)
    return bool(coerced or delegated)


def test_esc_helper_is_declared() -> None:
    """The legacy ``esc()`` helper must still exist in ``frontend/app.js``.

    The task / status-detail renderers call it.  A helper that no-one
    defined protects nothing — and this test pins the definition so a
    rename breaks the suite before it breaks the browser.
    """
    text = _APP_JS.read_text(encoding="utf-8")
    assert _esc_helper(text) is not None, (
        "frontend/app.js does not declare ``function esc(``. The task "
        "renderers (renderTaskCard, renderStatusDetailPanel) call it; "
        "rename it across the file or update this gate if the signature "
        "changes on purpose."
    )


def test_esc_coerces_non_string_values() -> None:
    """``esc()`` must not call ``.replace`` on a value that may not be a string.

    A numeric task id is legal input.  ``esc()`` receiving it must produce
    the id's text (``"1"``), not a ``TypeError`` that blanks the task list
    behind the page's empty state.
    """
    text = _APP_JS.read_text(encoding="utf-8")
    located = _esc_helper(text)
    assert located is not None, (
        "frontend/app.js does not declare ``function esc(`` — this gate "
        "cannot assert anything about a helper that is not there."
    )
    param, body = located
    direct = re.search(
        rf"(?<![\w$.]){re.escape(param)}\s*\.\s*replace\s*\(", body
    )
    assert direct is None, (
        f"``esc({param})`` calls ``{param}.replace(...)`` on the raw "
        "argument. A number has no ``.replace``, so a numeric task id "
        "raises a TypeError that the page turns into the empty state "
        "「任务尚未生成」. Coerce the argument to a string first "
        "(``String(param == null ? '' : param)``) or delegate to "
        "``escapeHtml(param)``, which already coerces."
    )
    assert esc_normalises_its_argument(text), (
        "``esc()`` escapes its argument without normalising it first. "
        "The helper must coerce the value to a string before escaping it "
        "so a numeric (or null) field renders instead of throwing."
    )


def test_the_checker_notices_a_helper_that_does_not_normalise() -> None:
    """A guard for the *gate itself*.

    It pins that ``esc_normalises_its_argument`` really inspects the
    helper's body.  Without this test, a refactor that breaks the regex
    would let the very regression this gate exists for through unnoticed.
    """
    broken = 'function esc(s) { return s.replace(/&/g,"&amp;"); }\n'
    assert esc_normalises_its_argument(broken) is False, (
        "esc_normalises_its_argument() accepted a helper that calls "
        "``s.replace`` on the raw argument — that is the exact shape that "
        "throws on a numeric task id. The checker is no longer inspecting "
        "the helper body; fix it before trusting any PASS."
    )

    fixed = 'function esc(s) { return String(s == null ? "" : s).replace(/&/g,"&amp;"); }\n'
    assert esc_normalises_its_argument(fixed) is True, (
        "esc_normalises_its_argument() rejected a helper that coerces its "
        "argument to a string before escaping it — the shape the fix is "
        "supposed to have. The checker is too strict."
    )