"""End-to-end: the scheduler panel, driven the way an operator drives it.

What this file is for
---------------------
The sibling case in this directory asks whether the scheduler *acts*
correctly: the four commands, the line, the restart.  This one asks
whether an operator can *use* the panel — whether the whole chain from
"the server is up" to "the operator has set the capacity" holds in one
run, in a real browser, against a real server.

The chain
---------
1. the server is a child process, with its own database and its own plans;
2. the operator opens the scheduling view (``#btn-open-scheduler``);
3. the line the panel draws has rows, and every row names the plan it
   is about — a plan id, not a dash;
4. the running plans carry a duration an operator can read: how long
   the plan has been running, in hours and minutes or minutes and seconds;
5. the operator picks a capacity (five) and presses "apply"
   (``#scheduler-capacity-select`` + ``#scheduler-capacity-apply``);
6. the server's own read model answers with that capacity, and says
   where the number came from: the operator's own override;
7. a screenshot of the panel is written to disk — the evidence.

What is real here
-----------------
* the server is a **process** (a child ``uvicorn``, killed by pid and
  by process group);
* the state is a **SQLite file** on disk;
* the plans are **directories**;
* the page is a **real browser** (Playwright, when a driver is
  installed), clicking the controls an operator clicks.

The test never writes a scheduler row and never mocks anything.  The
executor is replaced through the named production seam
(``PDT_EXECUTOR_CMD``) — the way the sibling case does it — and every
request the panel's own buttons make carries the request guard's
header, because the panel's own client is what carries it; the driver
reads the headers off the intercepted requests, so the header is
observed rather than assumed.

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
a panel that was not driven proves nothing about the panel, and a test
that pretends to have driven it is worse than no test at all.
"""

from __future__ import annotations

import json
import os
import re
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
# database, its own plans), the Playwright discovery, and the environment
# the browser driver runs under.
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (  # noqa: E402
    PLAN_IDS,
    _driver_env,
    _playwright_interpreter,
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
    os.environ.get("PDT_SCHED_PANEL_E2E_ROOT", "/tmp/sched-panel-ux-e2e")
)

#: The screenshot the panel leaves behind.
SCREENSHOT_PATH: Path = E2E_ROOT / "panel.png"

#: The capacity the operator picks.  It is not the configured one (the
#: world configures four), so the read model has to say where the
#: number came from.
CAPACITY_TO_APPLY: int = 5

#: How long the line may take to fill.  The line is the scheduler's
#: own reconciliation, so it takes a tick or two — not a minute — but
#: the budget is the one the acceptance criteria name.
QUEUE_TIMEOUT: float = 60.0

#: How long the panel may take to draw a duration an operator can
#: read.  The panel draws hours and minutes, or minutes and seconds —
#: so a plan that has been running for less than a minute draws a
#: number of seconds, which is not one of those two shapes.  The
#: panel is therefore asked to read again until the duration it draws
#: is one of them.
DURATION_TIMEOUT: float = 150.0

#: The duration the panel draws.
DURATION_PATTERN = re.compile(r"^(\d+[hms] )+\d+[smh]$")

#: What the panel draws when the start time of a plan is not known.
UNKNOWN_DURATION: str = "—"

#: The source the read model names when the capacity in force is the
#: operator's own number rather than the configured one.
OVERRIDE_SOURCE: str = "override"


