"""The plan page must show the human decisions that are waiting for an
answer; the scheduler panel is the one place that may answer them.

Why this gate exists
--------------------
A decision that blocks a task is invisible in the UI: the plan looks
like it is simply not moving. The backend answers "how is this plan
doing" with three projections — the plan summary, the execution
progress and the verification progress — and all three carry the same
``pending_decisions`` block (a count plus the items). A page that
renders that block tells the operator that the plan is waiting for a
person, which is the one fact that changes what the operator does
next.

Two things can go wrong, and neither fails loudly:

1. The block is never rendered, or is rendered from a field nobody
   reads. The page keeps looking idle, and "the plan is waiting for
   someone" is exactly what the operator cannot see.
2. The block is rendered by pasting the decision text into markup.
   ``question`` and ``decision_id`` are free text written by whoever
   raised the decision, so a decision that says ``<img src=x
   onerror=...>`` becomes markup instead of characters.

The rule
--------
* ``frontend/app.js`` carries ``renderPendingDecisions()``. It takes
  the ``pending_decisions`` block, writes the text with
  ``textContent`` — never ``innerHTML`` — and degrades to "0 条" when
  the block is missing or empty. The projection degrades to an empty
  block every time the decision table cannot be read, and a plan page
  that throws on that is a plan page that cannot be opened.
* The block is filled from the payloads the page already has: the
  plan page renderer (the summary projection) and the verification
  progress renderer (the progress projection) both hand the
  projection to it.
* **The decision service is called from the scheduler panel, and only
  from there.** The scheduler panel gives the operator three actions on
  a waiting row — extend, terminate and answer — and the answer reuses
  the decision service's own answer route
  (``/api/hitl/{plan}/decisions/{id}/answer``; 收尾 PRD 决策点 8 / A6).
  A request path naming the decision service (``/hitl``) may therefore
  appear in ``frontend/app.js`` only inside a **top-level function whose
  name starts with ``scheduler``** — the answer is a scheduler action,
  not a plan-page concern. Such a path anywhere else in
  ``frontend/app.js``, or in any other ``frontend/*.js`` file, is a
  violation: the plan page still reads the projection and nothing else.
* The page still goes through the ``api()`` / ``apiRequest()``
  wrappers: no bare ``fetch(`` anywhere (the canonical scanner from
  ``test_frontend_uses_api_wrapper``). The answer is a request like any
  other, and a bare ``fetch(`` answers 403 at runtime while breaking
  nothing at build time.

Scan target
-----------
``frontend/app.js`` read as source, plus the whole ``frontend/``
directory for the request rules. These are shape gates, like the rest
of ``static_gates/``: they run on a plain Python environment with no
JS toolchain, so they pin the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_frontend_uses_api_wrapper import find_bare_fetches  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_DIR = _REPO_ROOT / "frontend"
_APP_JS = _FRONTEND_DIR / "app.js"


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
    comments — which is adequate for the short, uniformly indented
    functions this file inspects. Template literals are safe here as
    long as their ``${...}`` holes are balanced, which they are.
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


def _top_level_function_ranges(source: str) -> list[tuple[str, int, int]]:
    """Return ``(name, body_start, body_end)`` for every top-level function.

    Only the outermost ``function foo(...)`` declarations are returned;
    a nested function inside one of them belongs to that body, not to
    this list.  ``body_start`` is the offset just after the opening
    brace and ``body_end`` the offset of the matching closing brace, so
    the body itself is ``source[body_start:body_end]``.

    The offsets are what lets a caller say *where* a request path sits:
    inside this function, or outside every function.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    ranges: list[tuple[str, int, int]] = []
    for match in pattern.finditer(source):
        name = match.group(1)
        brace = match.end() - 1
        depth = 0
        for index in range(brace, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    ranges.append((name, brace + 1, index))
                    break
    return ranges


def _top_level_functions(source: str) -> dict[str, str]:
    """Return ``{name: body}`` for every top-level function in ``source``.

    Only the outermost ``function foo(...)`` declarations are returned;
    a nested function inside one of them belongs to that body, not to
    this map.
    """
    return {
        name: source[start:end]
        for name, start, end in _top_level_function_ranges(source)
    }


#: A call to ``renderPendingDecisions(...)`` and its argument list. The
#: argument is captured without nested parentheses, which is enough for
#: the shapes this gate cares about (``data.pending_decisions``).
_RENDER_CALL = re.compile(r"renderPendingDecisions\(\s*([^()]*?)\s*\)")


def _render_call_arguments(source: str) -> list[str]:
    """Return the argument of every ``renderPendingDecisions(...)`` call.

    The declaration itself matches this pattern too (its parameter is
    captured as the "argument"), so a caller list has to be read with
    that in mind — which is why the checks below look for the
    projection *inside* an argument rather than for the call itself.
    """
    return [match.group(1) for match in _RENDER_CALL.finditer(source)]


def _pending_decision_violations(source: str) -> list[str]:
    """Return every way ``source`` breaks the pending-decisions contract.

    An empty list means the source satisfies the contract. The helper
    is pure so the tests below can pin it against synthetic sources —
    a gate that cannot fail is not a gate.
    """
    violations: list[str] = []

    try:
        body = _function_body(source, "renderPendingDecisions")
    except AssertionError as exc:
        return [str(exc)]

    arguments = _render_call_arguments(source)
    if not any("pending_decisions" in argument for argument in arguments):
        violations.append(
            "no call site hands the `pending_decisions` projection to "
            "renderPendingDecisions()"
        )

    if "textContent" not in body:
        violations.append(
            "renderPendingDecisions() does not write the text through "
            "textContent"
        )

    if "innerHTML" in body:
        violations.append(
            "renderPendingDecisions() writes through innerHTML; the "
            "decision text is free text and must not become markup"
        )

    if "Array.isArray" not in body:
        violations.append(
            "renderPendingDecisions() does not guard the item list, so a "
            "missing or malformed `items` throws instead of rendering 0"
        )

    if "items.length" not in body:
        violations.append(
            "renderPendingDecisions() does not derive the count from the "
            "item list, so a block without a `count` cannot render 0"
        )

    if "decision_id" not in body or "question" not in body:
        violations.append(
            "renderPendingDecisions() does not name the decision fields "
            "(`decision_id`, `question`)"
        )

    return violations


def _code_string_literals_with_spans(source: str) -> list[tuple[str, int, int]]:
    """Return the code-position string literals with their offsets.

    Comments are skipped, and the contents of a literal are not
    re-scanned, so a path mentioned in a comment never reaches the
    caller and a literal is reported once.  The offsets are the
    literal's start and end in ``source`` — they are what lets a caller
    say *where* a literal sits rather than only *what* it says.
    """
    literals: list[tuple[str, int, int]] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            newline = source.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char in ('"', "'", "`"):
            quote = char
            start = index
            index += 1
            buffer: list[str] = []
            while index < length:
                current = source[index]
                if current == "\\" and index + 1 < length:
                    buffer.append(source[index + 1])
                    index += 2
                    continue
                if current == quote:
                    index += 1
                    break
                buffer.append(current)
                index += 1
            literals.append(("".join(buffer), start, index))
            continue
        index += 1
    return literals


def _code_string_literals(source: str) -> list[str]:
    """Return the string literals that appear in code position.

    Comments are skipped, and the contents of a literal are not
    re-scanned, so a path mentioned in a comment never reaches the
    caller and a literal is reported once.
    """
    return [literal for literal, _, _ in _code_string_literals_with_spans(source)]


#: The path fragment of the decision service. The plan page reads the
#: projection the summary and the progress endpoints carry; the
#: scheduler panel answers a decision through the decision service's
#: own answer route (``/api/hitl/{plan}/decisions/{id}/answer``).
_DECISION_SERVICE_PATH = "/hitl"

#: The prefix a top-level function must carry to call the decision
#: service. The answer is a scheduler action, so it lives in a
#: scheduler function — a plan-page renderer that grew one would be
#: the page answering a decision, which is not what the page is for.
_SCHEDULER_FUNCTION_PREFIX = "scheduler"


def _owning_function(
    ranges: list[tuple[str, int, int]],
    start: int,
    end: int,
) -> str | None:
    """Return the top-level function whose body holds ``start``..``end``.

    ``None`` means the literal sits outside every top-level function.
    The innermost owner wins, so a literal in a nested function is
    attributed to the top-level function that contains it — that is the
    function the rule names.
    """
    owner: str | None = None
    owner_span = 0
    for name, body_start, body_end in ranges:
        if body_start <= start and end <= body_end:
            span = body_end - body_start
            if owner is None or span < owner_span:
                owner = name
                owner_span = span
    return owner


def _decision_service_violations(source: str, label: str) -> list[str]:
    """Return every way ``source`` breaks the decision-service contract.

    The contract is about *where* a decision-service call may live: the
    scheduler panel gives the operator three actions on a waiting row —
    extend, terminate and answer — and the answer reuses the decision
    service's own answer route (``/api/hitl/{plan}/decisions/{id}/answer``;
    收尾 PRD 决策点 8 / A6).  A request path naming the decision service
    is therefore allowed inside a top-level function whose name starts
    with ``scheduler``, and nowhere else.

    A path built outside every top-level function is a violation too:
    the rule says where a decision-service call may live, and module
    level is not one of those places.  Only code-position literals
    count — a comment that names the decision service is not a request.
    """
    violations: list[str] = []
    ranges = _top_level_function_ranges(source)

    for literal, start, end in _code_string_literals_with_spans(source):
        if _DECISION_SERVICE_PATH not in literal:
            continue
        owner = _owning_function(ranges, start, end)
        if owner is not None and owner.startswith(_SCHEDULER_FUNCTION_PREFIX):
            continue
        if owner is None:
            violations.append(
                f"{label}: the request path {literal!r} is built outside any "
                "top-level function; only a top-level function whose name "
                f"starts with {_SCHEDULER_FUNCTION_PREFIX!r} may call the "
                "decision service."
            )
            continue
        violations.append(
            f"{label}: the function {owner}() builds the request path "
            f"{literal!r}; only a top-level function whose name starts with "
            f"{_SCHEDULER_FUNCTION_PREFIX!r} may call the decision service."
        )
    return violations


def _decision_service_literals(source: str) -> list[str]:
    """Return the code-position literals that name the decision service."""
    return [
        literal
        for literal in _code_string_literals(source)
        if _DECISION_SERVICE_PATH in literal
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_app_reads_pending_decisions() -> None:
    """The plan page must render the ``pending_decisions`` projection.

    The projection is the only way the page can say "this plan is
    waiting for a person", so it has to be read, handed to the render
    function, and written with ``textContent``.

    The two call sites are pinned because they are the two payloads the
    page already has: ``renderTasks`` renders the plan page and is fed
    the plan summary, ``renderVerificationPanel`` renders the
    verification progress. A render function nobody calls renders
    nothing.
    """
    source = _source(_APP_JS)

    violations = _pending_decision_violations(source)
    assert not violations, (
        "frontend/app.js breaks the pending-decisions contract:\n  "
        + "\n  ".join(violations)
    )

    callers = {
        name
        for name, body in _top_level_functions(source).items()
        if "renderPendingDecisions(" in body
    }

    for renderer in ("renderTasks", "renderVerificationPanel"):
        assert renderer in callers, (
            f"{renderer}() does not hand the pending-decisions block to "
            "renderPendingDecisions(), so the plan page shows no count of "
            "the decisions that are waiting"
        )


def test_no_bare_fetch() -> None:
    """No bare ``fetch(`` in ``frontend/``, and the decision service is
    called from the scheduler panel only.

    The first half is the canonical rule: every request must go through
    the ``api()`` / ``apiRequest()`` wrappers so the guard header is
    sent — a bare ``fetch(`` answers 403 at runtime and breaks nothing
    at build time.  The scheduler panel's answer action gets no
    exception: it is a request like any other, so it goes through
    ``api()`` too.

    The second half is the HITL half of it, and it is about *where* the
    decision service may be called from.  The scheduler panel gives the
    operator three actions on a waiting row — extend, terminate and
    answer — and the answer reuses the decision service's own answer
    route (``/api/hitl/{plan}/decisions/{id}/answer``; 收尾 PRD 决策点 8 /
    A6).  A request path naming the decision service (``/hitl``) is
    therefore allowed in ``frontend/app.js`` only inside a top-level
    function whose name starts with ``scheduler`` — the answer is a
    scheduler action, not a plan-page concern.  Everywhere else (another
    top-level function, a module-level constant, or any other
    ``frontend/*.js`` file) the decision service is still out of bounds:
    the plan page reads the projection and nothing else.
    """
    findings = find_bare_fetches(_FRONTEND_DIR)
    assert not findings, (
        "frontend/ contains a bare fetch(...) outside the api() / "
        "apiRequest() wrappers. Each fetch() must go through api() in "
        "frontend/app.js or apiRequest() in frontend/api.js so that "
        "X-PDT-Request: 1 is always sent; without that header, /api/* "
        "answers 403.\n  "
        + "\n  ".join(f"{p.as_posix()}:{ln}: {s}" for p, ln, s in findings)
    )

    violations = _decision_service_violations(_source(_APP_JS), _APP_JS.name)
    assert not violations, (
        "frontend/app.js calls the decision service from outside a "
        "scheduler function. The scheduler panel is where an operator "
        "answers a decision (收尾 PRD 决策点 8 / A6), so the answer route "
        "may only be called from a top-level function whose name starts "
        f"with {_SCHEDULER_FUNCTION_PREFIX!r}.\n  "
        + "\n  ".join(violations)
    )

    for path in sorted(_FRONTEND_DIR.glob("*.js")):
        if path == _APP_JS:
            continue
        literals = _decision_service_literals(_source(path))
        assert not literals, (
            f"{path.name} builds the request path {literals[0]!r}. The "
            "decision service is called from the scheduler panel in "
            "frontend/app.js and from nowhere else; the plan page reads the "
            "pending_decisions projection the summary and the progress "
            "endpoints carry."
        )


def test_gate_flags_an_unsafe_render() -> None:
    """The scanner must catch a render that pastes text into markup.

    A gate that cannot fail is not a gate, so the helper the test above
    uses is pinned against a synthetic source that does the wrong thing
    in each of the ways the rule forbids.
    """
    unsafe = """
function renderPendingDecisions(block) {
    const items = block.items || [];
    const html = items.map((d) => `<b>${d.decision_id}</b> ${d.question}`).join("");
    document.querySelector(".pending-decisions").innerHTML = html;
}
"""
    violations = _pending_decision_violations(unsafe)

    assert any("innerHTML" in violation for violation in violations), violations
    assert any("textContent" in violation for violation in violations), violations
    assert any("guard the item list" in violation for violation in violations), violations


def test_gate_flags_a_block_that_is_never_read() -> None:
    """A render function nobody feeds the projection must be flagged.

    The function can be perfect — safe writes, guarded list, zero
    fallback — and still render nothing, because no call site hands it
    the ``pending_decisions`` block.
    """
    safe_but_unfed = """
function renderPendingDecisions(block) {
    const items = Array.isArray(block.items) ? block.items : [];
    const count = items.length || 0;
    const host = document.querySelector(".pending-decisions");
    host.textContent = `待决策 ${count} 条`;
}
"""
    violations = _pending_decision_violations(safe_but_unfed)

    assert any("pending_decisions" in violation for violation in violations), (
        violations
    )


def test_gate_accepts_a_safe_render() -> None:
    """A conforming source must produce no violations.

    The negative controls above prove the scanner bites; this one
    proves it does not bite on correct code, which is the other half of
    a useful gate.
    """
    conforming = """
function renderPendingDecisions(block) {
    const items = block && Array.isArray(block.items) ? block.items : [];
    const count = items.length || 0;
    const detail = items.map((d) => `${d.decision_id} ${d.question}`).join("；");
    const host = document.querySelector(".pending-decisions");
    host.textContent = detail ? `待决策 ${count} 条：${detail}` : `待决策 ${count} 条`;
}

function renderVerificationPanel(data) {
    renderPendingDecisions(data.pending_decisions);
}
"""
    assert _pending_decision_violations(conforming) == []


def test_decision_service_request_is_detected() -> None:
    """A request path into the decision service must be detected where
    the contract forbids it, and allowed where it permits it.

    The rule is no longer "the frontend never names the decision
    service": the scheduler panel answers a decision through it (收尾
    PRD 决策点 8 / A6).  So the scanner has to bite on the same path
    outside a scheduler function and stay quiet on it inside one — both
    halves are pinned here, because a gate that only knows one half is
    a gate that passes for the wrong reason.
    """
    conforming = """
async function schedulerAnswerWait(planId, itemId, option) {
    const result = await api(
        `/hitl/${planId}/decisions/${decisionId}/answer`,
        { method: "POST", body: JSON.stringify({ option: option }) },
    );
    return result;
}
"""
    assert _decision_service_violations(conforming, "app.js") == []

    offending = """
function renderTasks(data) {
    const decisions = await api(`/hitl/${data.planId}/decisions`);
    return decisions;
}
"""
    violations = _decision_service_violations(offending, "app.js")
    assert violations, (
        "the scanner did not catch a decision-service path in renderTasks()"
    )
    assert any("renderTasks" in violation for violation in violations), violations
    assert any("/hitl" in violation for violation in violations), violations


def test_decision_service_mention_in_a_comment_is_not_a_request() -> None:
    """A comment that names the decision service is not a request.

    The scan reads code position only. A comment that says the plan
    page never calls the decision service must not make the gate red, or
    the rule could only be satisfied by deleting the explanation.
    """
    source = (
        "// The plan page never calls /hitl — the scheduler panel answers.\n"
        'const summary = await api("/plan/plan-1/summary");\n'
    )

    assert not any("/hitl" in literal for literal in _code_string_literals(source))
    assert _decision_service_violations(source, "app.js") == []
