"""End-to-end: the panel updates itself, with no click and no reload.

What this file is for
---------------------
The scheduler panel now has a timer.  It reads the read model every
five seconds, redraws itself, keeps the last good frame when a read
fails, and stops while the tab is hidden.  Each of those has its own
test, and each of those tests a piece of the JavaScript on its own.

None of them answers the question the operator actually has: **if I
leave the panel open and something changes on the server, do I see
it?**  A panel that only redraws when it is clicked shows the past; a
poll that fails silently shows a stale truth without saying so; a
timer that is stopped the moment the tab is hidden never starts
again.  None of that is visible to a test that calls the JavaScript
directly.

This file asks the question against a running deployment: a real
server, a real browser, and the real panel.

The shape of the case
---------------------
The panel is opened **once**.  After that nothing on the page is
touched: no button is clicked, the page is not reloaded, and nothing
is evaluated in the page.  The only thing that happens is that the
server changes — the plan at the head of the line is parked through the
API — and the panel is watched, every 200 milliseconds, to see whether
it notices on its own.

The change is made *behind the panel's back* on purpose.  A click on
the panel's own pause button would prove that the button works, which
is not what this case is about: what has to be shown is that the panel
keeps up with a server it was not told about.

Two things the driver counts
----------------------------
The case claims that the page was not touched, and a claim about a
click that was not made is only worth as much as the evidence for it.
So the driver counts what the page itself did:

* the **requests the page issued** — a panel that acted on something
  would have asked the server to do something, and the case asserts
  that it did not;
* the **navigations the page made** — a page that reloaded itself would
  show the new state for the wrong reason, and the case asserts that
  it was loaded once and never again.

What is real here
-----------------
The server is the production one, booted as a child process; the state
is a SQLite file; the plans are directories; the executor is the
stand-in the sibling suite installs through the production seam
(``PDT_EXECUTOR_CMD``); the page is a real browser.

The case writes nothing of its own.  The plan it parks is named by the
read model the scheduler serves and by the scheduler's own facts — not
by a constant in this file — so the case cannot pass by pausing
something that was never in the line.

Two ways this case can be wrong
-------------------------------
A case that watches for a change can be wrong in two directions, and
both are closed here:

* **It can pass without a change.**  A number that moved for a reason
  of its own — a plan that finished, a line that was rebuilt — would
  satisfy a loose assertion.  So the case checks that the panel moved
  by exactly the one plan the pause took out of the line, and that the
  server says the same thing at the same time.
* **It can fail without a change.**  A panel that redraws for reasons
  of its own would make the case pass for the wrong reason.  So the
  case checks that the page issued no request of its own and never
  navigated again: the only thing that could have moved the number is
  the panel reading the server again.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from typing import Any, Dict

import pytest

from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (
    SCREEN_DIR,
    _World,
    _driver_env,
    _playwright_interpreter,
    _wait_for_settle,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: How long the panel is watched for the change.  The panel reads its
#: read model every five seconds, so ten seconds is two of its
#: intervals: one that has to carry the change and one that is there to
#: catch a panel that is late.
WATCH_SECONDS: float = 10.0

#: How often the panel is read while it is watched.
POLL_SECONDS: float = 0.2


# ---------------------------------------------------------------------------
# The driver — the panel, left alone
# ---------------------------------------------------------------------------

_AUTO_REFRESH_DRIVER_SOURCE = r'''
"""Open the panel, change the server behind its back, and watch the panel.

Arguments:
  1. the base url of the running server
  2. the plan to park through the API
  3. the directory the screenshots are written into
  4. how long the panel is watched, in seconds
  5. how often the panel is read while it is watched, in seconds

What this script does to the page
----------------------------------
It loads the page once and it opens the scheduler view once.  Those are
the only two things it does: after the view is open the script never
clicks anything, never reloads, and never evaluates anything in the
page.  It reads what the panel draws and nothing else.

The change is made with the browser's own API context rather than with
a click, so the panel has to notice a server it was not told about.
The request carries the guard's header and no ``Origin``, which is
what a local script's request looks like — the guard lets it through.

Everything the script reports is read off the page or off the API, so
the report is what happened rather than what the script meant to do.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GUARD_HEADER = "x-pdt-request"
GUARD_VALUE = "1"

#: The panel's own capacity widget, as the operator reads it.
QUEUE_SELECTOR = "#scheduler-view-capacity-queue"

#: The moment the snapshot on the panel was made.
STAMP_SELECTOR = "#scheduler-view-generated-at"

#: The endpoint the panel reads.  The driver reads it as well, so that
#: the panel is compared with the server and never with itself.
STATE_PATH = "/api/scheduler/state"


def read_text(page, selector):
    """What the panel draws in *selector*."""
    return page.inner_text(selector).strip()