# ---------------------------------------------------------------------------
# The browser driver — the panel, as an operator uses it
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = r'''
"""Drive the scheduler panel the way an operator drives it.

Arguments:
  1. the base url of the running server
  2. the path the screenshot is written to
  3. a JSON object: the capacity to pick, and how long to wait for the
     durations to become real ones

What it does, in the operator's own order: open the scheduling view,
read the line, wait until the running plans carry a duration an
operator can read, pick a capacity, press "apply", and leave a
screenshot behind.

Every request the panel's own client makes is watched on its way out,
so the report can say whether they carried the request guard's header.

One JSON document is printed on stdout: what the panel drew, what the
panel's requests carried, and where the screenshot went.
"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GUARD_HEADER = "x-pdt-request"
GUARD_VALUE = "1"

#: The duration the panel draws: hours and minutes, minutes and seconds.
DURATION = r"^(\d+[hms] )+\d+[smh]$"

#: What the panel draws when the start time of a plan is not known.
UNKNOWN = "—"


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


def read_rows(page, selector):
    """The cells of every row of a table, read in one go.

    The panel redraws its tables whenever it reads the model again, so
    a row that was located a moment ago may be gone by the time it is
    read.  Reading the whole table in one evaluation avoids that race:
    what is read is what is on the page at that moment.
    """
    return page.eval_on_selector_all(
        selector,
        """rows => rows.map(row => Array.from(row.querySelectorAll('td'))
                                 .map(cell => cell.innerText.trim()))""",
    )


def read_text(page, selector):
    """The text of one element, or the empty string when it is not there."""
    element = page.query_selector(selector)
    if element is None:
        return ""
    return (element.inner_text() or "").strip()


def refresh(page):
    """Ask the panel to read its model again.

    The panel reads the read model when the view is opened and after
    every command; it reads it on no timer of its own.  So the
    duration the panel draws is the duration as of its last read, and
    the operator who wants to watch it move presses "refresh".

    The waiting is done with the browser's own clock rather than with
    ``time.sleep``: the synchronous Playwright driver only hands its
    events to the page while the page is being asked something.
    """
    page.click("#btn-refresh-scheduler", timeout=30000)
    page.wait_for_timeout(500)


def readable(rows):
    """Whether every row carries a duration an operator can read.

    A duration is readable when it is the two-part shape the panel
    draws — hours and minutes, minutes and seconds — or when it is the
    dash the panel draws for a plan whose start time is not known.
    """
    durations = [row[1] for row in rows if len(row) > 1]
    if not durations:
        return False
    return all(
        text == UNKNOWN or re.match(DURATION, text) for text in durations
    )


url = sys.argv[1]
screenshot = Path(sys.argv[2])
options = json.loads(sys.argv[3])
capacity = str(options["capacity"])
duration_timeout = float(options["duration_timeout"])

screenshot.parent.mkdir(parents=True, exist_ok=True)

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

seen = []


with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
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

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_timeout(1000)

    # The operator opens the scheduling view.
    page.click("#btn-open-scheduler", timeout=30000)
    page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)
    page.wait_for_timeout(500)

    # The line: wait until the panel draws rows in it.
    deadline = time.time() + 60
    while time.time() < deadline:
        if read_rows(page, "#scheduler-queue-list tr"):
            break
        refresh(page)
    else:
        raise AssertionError("the panel drew no queue row within 60s")

    # The running plans, and how long they have been running.  A plan
    # that has been running for less than a minute draws a number of
    # seconds, which is not one of the two shapes the panel draws for
    # a plan that has been at it for a while; the panel is asked to
    # read again until it draws one of those shapes.
    #
    # The rows that pass the wait are the rows that are reported: the
    # read that was judged readable is the read that is kept, so
    # nothing can change in between the two.
    deadline = time.time() + duration_timeout
    running = read_rows(page, "#scheduler-running-list tr")
    while not readable(running) and time.time() < deadline:
        refresh(page)
        running = read_rows(page, "#scheduler-running-list tr")
    if not readable(running):
        raise AssertionError(
            "the panel drew no readable duration within {0}s; it drew "
            "{1!r}".format(duration_timeout, running)
        )

    # The operator picks a capacity and presses "apply".  Nothing reads
    # the model between the two: the panel follows the server, so a
    # read in between would put the configured number back into the
    # control and the pick would be lost.
    page.select_option("#scheduler-capacity-select", capacity)
    page.click("#scheduler-capacity-apply", timeout=30000)
    # The panel reads its model again after the command; two seconds is
    # what the acceptance criteria allow for the answer to arrive.
    page.wait_for_timeout(2000)

    report = {
        "queue_cells": read_rows(page, "#scheduler-queue-list tr"),
        "running_cells": running,
        "running_cells_after_capacity": read_rows(
            page, "#scheduler-running-list tr"
        ),
        "agent_count": read_text(page, "#scheduler-view-agent-count"),
        "panel_capacity_total": read_text(page, "#scheduler-view-capacity-total"),
        "panel_capacity_source": read_text(page, "#scheduler-capacity-source"),
        "requests": seen,
        "screenshot": str(screenshot),
    }
    page.screenshot(path=str(screenshot), full_page=True)

    print(json.dumps(report, ensure_ascii=False))
    browser.close()
'''


