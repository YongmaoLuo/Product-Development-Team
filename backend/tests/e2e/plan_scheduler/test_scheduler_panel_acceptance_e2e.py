"""End-to-end acceptance: the operator opens the panel, and sees the truth.

What this file is for
---------------------
Every layer under the scheduler's panel is proved on its own: the store
writes a row, the command executor carries out an action, the snapshot
builder assembles the read model, the routes turn an outcome into a
status.  None of them can prove the thing an operator actually asks —
*is what I see on the screen what the scheduler is doing?* — because
that question is only asked by a person opening a real page in a real
browser, and it is only answered by what that page draws.

This file asks it once, end to end, against something that is really
running:

1. **a real server** — a child process, its own SQLite file, its own
   plans, its own port.  The world is **reused**, not rewritten: the
   operator's own suite already builds one, and a second one would be a
   second thing to keep true;
2. **a real Chromium** — Playwright, launched against the browser the
   driver expects;
3. **a real click** — the button the operator presses,
   ``#btn-open-scheduler``, pressed in the page rather than called from
   outside it;
4. **the truth** — the rows the panel drew, compared with the read
   model the panel was fed.

The comparison is the point of the file.  The panel draws one read
model, and the rows on the screen belong to the model the panel read at
its last poll: a comparison against a model read at another moment
compares two answers to two different questions.  So the case compares
the panel's rows with **the model the panel was fed** — the model the
server answered the panel's own poll with — and it compares them
through the one rule the other end-to-end case established
(:func:`_panel_rows_match`, from ``test_scheduler_operator_e2e``).

What would make this case lie
-----------------------------
Three things, and each of them is refused here rather than tolerated:

* **an empty panel.**  A panel that draws no running plan agrees with a
  read model that carries none, so the comparison would hold and mean
  nothing.  The case therefore refuses to compare an empty panel, and
  asks for the number of running plans the read model says are running.
* **a capacity widget that never drew.**  The page's own HTML starts
  with a capacity of ``0`` in it, so a widget read too early reads the
  page's starting value and looks like a widget that drew a number.
  The case waits for the widget to say what the server says, and says
  out loud when it does not.
* **a screenshot left over from an earlier run.**  The screenshots are
  evidence, and evidence that was not written by this run is not this
  run's evidence.  The case checks that they were written during it.

What is not here
----------------
Nothing is simulated and nothing is stubbed: the executor is the
stand-in the operator suite installs through the named production seam
(``PDT_EXECUTOR_CMD``), the server is the product's own server, and the
browser is a browser.  A machine with no Playwright driver, or with the
driver but no browser binaries, **skips** — a panel that was not driven
proves nothing about the panel, and a case that pretends otherwise is
worse than no case at all.

The evidence
------------
The screenshots are written into the operator suite's own screenshot
directory, and they are evidence rather than an input: nothing in the
repository depends on them, and this case reads them only to prove they
were written.  The paths are the ones the case was told to use; they
are not paths into anybody's home directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import pytest

#: ``<repo>/backend`` — this file lives at ``backend/tests/e2e/plan_scheduler/``.
_BACKEND_DIR: Path = Path(__file__).resolve().parents[3]

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The world, the settle-wait, the row comparison and the screenshot
# directory are reused **as they are**.  There is one server-starting
# logic in this suite, one definition of what a panel row means, and one
# place the screenshots go; a second copy of any of the three is a
# second thing to keep true.
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (  # noqa: E402
    _driver_env,
    _panel_rows_match,
    _playwright_interpreter,
    _wait_for_settle,
    _World,
    SCREEN_DIR,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: The screenshot the operator's own walkthrough leaves behind.  It has a
#: name of its own so that it cannot be mistaken for the evidence the
#: operator suite writes — the two are read by two different cases.
WALKTHROUGH_SCREENSHOT: Path = SCREEN_DIR / "acceptance-walkthrough.png"

#: How long a capacity widget may take to say what the server says.  The
#: panel reads its model when the view is opened, so the widget is drawn
#: a moment after the view appears; the wait is for that moment, not for
#: the server.
WIDGET_TIMEOUT: float = 30.0


# ---------------------------------------------------------------------------
# The browser — the operator's walkthrough, in a real browser
# ---------------------------------------------------------------------------

_WALKTHROUGH_SOURCE = r'''
"""The operator's walkthrough, driven in a real browser.

Arguments:
  1. the base url of the running server
  2. the path the screenshot is written to
  3. the number of plans the read model says are running
  4. how long a capacity widget may take to say that number

What it does, in the operator's own order: open the home page, wait for
the home page's capacity widget to carry the number the server says,
press the button that opens the scheduling view, wait for the view to be
really there, and wait for the view's own capacity widget to carry that
number too.

