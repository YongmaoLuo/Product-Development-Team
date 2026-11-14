"""End-to-end: the scheduler panel's cards fold, open, and fold again.

What this case is for
---------------------
The panel draws each plan as a **card**, and the card is written folded:
it says so with ``data-expanded="false"`` and the stylesheet keeps the
detail out of sight.  The plan's progress lines are asked for when the
operator opens the card — and only then.  That is the whole point of
the design, and it is a claim about **requests**, not about pixels: a
folded card must cost nothing, or every five-second poll would spend a
request per plan on lines nobody asked for.

A claim like that cannot be checked by reading the page.  A card that
fetches its detail up front still *looks* folded: the detail is not in
sight, the attribute says ``false``, and the screenshot is exactly what
a folded card should look like.  The only thing that tells the two
apart is the request log — what the page asked the server for, and
when.

So this case drives a real browser against a real server and reads the
requests off the wire.  Three things are checked:

* a card that was never opened costs nothing — the page loads, the
  panel opens, the panel polls at least once, and **no** detail request
  is ever made;
* opening a card asks for that card's detail, exactly once, and the
  answer is written into the card;
* folding the card again changes nothing but the card: the read model
  the server serves is the same before and after.

The last one is the reason folding is worth anything.  A fold that
wrote to the server would be a second writer of the plan's state, and
the read model would move under the operator's feet because they
looked at a card.

How the requests are counted
----------------------------
The panel redraws itself every five seconds, and a card that is
**open** is asked for again once its answer has gone stale — an open
card is a window on what the plan is doing *now*, and a window that
never catches up is a picture of the past.  So the number of detail
requests in a whole session says nothing on its own: it depends on how
long the card happened to be open.

What is counted here is therefore a **window**, not a total: the
requests made between one click and the next.  The window of the click
that opens the card must hold exactly one detail request — that one is
the click's own.  The window of the click that folds it again must hold
none: the fold is a front-end state, and it asks the server for
nothing.  A request that arrives between the two clicks belongs to the
panel's own refresh, and is not counted against either.

The fold's window is the one that could be mistaken, so it is worth
saying why it is safe.  The panel asks for an **open** card's detail
again once that answer has gone stale; a **folded** card is not open,
so once the fold is through the panel cannot ask for that card's
detail at all.  Whatever the fold asks for, it asks for in the moment
the fold happens — and the window waits long enough to catch it.

How the case is run
-------------------
The browser is driven by a separate process, the way the sibling
end-to-end cases in this directory drive it: the driver script is
written into the world's own directory and run by an interpreter that
can import Playwright.  A machine without a Playwright driver **skips**
the case — a panel that was not driven proves nothing about the panel,
and a case that quietly passes without a browser would be a green lie.

Nothing is left behind either: the world is torn down at the end of the
case, and the browser is closed by the driver whether the flow
finished or raised.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (
    PLAN_IDS,
    _World,
    _driver_env,
    _playwright_interpreter,
    _wait_for_settle,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The paths
# ---------------------------------------------------------------------------

#: ``<repo>/backend`` — this file lives at ``backend/tests/e2e/plan_scheduler/``.
BACKEND_DIR: Path = Path(__file__).resolve().parents[3]

#: Where the screenshots are kept.  They are the evidence of the run, not
#: an input: nothing in the repository reads them.
#:
#: The directory is the suite's own ``artifacts`` tree, which
#: ``.gitignore`` keeps out of the index on purpose: a screenshot is a
#: runtime output, and a binary blob in a commit stops the
#: commit-range privacy scan (see
#: ``tests/static_gates/test_e2e_artifacts_are_not_tracked.py``).
ARTIFACT_DIR: Path = BACKEND_DIR / "tests" / "e2e" / "artifacts" / "e2e"

#: The screenshot of the folded card — what the panel looks like before
#: anybody touched it.
COLLAPSED_SHOT: Path = ARTIFACT_DIR / "scheduler-card-collapsed.png"

#: The screenshot of the open card — what the panel looks like once the
#: operator has opened it.
EXPANDED_SHOT: Path = ARTIFACT_DIR / "scheduler-card-expanded.png"


# ---------------------------------------------------------------------------
# The driver — the panel, as an operator uses it
# ---------------------------------------------------------------------------

_COLLAPSE_DRIVER_SOURCE = r'''
"""Open the panel's cards, fold them, open them and fold them again.

Arguments:
  1. the base url of the running server
  2. the mode: ``load`` | ``flow`` | ``state``
  3. the directory the screenshots are written into

