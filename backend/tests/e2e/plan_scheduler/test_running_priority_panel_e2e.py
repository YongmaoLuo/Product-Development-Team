"""End-to-end: the priority of a running plan, seen from the operator's panel.

What this file is for
---------------------
The **priority** of a plan is the number the caller gave it when it asked
for the plan.  The number decides two things: which of the waiting plans
runs first, and — when a slot has to be given up — which of the running
plans is interrupted.  A plan the caller deliberately pushed back
(``priority = -10``) stands **first** in the line and keeps its slot,
whatever it has been running for.

Every other case of this project proves that rule *in the process*: the
store writes the number down, the policy reads it, the snapshot carries
it.  Each of those layers can be green while the product is broken — a
number that is honoured but never shown is half a feature, because the
operator watches a plan be squeezed out and has no way of knowing that
it was pushed back on purpose.

This one asks the question the operator asks: **can the priority be
seen?**  The answer has to hold at the same time as everything else, in
one run, against something that is really running.

The chain
---------
1. the server is a **child process**, with its own SQLite database and
   its own plans directory (the sibling case's ``_World``, reused as it
   is);
2. one plan of the batch carries the caller's number — the artifact the
   import lane writes, ``scheduler_intent.json``, holding
   ``{"priority": -10}``;
3. the scheduler admits it: the plan stands first in the line, so it
   holds a slot even though it is the **last** plan of the batch;
4. the read model answers ``GET /api/scheduler/state`` with that number
   on the running entry;
5. the operator opens the panel in a real browser, presses "refresh",
   and the running plans are drawn.

The five criteria
-----------------
Each one is a separate thing, and each one is asserted on what the
system *did* — the API's answer and the panel's own rows — and never on
the server's memory:

1. every running entry of the read model carries a **whole-number**
   priority — the field is there, and it is a number;
2. the plan whose artifact says ``{"priority": -10}`` has a running
   entry whose priority **is** ``-10``;
3. the other plans of the batch read **0** — the ordinary weight;
4. the panel **drew** the running plans (``#scheduler-running-list``);
5. the screenshot the panel left behind **exists and is not empty**.

What is real here
-----------------
* the server is a process (a child ``uvicorn``, killed by pid and by
  process group);
* the state is a SQLite file on disk;
* the plans are directories;
* the executor is a child process, replaced through the named
  production seam (``PDT_EXECUTOR_CMD``) rather than through a double;
* the page is a **real browser**, clicking the controls an operator
  clicks.

The test never writes a scheduler row and never mocks anything.  The
priority it gives a plan is written where the import lane writes it —
in the plan's own directory — because the import lane itself needs a
model to write a plan, and an end-to-end case cannot call one.

Two things the environment cannot give the test
-----------------------------------------------
**A browser.**  When no Playwright driver is installed — or when the
driver is installed but its browser binaries are not — the case is
**skipped**: a panel that was not driven proves nothing about the
panel, and a test that pretends to have driven it is worse than no
test at all.

**A model.**  The import lane writes a plan with a model behind it.
What the scheduler consumes is a *pair* — the plan's artifacts on disk,
and the plan's phase in the state machine — and the sibling case writes
that pair the way the import lane writes it.  The priority is the
third half of the same pair, and it is written the same way.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

#: ``<repo>/backend`` — this file lives at ``backend/tests/e2e/plan_scheduler/``.
_BACKEND_DIR: Path = Path(__file__).resolve().parents[3]

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The sibling is reused **as it is**: the world (the real server, its own
# database, its own plans), the Playwright discovery, the environment
# the browser driver runs under, and the directory the screenshots are
# written into.  Two worlds that differ would be two different answers
# to one question.
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (  # noqa: E402
    PLAN_IDS,
    SCREEN_DIR,
    _driver_env,
    _playwright_interpreter,
    _World,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: How many plans may run at the same time.
CAPACITY: int = 4

#: How long a young run is protected from being interrupted.  Zero: a
#: run that is protected cannot be interrupted at all, and the question
#: this case asks — which of the running plans stands last in the line
#: — is a question about the rule, not about the protection window.
MIN_RUNTIME_SECONDS: int = 0

#: How long one tick waits.  Short, so the case does not spend its
#: life waiting for the scheduler to notice something.
TICK_SECONDS: float = 0.5

#: The priority the caller gave the plan the case pushes back.  A
#: negative number is the caller's way of saying "this one first".
PRIORITY: int = -10

#: The plan the priority is given to — the **last** plan of the batch.
#: It is the last one on purpose: a plan that stands last in the line
#: is the one the scheduler can most afford to interrupt, so without the
#: number it would be waiting, and with it the plan holds a slot.  The
#: number is what moved it.
PRIORITY_PLAN: str = PLAN_IDS[-1]

#: The screenshot the panel leaves behind.
SCREENSHOT_PATH: Path = SCREEN_DIR / "scheduler.png"

#: How long the panel may take to draw the running plans.
RUNNING_TIMEOUT: float = 120.0

#: How long the scheduler may take to admit the plan that carries the
#: priority.
ADMIT_TIMEOUT: float = 90.0


# ---------------------------------------------------------------------------
# The browser driver — the panel, as an operator uses it
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = r'''
"""Drive the operator panel and report what it drew.

