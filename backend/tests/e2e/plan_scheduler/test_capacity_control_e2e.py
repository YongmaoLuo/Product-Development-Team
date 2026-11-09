"""End-to-end: the capacity control, driven the way an operator drives it.

What this file is for
---------------------
The capacity control has one rule: **the number in force is the
server's number**.  Every layer of that rule has a test of its own —
the store writes the override, the engine reads it, the panel draws it
— and every one of them can be green while the *product* is broken,
because the layers never meet in one run:

* a poll that wipes the operator's pick before it is applied loses the
  operator's number without a word: the operator presses 「应用」, the
  server's **old** number is written, and the override that lands is
  not the one that was picked;
* a pick that survives the switch away from the view is a pick the
  operator left behind, and it can be applied long after they went;
* an apply that draws the pick instead of the server's answer puts a
  number on the panel that the scheduler is not running with.

None of that is visible to a test that calls the JavaScript directly.
This file asks the question against a running deployment: a real
server, a real browser, and the real panel.

The chain
---------
1. the server is a child process, with its own database and its own
   plans — six of them against a capacity of four, so two are waiting
   and there is something to evict when the capacity comes down;
2. the operator opens the scheduling view and the control shows the
   capacity that is in force;
3. the operator picks a number and **does not** press 「应用」 — the
   panel's own five-second poll comes round, and the pick is still
   there;
4. the operator leaves the view and comes back — the pick is gone, and
   the control shows the number the server is running with;
5. the operator picks a number and presses 「应用」 — the control and
   the numbers beside it are the server's number, and the server says
   the same;
6. a screenshot is written to disk — the evidence.

What is real here
-----------------
* the server is a **process** (a child ``uvicorn``, killed by pid and
  by process group);
* the state is a **SQLite file** on disk, read back by this case and
  never written by it;
* the plans are **directories**;
* the executor is the stand-in the sibling suite installs through the
  named production seam (``PDT_EXECUTOR_CMD``) — the way the sibling
  cases do it;
* the page is a **real browser** (Playwright, when a driver is
  installed), clicking the controls an operator clicks.

The world is the sibling's
--------------------------
The server process, the temporary state database, the plan-import seam
and the Playwright discovery all belong to the sibling case's
``_World``, and this file reuses it rather than writing its own.  Two
worlds that differ would be two different answers to one question.

What the environment cannot give the test
-----------------------------------------
A browser.  When no Playwright driver is installed — or when the driver
is installed but its browser binaries are not — the case is **skipped**:
a control that was not driven proves nothing about the control, and a
test that pretends to have driven it is worse than no test at all.

What this case does not claim
-----------------------------
The case claims what the read model and the scheduler's own tables say
about **each other**.  It does not claim that every plan that was
imported is either running or waiting once the capacity has come down.
A plan that was interrupted for the capacity and whose stop the reaper
could not prove clean is in neither list: its process is gone, so it
holds no slot, and its phase was left where it was, so it is not in the
line either.  That gap belongs to the product, and it is reported
where it was found rather than papered over with a weaker assertion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

#: ``<repo>/backend`` — this file lives at ``backend/tests/e2e/plan_scheduler/``.
_BACKEND_DIR: Path = Path(__file__).resolve().parents[3]

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The sibling is reused **as it is**: the world (the real server, its own
# database, its own plans), the Playwright discovery, the environment the
# browser driver runs under, and the reader the case uses to look at the
# state database without writing to it.
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (  # noqa: E402
    PLAN_IDS,
    _driver_env,
    _playwright_interpreter,
    _rows,
    _wait_for_settle,
    _World,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: Where the evidence is written.  The path is the one the case was
#: told to use; it is not a path into anybody's home directory, and
#: nothing in the repository depends on it — the screenshot is
#: evidence, not an input.
E2E_ROOT: Path = Path(
    os.environ.get("PDT_CAPACITY_E2E_ROOT", "/tmp/capacity-control-e2e")
)

#: The screenshot the panel leaves behind.
SCREENSHOT_PATH: Path = E2E_ROOT / "after-apply.png"

#: The capacity the world is configured with.  Six plans against a
#: capacity of four leaves two waiting — and a waiting plan is what
#: makes the line, and the eviction that follows the capacity coming
#: down, observable at all.
INITIAL_CAPACITY: int = 4

#: How long a young run is protected.  Zero: a plan that is working
#: may be interrupted the moment the capacity asks for it, which is
#: what this case is about.  A protected run would simply not be
#: evicted, and the case would prove nothing.
MIN_RUNTIME_SECONDS: float = 0

#: How long one tick waits.  Short, so the convergence the case is
#: about happens inside the case's own budget.
TICK_SECONDS: float = 0.05

#: The number the operator picks and does **not** apply.  It is not
#: the number in force, so a control that forgot the pick would show
#: it.
DRAFT_CAPACITY: str = "1"

#: The number the operator picks and applies.  It is not the number
#: in force either, so a control that drew the pick instead of the
#: server's answer would look the same as one that did not.
APPLIED_CAPACITY: str = "2"

#: How long the driver waits for the panel's own poll to come round
#: after the pick.  The panel reads the read model every five seconds,
#: so this is a little over one of its own intervals: the pick has to
#: survive a poll, not a moment.
POLL_CROSS_SECONDS: float = 6.0

#: The ceiling on every wait.  Nothing waits for ever: a control that
#: never gets there has to fail, not hang.
WAIT_TIMEOUT_SECONDS: float = 30.0

#: The source the read model names when the capacity in force is the
#: operator's own number rather than the configured one.
OVERRIDE_SOURCE: str = "override"


# ---------------------------------------------------------------------------
# The browser driver — the capacity control, as an operator uses it
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = r'''
"""Drive the capacity control the way an operator drives it.