The waiting is the point.  The page's own HTML starts with a capacity of
``0`` in it, so a widget read too early reads the page's starting value
and looks like a widget that drew a number.  The case waits for the
number the server says, and reports every value the widget held while
it waited — so a widget that never drew is *reported*, not passed.

One JSON document is printed on stdout.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def read_text(page, selector):
    """What the panel draws in *selector*."""
    return page.inner_text(selector).strip()


def wait_for_number(page, selector, expected, timeout=30.0, poll=0.2):
    """Wait until the widget in *selector* says *expected*.

    Returns the distinct values the widget held while the case waited.
    The last of them is what the panel drew when the wait was over: the
    wanted number when the panel drew it, and something else when it
    did not.
    """
    deadline = time.time() + timeout
    seen = []
    while True:
        text = read_text(page, selector)
        if text not in seen:
            seen.append(text)
        if text == expected or time.time() >= deadline:
            return seen
        page.wait_for_timeout(int(poll * 1000))


base = sys.argv[1]
screenshot = Path(sys.argv[2])
expected_used = str(sys.argv[3])
timeout = float(sys.argv[4])

screenshot.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as driver:
    browser = driver.chromium.launch()
    page = browser.new_page()

    page.goto(base, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)

    # The home page: the widget that answers "how full is it" before the
    # operator opens anything.
    home_seen = wait_for_number(
        page, "#scheduler-capacity-used", expected_used, timeout
    )

    # The operator opens the scheduling view: the button is pressed in
    # the page, and the view is waited for until it is really there.
    page.click("#btn-open-scheduler")
    page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)

    view_visible = page.is_visible("#scheduler-view")
    used_visible = page.is_visible("#scheduler-view-capacity-used")
    view_seen = wait_for_number(
        page, "#scheduler-view-capacity-used", expected_used, timeout
    )

    page.screenshot(path=str(screenshot), full_page=True)

    print(json.dumps({
        "home_used": home_seen[-1],
        "home_seen": home_seen,
        "view_used": view_seen[-1],
        "view_seen": view_seen,
        "view_visible": view_visible,
        "used_visible": used_visible,
        "screenshot": str(screenshot),
    }, ensure_ascii=False))
    browser.close()