The three modes
---------------
``load``
    Open the panel and leave it alone for one of the panel's own poll
    intervals.  Nothing is clicked.  The report says what the page
    asked for while it was doing that — which is the only way to show
    that a folded card costs nothing.

``flow``
    The three steps: the card as it was drawn (folded), the card after
    the operator opened it, and the card after the operator folded it
    again.  A screenshot is taken of the first two.

``state``
    Read the server's read model, open a card, fold it again, and read
    the read model once more.  Folding is a front-end state, so the two
    reads must agree.

What the report holds
---------------------
Everything the driver read off the page or off the API, and nothing it
meant to do.  Every request the page made is recorded with the moment
it was made, because *when* a request was made is half of what this
case is about: a detail request that happened before the click is a
different failure from one that happened after it.

The requests are also reported as **windows** — the ones made before
the click that opens a card, the ones the opening itself caused, and
the ones the folding caused.  The panel asks for an open card's detail
again once it has gone stale, so a whole session's total says nothing
about what a click did; a window does.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GUARD_HEADER = "x-pdt-request"
GUARD_VALUE = "1"

#: The read model the panel polls.
STATE_PATH = "/api/scheduler/state"

#: The running plans, as the panel draws them: one card per plan.
RUNNING_CARD_SELECTOR = "#scheduler-running-cards .scheduler-plan-card"

#: The card's own two parts.
HEADER_SELECTOR = ".scheduler-plan-header"
DETAIL_SELECTOR = ".scheduler-plan-detail"

#: How long the panel is left alone in the ``load`` mode, in
#: milliseconds.  The panel reads the model every five seconds, so six
#: seconds is more than one of its intervals: a page that fetched a
#: detail up front would have shown it by then, and a page that fetched
#: one on every repaint would have shown that too.
LOAD_WAIT_MS = 6000

#: How long the driver waits for the detail's text to arrive, in
#: seconds.  The detail becomes visible the moment the card is opened;
#: the text arrives with the answer, which is a separate moment.
TEXT_TIMEOUT = 30.0