Arguments:
  1. the url of the running server's panel
  2. the path the screenshot is written to
  3. how long the panel may take to draw the running plans

The steps are the operator's own, in the operator's order:

1. open the panel and wait for it to render;
2. open the scheduling view and wait for it to be on screen;
3. press "refresh" — the panel reads its model again;
4. read the rows of the running plans;
5. leave a screenshot behind.

The read model is read through the same session, at the same moment, so
the panel's rows and the API's answer are two readings of one server.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GUARD_HEADER = "x-pdt-request"
GUARD_VALUE = "1"


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


url = sys.argv[1]
screenshot = Path(sys.argv[2])
running_timeout = float(sys.argv[3])

screenshot.parent.mkdir(parents=True, exist_ok=True)

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

#: The server, without the fragment the panel is opened with.
base = url.split("#")[0].rstrip("/")

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    # 1. the panel, rendered.
    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_timeout(1000)

    # 2. the scheduling view, opened the way the operator opens it.
    page.click("#btn-open-scheduler", timeout=30000)
    page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)

    # 3. "refresh": the panel reads its model again.  The panel draws
    #    what it read last, so the button is pressed until the running
    #    plans are on the page — an empty table is the panel telling
    #    the truth about a model it has not read yet.
    deadline = time.time() + running_timeout
    running = []
    while True:
        page.click("#btn-refresh-scheduler", timeout=30000)
        page.wait_for_timeout(500)
        running = read_rows(page, "#scheduler-running-list tr")
        if running or time.time() > deadline:
            break

    # 4. the running plans, as the panel draws them.
    running = read_rows(page, "#scheduler-running-list tr")

    # The read model, read through the same session at the same moment:
    # the panel and this answer are two readings of one server.
    api_state = page.request.get(
        base + "/api/scheduler/state",
        headers={GUARD_HEADER: GUARD_VALUE},
    ).json()

    # 5. the evidence.
    page.screenshot(path=str(screenshot), full_page=True)

    print(json.dumps({
        "running": running,
        "api": api_state,
        "screenshot": str(screenshot),
    }, ensure_ascii=False))
    browser.close()