def wait_until_painted(page, expected, timeout=20.0, poll=0.2):
    """Wait until the panel draws the queue length the API is serving.

    The view is opened before the model behind it has been read, so the
    number on the screen is the one the page was served with until the
    first read comes back.  Reading the number before the panel has
    drawn it would compare the panel with nothing.
    """
    deadline = time.time() + timeout
    seen = []
    while True:
        text = read_text(page, QUEUE_SELECTOR)
        seen.append(text)
        if text == str(expected):
            return text
        if time.time() >= deadline:
            raise AssertionError(
                "the panel never drew the queue length "
                + repr(expected)
                + "; it drew "
                + repr(seen)
            )
        page.wait_for_timeout(int(poll * 1000))


base = sys.argv[1]
plan_id = sys.argv[2]
screens = Path(sys.argv[3])
watch_seconds = float(sys.argv[4])
poll_seconds = float(sys.argv[5])
screens.mkdir(parents=True, exist_ok=True)

#: The navigations the page made.  One is the load; a second one would
#: be a reload, which would show the new state for the wrong reason.
navigations = []

#: The action requests the page issued.  A panel that acted on
#: something would have asked the server to do something.
actions = []

with sync_playwright() as driver:
    browser = driver.chromium.launch()
    page = browser.new_page()

    def on_navigation(frame):
        if frame == page.main_frame:
            navigations.append(frame.url)

    def on_request(request):
        url = request.url
        if "/api/plan/" in url and "/scheduler/" in url:
            actions.append(url)

    page.on("framenavigated", on_navigation)
    page.on("request", on_request)

    page.goto(base, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)

    # The one click: the operator opens the panel.
    page.click("#btn-open-scheduler")
    page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)

    # What the server serves right now.  The panel is watched against
    # this, so the panel is never compared with a number made up here.
    api_before = page.request.get(
        base.rstrip("/") + STATE_PATH,
        headers={GUARD_HEADER: GUARD_VALUE},
    ).json()
    queue_length_before = api_before.get("queue_length")
    head_seq = (api_before.get("queue") or [{}])[0].get("seq")

    wait_until_painted(page, queue_length_before, poll=poll_seconds)

    queue_before = read_text(page, QUEUE_SELECTOR)
    stamp_before = read_text(page, STAMP_SELECTOR)
    page.screenshot(path=str(screens / "auto-refresh-before.png"), full_page=True)

    # The change, behind the panel's back.
    pause = page.request.post(
        base.rstrip("/") + "/api/plan/" + plan_id + "/scheduler/pause",
        headers={GUARD_HEADER: GUARD_VALUE},
    )
    pause_status = pause.status

    # Now the panel is watched.  Nothing is clicked; nothing is
    # reloaded; the only thing that happens is that it is read.
    started = time.time()
    deadline = started + watch_seconds
    queue_after = queue_before
    stamp_after = stamp_before
    changed = False
    while True:
        queue_after = read_text(page, QUEUE_SELECTOR)
        stamp_after = read_text(page, STAMP_SELECTOR)
        if queue_after != queue_before:
            changed = True
            break
        if time.time() >= deadline:
            break
        page.wait_for_timeout(int(poll_seconds * 1000))
    elapsed = round(time.time() - started, 2)

    page.screenshot(path=str(screens / "auto-refresh-after.png"), full_page=True)

    api_after = page.request.get(
        base.rstrip("/") + STATE_PATH,
        headers={GUARD_HEADER: GUARD_VALUE},
    ).json()

    print(json.dumps({
        "queue_before": queue_before,
        "queue_after": queue_after,
        "generated_at_before": stamp_before,
        "generated_at_after": stamp_after,
        "pause_status": pause_status,
        "elapsed_sec": elapsed,
        "changed": changed,
        "api_queue_before": queue_length_before,
        "api_queue_after": api_after.get("queue_length"),
        "api_head_seq": head_seq,
        "page_action_requests": actions,
        "page_navigations": len(navigations),
    }, ensure_ascii=False))
    browser.close()
