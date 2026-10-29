"""The SPA must carry the scheduler: a view, the home capacity widget,
and the four actions — every request through the guard wrapper.

Why this gate exists
--------------------
The scheduler's control plane moved from the old read-only page on 8004
into this SPA (PRD decision point 6).  A panel that cannot answer "why
is this plan not running" is a panel that sends the operator back to the
logs, so the read model
(``GET /api/scheduler/state``) has to be *shown* here, and the four
operator actions have to be *reachable* here.

Three things about that are invisible in a browser and loud in a source
scan:

1. **The view has to exist.**  A panel section that was never added to
   ``index.html`` leaves the operator with the old behaviour — no view,
   no error, just a nav button that goes nowhere.
2. **The four actions have to go through ``api()``.**  ``api()`` is
   what attaches ``X-PDT-Request: 1``; the server answers 403 to a
   request without it.  A bare ``fetch(`` for one of the four actions
   therefore fails *at the browser*, with a 403 and no explanation —
   which is why this is a source gate and not a runtime one.
3. **The panel must not grow a global on/off switch.**
   ``scheduler.enabled`` is the operator's rollback lever and it lives
   in the configuration *on purpose* (PRD decision point 7): a button
   that stops the whole scheduler is one mis-click away from stopping
   every plan.  The panel may act on one plan; it may not act on the
   scheduler itself.

The rules
---------
* ``frontend/index.html`` carries a ``<section id="scheduler-view">``,
  and the home view (``#plans-view``) carries the capacity widget:
  used / total, and the queue length.
* ``frontend/app.js`` carries the four actions — ``promote``, ``pause``,
  ``resume``, ``start`` — and each one reaches the API through
  ``api(...)``, with the plan's own id in the path.
* No ``fetch(`` outside the wrapper (the canonical scanner is imported,
  not re-implemented — see ``test_frontend_uses_api_wrapper``).
* No ``scheduler.enabled`` anywhere in ``frontend/``, and no request
  path that carries a global switch.

Scan target
-----------
``frontend/index.html``, ``frontend/app.js`` and ``frontend/style.css``,
read as source.  These are shape gates like the rest of
``static_gates/``: they run on a plain Python environment with no JS
toolchain, so they pin the contract in the source rather than executing
the page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ``static_gates/`` is on sys.path for this directory's modules (the
# same move ``commit_range_scan.py`` makes), so the canonical bare-fetch
# scanner can be imported instead of re-implemented: two scanners would
# be two judgements about the same rule, and they would drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_frontend_uses_api_wrapper import find_bare_fetches  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_DIR = _REPO_ROOT / "frontend"
_INDEX_HTML = _FRONTEND_DIR / "index.html"
_APP_JS = _FRONTEND_DIR / "app.js"
_STYLE_CSS = _FRONTEND_DIR / "style.css"


#: The four operator actions the panel offers, and the command each one
#: issues.  The vocabulary is the server's
#: (``backend/routes/scheduler.py``) — the panel does not invent a fifth
#: one, and it does not rename these four.
SCHEDULER_ACTIONS = ("promote", "pause", "resume", "start")

#: The ids the home capacity widget is made of.  "Used / total" and the
#: queue length are the two numbers an operator looks at first, so they
#: are separate elements rather than one blob of text: the E2E compares
#: them field by field against ``GET /api/scheduler/state``.
CAPACITY_WIDGET_IDS = (
    "scheduler-capacity",
    "scheduler-capacity-used",
    "scheduler-capacity-total",
    "scheduler-capacity-queue",
)

#: A request path that would switch the scheduler on or off.  None of
#: these may appear in an ``api(...)`` path: the panel acts on plans.
GLOBAL_SWITCH_WORDS = ("enabled", "enable", "disable", "toggle", "config")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _section_html(html: str, section_id: str) -> str:
    """Return the HTML of the ``<section id=section_id>`` element.

    The element ends at the first ``</section>`` after its opening tag:
    the scheduler view and the plans view hold no nested ``<section>``,
    so the first close is the right one, and a wrong id raises rather
    than returning the whole document.
    """
    match = re.search(
        r"<section[^>]*\bid=\"" + re.escape(section_id) + r"\"[^>]*>", html
    )
    assert match is not None, f'<section id="{section_id}"> not found'
    end = html.find("</section>", match.end())
    assert end != -1, f'<section id="{section_id}"> is never closed'
    return html[match.start() : end]


def _api_paths(source: str) -> list[str]:
    """Return the path template of every ``api(...)`` call in ``source``."""
    return re.findall(r"""api\(\s*(`[^`]*`|"[^"]*"|'[^']*')""", source)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scheduler_view_section_exists() -> None:
    """``index.html`` must carry the scheduler's view, and the app must
    be able to open it.

    A section that is declared but never shown is as good as no section:
    the operator still has nowhere to look.  So the gate checks both the
    markup and the call that reveals it.
    """
    html = _source(_INDEX_HTML)

    match = re.search(r"<section[^>]*\bid=\"scheduler-view\"[^>]*>", html)
    assert match is not None, (
        'index.html has no <section id="scheduler-view">. The scheduler '
        "has no panel: the operator cannot see the queue, cannot see why "
        "a plan is not running, and cannot act on a plan from the SPA."
    )
    assert "view" in match.group(0), (
        'the scheduler section is not a ".view" element, so show() '
        "cannot hide it when another view opens and the two views would "
        "be on screen at once."
    )

    source = _source(_APP_JS)
    assert 'show("scheduler-view")' in source, (
        "the scheduler view is never shown. The markup alone is not "
        "reachable — the operator would never get to it."
    )
    assert "renderScheduler" in source, (
        "app.js has no renderScheduler(): the view would be opened and "
        "stay empty, because nothing reads the read model."
    )


def test_four_actions_use_api_wrapper() -> None:
    """Each of the four actions must reach the API through ``api(...)``.

    The wrapper is what carries ``X-PDT-Request: 1``.  A request without
    that header is answered 403 by the request guard, so a bare
    ``fetch(`` for an action is a button that silently does nothing —
    and it fails in the browser, not in the build.

    The plan's own id has to be in the path: the command acts on one
    plan, and the path is the only place the plan is named.
    """
    source = _source(_APP_JS)

    for action in SCHEDULER_ACTIONS:
        function_name = "scheduler" + action.capitalize()
        body = _function_body(source, function_name)

        assert "api(" in body, (
            f"{function_name}() does not go through api(). The request "
            "guard needs X-PDT-Request: 1, which only the wrapper "
            "attaches — without it the server answers 403."
        )
        assert f"/scheduler/{action}" in body, (
            f"{function_name}() does not address /scheduler/{action}. The "
            "action would be carried out against a different endpoint."
        )
        assert 'method: "POST"' in body, (
            f"{function_name}() does not POST. The four scheduler "
            "endpoints are commands; a GET would read, not act."
        )


def test_capacity_widget_ids_present() -> None:
    """The home view must carry the capacity widget.

    The operator's first question is "how full is it" — that question is
    answered on the page they land on, so the widget belongs to
    ``#plans-view`` and not only to the scheduler view.  The two numbers
    are separate elements because the E2E compares them field by field
    against ``GET /api/scheduler/state``.
    """
    html = _source(_INDEX_HTML)
    home = _section_html(html, "plans-view")

    for element_id in CAPACITY_WIDGET_IDS:
        assert f'id="{element_id}"' in home, (
            f'the home view has no id="{element_id}". The capacity '
            "widget is what tells the operator how many slots are taken "
            "and how many plans are waiting, before they open the "
            "scheduler view at all."
        )


def test_no_bare_fetch_and_no_global_toggle() -> None:
    """No bare ``fetch(``, and no global on/off switch in the panel.

    Two properties, one gate: they are the two ways this panel could
    quietly become dangerous.

    The bare fetch is the request-guard failure described above — a 403
    the operator cannot explain.

    The global switch is the rollback lever.  ``scheduler.enabled``
    lives in the configuration *on purpose*: a button that stops the
    whole scheduler is one mis-click away from stopping every plan that
    is running.  The panel may act on one plan; it may not act on the
    scheduler itself.
    """
    findings = find_bare_fetches(_FRONTEND_DIR)
    assert not findings, (
        "frontend/ contains a bare fetch(...) outside the api() wrapper. "
        "Every request must go through api() so X-PDT-Request: 1 is "
        "sent; without it /api/* answers 403.\n  "
        + "\n  ".join(f"{p.as_posix()}:{ln}: {s}" for p, ln, s in findings)
    )

    for path in (_INDEX_HTML, _APP_JS, _STYLE_CSS):
        text = _source(path)
        assert "scheduler.enabled" not in text, (
            f"{path.name} mentions scheduler.enabled. The global switch is "
            "a configuration lever, not a panel control: a button that "
            "stops every plan at once is one mis-click away from an "
            "outage."
        )

    for path_template in _api_paths(_source(_APP_JS)):
        lowered = path_template.lower()
        for word in GLOBAL_SWITCH_WORDS:
            assert word not in lowered, (
                f"the request path {path_template} carries {word!r}. The "
                "panel may act on a plan; it may not act on the "
                "scheduler's own configuration."
            )