# ---------------------------------------------------------------------------
# The world, and the browser that drives the panel
# ---------------------------------------------------------------------------


def _write_lark_stub(work: Path) -> Path:
    """A directory whose ``lark_oapi`` is a stub, so the boot stays quick.

    The server's lifespan builds the Feishu notifier, and the notifier's
    client imports ``lark_oapi`` — an SDK of more than ten thousand
    modules.  On a loaded machine that import alone costs tens of
    seconds, which is more than the boot's budget allows: the port is
    never opened and the case fails before it has begun.

    The stub raises :class:`ImportError` on import, which is exactly what
    the client does with a missing SDK: the notifier logs that it is
    disabled and stays inert.  It is inert in this test either way —
    Feishu is not configured on a test machine, and nothing else in the
    backend imports ``lark_oapi``.

    The stub is a **test-side** seam: the product's files are untouched,
    and the server's behaviour is the behaviour of a machine without the
    optional SDK.
    """
    stub_dir = work / "no-lark"
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "lark_oapi.py").write_text(
        'raise ImportError(\n'
        '    "lark-oapi is stubbed out for this E2E run: the real SDK is "\n'
        '    "more than ten thousand modules and importing it costs tens "\n'
        '    "of seconds, which is more than the boot budget allows. The "\n'
        '    "Feishu notifier is inert in this test either way -- Feishu "\n'
        '    "is not configured here."\n'
        ')\n',
        encoding="utf-8",
    )
    return stub_dir


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A running server, its own database, its own plans, and its plans in them."""
    # The boot has to fit into its budget: the Feishu notifier's client
    # imports the ``lark_oapi`` SDK, and that import alone costs tens of
    # seconds on a loaded machine, so the boot would time out before the
    # port was ever opened.  The stub makes that import fail fast; the
    # notifier then does what it does on a machine without the optional
    # SDK — it logs that it is disabled and stays inert.
    stub_dir = _write_lark_stub(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(stub_dir), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    )

    the_world = _World(tmp_path)
    try:
        the_world.start()
        yield the_world
    finally:
        the_world.teardown()


@pytest.fixture
def interpreter():
    """An interpreter that can drive a real browser — or a skip.

    The instrument is asked for **before** the world is built, so a
    machine without a browser pays nothing: no server is started, no
    plan is run, and the case says out loud that the panel was not
    driven.  A panel that was not driven proves nothing about the
    panel, so a test that pretends to have driven it is worse than no
    test at all.
    """
    found = _playwright_interpreter()
    if found is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "panel cannot be driven, and a panel that was not driven "
            "proves nothing about the panel"
        )
    return found


def _wait_for_a_queue(world: "_World", timeout: float = QUEUE_TIMEOUT) -> Dict[str, Any]:
    """Wait until the server's own read model has a line in it.

    The line is the scheduler's own reconciliation of the plans that
    are waiting, so it takes a tick or two to appear.  The line the
    server draws is the line the panel draws.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = world.scheduler_state()
        if state.get("queue"):
            return state
        time.sleep(0.5)
    raise AssertionError(f"the read model had no queue within {timeout}s")