'''


def _walkthrough(
    world: "_World", interpreter: str, expected_used: str
) -> Dict[str, Any]:
    """Drive the operator's walkthrough in a real browser and report it.

    The walkthrough is the part a person does: open the page, press the
    button, look at the panel.  It runs against the **same** server the
    parity comparison reads — one deployment, two readings of it.
    """
    script = world.root / "drive_operator_walkthrough.py"
    script.write_text(_WALKTHROUGH_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/",
            str(WALKTHROUGH_SCREENSHOT),
            str(expected_used),
            str(WIDGET_TIMEOUT),
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
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def interpreter():
    """An interpreter that can drive a real browser — or a skip.

    The instrument is asked for **before** the world is built, so a
    machine without a browser pays nothing: no server is started, no
    plan is run, and the case says out loud that the panel was not
    driven.  A panel that was not driven proves nothing about the panel.
    """
    found = _playwright_interpreter()
    if found is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "panel cannot be driven, and a panel that was not driven "
            "proves nothing about the panel"
        )
    return found


@pytest.fixture
def world(tmp_path, interpreter):
    """A running server, its own database, its own plans, and its plans in them."""
    the_world = _World(tmp_path)
    try:
        the_world.start()
        _wait_for_settle(the_world)
        yield the_world
    finally:
        the_world.teardown()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_panel_acceptance_operator_walkthrough(world, interpreter):
    """The operator opens the panel, and the panel is telling the truth.

    Four things are asked, in the order an operator meets them:

    1. **the walkthrough** — a real browser opens the home page, the
       operator presses ``#btn-open-scheduler``, and the scheduling
       view appears and carries the number the server says;
    2. **the running rows** — the rows the panel drew are the rows of
       the read model the panel was fed, field by field;
    3. **the capacity widgets** — what the panel draws is a number, and
       it is the number the read model carries;
    4. **the evidence** — the screenshots were written by this run.
    """
    # What the server says, read through the case's own session: what is
    # running, and how full the capacity is.
    state = world.scheduler_state()
    expected_used = str(state["capacity"]["used"])
    expected_running = len(state["running"])
    assert expected_running > 0, (
        f"the read model carries no running plan, so there is nothing for "
        f"the panel to be truthful about: {state!r}"
    )

    # -- 1. the walkthrough ------------------------------------------------
    #
    # A real browser, a real click, and the view the operator sees.
    walkthrough = _walkthrough(world, interpreter, expected_used)

    assert walkthrough["view_visible"] is True, (
        "the scheduling view is not visible after the operator pressed "
        "#btn-open-scheduler; the page says the view is still hidden"
    )
    assert walkthrough["used_visible"] is True, (
        "#scheduler-view-capacity-used is not visible in the scheduling "
        "view the operator opened"
    )

    # The capacity the panel draws is a number, and it is the number the
    # server says — not the ``0`` the page's own HTML starts with.
    assert walkthrough["home_used"].isdigit(), (
        f"the home page's capacity widget says {walkthrough['home_used']!r}, "
        f"which is not a number; it held {walkthrough['home_seen']!r}"
    )
    assert walkthrough["view_used"].isdigit(), (
        f"#scheduler-view-capacity-used says {walkthrough['view_used']!r}, "
        f"which is not a number; it held {walkthrough['view_seen']!r}"
    )
    assert walkthrough["home_used"] == expected_used, (
        f"the home page's capacity widget says {walkthrough['home_used']!r} "
        f"plans are running; the read model says {expected_used}; the "
        f"widget held {walkthrough['home_seen']!r}"
    )
    assert walkthrough["view_used"] == expected_used, (
        f"#scheduler-view-capacity-used says {walkthrough['view_used']!r} "
        f"plans are running; the read model says {expected_used}; the "
        f"widget held {walkthrough['view_seen']!r}"
    )

    # -- 2. the running rows ----------------------------------------------
    #
    # The panel draws one read model, and the rows on the screen belong to
    # that model and to no other.  The comparison is therefore made
    # against the model the panel was **fed** — the one the server
    # answered the panel's own poll with.
    started = time.time()
    report = world.drive([], parity=True)

    panel_state = report["panel_state"]
    assert panel_state is not None, (
        "the driver did not report the read model the panel was fed; the "
        "panel's rows cannot be compared with a model that was never "
        "captured"
    )
    served_running = list(panel_state.get("running") or [])
    assert served_running, (
        f"the read model the panel was fed carries no running plan: "
        f"{panel_state!r}"
    )

    panel_running = report["running"]
    assert panel_running, (
        "the panel drew no running plan. A panel that draws nothing "
        "agrees with a model that carries nothing, so the comparison "
        "below would hold and mean nothing."
    )
    assert len(panel_running) == expected_running, (
        f"the panel draws {len(panel_running)} running plans; the read "
        f"model says {expected_running} are running"
    )

    matched, why = _panel_rows_match(panel_running, served_running)
    assert matched, (
        f"the panel's running rows are not the rows of the read model it "
        f"was fed: {why}"
    )

    # -- 3. the capacity widgets ------------------------------------------
    #
    # What the panel draws in this run is a number too, and it is the
    # number the read model carries.  The panel draws the capacity
    # widgets out of the same read model it draws the running rows from,
    # so the two are one drawing and not two.
    api = report["api"]
    assert isinstance(api, dict) and "capacity" in api, (
        f"the API did not answer with a read model: {api!r}"
    )
    used = str(api["capacity"]["used"])
    assert report["view"]["used"].isdigit(), (
        f"#scheduler-view-capacity-used says {report['view']['used']!r}, "
        f"which is not a number"
    )
    assert report["view"]["used"] == used, (
        f"#scheduler-view-capacity-used says {report['view']['used']!r} "
        f"plans are running; the read model says {used}"
    )
    assert report["home"]["used"].isdigit(), (
        f"the home page's capacity widget says {report['home']['used']!r}, "
        f"which is not a number"
    )
    assert report["home"]["used"] == used, (
        f"the home page's capacity widget says {report['home']['used']!r} "
        f"plans are running; the read model says {used}"
    )

    # -- 4. the evidence ---------------------------------------------------
    #
    # The screenshots are evidence, and evidence that was not written by
    # this run is not this run's evidence: a file left over from an
    # earlier run would satisfy a bare existence check.
    for name in ("home.png", "scheduler.png"):
        path = SCREEN_DIR / name
        assert path.is_file(), f"{path} was not written"
        assert path.stat().st_size > 0, f"{path} is empty"
        written_at = path.stat().st_mtime
        assert written_at >= started - 1.0, (
            f"{path} was not written by this run: it was last written at "
            f"{written_at}, before the run started at {started}"
        )

    walkthrough_screenshot = Path(walkthrough["screenshot"])
    assert walkthrough_screenshot.is_file(), (
        f"{walkthrough_screenshot} was not written"
    )
    assert walkthrough_screenshot.stat().st_size > 0, (
        f"{walkthrough_screenshot} is empty"
    )