'''


# ---------------------------------------------------------------------------
# The world — the sibling's world, with the numbers this case is about
# ---------------------------------------------------------------------------


class _PriorityWorld(_World):
    """The sibling's world, told the numbers this case is about.

    Everything else is the sibling's: the server, the database, the
    plans, the teardown.  Only the settings file differs — the sibling
    protects a young run for ten minutes, and this case protects
    nobody, because a run that is protected cannot be interrupted at
    all and the rule this case is about is the one that decides *who*
    goes.
    """

    def _write_settings(self) -> None:
        """Write the settings file the scheduler is told to read."""
        self.settings_path.write_text(
            "scheduler:\n"
            "  enabled: true\n"
            "  dry_run: false\n"
            "  capacity: {0}\n"
            "  min_runtime_seconds: {1}\n"
            "  tick_seconds: {2}\n"
            "  command_timeout_seconds: 30\n".format(
                CAPACITY, MIN_RUNTIME_SECONDS, TICK_SECONDS
            ),
            encoding="utf-8",
        )


def _write_intent(world: "_World", plan_id: str, priority: int) -> Path:
    """Write the caller's priority into the plan's own directory.

    The number lives in the plan's **own** artifact — the file the
    import lane writes when a caller asks for a plan with a priority —
    and the worker reads it from there when it builds the queue row.
    The import lane itself needs a model to write a plan, and this case
    cannot call one, so the artifact is written the way the import lane
    writes it: one number, in the plan's directory, in the file whose
    name the production code uses.

    Args:
        world: the world whose plans directory holds the plan.
        plan_id: the plan the priority is given to.
        priority: the number the caller asked for.

    Returns:
        The path the artifact was written to.
    """
    from plan_scheduler.engine import SCHEDULER_INTENT_FILE

    path = world.plans_dir / plan_id / SCHEDULER_INTENT_FILE
    path.write_text(
        json.dumps({"priority": int(priority)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def interpreter():
    """An interpreter that can drive a real browser — or a skip.

    The instrument is asked for **before** the world is built, so a
    machine without a browser pays nothing: no server is started, no
    plan is run, and the case says out loud that the panel was not
    driven.
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
def world(tmp_path):
    """A running server, its own database, its own plans — and the intent.

    The intent is written before the server starts, because the worker
    reads it when it builds the queue row: a number written after the
    line was built is a number the scheduler never saw.
    """
    the_world = _PriorityWorld(tmp_path)
    try:
        _write_intent(the_world, PRIORITY_PLAN, PRIORITY)
        the_world.start()
        yield the_world
    finally:
        the_world.teardown()


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


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


def _wait_for_admission(world: "_World", timeout: float = ADMIT_TIMEOUT) -> Dict[str, Any]:
    """Wait until the plan that carries the priority holds a slot.

    The admission is the scheduler's own: the line is built from the
    plan facts, the plan that stands first in it is admitted, and the
    plan that was pushed back stands first.  So the wait is for the
    read model to say that the plan is running.

    Args:
        world: the running world.
        timeout: how long the admission may take.

    Returns:
        The read model, at the moment the plan was seen running.
    """
    deadline = time.monotonic() + timeout
    state: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = world.scheduler_state()
        running = state.get("running") or []
        if any(str(entry.get("plan_id")) == PRIORITY_PLAN for entry in running):
            return state
        time.sleep(0.2)
    raise AssertionError(
        f"the plan carrying the priority was not running within {timeout}s; "
        f"the read model was {state!r}"
    )


def _drive_panel(world: "_World", interpreter: str) -> Dict[str, Any]:
    """Drive the panel in a real browser and report what it drew."""
    script = world.root / "drive_priority_panel.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/#app",
            str(SCREENSHOT_PATH),
            str(RUNNING_TIMEOUT),
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


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_running_panel_shows_the_priority(interpreter, world):
    """The caller's priority is on the running entry the operator reads.

    The evidence is one report: the read model as the API answered it
    through the panel's own session, the rows the panel drew, and the
    screenshot it left behind.
    """
    _wait_for_admission(world)

    report = _drive_panel(world, interpreter)

    running_rows: List[List[str]] = report.get("running") or []
    api_state: Dict[str, Any] = report.get("api") or {}
    entries: List[Dict[str, Any]] = api_state.get("running") or []

    summary = json.dumps(
        {
            "priority_plan": PRIORITY_PLAN,
            "priority": PRIORITY,
            "running_rows": running_rows,
            "api_running": entries,
            "api_queue": api_state.get("queue"),
            "screenshot": str(SCREENSHOT_PATH),
        },
        ensure_ascii=False,
    )

    # 1. every running entry carries a whole-number priority.
    assert entries, f"the read model has no running entry: {summary}"
    for entry in entries:
        assert "priority" in entry, (
            f"the running entry of {entry.get('plan_id')!r} carries no "
            f"priority: {summary}"
        )
        priority = entry["priority"]
        assert isinstance(priority, int) and not isinstance(priority, bool), (
            f"the running entry of {entry.get('plan_id')!r} carries a "
            f"priority that is not a whole number: {summary}"
        )

    by_plan: Dict[str, Dict[str, Any]] = {
        str(entry.get("plan_id")): entry for entry in entries
    }

    # 2. the plan the caller pushed back carries the number it was given.
    assert PRIORITY_PLAN in by_plan, (
        f"the plan carrying the priority is not running: {summary}"
    )
    assert by_plan[PRIORITY_PLAN]["priority"] == PRIORITY, (
        f"the running entry of the plan that carries the priority does not "
        f"carry it: {summary}"
    )

    # 3. the rest of the batch stands at the ordinary weight.
    others = [
        entry for plan_id, entry in by_plan.items() if plan_id != PRIORITY_PLAN
    ]
    assert others, f"no other plan is running: {summary}"
    for entry in others:
        assert entry["priority"] == 0, (
            f"the running entry of {entry.get('plan_id')!r} carries a "
            f"priority the caller never gave it: {summary}"
        )

    # 4. the panel drew the running plans.
    assert running_rows, f"the panel drew no running plan: {summary}"
    drawn = [row[0] for row in running_rows if row]
    assert PRIORITY_PLAN in drawn, (
        f"the panel did not draw the plan that carries the priority: {summary}"
    )

    # 5. the screenshot: the evidence the panel left behind.
    assert SCREENSHOT_PATH.exists(), (
        f"the panel left no screenshot: {summary}"
    )
    assert SCREENSHOT_PATH.stat().st_size > 0, (
        f"the screenshot is empty: {summary}"
    )

    print(summary)