#: How long the driver waits after the card has been folded, in
#: milliseconds, before it counts the requests the fold caused.
#:
#: The wait makes the window cover a request the fold might have
#: started and not finished.  It cannot be confused with the panel's
#: own refresh: a repaint asks for an **open** card's detail again,
#: and a folded card is not open — so once the fold is through, the
#: panel cannot ask for this card's detail at all.
FOLD_SETTLE_MS = 800


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot
    launch anything.  The binaries that *are* installed are found here
    instead — nothing is downloaded, and nothing is hard-coded: the
    cache lives under the home the run was launched with.
    """
    home = Path.home()
    roots = [
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
    ]
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern)):
                if candidate.exists():
                    return str(candidate)
    return None


def detail_requests(log):
    """The detail requests the page made, in the order it made them.

    A request is a detail request when its path is the detail
    endpoint's own — ``/api/scheduler/plan/<id>/detail``.  The query
    string is dropped first, so a request that carried one is still
    counted as what it is.
    """
    found = []
    for entry in log:
        path = entry["url"].split("?")[0]
        if "/api/scheduler/plan/" in path and path.endswith("/detail"):
            found.append(entry)
    return found


def state_shape(document):
    """The read model, read as the plans it is about.

    The clocks are left out on purpose.  ``elapsed_sec`` and
    ``running_sec`` count on their own, so two reads of the same
    running plan never agree — comparing them would compare two
    different moments rather than two answers about the same state.
    What must not change is *which* plans are running, *which* are
    waiting, and in what order; that is what the shape keeps.
    """
    data = document if isinstance(document, dict) else {}
    running = data.get("running")
    queue = data.get("queue")
    running = running if isinstance(running, list) else []
    queue = queue if isinstance(queue, list) else []
    return {
        "running": [
            {
                "plan_id": str(row.get("plan_id")),
                "phase": row.get("phase"),
            }
            for row in running
            if isinstance(row, dict)
        ],
        "queue": [
            {
                "plan_id": str(row.get("plan_id")),
                "position": row.get("position"),
                "phase": row.get("phase"),
            }
            for row in queue
            if isinstance(row, dict)
        ],
        "queue_length": data.get("queue_length"),
    }


def card_state(page):
    """What the first running card says about itself."""
    card = page.locator(RUNNING_CARD_SELECTOR).first
    detail = card.locator(DETAIL_SELECTOR)
    visible = bool(detail.is_visible())
    return {
        "plan_id": (card.get_attribute("data-plan-id") or "").strip(),
        "expanded": (card.get_attribute("data-expanded") or "").strip(),
        "detail_visible": visible,
        "detail_text": detail.inner_text().strip() if visible else "",
        "card_count": page.locator(RUNNING_CARD_SELECTOR).count(),
    }


def open_card(page):
    """Open the first running card and wait for its detail to arrive.

    The detail becomes visible the moment the card is opened; its text
    arrives with the answer, which is a request away.  Both are waited
    for, because a card that is open and empty has proved nothing.

    The plan the card is about is read **before** the click and returned
    with the text.  The answer that came back is then matched against
    the card that was actually opened, rather than against a card the
    panel happened to draw first a moment earlier.
    """
    card = page.locator(RUNNING_CARD_SELECTOR).first
    plan_id = (card.get_attribute("data-plan-id") or "").strip()
    card.locator(HEADER_SELECTOR).click()
    detail = card.locator(DETAIL_SELECTOR)
    detail.wait_for(state="visible", timeout=30000)
    deadline = time.time() + TEXT_TIMEOUT
    while True:
        text = detail.inner_text().strip()
        if text:
            return plan_id, text
        if time.time() >= deadline:
            raise AssertionError("the detail never carried any text")
        page.wait_for_timeout(100)


def close_card(page):
    """Fold the first running card again, and wait until it is folded.

    The moment of the click is returned: every request the page made
    from then on was made because of the fold.
    """
    card = page.locator(RUNNING_CARD_SELECTOR).first
    clicked_at = time.time()
    card.locator(HEADER_SELECTOR).click()
    card.locator(DETAIL_SELECTOR).wait_for(state="hidden", timeout=30000)
    return clicked_at


def read_state(page, base):
    """The read model, as the server answers it.

    Read through the browser's own API context, with the guard's
    header — the same request the panel makes, so the two are answered
    by the same server.
    """
    answer = page.request.get(
        base.rstrip("/") + STATE_PATH,
        headers={GUARD_HEADER: GUARD_VALUE},
    )
    return answer.json()


base = sys.argv[1]
mode = sys.argv[2]
shots = Path(sys.argv[3])
shots.mkdir(parents=True, exist_ok=True)

#: Every request the page made, with the moment it made it.
log = []

report = {"mode": mode}

with sync_playwright() as driver:
    executable = browser_executable()
    launch_kwargs = {"executable_path": executable} if executable else {}
    browser = driver.chromium.launch(**launch_kwargs)
    try:
        page = browser.new_page()

        def on_request(request):
            log.append({"url": request.url, "at": round(time.time(), 3)})

        page.on("request", on_request)

        page.goto(base, wait_until="load")
        page.wait_for_selector("#app", timeout=30000)

        # The one click the operator makes to get here.
        page.click("#btn-open-scheduler")
        page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)

        # The panel draws its running plans as cards; the driver waits
        # for one, so that what is read is a panel that has drawn them.
        page.wait_for_selector(RUNNING_CARD_SELECTOR, timeout=30000)

        if mode == "load":
            # The card as it was drawn, before the panel is left alone.
            report["cards"] = card_state(page)
            page.wait_for_timeout(LOAD_WAIT_MS)
            report["cards_after"] = card_state(page)
            report["detail_requests"] = detail_requests(log)

        elif mode == "flow":
            report["collapsed"] = card_state(page)
            page.screenshot(
                path=str(shots / "scheduler-card-collapsed.png"), full_page=True
            )

            # The window of the click that opens the card: what the
            # page had asked for before the operator touched anything,
            # and what it asked for because of the touch.
            before_expand = len(log)
            report["opened_plan_id"], report["detail_text"] = open_card(page)
            after_expand = len(log)
            report["detail_requests_before_expand"] = detail_requests(
                log[:before_expand]
            )
            report["detail_requests_at_expand"] = detail_requests(
                log[before_expand:after_expand]
            )
            report["expanded"] = card_state(page)

            page.screenshot(
                path=str(shots / "scheduler-card-expanded.png"), full_page=True
            )

            # The window of the click that folds it again.  The panel
            # may have asked for the stale detail in between — that is
            # its own refresh, and it belongs to neither window.  Once
            # the card is folded the panel cannot ask for its detail
            # again, so this window is the fold's alone.
            before_fold = len(log)
            report["folded_at"] = close_card(page)
            page.wait_for_timeout(FOLD_SETTLE_MS)
            after_fold = len(log)
            report["detail_requests_at_fold"] = detail_requests(
                log[before_fold:after_fold]
            )
            report["refolded"] = card_state(page)

        elif mode == "state":
            report["state_before"] = state_shape(read_state(page, base))
            before_expand = len(log)
            open_card(page)
            after_expand = len(log)
            before_fold = len(log)
            report["folded_at"] = close_card(page)
            page.wait_for_timeout(FOLD_SETTLE_MS)
            after_fold = len(log)
            report["state_after"] = state_shape(read_state(page, base))
            report["detail_requests_at_expand"] = detail_requests(
                log[before_expand:after_expand]
            )
            report["detail_requests_at_fold"] = detail_requests(
                log[before_fold:after_fold]
            )
            report["detail_requests"] = detail_requests(log)

        else:
            raise AssertionError("unknown mode: " + repr(mode))

        report["all_requests"] = log
        print(json.dumps(report))
    finally:
        browser.close()
'''


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


def _shutdown(world: _World) -> None:
    """Shut the world down, and prove that nothing is left behind.

    The world's own teardown reads the process table with ``ps`` — a
    setuid binary that some locked-down machines refuse to execute.
    Where it cannot be executed the teardown cannot reach its own kills
    either, because it reads the table before it signals anything, and
    the world would be left running: a server and four executors, for
    every run.

    So the fallback below does the same work from the facts the world
    keeps — the pid each executor's own record holds.
    """
    try:
        world.teardown()
        return
    except PermissionError:
        pass

    # The server's group first: it leads the session the executors were
    # spawned into.
    if world.proc is not None:
        try:
            os.killpg(os.getpgid(world.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            world.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        world.proc = None

    # Each executor leads a group of its own.
    for row in world.exec_rows():
        pid = row.get("exec_pid")
        if not pid:
            continue
        pid = int(pid)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    handle = getattr(world, "_log_handle", None)
    if handle is not None:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 — closing a log must not fail a test
            pass
        world._log_handle = None


@pytest.fixture
def card_world(tmp_path):
    """A running server, its own database, its own plans, and a browser.

    The browser is looked for **before** the server is booted: a
    machine without a Playwright driver cannot be shown the panel, and
    a server booted for a case that is about to skip is a server that
    was started for nothing.
    """
    if _playwright_interpreter() is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "panel cannot be driven, and a panel that was not driven "
            "proves nothing about the panel"
        )

    the_world = _World(tmp_path)
    try:
        the_world.start()
        _wait_for_settle(the_world)
        yield the_world
    finally:
        _shutdown(the_world)


def _drive(world: _World, mode: str) -> Dict[str, Any]:
    """Drive the panel in *mode* and report what it did.

    The report is what the panel drew and what the page asked for,
    read off the page and off the wire by a separate process — one
    line of JSON, printed by the driver and read back here.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "panel cannot be driven, and a panel that was not driven "
            "proves nothing about the panel"
        )

    script = world.root / "drive_card_collapse.py"
    script.write_text(_COLLAPSE_DRIVER_SOURCE, encoding="utf-8")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/",
            mode,
            str(ARTIFACT_DIR),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if (
            "Executable doesn't exist" in combined
            or "playwright install" in combined
            or "BrowserType.launch" in combined
        ):
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not; the panel cannot be driven"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _detail_urls(requests: List[Dict[str, Any]]) -> List[str]:
    """The detail requests of a report, as plain paths."""
    return [str(entry["url"]).split("?")[0] for entry in requests]


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_card_collapse_flow(card_world):
    """A card is folded, is opened, and is folded again.

    The three steps the panel owes the operator: the card is drawn
    folded, the operator opens it and the plan's detail arrives, and
    the operator folds it again and the card is folded.
    """
    report = _drive(card_world, "flow")

    # -- the card as it was drawn -------------------------------------------

    collapsed = report["collapsed"]
    assert collapsed["card_count"] >= 1, (
        f"the panel drew no running card at all: {collapsed!r}"
    )
    assert collapsed["plan_id"], (
        f"the first running card does not name its plan: {collapsed!r}"
    )
    assert collapsed["plan_id"] in PLAN_IDS, (
        f"the first running card is about {collapsed['plan_id']!r}, which "
        f"is not one of the plans this world imported: {PLAN_IDS!r}"
    )
    assert collapsed["expanded"] == "false", (
        f"the card was drawn open: {collapsed!r}"
    )
    assert collapsed["detail_visible"] is False, (
        f"the detail of a folded card was on screen: {collapsed!r}"
    )

    # -- and nothing was asked for before the operator opened anything -----

    assert report["detail_requests_before_expand"] == [], (
        "the page asked for a detail before the operator opened "
        "anything: "
        f"{_detail_urls(report['detail_requests_before_expand'])!r}"
    )

    # -- and opening it is what asks for the detail ------------------------

    opened = report["opened_plan_id"]
    assert opened, (
        "the card that was opened does not name its plan: "
        f"{report['expanded']!r}"
    )
    assert opened in PLAN_IDS, (
        f"the card that was opened is about {opened!r}, which is not one "
        f"of the plans this world imported: {PLAN_IDS!r}"
    )

    at_expand = report["detail_requests_at_expand"]
    assert len(at_expand) == 1, (
        "opening a card asked for "
        f"{len(at_expand)} details, not one: "
        f"{_detail_urls(at_expand)!r}"
    )
    assert _detail_urls(at_expand)[0].endswith(
        f"/api/scheduler/plan/{opened}/detail"
    ), (
        "the detail that was asked for is not the one of the card that "
        f"was opened: {_detail_urls(at_expand)[0]!r} for "
        f"{opened!r}"
    )

    expanded = report["expanded"]
    assert expanded["expanded"] == "true", (
        f"the card did not say it was open: {expanded!r}"
    )
    assert expanded["detail_visible"] is True, (
        f"the detail of an open card was not on screen: {expanded!r}"
    )
    assert report["detail_text"].strip(), (
        "the detail was on screen and said nothing"
    )

    # -- and folding it again is free ---------------------------------------

    at_fold = report["detail_requests_at_fold"]
    assert at_fold == [], (
        "folding a card asked for another detail: "
        f"{_detail_urls(at_fold)!r}"
    )

    refolded = report["refolded"]
    assert refolded["expanded"] == "false", (
        f"the card did not fold again: {refolded!r}"
    )
    assert refolded["detail_visible"] is False, (
        f"the detail of a folded card was on screen again: {refolded!r}"
    )

    # -- the evidence --------------------------------------------------------

    assert COLLAPSED_SHOT.is_file(), (
        f"the screenshot of the folded card is not there: {COLLAPSED_SHOT}"
    )
    assert EXPANDED_SHOT.is_file(), (
        f"the screenshot of the open card is not there: {EXPANDED_SHOT}"
    )