'''


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def panel_world(tmp_path):
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


def _shutdown(world: _World) -> None:
    """Shut the world down, and prove that nothing is left behind.

    The sibling's teardown reads the process table with ``ps`` — a
    setuid binary that some locked-down machines refuse to execute.
    Where it cannot be executed the teardown cannot reach its own
    kills either, because it reads the table before it signals
    anything, and the world would be left running: a server and four
    executors, for every run.

    So the fallback below does the same work from the facts the world
    keeps — the pid each executor's own record holds — and checks the
    result with the kernel's own answer to "is this process still
    there?" instead of with a second reading of the table.  It is only
    reached where the sibling's own teardown cannot run at all.
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

    # Each executor leads a group of its own, and the group holds the
    # grandchild the stand-in forks into it.
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

    # Nothing may be left.  A killed process is reaped by the system,
    # and a reaped process is gone only after the system has done it,
    # so the question is asked again until the answer is yes.
    alive: Dict[int, str] = {}
    deadline = time.monotonic() + 15.0
    while True:
        alive = {}
        for row in world.exec_rows():
            pid = row.get("exec_pid")
            if not pid:
                continue
            pid = int(pid)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            alive[pid] = str(row["plan_id"])
        if not alive or time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    handle = getattr(world, "_log_handle", None)
    if handle is not None:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 — closing a log must not fail a test
            pass
        world._log_handle = None

    assert not alive, (
        "the teardown left something behind: "
        + ", ".join(f"{plan_id} (pid {pid})" for pid, plan_id in alive.items())
    )


# ---------------------------------------------------------------------------
# Driving the panel
# ---------------------------------------------------------------------------


def _drive_auto_refresh(world: _World, plan_id: str) -> Dict[str, Any]:
    """Open the panel, park *plan_id* behind its back, and report.

    The report is what the panel drew and what the server said while
    the panel was watched — one line of JSON, printed by the driver and
    read back here.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "panel cannot be driven, and a panel that was not driven "
            "proves nothing about the panel"
        )

    script = world.root / "drive_auto_refresh.py"
    script.write_text(_AUTO_REFRESH_DRIVER_SOURCE, encoding="utf-8")
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/",
            plan_id,
            str(SCREEN_DIR),
            str(WATCH_SECONDS),
            str(POLL_SECONDS),
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


def _head_of_the_line(world: _World) -> Dict[str, Any]:
    """The plan at the head of the line, and the number it carries.

    The read model says how many plans are waiting and in what order;
    the scheduler's own facts say which plan carries which number.  The
    two are joined on the number they share, so the plan named here is
    the one the panel draws first — not a constant, and not a guess.
    """
    order = world.queue_plan_ids()
    assert order, "the line is empty, so there is nothing to park"
    head = order[0]
    row = next(
        row for row in world.queue_rows() if str(row["plan_id"]) == head
    )
    return {"plan_id": head, "seq": int(row["seq"])}


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_panel_updates_itself_without_a_click(panel_world):
    """The panel notices a change it was not told about.

    The panel is opened, the plan at the head of the line is parked
    through the API, and the panel is then watched without a single
    click or a reload.  What has to be true afterwards is that the
    panel drew the shorter line, that it did so within one of its own
    intervals, and that the only thing that could have moved it was
    the panel reading the server again.
    """
    world = panel_world
    head = _head_of_the_line(world)

    report = _drive_auto_refresh(world, head["plan_id"])

    # -- the change was made, and the server took it -----------------------

    assert report["pause_status"] == 200, (
        f"the pause was answered with {report['pause_status']!r}; the "
        f"report was {report!r}"
    )
    assert report["api_queue_after"] == report["api_queue_before"] - 1, (
        f"the line the server serves went from {report['api_queue_before']!r} "
        f"to {report['api_queue_after']!r}; one plan was parked, so it "
        f"should have gone down by one"
    )
    assert report["api_head_seq"] == head["seq"], (
        f"the plan the server had at the head of the line carried the "
        f"number {report['api_head_seq']!r}; the plan this case parked "
        f"({head['plan_id']!r}) carries {head['seq']!r}"
    )
    assert head["plan_id"] not in world.queue_plan_ids(), (
        f"the parked plan {head['plan_id']!r} is still in the line: "
        f"{world.queue_plan_ids()!r}"
    )

    # -- the panel noticed, on its own -------------------------------------

    assert report["changed"] is True, (
        f"the panel did not change within {WATCH_SECONDS}s of the park; "
        f"it drew the queue length {report['queue_before']!r} before and "
        f"{report['queue_after']!r} after, and the snapshot it drew went "
        f"from {report['generated_at_before']!r} to "
        f"{report['generated_at_after']!r} ({report['elapsed_sec']}s)"
    )
    assert int(report["queue_after"]) == int(report["queue_before"]) - 1, (
        f"the panel drew the queue length {report['queue_before']!r} "
        f"before and {report['queue_after']!r} after; one plan was "
        f"parked, so the panel should have drawn one fewer"
    )
    assert report["elapsed_sec"] <= WATCH_SECONDS, (
        f"the panel took {report['elapsed_sec']}s to notice the change; "
        f"it should have noticed within {WATCH_SECONDS}s"
    )
    assert report["generated_at_after"] != report["generated_at_before"], (
        f"the panel drew the same snapshot both times "
        f"({report['generated_at_before']!r}); a panel that read the "
        f"server again would have drawn a later snapshot"
    )
    assert report["api_queue_after"] == int(report["queue_after"]), (
        f"the panel drew the queue length {report['queue_after']!r} "
        f"while the server serves {report['api_queue_after']!r}"
    )

    # -- and nothing else could have moved it -----------------------------

    assert report["page_action_requests"] == [], (
        f"the page asked the server to act on something: "
        f"{report['page_action_requests']!r}; the panel was to be watched, "
        f"not to act"
    )
    assert report["page_navigations"] == 1, (
        f"the page navigated {report['page_navigations']} times; it was to "
        f"be loaded once and never reloaded"
    )

    # -- the evidence: the two screenshots the case was asked to keep ------

    for name in ("auto-refresh-before.png", "auto-refresh-after.png"):
        screenshot = SCREEN_DIR / name
        assert screenshot.is_file(), f"{screenshot} was not written"
        assert screenshot.stat().st_size > 0, f"{screenshot} is empty"
