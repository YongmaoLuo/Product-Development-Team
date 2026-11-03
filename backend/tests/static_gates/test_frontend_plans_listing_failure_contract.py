"""A failed plans listing must be shown, not thrown at the page.

Why this gate exists
--------------------
``GET /api/plans`` answers ``503`` when the data plane the instance
reads does not describe the plans tree it serves
(``state_db_does_not_describe_plans_dir`` — see
``backend/routes/plans.py::_refuse_when_data_source_does_not_describe_the_tree``).
That refusal is by design: an empty answer from somebody else's database
is indistinguishable from "this instance has no plans".

The frontend made that refusal a page-level failure.  ``api()`` throws on
a non-``ok`` response (correctly — the failure must not be swallowed),
and ``init()`` called ``loadPlans()`` fire-and-forget, so the rejection
was nobody's: it became an **unhandled promise rejection**, which is
what the end-to-end acceptance spec counts as "页面有未捕获的 JS 异常".
The page looked fine — the interview view opened, the plan list was
simply empty — while the browser console carried the refusal.

Two ways to "fix" that, and only one of them is honest:

* Swallow the failure and paint 「暂无计划」.  That dresses a data-plane
  failure up as an ordinary empty list: the operator is told there is
  nothing to work on when in fact the backend refused to answer.  The
  refusal disappears from the screen and stays only in the console.
* Show the failure where the operator is looking, and let the promise
  settle.  The listing area says the read failed and why; the page has
  no uncaught exception.

This gate pins the second one.  It also pins the rule that keeps the
first one out: ``api()`` must keep throwing.

The rule
--------
* ``api()`` still throws when the response is not ``ok``.  A failure
  that is turned into an empty list is a lie about the data plane.
* ``loadPlans()`` catches the read failure and paints it into
  ``#plans-list`` — the container the plans view is rendered into — with
  an error renderer (``render*Error``), so the operator sees a failure
  where the plans would have been.
* ``loadSidebar()`` does not fall back to 「暂无计划」 after a failed
  read: it paints the failure too.  The sidebar's empty state is a
  statement about a read that *succeeded* and found nothing.
* Every fire-and-forget load of the plans listing / sidebar / scheduler
  state carries a rejection handler: the call is either awaited or
  wrapped by the background loader.  A rejected promise nobody holds is
  an uncaught rejection, which is the failure this gate exists to
  prevent.
* The error state names the failure and escapes it: the reason comes
  from the error the wrapper threw, and it reaches the DOM through
  ``escapeHtml()``.

Scan target
-----------
``frontend/app.js``, read as source.  This is a shape gate, like the
rest of ``static_gates/``: it runs on a plain Python environment with
no JS toolchain, so it pins the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The loads that ``init()`` fires and forgets.  Each one reads the
#: backend, so each one can reject.
_FIRE_AND_FORGET_LOADERS = ("loadPlans", "loadSidebar", "loadSchedulerState")

#: The helper that attaches the rejection handler to a fire-and-forget
#: load.  Named in the gate so the rule is checkable, not implied.
_BACKGROUND_LOADER = "loadInBackground"

#: An error renderer: ``renderPlansError``, ``renderSidebarError``, …
_ERROR_RENDERER = re.compile(r"\brender[A-Za-z]*Error\s*\(")


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError``.
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


def _function_names(source: str) -> list[str]:
    """Every top-level function name declared in ``source``."""
    return re.findall(r"^(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(", source, re.MULTILINE)


def _catch_blocks(body: str) -> list[str]:
    """The text of every ``catch`` block in ``body``.

    A ``catch`` block is what turns a rejection into something the page
    can show.  The body is scanned for ``catch`` and then brace-matched
    from there, which is enough for the shapes this file inspects.
    """
    blocks: list[str] = []
    for match in re.finditer(r"\bcatch\s*(?:\([^)]*\))?\s*\{", body):
        start = match.end() - 1
        depth = 0
        for index in range(start, len(body)):
            char = body[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(body[start + 1 : index])
                    break
    return blocks


def _call_sites(source: str, name: str) -> list[tuple[int, str]]:
    """Every call of ``name(...)`` in ``source``, as ``(line, text)``.

    The declaration itself is not a call site, and neither is the
    function reference the background loader is handed (``loadPlans``,
    without parentheses).
    """
    pattern = re.compile(r"(?<![\w.$])" + re.escape(name) + r"\s*\(")
    sites: list[tuple[int, str]] = []
    for match in pattern.finditer(source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        line_end = source.find("\n", match.end())
        if line_end == -1:
            line_end = len(source)
        line = source[line_start:line_end]
        if re.search(r"function\s+" + re.escape(name) + r"\s*\(", line):
            continue  # the declaration
        sites.append((source.count("\n", 0, match.start()) + 1, line))
    return sites


def test_api_keeps_throwing_on_a_failed_response() -> None:
    """``api()`` must not turn a failed read into an empty answer.

    This is the other half of the contract: the frontend may *show* a
    failure, but it may not *swallow* one.  A ``!res.ok`` response that
    resolves to ``[]`` is indistinguishable from a backend that really
    has no plans — and the whole point of the backend's refusal is that
    the two must not look alike.
    """
    body = _function_body(_source(_APP_JS), "api")

    assert "if (!res.ok)" in body, (
        "api() no longer branches on a failed response — a non-ok "
        "answer would be parsed as data."
    )
    assert "throw new Error" in body, (
        "api() no longer throws on a failed response: the refusal would "
        "be swallowed, and an empty plans list would look like a normal "
        "answer."
    )


def test_the_plans_listing_failure_is_caught() -> None:
    """``loadPlans()`` must catch the read failure.

    The rejection has to land somewhere: an uncaught one is a
    page-level exception, which is exactly what the acceptance
    criteria forbid.
    """
    body = _function_body(_source(_APP_JS), "loadPlans")

    assert _catch_blocks(body), (
        "loadPlans() has no catch: the rejection from a refused listing "
        "escapes as an unhandled promise rejection."
    )


def test_the_plans_listing_failure_is_painted_into_the_plans_list() -> None:
    """The caught failure must be shown in ``#plans-list``.

    The plans view is where the operator looks for the plans.  A
    failure that is only logged leaves the view looking empty — the
    refusal is real, but the screen says there is simply nothing
    there.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "loadPlans")

    renderers = [
        name
        for name in _function_names(source)
        if re.fullmatch(r"render[A-Za-z]*Error", name)
    ]
    assert renderers, (
        "no error renderer found in frontend/app.js — the refused "
        "listing has nowhere to be shown."
    )

    painted = [
        name
        for name in renderers
        if "#plans-list" in _function_body(source, name)
    ]
    assert painted, (
        "no error renderer writes into #plans-list: the refused listing "
        "is not shown where the plans would have been."
    )

    called = [
        name
        for name in painted
        if re.search(r"\b" + re.escape(name) + r"\s*\(", body)
    ]
    assert called, (
        "loadPlans() does not call the error renderer that writes into "
        "#plans-list: the caught failure is swallowed instead of shown."
    )