Arguments:
  1. the base url of the running server
  2. the path the screenshot is written to
  3. a JSON object: the capacity that is in force, the number the
     operator picks without applying it, the number the operator picks
     and applies, and how long the driver waits for the panel's poll

What it does, in the operator's own order:

  1. open the scheduling view and read the capacity that is in force;
  2. pick a number and **do not** press "apply" — wait out one of the
     panel's own polls, and read the control again;
  3. leave the view and come back — the pick is gone;
  4. pick a number and press "apply" — the control and the numbers
     beside it are the server's number;
  5. leave a screenshot behind.

Every read of the server goes through the browser's own request context
and carries the request guard's header.  The panel's own requests are
watched on their way out, so the report can say whether they carried
the header too.

One JSON document is printed on stdout: what the control showed at
every step, what the server said at every step, and where the
screenshot went.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GUARD_HEADER = "x-pdt-request"
GUARD_VALUE = "1"

STATE_PATH = "/api/scheduler/state"

#: The control, and the two numbers beside it.
SELECT = "#scheduler-capacity-select"
TOTAL = "#scheduler-view-capacity-total"
USED = "#scheduler-view-capacity-used"

#: Nothing waits for ever: every wait has a ceiling.
WAIT_MS = 30000


def read_text(page, selector):
    """The text of one element, or the empty string when it is not there."""
    element = page.query_selector(selector)
    if element is None:
        return ""
    return (element.inner_text() or "").strip()


def select_value(page):
    """What the control shows."""
    return page.eval_on_selector(SELECT, "el => el.value")