def _drive_panel(world: "_World", interpreter: str) -> Dict[str, Any]:
    """Drive the panel in a real browser and report what it drew."""
    script = world.root / "drive_scheduler_panel.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/",
            str(SCREENSHOT_PATH),
            json.dumps(
                {
                    "capacity": CAPACITY_TO_APPLY,
                    "duration_timeout": DURATION_TIMEOUT,
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


def _unguarded(requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The requests that went out without the request guard's header."""
    return [
        entry
        for entry in requests
        if entry.get("guard") != "1"
    ]


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_panel_ux_end_to_end(interpreter, world):
    """The whole chain, in one run: open the panel, read it, set the capacity.

    The evidence is one report — the queue as the panel drew it, the
    running plans as the panel drew them, the numbers the capacity
    area shows, and the screenshot the panel left behind.
    """
    _wait_for_a_queue(world)

    report = _drive_panel(world, interpreter)

    queue_cells: List[List[str]] = report.get("queue_cells") or []
    running_cells: List[List[str]] = report.get("running_cells") or []
    requests: List[Dict[str, Any]] = report.get("requests") or []

    # The read model, read after the operator's write.
    state = world.scheduler_state()
    capacity = state.get("capacity") or {}

    summary = json.dumps(
        {
            "queue_cells": queue_cells,
            "running_cells": running_cells,
            "running_cells_after_capacity": report.get(
                "running_cells_after_capacity"
            ),
            "agent_count": report.get("agent_count"),
            "capacity_total": capacity.get("total"),
            "capacity_source": capacity.get("source"),
            "panel_api_requests": len(requests),
            "panel_api_requests_with_guard_header": sum(
                1 for entry in requests if entry.get("guard") == "1"
            ),
            "screenshot": str(SCREENSHOT_PATH),
        },
        ensure_ascii=False,
    )

    # 1. the line the panel draws, and the plan every row is about.
    assert queue_cells, f"the panel drew no queue row: {summary}"
    for row in queue_cells:
        assert len(row) >= 2, f"the queue row has no plan column: {summary}"
        plan_cell = row[1]
        assert plan_cell and plan_cell != UNKNOWN_DURATION, (
            f"the queue row names no plan: {summary}"
        )
        assert plan_cell in PLAN_IDS, (
            f"the queue row names a plan that was never imported: {summary}"
        )

    # 2. the duration the running plans carry.
    #
    # These are the rows as the operator read them when the scheduling
    # view was opened.  They are read there and not after the capacity
    # is applied, because raising the capacity lets the scheduler start
    # another plan — and a plan that started a second ago draws "0s",
    # which is the panel telling the truth about a plan that has not
    # been running for a minute yet.  The criterion is about the
    # durations the panel draws for the plans that are running.
    assert running_cells, f"the panel drew no running plan: {summary}"
    for row in running_cells:
        assert len(row) >= 2, f"the running row has no duration column: {summary}"
        duration = row[1]
        assert duration == UNKNOWN_DURATION or DURATION_PATTERN.match(duration), (
            f"the running row draws no duration an operator can read: {summary}"
        )

    # 3. the capacity the operator picked, and where it came from.
    assert capacity.get("total") == CAPACITY_TO_APPLY, (
        f"the capacity in force is not the one that was picked: {summary}"
    )
    assert capacity.get("source") == OVERRIDE_SOURCE, (
        f"the capacity in force is not the operator's own number: {summary}"
    )

    # 4. the screenshot: the evidence the panel left behind.
    assert SCREENSHOT_PATH.exists(), f"the panel left no screenshot: {summary}"
    assert SCREENSHOT_PATH.stat().st_size > 0, (
        f"the screenshot is empty: {summary}"
    )

    # 5. every request the panel's own client made carried the request
    #    guard's header — the header the server refuses to answer
    #    without.
    assert requests, f"the panel made no request at all: {summary}"
    unguarded = _unguarded(requests)
    assert not unguarded, (
        f"the panel made a request without the request guard's header: "
        f"{unguarded!r}"
    )

    print(summary)