def test_the_sidebar_does_not_paint_an_empty_list_after_a_failed_read() -> None:
    """The sidebar must not say 「暂无计划」 when the read failed.

    The empty state is a statement about a read that succeeded and
    found nothing.  After a refusal it is a lie, and it is the same
    lie the acceptance round was defeated by.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "loadSidebar")

    blocks = _catch_blocks(body)
    assert blocks, (
        "loadSidebar() has no catch: the refusal escapes as an "
        "unhandled promise rejection."
    )

    for block in blocks:
        assert "renderSidebar(" not in block, (
            "loadSidebar() paints the empty sidebar state after a failed "
            "read — a refusal would look like 'there are no plans'."
        )
        assert _ERROR_RENDERER.search(block), (
            "loadSidebar()'s catch paints nothing: the refusal leaves "
            "no trace on the screen."
        )


def test_no_fire_and_forget_load_is_left_unhandled() -> None:
    """Every load fired by the page must carry a rejection handler.

    A call that is neither awaited nor wrapped is a promise nobody
    holds; when it rejects, the browser reports an uncaught exception
    and the page fails the acceptance criteria.
    """
    source = _source(_APP_JS)

    for name in _FIRE_AND_FORGET_LOADERS:
        for line_no, line in _call_sites(source, name):
            before = line.split(name, 1)[0]
            awaited = "await" in before
            wrapped = _BACKGROUND_LOADER in line
            assert awaited or wrapped, (
                f"{name}() is called without a rejection handler on line "
                f"{line_no}: {line.strip()!r}. A rejected load nobody "
                "holds is an uncaught exception."
            )


def test_the_error_state_names_the_failure_and_escapes_it() -> None:
    """The error state must say what failed, and must escape it.

    The reason comes from the error the request wrapper threw — it is
    the only thing that tells the operator what to do next.  It also
    travels through the DOM, so it goes through ``escapeHtml()`` like
    every other piece of server-supplied text.
    """
    source = _source(_APP_JS)
    renderers = [
        name
        for name in _function_names(source)
        if re.fullmatch(r"render[A-Za-z]*Error", name)
    ]

    named = [
        name
        for name in renderers
        if "escapeHtml(" in _function_body(source, name)
        and "message" in _function_body(source, name)
    ]
    assert named, (
        "no error renderer carries the failure's message through "
        "escapeHtml(): the operator is told that something failed, but "
        "not what — and the text reaches the DOM unescaped."
    )


def test_the_background_loader_attaches_a_rejection_handler() -> None:
    """The helper must actually catch, not just call the loader.

    A wrapper that forwards the promise unchanged would look correct in
    the source and still leave the rejection unhandled.
    """
    body = _function_body(_source(_APP_JS), _BACKGROUND_LOADER)

    assert ".catch(" in body, (
        f"{_BACKGROUND_LOADER}() forwards the loader's promise without "
        "attaching a rejection handler: the failure still escapes."
    )