def wait_for(page, what, probe, timeout_ms=WAIT_MS):
    """Wait until *probe* says the panel reached *what*.

    *probe* answers a pair: whether the panel got there, and what it
    showed when it did not.  The second half is what the failure
    message quotes — a wait that failed without saying what the panel
    showed is a wait that failed without saying why.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while True:
        reached, seen = probe()
        if reached:
            return seen
        if time.time() >= deadline:
            raise AssertionError(
                "the panel never reached {0} within {1}s; it showed "
                "{2!r}".format(what, timeout_ms / 1000.0, seen)
            )
        page.wait_for_timeout(200)


url = sys.argv[1]
screenshot = Path(sys.argv[2])
options = json.loads(sys.argv[3])
initial_capacity = str(options["initial_capacity"])
draft_capacity = str(options["draft_capacity"])
applied_capacity = str(options["applied_capacity"])
poll_cross_seconds = float(options["poll_cross_seconds"])

screenshot.parent.mkdir(parents=True, exist_ok=True)

seen = []


with sync_playwright() as driver:
    browser = driver.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    def record(route, request):
        """Watch every request the panel's own client makes."""
        seen.append({
            "url": request.url,
            "method": request.method,
            "guard": request.headers.get(GUARD_HEADER),
        })
        route.continue_()

    page.route("**/api/**", record)

    def state_reads():
        """How often the panel has read the read model."""
        return len([e for e in seen if e["url"].endswith(STATE_PATH)])

    def api_state():
        """The read model, read the way the panel's own client reads it."""
        response = page.request.get(
            url.rstrip("/") + STATE_PATH,
            headers={GUARD_HEADER: GUARD_VALUE},
        )
        return response.json()

    def capacity_of(state):
        """The capacity a read model says is in force."""
        return ((state or {}).get("capacity") or {}).get("total")

    def probe_select(expected):
        """Whether the control shows *expected*."""
        def probe():
            value = select_value(page)
            return (value == expected, value)
        return probe

    def probe_text(selector, expected):
        """Whether the element shows *expected*."""
        def probe():
            value = read_text(page, selector)
            return (value == expected, value)
        return probe

    def probe_used_within(limit):
        """Whether the used count is inside *limit*."""
        def probe():
            value = read_text(page, USED)
            reached = value.isdigit() and int(value) <= int(limit)
            return (reached, value)
        return probe

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=WAIT_MS)

    # -- 1. the operator opens the scheduling view -------------------------

    page.click("#btn-open-scheduler", timeout=WAIT_MS)
    page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=WAIT_MS)

    api_initial = api_state()
    # The control follows the server, so the panel has to be given the
    # moment to draw it.  The number waited for is the server's, not a
    # constant: the panel is compared with the server and never with
    # itself.
    wait_for(
        page,
        "the capacity the server is running with",
        probe_select(str(capacity_of(api_initial))),
    )
    initial = {
        "select": select_value(page),
        "total": read_text(page, TOTAL),
        "used": read_text(page, USED),
        "api": api_initial,
    }

    # -- 2. the operator picks a number and does NOT press "apply" ---------

    polls_before = state_reads()
    picked_at = time.time()
    page.select_option(SELECT, draft_capacity)
    # Wait out the panel's own poll: the pick has to be there when the
    # read model comes back.  The wait is for the poll itself — the
    # count of reads has to move — and not for a duration, so a panel
    # that stopped polling is caught as well as a panel that wiped
    # the pick.
    deadline = time.time() + WAIT_MS / 1000.0
    while True:
        polled = (
            state_reads() > polls_before
            and (time.time() - picked_at) >= poll_cross_seconds
        )
        if polled:
            break
        if time.time() >= deadline:
            raise AssertionError(
                "the panel never read the read model again within {0}s of "
                "the pick; it had read it {1} times".format(
                    WAIT_MS / 1000.0, state_reads()
                )
            )
        page.wait_for_timeout(200)
    draft = {
        "select": select_value(page),
        "api": api_state(),
    }

    # -- 3. the operator leaves the view and comes back --------------------

    page.click("#btn-scheduler-back-home", timeout=WAIT_MS)
    page.click("#btn-open-scheduler", timeout=WAIT_MS)
    wait_for(
        page,
        "the capacity the server is running with, after the view switch",
        probe_select(str(capacity_of(draft["api"]))),
    )
    switch = {"select": select_value(page)}

    # -- 4. the operator picks a number and presses "apply" ----------------

    page.select_option(SELECT, applied_capacity)
    page.click("#scheduler-capacity-apply", timeout=WAIT_MS)
    # The number in force afterwards is the server's, so the panel has
    # to be given the moment to read it back.
    wait_for(
        page,
        "the applied capacity on the control",
        probe_select(applied_capacity),
    )
    wait_for(
        page,
        "the applied capacity beside the control",
        probe_text(TOTAL, applied_capacity),
    )
    wait_for(
        page,
        "the used count inside the applied capacity",
        probe_used_within(applied_capacity),
    )
    applied = {
        "select": select_value(page),
        "total": read_text(page, TOTAL),
        "used": read_text(page, USED),
        "api": api_state(),
    }

    page.screenshot(path=str(screenshot), full_page=True)

    report = {
        "initial": initial,
        "draft": draft,
        "switch": switch,
        "apply": applied,
        "requests": seen,
        "screenshot": str(screenshot),
    }
    print(json.dumps(report, ensure_ascii=False))
    browser.close()
