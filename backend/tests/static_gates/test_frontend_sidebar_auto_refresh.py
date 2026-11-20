"""The sidebar must keep itself current while the page is open.

Why this gate exists
--------------------
The sidebar is the operator's map of the whole plan tree: it is the one
place that shows every plan and the phase each plan is in.  It was
painted once, when the page loaded, and never again — so the phase a
plan showed was the phase it had at that moment, and a plan that
started, finished, failed or archived afterwards kept showing its old
phase until the operator reloaded the page by hand.

A backend that is working perfectly therefore looks like a backend
that has stopped: the sidebar says 「排队中」 for a plan that has been
running for ten minutes, and says 「执行中」 for a plan whose run ended
an hour ago.

The fix is a timer, and a timer has four ways to go wrong, each of
them invisible in the browser:

1. **No named interval.**  ``setInterval(refresh, 30000)`` hides the
   cadence in a literal, so the number cannot be found, cannot be
   changed, and cannot be asserted on.  The gate pins the interval to
   a named constant instead.
2. **Two timers.**  A timer that is started on every page load and
   never stopped doubles: two requests per tick, then three.  The
   sidebar looks fine and the API quietly takes twice the load.  The
   gate requires the start function to clear the previous timer
   *before* it sets a new one.
3. **A hidden tab that keeps polling.**  A tab in the background is
   nobody's screen: every tick it issues is a read nobody looks at.
   The gate requires the page's visibility to decide whether a timer
   runs and whether a read happens.
4. **A foreground that does not refresh.**  Stopping the timer while
   the page is hidden is only half the contract: when the operator
   comes back, the list must be read **at once**, not one tick later.
   The gate requires the visibility handler to read before it resumes
   the timer.

The rules
---------
* ``SIDEBAR_POLL_MS`` is a named constant and the timer is created
  with it.
* ``startSidebarPolling()`` stops the previous timer before it sets a
  new one, and a hidden page starts no timer at all.
* The timer reads the plan list through the loader that already exists
  — ``loadSidebar()`` — and the read is awaited, so a refused read
  keeps the failure behaviour ``loadSidebar()`` already has: the
  failure is painted in the sidebar and no dialog is raised.
* The page's visibility decides: hidden, the timer is stopped and
  nothing is read; visible again, the list is read once at once and
  only then the timer resumes.
* The ``"visibilitychange"`` listener is registered once, outside
  ``init()``, and it reaches the sidebar's handler — a handler nobody
  calls refreshes nothing.

Scan target
-----------
``frontend/app.js``, read as source.  These are shape gates like the
rest of ``static_gates/``: they run on a plain Python environment with
no JS toolchain, so they pin the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ``static_gates/`` is on sys.path for this directory's modules, so the
# body-slicing helpers of the sibling gates are imported instead of
# re-implemented: two slicers would be two judgements about the same
# file, and they would drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_scheduler_view_present import _function_body, _source  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hidden_branch(source: str, function_name: str) -> str:
    """Return the text of the block that runs while the page is hidden.

    The block is the ``{ ... }`` that follows the ``document.hidden``
    test inside *function_name*.  It is the half of the handler that
    must do no reading at all: stopping the timer is allowed, asking
    the API is not.

    Raises :class:`AssertionError` when the ``document.hidden`` test is
    absent, so a handler that never looks at the page's visibility
    fails loudly instead of matching nothing and passing.
    """
    body = _function_body(source, function_name)

    marker = body.find("document.hidden")
    assert marker != -1, (
        f"{function_name}() never tests document.hidden. The page's "
        "visibility is what decides whether the sidebar polls at all."
    )

    brace = body.find("{", marker)
    if brace == -1:
        return ""

    depth = 0
    for index in range(brace, len(body)):
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[brace + 1 : index]
    raise AssertionError(
        f"unbalanced braces in the hidden branch of {function_name}()"
    )


def _visibility_listener(source: str) -> str:
    """Return the name of the function registered for ``"visibilitychange"``.

    There must be exactly one registration.  A second listener for the
    same event is a second registration of the same thing, and the
    sibling scheduler gate already pins the count to one.

    Raises :class:`AssertionError` when the registration is absent or
    repeated, so a handler nobody calls fails loudly instead of
    matching nothing and passing.
    """
    matches = re.findall(
        r'document\.addEventListener\(\s*"visibilitychange"\s*,\s*([A-Za-z_$][\w$]*)',
        source,
    )

    assert matches, (
        'frontend/app.js registers no "visibilitychange" listener. The '
        "sidebar would never learn that the page came back to the "
        "foreground, so the list would stay as it was when the page "
        "was hidden."
    )
    assert len(matches) == 1, (
        f'frontend/app.js registers the "visibilitychange" listener '
        f"{len(matches)} times. Each registration is another handler "
        "running on the same event; the sibling scheduler gate pins "
        "this count to one."
    )
    return matches[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poll_interval_is_a_named_constant() -> None:
    """The cadence must be a named constant, and the timer must use it.

    A literal in the ``setInterval`` call is a number nobody can find,
    change, or assert on: the only way to learn how often the sidebar
    refreshes is to read the line and count.  The constant makes it a
    property of the sidebar rather than a detail of one call.
    """
    source = _source(_APP_JS)

    assert "const SIDEBAR_POLL_MS = 30000;" in source, (
        "frontend/app.js has no `const SIDEBAR_POLL_MS = 30000;`. The "
        "refresh cadence must be a named constant: the sidebar's poll "
        "interval is a property of the sidebar, and a literal inside "
        "the setInterval call cannot be found or changed."
    )

    body = _function_body(source, "startSidebarPolling")
    assert "setInterval(refreshSidebar, SIDEBAR_POLL_MS)" in body, (
        "startSidebarPolling() does not create the timer with "
        "setInterval(refreshSidebar, SIDEBAR_POLL_MS). Either the "
        "timer polls something other than refreshSidebar(), or the "
        "interval is a literal instead of the named constant."
    )


def test_one_timer_replaces_the_previous_one() -> None:
    """Starting the timer must clear the previous one first.

    A timer that is started on every page load and never stopped
    doubles on the second load: the sidebar still looks right, and the
    API quietly takes twice the load.  The order matters — the previous
    timer has to be stopped *before* the new one is set, or the two
    overlap for an interval.

    The handle is one variable, declared once: two handles would be
    two timers with no way to tell which one is the live one.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "startSidebarPolling")

    stop_index = body.find("stopSidebarPolling()")
    interval_index = body.find("setInterval(")

    assert stop_index != -1, (
        "startSidebarPolling() never calls stopSidebarPolling(). Each "
        "page load would leave another timer running, and the requests "
        "per tick would grow with every load."
    )
    assert interval_index != -1, (
        "startSidebarPolling() sets no timer at all — the sidebar "
        "would still be read only once, when the page is loaded."
    )
    assert stop_index < interval_index, (
        "startSidebarPolling() sets the new timer before stopping the "
        "old one. The two overlap, so the sidebar issues two requests "
        "per tick for as long as both are alive."
    )

    assert source.count("let _sidebarPoller") == 1, (
        "frontend/app.js declares the sidebar's timer handle more than "
        "once. Two handles are two timers, and only one of them can be "
        "stopped."
    )

    stop_body = _function_body(source, "stopSidebarPolling")
    assert "clearInterval(" in stop_body, (
        "stopSidebarPolling() does not clear the interval it holds. The "
        "timer would keep ticking after the page decided it should "
        "not."
    )
    assert "_sidebarPoller = null" in stop_body, (
        "stopSidebarPolling() does not clear the handle it holds. A "
        "stale handle cannot be stopped a second time, and the guard "
        "that decides whether a timer is running reads the handle."
    )