def test_no_detail_request_before_expand(card_world):
    """A card nobody opened costs nothing.

    The page is loaded and the scheduler view is opened, and then the
    panel is left alone for longer than one of its own poll intervals.
    A panel that fetched the details up front — or that fetched them
    again on every repaint — would have asked by then.
    """
    report = _drive(card_world, "load")

    assert report["detail_requests"] == [], (
        "the page asked for a detail without the operator opening "
        "anything: "
        f"{_detail_urls(report['detail_requests'])!r}"
    )

    cards = report["cards"]
    assert cards["card_count"] >= 1, (
        f"the panel drew no running card at all: {cards!r}"
    )
    assert cards["expanded"] == "false", (
        f"the card was drawn open: {cards!r}"
    )
    assert cards["detail_visible"] is False, (
        f"the detail of a folded card was on screen: {cards!r}"
    )

    # The card was still folded after the panel had repainted itself.
    after = report["cards_after"]
    assert after["card_count"] >= 1, (
        f"the panel drew no running card after its own repaint: {after!r}"
    )
    assert after["expanded"] == "false", (
        f"the card came open by itself: {after!r}"
    )
    assert after["detail_visible"] is False, (
        f"the detail of a folded card was on screen after the repaint: "
        f"{after!r}"
    )


def test_collapse_does_not_change_state(card_world):
    """Folding a card does not move the server's state.

    The read model is read before the card is opened and again after it
    is folded.  Folding is a front-end state: the same plans are
    running, the same plans are waiting, in the same order.
    """
    report = _drive(card_world, "state")

    before = report["state_before"]
    after = report["state_after"]

    assert before["running"], (
        f"the read model held no running plan to compare: {before!r}"
    )
    assert after["running"] == before["running"], (
        "the running plans changed while a card was opened and folded "
        f"again: {before['running']!r} -> {after['running']!r}"
    )
    assert after["queue"] == before["queue"], (
        "the line changed while a card was opened and folded again: "
        f"{before['queue']!r} -> {after['queue']!r}"
    )
    assert after["queue_length"] == before["queue_length"], (
        "the length of the line changed while a card was opened and "
        f"folded again: {before['queue_length']!r} -> "
        f"{after['queue_length']!r}"
    )

    # The card really was opened: the click asked for the detail once.
    at_expand = report["detail_requests_at_expand"]
    assert len(at_expand) == 1, (
        "the card was not opened exactly once: "
        f"{_detail_urls(at_expand)!r}"
    )

    # And the fold that followed asked for nothing.
    at_fold = report["detail_requests_at_fold"]
    assert at_fold == [], (
        "folding the card asked for another detail: "
        f"{_detail_urls(at_fold)!r}"
    )