'''


# ---------------------------------------------------------------------------
# The world, and the browser that drives the control
# ---------------------------------------------------------------------------


def _write_settings(world: "_World") -> None:
    """Write the settings this case runs against.

    The capacity is four, so six plans leave two waiting; the minimum
    runtime is zero, so a plan that is working may be interrupted the
    moment the capacity asks for it; the tick is short, so the
    convergence the case is about happens inside the case's own budget.

    The file is the backend's own settings file, read through the seam
    the server exposes for exactly this purpose — the sibling case
    writes its settings the same way, and the repository's own file is
    not touched.
    """
    world.settings_path.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: false\n"
        "  capacity: {0}\n"
        "  min_runtime_seconds: {1}\n"
        "  tick_seconds: {2}\n"
        "  command_timeout_seconds: 30\n".format(
            INITIAL_CAPACITY, MIN_RUNTIME_SECONDS, TICK_SECONDS
        ),
        encoding="utf-8",
    )


def _last_document(text: str) -> Dict[str, Any]:
    """The last JSON document in *text*.

    The driver prints one document; the browser's own chatter may sit
    around it, so the last line that parses is the report.
    """
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            return document
    raise AssertionError(f"the driver printed no report: {text!r}")


def _drive(world: "_World", interpreter: str) -> Dict[str, Any]:
    """Drive the capacity control in a real browser and report what it drew."""
    script = world.root / "drive_capacity_control.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/",
            str(SCREENSHOT_PATH),
            json.dumps(
                {
                    "initial_capacity": INITIAL_CAPACITY,
                    "draft_capacity": DRAFT_CAPACITY,
                    "applied_capacity": APPLIED_CAPACITY,
                    "poll_cross_seconds": POLL_CROSS_SECONDS,
                }
            ),
        ],
        capture_output=True,
        text=True,
        timeout=300,
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
    return _last_document(result.stdout)


def _unguarded(requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The requests that went out without the request guard's header."""
    return [entry for entry in requests if entry.get("guard") != "1"]


def _wait_for_the_capacity_to_settle(
    world: "_World",
    total: int,
    timeout: float = WAIT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Wait until the server has taken the capacity that was applied.

    The capacity is written at once, but the plans that are over it are
    interrupted a tick later, and their slots are given back a tick
    after that — so the number of running plans settles after the
    number in force does.  The wait is for the **server** to settle;
    what the panel drew is asserted afterwards, and is not waited for.
    """
    deadline = time.monotonic() + timeout
    state: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = world.scheduler_state()
        capacity = state.get("capacity") or {}
        running = state.get("running") or []
        if (
            int(capacity.get("total") or 0) == int(total)
            and int(capacity.get("used") or 0) <= int(total)
            and int(capacity.get("used") or 0) == len(running)
        ):
            return state
        time.sleep(0.2)
    raise AssertionError(
        f"the server had not settled on a capacity of {total} within "
        f"{timeout}s; the last read model was {state!r}"
    )


@pytest.fixture(scope="module")
def driven_control(tmp_path_factory):
    """A running server, its own database, its own plans — and the browser.

    The world is built once and the browser drives it once: the three
    cases below are three readings of **one** run, not three runs.  That
    is what makes them worth having — the pick that survived the poll
    is the same pick that the applied capacity is read against, and the
    panel and the server are compared at the same moment.

    The instrument is asked for **before** the world is built, so a
    machine without a browser pays nothing: no server is started, no
    plan is run, and the case says out loud that the control was not
    driven.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "capacity control cannot be driven, and a control that was "
            "not driven proves nothing about the control"
        )

    the_world = _World(tmp_path_factory.mktemp("capacity-control"))
    try:
        _write_settings(the_world)
        the_world.start()
        _wait_for_settle(the_world)
        report = _drive(the_world, interpreter)
        yield {"world": the_world, "report": report}
    finally:
        # The world is stopped in a ``finally`` whatever happens: a
        # server that is left running holds the state database, the
        # plans and the executors of every run that comes after.
        the_world.teardown()


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_operator_draft_survives_polling_and_not_the_view_switch(driven_control):
    """The pick is the operator's until it is applied — and only until then.

    Two things have to hold, and they are opposite:

    * the pick survives the panel's own poll — a poll that wiped it
      would lose the operator's number without a word, and the override
      that would land on the server is the server's old number;
    * the pick does **not** survive the switch away from the view — a
      pick that outlives the view can be applied long after the
      operator went, and what the control shows must always be what the
      scheduler runs with.

    The driver ran to the end (it exited zero) and left the screenshot
    behind: a control that was not driven proves nothing.
    """
    report = driven_control["report"]
    world = driven_control["world"]

    initial = report.get("initial") or {}
    draft = report.get("draft") or {}
    switch = report.get("switch") or {}

    summary = json.dumps(
        {
            "initial": initial,
            "draft": draft,
            "switch": switch,
            "screenshot": str(SCREENSHOT_PATH),
        },
        ensure_ascii=False,
    )

    # 1. the control showed the capacity that is in force.
    assert initial.get("select") == str(INITIAL_CAPACITY), (
        f"the control did not show the capacity in force: {summary}"
    )
    assert initial.get("total") == str(INITIAL_CAPACITY), (
        f"the panel did not show the capacity in force: {summary}"
    )

    # 2. the pick survived the poll — and the server never saw it.
    assert draft.get("select") == DRAFT_CAPACITY, (
        f"the pick did not survive the panel's own poll: {summary}"
    )
    draft_capacity = ((draft.get("api") or {}).get("capacity") or {})
    assert int(draft_capacity.get("total") or 0) == INITIAL_CAPACITY, (
        f"a pick that was never applied changed the capacity in force: "
        f"{summary}"
    )
    assert draft_capacity.get("source") != OVERRIDE_SOURCE, (
        f"a pick that was never applied was taken for an override: {summary}"
    )

    # 3. the pick did not survive the view switch.
    assert switch.get("select") == str(INITIAL_CAPACITY), (
        f"the pick outlived the view it was made in: {summary}"
    )

    # 4. the screenshot: the evidence the panel left behind.
    assert SCREENSHOT_PATH.exists(), f"the panel left no screenshot: {summary}"
    assert SCREENSHOT_PATH.stat().st_size > 0, (
        f"the screenshot is empty: {summary}"
    )

    # 5. the requests the panel's own client made carried the request
    #    guard's header — the header the server refuses to answer
    #    without.
    requests = report.get("requests") or []
    assert requests, f"the panel made no request at all: {summary}"
    unguarded = _unguarded(requests)
    assert not unguarded, (
        f"the panel made a request without the request guard's header: "
        f"{unguarded!r}"
    )

    # 6. the world is still the one this case built: its own database,
    #    its own plans — never the repository's.
    assert str(world.db_path).startswith(str(world.root)), (
        f"the case ran against a database that is not its own: "
        f"{world.db_path!r}"
    )


def test_server_state_after_apply_is_self_consistent(driven_control):
    """The server says the same thing as the panel, and the database agrees.

    The number in force is the operator's own number — so the read
    model names it as an override, and the database holds the override
    the operator's number was written as.  And a plan that was
    interrupted for the capacity is **back in the line**: an eviction is
    a plan giving way, not a plan that failed.
    """
    report = driven_control["report"]
    world = driven_control["world"]

    applied = report.get("apply") or {}
    summary = json.dumps(
        {
            "apply": applied,
            "screenshot": str(SCREENSHOT_PATH),
        },
        ensure_ascii=False,
    )

    # 1. the panel shows the number that was applied, and the number
    #    the server answered with is the same one.
    assert applied.get("select") == APPLIED_CAPACITY, (
        f"the control does not show the capacity that was applied: {summary}"
    )
    assert applied.get("total") == APPLIED_CAPACITY, (
        f"the panel does not show the capacity that was applied: {summary}"
    )
    used = str(applied.get("used") or "")
    assert used.isdigit() and int(used) <= int(APPLIED_CAPACITY), (
        f"the panel shows {used!r} plans running under a capacity of "
        f"{APPLIED_CAPACITY}: {summary}"
    )

    # 2. the server, read after the apply, says the same thing.
    state = _wait_for_the_capacity_to_settle(world, int(APPLIED_CAPACITY))
    capacity = state.get("capacity") or {}
    assert int(capacity.get("total") or 0) == int(APPLIED_CAPACITY), (
        f"the capacity in force is not the one that was applied: "
        f"{state!r}"
    )
    assert capacity.get("source") == OVERRIDE_SOURCE, (
        f"the capacity in force is not the operator's own number: {state!r}"
    )

    # 3. the database holds the override the operator's number was
    #    written as — the number in force is not a number the panel
    #    made up.
    rows = _rows(
        world.db_path,
        "SELECT key, value FROM scheduler_setting WHERE key = ?",
        ("capacity_override",),
    )
    assert len(rows) == 1, (
        f"the capacity override is not written once in the state database: "
        f"{rows!r}"
    )
    assert str(rows[0]["value"]) == APPLIED_CAPACITY, (
        f"the capacity override in the state database is not the number "
        f"that was applied: {rows!r}"
    )

    # 4. the verdict: a plan that was interrupted for the capacity is
    #    back in the line, not lost and not running.
    verdicts = _rows(
        world.db_path,
        "SELECT plan_id, stop_reason FROM plan_execution "
        "WHERE stop_reason LIKE ?",
        ("evicted%",),
    )
    queue_ids = {str(row["plan_id"]) for row in world.queue_rows()}
    for row in verdicts:
        plan_id = str(row["plan_id"])
        assert plan_id in queue_ids, (
            f"the plan {plan_id!r} was evicted ({row['stop_reason']!r}) and "
            f"is not in the line any more: the line holds {sorted(queue_ids)!r}"
        )

    # 5. the scheduler's own tables agree with the read model the panel
    #    draws: the slots are the running plans, and the line is the
    #    queue.  The read model is a copy, and a copy that disagrees
    #    with the thing it was copied from is a copy that lies.
    slot_ids = {str(row["plan_id"]) for row in world.slot_rows()}
    running_ids = {
        str(row.get("plan_id")) for row in (state.get("running") or [])
    }
    assert slot_ids == running_ids, (
        f"the slots the scheduler holds are not the plans the read model "
        f"draws as running: the slots hold {sorted(slot_ids)!r} and the "
        f"read model draws {sorted(running_ids)!r}"
    )
    assert queue_ids == {
        str(row.get("plan_id")) for row in (state.get("queue") or [])
    }, (
        f"the line the scheduler holds is not the queue the read model "
        f"draws: the table holds {sorted(queue_ids)!r} and the read model "
        f"draws "
        f"{sorted(str(r.get('plan_id')) for r in (state.get('queue') or []))!r}"
    )


def test_no_plan_is_both_running_and_queued(driven_control):
    """A plan is running or it is waiting — never both.

    The line is the plans that are waiting and the running list is the
    plans that are working.  A plan that is in both is a plan that is
    counted twice: it holds a slot while it waits, and the capacity
    the operator sees is then wrong in a way nobody can see.

    This is the invariant the capacity cycle leans on: a plan that
    gives way its place comes back to the line, and the line and the
    running list never share a plan.
    """
    report = driven_control["report"]
    world = driven_control["world"]

    state = _wait_for_the_capacity_to_settle(world, int(APPLIED_CAPACITY))

    running = {str(row.get("plan_id")) for row in (state.get("running") or [])}
    queued = {str(row.get("plan_id")) for row in (state.get("queue") or [])}

    summary = json.dumps(
        {
            "running": sorted(running),
            "queued": sorted(queued),
            "capacity": state.get("capacity"),
            "queue_length": state.get("queue_length"),
            "apply": report.get("apply"),
        },
        ensure_ascii=False,
    )

    both = running & queued
    assert not both, (
        f"the plans {sorted(both)!r} are running and waiting at the same "
        f"time: {summary}"
    )

    # Every plan the read model names is one of the plans that were
    # imported: the panel is not drawing a plan that does not exist.
    assert (running | queued) <= set(PLAN_IDS), (
        f"the read model names plans that were never imported: {summary}"
    )

    # The two lists are the two halves of what the scheduler is doing:
    # the number of running plans is the number the read model reports,
    # and the queue length is the length of the line it draws.
    capacity = state.get("capacity") or {}
    assert int(capacity.get("used") or 0) == len(running), (
        f"the read model reports {capacity.get('used')!r} plans running "
        f"and draws {len(running)}: {summary}"
    )
    assert state.get("queue_length") == len(queued), (
        f"the queue length the read model reports is not the length of "
        f"the line it draws: {summary}"
    )