def test_a_hidden_page_starts_no_timer() -> None:
    """A hidden page must not run the sidebar's timer at all.

    The guard belongs in the start function: a timer created while the
    page is hidden would tick on a screen nobody is looking at until
    the next visibility event came along.  The guard must come *before*
    the timer is created — a check that runs after ``setInterval`` has
    already started the timer guards nothing.
    """
    body = _function_body(_source(_APP_JS), "startSidebarPolling")

    guard_index = body.find("document.hidden")
    interval_index = body.find("setInterval(")

    assert guard_index != -1, (
        "startSidebarPolling() never tests document.hidden. A hidden "
        "tab would keep asking the API for a plan list nobody is "
        "looking at."
    )
    assert guard_index < interval_index, (
        "startSidebarPolling() creates the timer before it looks at "
        "document.hidden. The guard runs after the timer exists, so it "
        "guards nothing."
    )
    assert "return" in body[guard_index:interval_index], (
        "startSidebarPolling() tests document.hidden but does not "
        "return before the timer is created. The hidden page would "
        "still get a timer."
    )


def test_a_hidden_page_reads_nothing() -> None:
    """The read itself must be skipped while the page is hidden.

    The guard in the start function covers the timer that was created
    while the page was visible.  The read has its own guard, because
    the timer can still be in flight when the page goes to the
    background, and because every other caller of the refresh goes
    through the same function.
    """
    body = _function_body(_source(_APP_JS), "refreshSidebar")

    guard_index = body.find("document.hidden")
    read_index = body.find("loadSidebar(")

    assert guard_index != -1, (
        "refreshSidebar() does not test document.hidden. A hidden tab "
        "would keep asking the API for a plan list nobody is looking "
        "at."
    )
    assert guard_index < read_index, (
        "refreshSidebar() reads the plan list before it looks at "
        "document.hidden. The hidden page would still be read."
    )
    assert "return" in body[guard_index:read_index], (
        "refreshSidebar() tests document.hidden but does not return "
        "before the read. The hidden page would still be read."
    )


