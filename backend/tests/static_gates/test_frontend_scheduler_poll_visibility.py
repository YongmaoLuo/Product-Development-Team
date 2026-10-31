"""A hidden page must stop polling, and coming back must refresh at once.

Why this gate exists
--------------------
The scheduler panel polls the read model every five seconds.  That is
the right cadence for a panel somebody is looking at, and the wrong
cadence for a panel nobody is looking at: a browser tab that sits in
the background keeps firing the interval, so the API answers two
requests every five seconds for a view that is not on screen, and the
read model is being read by nobody.

The other half of the same defect is the wait.  A tab that was hidden
for a minute and comes back shows the state from the moment it was
hidden, and the operator has to wait up to a full interval before the
panel catches up — on the very screen where they are now looking for
the answer.  Coming back has to refresh immediately, not on the next
tick.

Both halves are decided by the same event, ``visibilitychange``, and
both are invisible to a test that only looks at the rendered page.  So
this gate pins the shape of the source instead:

* ``refreshScheduler()`` refuses to read anything while the page is
  hidden — the guard is in the refresh itself, so *every* caller is
  covered, not just the timer.
* ``handleSchedulerVisibilityChange()`` stops the timer when the page
  is hidden, and on the way back it refreshes once and then restarts
  the timer.
* The listener is registered once at the top level of the module.  It
  is not registered inside ``init()``: ``init()`` is called from
  ``DOMContentLoaded`` but it is also re-callable, and a listener
  registered per call would fire the handler more than once per
  visibility change.
* The manual refresh button is untouched: the timer is not a
  replacement for the button, and the button is the one that answers
  immediately.

The panel reads the read model alone
-------------------------------------
The 「可操作的计划」 block was a superset of the two blocks that
remain, and it is gone from the markup; the loader that filled it went
with it.  The poll reads ``/scheduler/state`` alone, so the rules above
are pinned against that one read.

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
# body-slicing helpers of the sibling scheduler gate are imported
# instead of re-implemented: two slicers would be two judgements about
# the same file, and they would drift.
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
    must do no work at all: stopping the timer is allowed, asking the
    API is not.

    Raises :class:`AssertionError` when the ``document.hidden`` test is
    absent, so a handler that never looks at the page's visibility
    fails loudly instead of matching nothing and passing.
    """
    body = _function_body(source, function_name)

    marker = body.find("document.hidden")
    assert marker != -1, (
        f"{function_name}() never tests document.hidden. The page's "
        "visibility is what decides whether the panel polls at all."
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


def _binding_body(source: str, selector: str) -> str:
    """Return the text of the ``safeBind("<selector>", ...)`` block."""
    marker = f'safeBind("{selector}"'
    start = source.find(marker)
    assert start != -1, f'safeBind("{selector}") not found in the source'
    end = source.find('safeBind("', start + len(marker))
    if end == -1:
        end = len(source)
    return source[start:end]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_refresh_skips_a_hidden_page() -> None:
    """``refreshScheduler()`` must not read anything while the page is hidden.

    The guard belongs in the refresh itself rather than in the timer,
    so that every caller is covered: the interval, the manual refresh
    and the refresh that happens on the way back from hidden.  A guard
    in the timer alone would leave the manual refresh reading a panel
    that is not on screen.
    """
    body = _function_body(_source(_APP_JS), "refreshScheduler")

    assert "document.hidden" in body, (
        "refreshScheduler() does not test document.hidden. A hidden tab "
        "would keep asking the API for a panel nobody is looking at."
    )


def test_visibilitychange_restarts_the_timer() -> None:
    """Coming back from hidden must refresh once and then restart the timer.

    The order is the whole contract:

    1. ``document.hidden`` — the handler branches on it first, because
       the two directions do opposite things.
    2. ``stopSchedulerPolling()`` — the hidden direction stops the
       timer.  Stopping after starting would leave a tick in flight.
    3. ``refreshScheduler()`` — the visible direction refreshes
       immediately, so the operator does not wait out an interval to
       see the state they came back for.
    4. ``startSchedulerPolling()`` — and only then the timer is
       restarted, so the cadence continues from the moment of return.

    The listener must also be wired at all: a handler nobody calls
    never stops a timer and never refreshes.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "handleSchedulerVisibilityChange")

    hidden_index = body.find("document.hidden")
    stop_index = body.find("stopSchedulerPolling()")
    refresh_index = body.find("refreshScheduler()")
    start_index = body.find("startSchedulerPolling()")

    assert hidden_index != -1, (
        "handleSchedulerVisibilityChange() never tests document.hidden. "
        "The handler cannot tell 'the tab was hidden' from 'the tab is "
        "back' without it."
    )
    assert stop_index != -1, (
        "handleSchedulerVisibilityChange() never calls "
        "stopSchedulerPolling(). A hidden tab would keep its timer and "
        "keep asking the API about a panel nobody is looking at."
    )
    assert refresh_index != -1, (
        "handleSchedulerVisibilityChange() never calls "
        "refreshScheduler(). Coming back to the tab would show the state "
        "from the moment it was hidden, until the next tick."
    )
    assert start_index != -1, (
        "handleSchedulerVisibilityChange() never calls "
        "startSchedulerPolling(). The cadence would not resume after "
        "the tab comes back."
    )

    assert hidden_index < stop_index, (
        "handleSchedulerVisibilityChange() tests document.hidden after it "
        "stops the timer. The handler has to branch on visibility first, "
        "because the two directions do opposite things."
    )
    assert stop_index < refresh_index, (
        "handleSchedulerVisibilityChange() refreshes before it stops the "
        "timer. The old timer has to be stopped first, or a tick that "
        "was already in flight lands on top of the refresh."
    )
    assert refresh_index < start_index, (
        "handleSchedulerVisibilityChange() restarts the timer before it "
        "refreshes. The refresh is the answer to coming back; the timer "
        "is what keeps the panel current afterwards. The refresh comes "
        "first."
    )

    assert 'document.addEventListener("visibilitychange"' in source, (
        'frontend/app.js never registers a "visibilitychange" listener. '
        "handleSchedulerVisibilityChange() exists but is never called, so "
        "the page keeps polling while it is hidden and shows stale state "
        "when it comes back."
    )


def test_hidden_page_stops_the_timer_without_asking_the_api() -> None:
    """The hidden direction stops the timer and asks for nothing.

    Stopping the timer is the whole job of the hidden direction.  A
    request issued on the way to hidden is a request for a panel that
    is about to stop being drawn — and the loader the refresh uses is
    exactly the one that must not run here.
    """
    branch = _hidden_branch(_source(_APP_JS), "handleSchedulerVisibilityChange")

    assert "stopSchedulerPolling()" in branch, (
        "the hidden branch of handleSchedulerVisibilityChange() does not "
        "stop the timer. The interval would keep firing on a tab the "
        "operator is not looking at."
    )
    assert "refreshScheduler(" not in branch, (
        "the hidden branch of handleSchedulerVisibilityChange() calls "
        "refreshScheduler(). Hiding the tab would issue the very "
        "requests the pause is meant to stop."
    )
    assert "loadSchedulerState(" not in branch, (
        "the hidden branch of handleSchedulerVisibilityChange() calls "
        "loadSchedulerState(). Hiding the tab would issue the very "
        "request the pause is meant to stop."
    )
    # No loader at all may run on the way to hidden: the pause is about
    # the requests that are not needed, and every one of them is a
    # request for a panel that is not on screen.
    loaders = re.findall(r"loadScheduler\w*\(", branch)
    assert not loaders, (
        "the hidden branch of handleSchedulerVisibilityChange() calls "
        f"{loaders!r}. Hiding the tab would issue the very requests the "
        "pause is meant to stop."
    )


def test_view_that_is_not_on_screen_does_not_start_a_timer() -> None:
    """Coming back to the tab while another view is on screen starts nothing.

    The page being visible is not the same as the scheduler panel being
    visible: the operator can come back to the tab while sitting on the
    plans view.  Polling then would refresh a panel that is not drawn,
    so the visibility of the view is checked before the timer is
    started.
    """
    body = _function_body(
        _source(_APP_JS), "handleSchedulerVisibilityChange"
    )

    visible_index = body.find("schedulerViewVisible()")
    refresh_index = body.find("refreshScheduler()")
    start_index = body.find("startSchedulerPolling()")

    assert visible_index != -1, (
        "handleSchedulerVisibilityChange() never calls "
        "schedulerViewVisible(). Coming back to the tab while another "
        "view is on screen would start a timer for a panel that is not "
        "drawn."
    )
    assert visible_index < refresh_index, (
        "handleSchedulerVisibilityChange() refreshes before it checks "
        "whether the scheduler view is the one on screen."
    )
    assert visible_index < start_index, (
        "handleSchedulerVisibilityChange() starts the timer before it "
        "checks whether the scheduler view is the one on screen. The "
        "timer would poll a panel that is not drawn."
    )


def test_visibility_listener_is_registered_once_outside_init() -> None:
    """The listener is registered once, at the top level of the module.

    ``init()`` is reachable more than once — it is bound to
    ``DOMContentLoaded`` today and it is the obvious place to put
    everything that has to happen at startup.  A listener registered
    inside it is registered again on every call, and the handler then
    runs twice per visibility change: the second run refreshes again
    and starts a second timer on top of the first.

    Registering at the top level means the handler is wired exactly
    once, no matter how often ``init()`` runs.
    """
    source = _source(_APP_JS)

    registrations = source.count('document.addEventListener("visibilitychange"')
    assert registrations == 1, (
        "frontend/app.js registers the \"visibilitychange\" listener "
        f"{registrations} times. Each registration is another handler "
        "running per visibility change — another refresh and another "
        "timer."
    )

    init_body = _function_body(source, "init")
    assert "visibilitychange" not in init_body, (
        'init() registers the "visibilitychange" listener. init() is '
        "re-callable, so the handler would be registered again on every "
        "call and would fire more than once per visibility change. The "
        "registration belongs at the top level of the module."
    )


def test_manual_refresh_button_is_untouched() -> None:
    """The manual refresh button still reads the read model.

    The pause is about a tab that is not being looked at.  The button
    is the opposite: it is the operator asking for the current state,
    right now.  Adding a timer does not take that away, and the button
    keeps working exactly as it did before the poll existed.
    """
    source = _source(_APP_JS)

    body = _binding_body(source, "#btn-refresh-scheduler")
    assert "loadSchedulerState(" in body, (
        'the "#btn-refresh-scheduler" binding no longer reads the read '
        "model. The manual refresh is the button that answers "
        "immediately; the timer does not replace it."
    )
    assert "loadSchedulerPlans(" not in body, (
        'the "#btn-refresh-scheduler" binding reads the deleted plans '
        "block. The 「可操作的计划」 block is gone from the markup, so "
        "the read finds nothing and the panel throws on the next line."
    )