def test_the_timer_reads_the_plan_list_through_the_existing_loader() -> None:
    """The refresh must go through ``loadSidebar()``, and await it.

    The sidebar already has a loader that reads the plan list and
    paints it, with the one failure behaviour the contract asks for: a
    refused read paints 「计划列表加载失败」 and raises no dialog.  A
    refresh that read the list itself would have to re-implement that
    behaviour, and would have to re-implement it correctly on every
    tick.

    The read is awaited: a promise nobody holds is a promise whose
    rejection nobody catches.
    """
    body = _function_body(_source(_APP_JS), "refreshSidebar")

    assert "await loadSidebar(" in body, (
        "refreshSidebar() does not await loadSidebar(). Either the "
        "refresh reads the plan list by another route, or the read is "
        "a promise nobody holds — and a rejected one is an uncaught "
        "exception on the page."
    )


def test_coming_back_to_the_foreground_refreshes_at_once() -> None:
    """Hidden stops the timer; visible reads once and then resumes it.

    Stopping the timer while the page is hidden is only half the
    contract.  The list may have moved on while the page was in the
    background — a plan started from another tab, a run that ended —
    so the operator must not have to wait a whole interval for the
    first read after a long absence.  The read comes first, the timer
    resumes after it.

    The hidden half must read nothing: a read there is a read for a
    screen that is not on display.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "handleSidebarVisibilityChange")

    branch = _hidden_branch(source, "handleSidebarVisibilityChange")
    assert "stopSidebarPolling()" in branch, (
        "the hidden branch of handleSidebarVisibilityChange() does not "
        "stop the timer. A hidden tab would keep polling."
    )
    assert "loadSidebar(" not in branch and "refreshSidebar(" not in branch, (
        "the hidden branch of handleSidebarVisibilityChange() reads the "
        "plan list. A read for a screen that is not on display is a "
        "read nobody asked for."
    )

    refresh_index = body.find("refreshSidebar()")
    start_index = body.find("startSidebarPolling()")

    assert refresh_index != -1, (
        "handleSidebarVisibilityChange() never reads the plan list. The "
        "operator would come back to the list as it was when the page "
        "was hidden."
    )
    assert start_index != -1, (
        "handleSidebarVisibilityChange() never restarts the timer. The "
        "sidebar would refresh once after every return to the "
        "foreground and never again."
    )
    assert refresh_index < start_index, (
        "handleSidebarVisibilityChange() restarts the timer before it "
        "reads the plan list. The first read after a long absence "
        "would then be one whole interval late."
    )


def test_the_visibilitychange_listener_reaches_the_sidebar_handler() -> None:
    """The one ``"visibilitychange"`` listener must call the sidebar's
    handler, and it must be registered outside ``init()``.

    A handler that exists but is never called refreshes nothing, and a
    listener registered inside ``init()`` runs once per registration —
    ``init()`` is re-callable, so the page would read the list and
    start a timer every time the tab is switched.
    """
    source = _source(_APP_JS)

    listener = _visibility_listener(source)
    listener_body = _function_body(source, listener)

    assert "handleSidebarVisibilityChange(" in listener_body, (
        f'the "visibilitychange" listener is {listener}(), which never '
        "calls handleSidebarVisibilityChange(). The sidebar would never "
        "learn that the page came back to the foreground."
    )

    init_body = _function_body(source, "init")
    assert "visibilitychange" not in init_body, (
        "init() registers the \"visibilitychange\" listener. init() is "
        "re-callable, so the listener would run once per registration "
        "— another read and another timer every time the tab is "
        "switched."
    )


def test_the_page_starts_the_sidebar_poll() -> None:
    """The poll must be started where the page starts.

    A timer that is never started leaves the sidebar stale — the very
    thing this gate exists for — and the failure is invisible: the
    sidebar renders correctly on load and then never changes.
    """
    body = _function_body(_source(_APP_JS), "init")

    assert "startSidebarPolling()" in body, (
        "init() does not call startSidebarPolling(). The sidebar would "
        "be read once, when the page loads, and never again."
    )


def test_the_polling_path_raises_no_dialog() -> None:
    """The poll must not turn a refused read into a dialog.

    The contract the sidebar already has: a refused read paints
    「计划列表加载失败」 in the sidebar, with the reason, and raises
    nothing.  A poll that added a dialog would put one on the
    operator's screen every thirty seconds — and a dialog nobody can
    dismiss is a page that cannot be used.
    """
    source = _source(_APP_JS)

    for name in ("refreshSidebar", "startSidebarPolling", "handleSidebarVisibilityChange"):
        body = _function_body(source, name)
        assert "alert(" not in body, (
            f"{name}() raises a dialog. A refused read is painted in "
            "the sidebar by renderSidebarError(); a poll that also "
            "raised a dialog would interrupt the operator every tick."
        )

    assert "location.reload" not in source, (
        "frontend/app.js contains location.reload. A reload throws away "
        "the operator's place in the page — the view they were in, "
        "their scroll position, whatever they had typed — so the "
        "refresh must repaint the sidebar instead."
    )
