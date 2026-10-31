"""End-to-end: the operator's panel, the four actions, and a ``kill -9``.

What this file is for
---------------------
Every other test of the native scheduler proves **one layer**: the
store writes a row, the command executor carries out an action, the
snapshot builder assembles the read model, the routes turn an outcome
into a status.  Each of those layers can be green while the *product*
is broken — the panel can show an order the admission check does not
use, a restart can drop the line, an action can be unreachable from the
page, and the executor that runs the plans can be something other than
the one the operator believes is running.

This file asks the question the product asks: can the acceptance
criteria hold **at the same time**, in one run, against something that
is really running?

The eight phases
----------------
``test_operator_full_flow`` walks them in order:

1. **the import** — a plan's artifacts and its phase in the state
   machine are what the scheduler consumes;
2. **the enqueue** — the scheduler puts the waiting plans into the
   line, and the line is the scheduler's own;
3. **the backfill** — a plan that was waiting without a line joins the
   tail, in the order the plan facts give;
4. **the promotion** — a promotion moves a plan to the front of the
   line and takes no slot;
5. **the pause** — a parked plan stops waiting and its line is gone;
6. **the resume** — a parked plan comes back **at the front**;
7. **the refused start** — a start that may interrupt nobody is
   refused with ``blocked_by_capacity``, and nobody is evicted;
8. **the restart** — a ``kill -9`` loses neither the line nor the
   running plans: the line is used as it stands, and the running plans
   are adopted and timed from the moment of the restart.

``test_panel_and_api_parity`` asks the panel and the API the same
question and compares the answers field by field.
``test_queue_survives_kill9`` is the restart on its own: row by row,
before and after.
``test_seam_selfcheck`` is the question every other phase rests on: is
the process the scheduler recorded really the stand-in this suite
installed?

How the four actions are issued
-------------------------------
The panel draws a waiting plan's buttons in its **queue** row: a
promotion and a start.  It draws a pause for a plan that holds a slot,
and it draws no resume at all.

The promotion and the start are therefore **pressed** — the queue
row's own ``.scheduler-action`` button, watched on its way out, so the
report can say that the request really carried the guard's header.
The pause and the resume are **issued** as commands: the same endpoint
the button would have called, the same header, the same server — what
is missing is the button, not the product.  The report says which of
the two each action was, so the two are never confused for one another.

What is real here
-----------------
Everything but the executor, and the executor is replaced through the
named production seam (``PDT_EXECUTOR_CMD``) rather than through a
double — the gate in ``backend/tests/static_gates/`` says so, and this
file obeys it:

* the server is a **process** (a child ``uvicorn``, killed by pid and
  by process group);
* the state is a **SQLite file** on disk;
* the plans are **directories**;
* the executor is a **child process** with a pid the operating system
  knows, running in a process group of its own;
* the page is a **real browser** (Playwright, when a driver is
  installed), clicking the buttons an operator clicks.

The test never writes a scheduler row.  Every row the scheduler's
tables carry was written by the scheduler; the test only reads them.

Two things the environment cannot give the test
-----------------------------------------------
**The import lane needs a model.**  ``POST /api/plan/import`` writes a
plan with a model behind it, and an end-to-end case cannot call a
model.  What the scheduler consumes is a *pair* — the plan's artifacts
on disk, and the plan's phase in the state machine — and this file
writes that pair the way the import lane writes it: the artifacts the
import lane writes, the bootstrap insert the state machine itself
performs for a plan that has just entered it (through the production
repository, not through the test's own SQL), and the state machine's
own transition to ``queued``.

The import lane also puts the new plan into the line itself.  That
write is not made here: a line the test wrote would be a fact the test
made up, and the whole point of the enqueue and backfill phases is
that the line is the **scheduler's** own.  The line in this case is
therefore the one the scheduler's reconciliation builds, which is the
path a plan that was waiting without a line takes — and the backfill
phase asserts that it did so in the order the plan facts give.

**The settings file is the backend's own.**  The scheduler reads the
``scheduler:`` section of ``backend/config.yaml``, and the path it
reads is a seam the server exposes for exactly this purpose
(``server._scheduler_settings_path`` — the seam the repository's own
integration cases use).  The server this file boots is pointed at a
settings file of its own, so the case can say what the scheduler is
told without touching the repository's file.  The seam is *named*, not
replaced: no component of the application is swapped for a double.
The numbers in that file are the ones this case is about — the
scheduler on, not a dry run, four plans at a time, a young run
protected for ten minutes, a half-second tick — because the
repository's own file leaves the scheduler switched off.

The seam self-check
-------------------
The first thing every phase rests on is that the executor really is
the fixture: the pid the scheduler recorded must be a live process
whose command line names ``deterministic_executor.py`` and does not
name ``cli.py``.  When that check fails the run is worthless, so it
fails **loudly and in every phase** — not only in the case that owns
the check.

A note on where the pid comes from
----------------------------------
The brief for this case named ``execution.json``; that file is gone.
The pid the production spawn records lives in the state database
(``plan_execution.exec_pid``), and that is where this file reads it
from: the record the *scheduler* keeps, not a side file the test
writes.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The paths
# ---------------------------------------------------------------------------

#: ``<repo>/backend`` — this file lives at ``backend/tests/e2e/plan_scheduler/``.
BACKEND_DIR: Path = Path(__file__).resolve().parents[3]

#: ``<repo>`` — the checkout the server is booted from.
REPO_ROOT: Path = BACKEND_DIR.parent

#: The stand-in executor this suite installs through ``PDT_EXECUTOR_CMD``.
EXECUTOR_FIXTURE: Path = (
    BACKEND_DIR / "tests" / "e2e" / "fixtures" / "deterministic_executor.py"
)

#: The scratch root the case uses, and where the evidence is kept.
#:
#: The path is the one the case was told to use; it is not a path into
#: anybody's home directory, and nothing in the repository depends on
#: it — the screenshots are evidence, not an input.
E2E_ROOT: Path = Path(os.environ.get("PDT_E2E_ROOT", "/tmp/pdt-e2e"))

#: Where the panel's screenshots are written.
SCREEN_DIR: Path = E2E_ROOT / "screens"

#: The interpreter the server and the executor run on.  The project's own
#: virtualenv when there is one, the interpreter running pytest otherwise
#: — a checkout without a virtualenv must still boot a server rather
#: than fail before it starts.
_VENV_PYTHON: Path = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON: Path = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: The plans the case imports.  Six plans against a capacity of four
#: leaves two waiting — which is what makes the line, the promotion and
#: the refused start observable at all.
PLAN_IDS: Tuple[str, ...] = tuple("pdt-e2e-{0}".format(n) for n in range(1, 7))

#: How many plans may run at the same time.
CAPACITY: int = 4

#: How long a young run is protected from being interrupted.  Long
#: enough that every run in this case is young: the refused start is
#: refused because *nobody* may be interrupted, not because one
#: particular plan was chosen.
MIN_RUNTIME_SECONDS: int = 600

#: How long one tick waits.  Short, so the case does not spend its
#: life waiting for the scheduler to notice something.
TICK_SECONDS: float = 0.5

#: The header every ``/api/*`` request must carry.
REQUEST_HEADER: str = "X-PDT-Request"
REQUEST_HEADER_VALUE: str = "1"

#: The state machine's phase a plan waits in.
QUEUED_PHASE: str = "queued"

#: The phase a parked plan is in.
STOPPED_PHASE: str = "stopped"

#: The phase a working plan is in.
EXECUTING_PHASE: str = "executing"

#: The outcome a refused start is answered with.
BLOCKED_BY_CAPACITY: str = "blocked_by_capacity"

#: The outcome a carried-out action is answered with.
APPLIED: str = "applied"


# ---------------------------------------------------------------------------
# The launcher — the server, pointed at a settings file of its own
# ---------------------------------------------------------------------------

_LAUNCHER_SOURCE = r'''
"""Boot the backend with the scheduler's settings file pointed elsewhere.

The scheduler reads the ``scheduler:`` section of the backend's own
``config.yaml``.  The path it reads is a seam the server exposes
(``server._scheduler_settings_path``), so this run points it at a file
of its own: the case can then say what the scheduler is told without
touching the repository's configuration.

Nothing is replaced.  The seam is *named*, exactly the way the
repository's own integration cases name it.
"""
import sys
from pathlib import Path

backend_dir = Path(sys.argv[1])
settings_path = Path(sys.argv[2])
port = int(sys.argv[3])

sys.path.insert(0, str(backend_dir))
sys.path.insert(0, str(backend_dir.parent))

import server

server._scheduler_settings_path = lambda: settings_path

import uvicorn

uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
'''


# ---------------------------------------------------------------------------
# The browser driver — the panel, as an operator uses it
# ---------------------------------------------------------------------------

_PANEL_DRIVER_SOURCE = r'''
"""Drive the operator panel and report what it did.

Arguments:
  1. the base url of the running server
  2. a JSON list of steps: ``[{"action": ..., "plan": ..., ...}, ...]``
  3. the directory the screenshots are written into

A step names an action and a plan, and says how the action is
issued:

* ``"via": "panel"`` (the default) — the button the panel drew is
  pressed.  A waiting plan's buttons are drawn in its **queue** row,
  so the button that is pressed is the queue row's own
  ``.scheduler-action`` button;
* ``"via": "api"`` — the command is issued as a command.  Two of the
  four actions have no button to press: the panel draws a promotion
  and a start for a waiting plan, and a pause for a plan that holds a
  slot, and it draws no resume at all.  Such a command goes to the
  endpoint the button would have called, with the request guard's
  header, on the same session — only the button is missing.

Every step reads the panel back afterwards: what the action box says,
what the line looks like now, and what is running.

The requests the panel's buttons issue are intercepted on the way out,
so the report can say whether they carried the request guard's header.
The commands the case issues itself are recorded the same way, with
the headers the request really carried.  The read model is read
through the same session as well, at the same moment, so the panel and
the API can be compared without the clock moving between the two
reads.
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


def read_capacity(page, prefix):
    """The capacity widget's numbers."""
    return {
        "used": page.inner_text(prefix + "-used").strip(),
        "total": page.inner_text(prefix + "-total").strip(),
        "queue": page.inner_text(prefix + "-queue").strip(),
        "over": page.inner_text(prefix + "-over").strip(),
        "over_visible": page.is_visible(prefix + "-over"),
    }


def read_queue(page):
    """The line, as the panel draws it."""
    return read_rows(page, "#scheduler-queue-list tr")


def read_running(page):
    """The running plans, as the panel draws them."""
    return read_rows(page, "#scheduler-running-list tr")


def outcome_text(page):
    """What the action box says."""
    return page.inner_text("#scheduler-action-outcome").strip()


def queue_plan_ids(page):
    """The plans the line holds, in the order the panel draws them."""
    return [row[1] for row in read_queue(page) if len(row) > 1]


def expectation_met(page, step, api_outcome):
    """Whether the step's expectation holds on the panel, right now.

    A step the panel drove is judged by what the panel says — the
    action box is the panel's own answer, and it is read again on
    every turn of the waiting loop, because the box is written when
    the command's answer reaches the page.  A step that was issued as
    a command has no such box: the answer came back from the server,
    and that answer is what the step is judged by.
    """
    if "expect_outcome" in step:
        outcome = api_outcome if step.get("via") == "api" else outcome_text(page)
        if outcome != step["expect_outcome"]:
            return False
    if "expect_queue_head_seq" in step:
        rows = read_queue(page)
        if not rows or len(rows[0]) < 3 or rows[0][2] != str(step["expect_queue_head_seq"]):
            return False
    if "expect_queue_head_plan" in step:
        rows = read_queue(page)
        if not rows or len(rows[0]) < 2 or rows[0][1] != str(step["expect_queue_head_plan"]):
            return False
    if "expect_queue_absent" in step:
        if str(step["expect_queue_absent"]) in queue_plan_ids(page):
            return False
    return True


def settle(page, step, api_outcome="", timeout=30.0):
    """Wait until the panel has stopped changing and the step's expectation holds.

    The panel reloads the read model and the listing after every
    command, so "the click is over" and "the panel shows the result" are
    two different moments.  The panel is watched until it draws the same
    thing twice in a row *and* the expectation holds.

    The panel is asked to read its model again on every turn of the
    loop: the listing it reads is served from a short-lived cache, so
    the panel's own reload may hand it the answer from before the
    command.  Opening the view again is what the operator does when the
    panel does not move.

    The waiting is done with the browser's own clock rather than with
    ``time.sleep``: the synchronous Playwright driver only hands its
    events to the page while the page is being asked something, so a
    plain sleep would leave the browser's answers (the network events)
    undelivered.
    """
    deadline = time.time() + timeout
    previous = None
    while time.time() < deadline:
        page.click("#btn-open-scheduler", timeout=30000)
        current = json.dumps(
            {"queue": read_queue(page), "running": read_running(page)},
            sort_keys=True,
            ensure_ascii=False,
        )
        if current == previous and expectation_met(page, step, api_outcome):
            return
        previous = current
        page.wait_for_timeout(400)
    raise AssertionError(
        "the panel never showed what the step expected: "
        + json.dumps(
            {
                "step": step,
                "outcome": outcome_text(page),
                "queue": read_queue(page),
                "running": read_running(page),
            },
            ensure_ascii=False,
        )
    )


url = sys.argv[1]
steps = json.loads(sys.argv[2])
screens = Path(sys.argv[3])
screens.mkdir(parents=True, exist_ok=True)

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

seen = []
answered = []
api_actions = []

#: Who issued the request that is on its way out right now: the panel's
#: own button, or the command the case issues itself.  The route
#: handler reads it, so every request is labelled by whoever sent it.
issued_by = {"source": "panel"}


def is_action(url):
    """Whether the URL is one of the four actions' URLs."""
    return "/api/plan/" in url and "/scheduler/" in url


def detail_text(detail):
    """One detail, in the words a reader reads."""
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False)


def press_queue_button(page, action, plan):
    """Press the button the queue row of *plan* draws.

    The panel draws a waiting plan's buttons in its queue row: a
    promotion and a start.  The button is waited for, pressed, and the
    request it made is watched on its way out — the intercepted request
    is the proof that the button was really pressed, and the answer
    that comes back is the proof that the command was carried out.
    """
    selector = (
        "#scheduler-queue-list button.scheduler-action"
        "[data-action='{0}'][data-plan='{1}']"
    ).format(action, plan)
    page.wait_for_selector(selector, timeout=30000)
    before = len(seen)
    before_answered = len(answered)
    page.click(selector, timeout=30000)
    # The command left the browser: the intercepted request is the
    # proof that the button was really pressed.  Then the answer came
    # back: until it has, the panel is still showing the state the
    # command has not changed yet.
    deadline = time.time() + 30
    while len(seen) == before and time.time() < deadline:
        page.wait_for_timeout(50)
    if len(seen) == before:
        raise AssertionError(
            "the button was pressed but no request left the browser: " + selector
        )
    deadline = time.time() + 30
    while len(answered) == before_answered and time.time() < deadline:
        page.wait_for_timeout(50)
    if len(answered) == before_answered:
        raise AssertionError(
            "the request left the browser but no answer came back: " + selector
        )
    return selector


def issue_command(page, base_url, action, plan):
    """Issue one command the panel draws no button for.

    The command goes to the endpoint the button would have called, on
    the same session and with the request guard's header — what is
    missing is the button, not the product.

    It is sent **from the page**, so it is a request of the browser
    like any other: it is intercepted on its way out, the headers it
    really carried are read off the intercepted request, and the report
    can say about it what it says about the requests the buttons made.
    """
    path = base_url.rstrip("/") + "/api/plan/{0}/scheduler/{1}".format(plan, action)
    before = len(seen)
    before_answered = len(answered)
    issued_by["source"] = "command"
    try:
        answer = page.evaluate(
            """async (arg) => {
                   const response = await fetch(arg.path, {
                       method: "POST",
                       headers: {[arg.header]: arg.value},
                   });
                   const text = await response.text();
                   let body = {};
                   try { body = JSON.parse(text); } catch (err) { body = {raw: text}; }
                   return {status: response.status, body: body};
               }""",
            {"path": path, "header": GUARD_HEADER, "value": GUARD_VALUE},
        )
    finally:
        issued_by["source"] = "panel"

    # The command left the browser: the intercepted request is the
    # proof that it was really sent.  Then the answer came back.
    deadline = time.time() + 30
    while len(seen) == before and time.time() < deadline:
        page.wait_for_timeout(50)
    if len(seen) == before:
        raise AssertionError(
            "the command was issued but no request left the browser: " + path
        )
    deadline = time.time() + 30
    while len(answered) == before_answered and time.time() < deadline:
        page.wait_for_timeout(50)
    if len(answered) == before_answered:
        raise AssertionError(
            "the request left the browser but no answer came back: " + path
        )

    request = seen[-1]
    body = answer.get("body") or {}
    api_actions.append({
        "url": request["url"],
        "method": request["method"],
        "guard": request["guard"],
        "status": answer.get("status"),
        "outcome": str(body.get("outcome") or ""),
    })
    return body


with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page()

    def record(route, request):
        seen.append({
            "url": request.url,
            "method": request.method,
            "guard": request.headers.get(GUARD_HEADER),
            "source": issued_by["source"],
        })
        route.continue_()

    def answered_request(response):
        if is_action(response.url):
            answered.append(response.url)

    page.on("response", answered_request)
    page.route("**/api/plan/*/scheduler/*", record)

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_timeout(1000)

    # The home page first: the capacity widget lives there.
    home = read_capacity(page, "#scheduler-capacity")
    page.screenshot(path=str(screens / "home.png"), full_page=True)

    # Then the scheduler view, the way the operator opens it.
    page.click("#btn-open-scheduler")
    page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)
    page.wait_for_timeout(1000)
    view = read_capacity(page, "#scheduler-view-capacity")
    queue = read_queue(page)
    running = read_running(page)
    page.screenshot(path=str(screens / "scheduler.png"), full_page=True)

    # The read model, read through the same session at the same moment:
    # the panel and this answer are two readings of one server.
    api_state = page.request.get(
        url.rstrip("/") + "/api/scheduler/state",
        headers={GUARD_HEADER: GUARD_VALUE},
    ).json()

    observations = []
    for step in steps:
        action = step["action"]
        plan = step["plan"]
        via = step.get("via", "panel")
        started = time.time()
        if via == "api":
            # The panel draws no button for this one, so the command is
            # issued — on the same session, with the same header.
            body = issue_command(page, url, action, plan)
            api_outcome = str(body.get("outcome") or "")
            detail = detail_text(body.get("detail"))
        else:
            press_queue_button(page, action, plan)
            api_outcome = ""
            detail = ""
        # The panel reads its model again when the view is opened, and
        # the listing it reads is served from a short-lived cache — so
        # the view is opened again to see what the command produced.
        settle(page, step, api_outcome)
        if via == "api":
            outcome = api_outcome
        else:
            outcome = outcome_text(page)
            detail = page.inner_text("#scheduler-action-detail").strip()
        observations.append({
            "action": action,
            "plan": plan,
            "via": via,
            "outcome": outcome,
            "detail": detail,
            "capacity": read_capacity(page, "#scheduler-view-capacity"),
            "queue": read_queue(page),
            "running": read_running(page),
            "seconds": round(time.time() - started, 2),
        })
        page.screenshot(path=str(screens / ("step-" + action + ".png")), full_page=True)

    print(json.dumps({
        "home": home,
        "view": view,
        "queue": queue,
        "running": running,
        "api": api_state,
        "steps": observations,
        "requests": [entry for entry in seen if entry["source"] == "panel"],
        "api_actions": api_actions,
    }, ensure_ascii=False))
    browser.close()
'''


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    """Whether the port can be bound again.

    The check binds the way a *server* binds — with ``SO_REUSEADDR`` —
    because that is the question being asked.  A plain bind would answer
    "no" for a port whose connections are still in ``TIME_WAIT``, which
    says nothing about whether anything is still listening on it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _utcnow_iso() -> str:
    """Now, the way the state database writes timestamps."""
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _rows(db_path: Path, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Read rows out of the state database.

    The case only ever *reads* the scheduler's tables: a row the test
    wrote itself would be a fact the test made up, and a scheduler that
    reads a made-up row has demonstrated nothing.
    """
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]
    finally:
        connection.close()


def _group_members(pgid: int) -> List[int]:
    """The pids of every process in the process group *pgid*.

    Read from the process table rather than from anything a process
    said about itself: the group is a real group, and the membership is
    taken from where the operating system keeps it.
    """
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    members: List[int] = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        pid, group = int(fields[0]), int(fields[1])
        if group == pgid:
            members.append(pid)
    return members


def _process_command(pid: int) -> str:
    """The command line of the process *pid*, or the empty string.

    The two ``w`` options are load-bearing: ``ps`` cuts the command
    column down to the width of the screen it believes it is writing to
    — eighty columns when it cannot find one — and the stand-in's
    command line is longer than that, so a cut line ends halfway through
    the file name.  The cut command is not a wrong answer, it is a
    partial one, and a partial command line is read as "this is not the
    process I installed".  ``-ww`` turns the cutting off.
    """
    result = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _process_pgid(pid: int) -> Optional[int]:
    """The process group *pid* belongs to, or ``None``."""
    result = subprocess.run(
        ["ps", "-o", "pgid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    fields = result.stdout.split()
    if not fields:
        return None
    try:
        return int(fields[0])
    except ValueError:
        return None


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with (the
    hermetic environment redirects it), so that Playwright finds the
    browser binaries it installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        env["HOME"] = real_home
    return env


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    Two candidates, in order: the interpreter running pytest, and the
    interpreter behind a ``playwright`` command on ``PATH`` — the
    command is a Python entry point, so its shebang names an
    interpreter that has the package installed.  Nothing is hard-coded:
    both are read out of the environment.
    """
    candidates = [sys.executable]

    command = shutil.which("playwright")
    if command:
        try:
            first_line = Path(command).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.startswith("#!"):
            parts = shlex.split(first_line[2:].strip())
            interpreter = ""
            if parts:
                if os.path.basename(parts[0]) == "env" and len(parts) > 1:
                    interpreter = parts[1]
                else:
                    interpreter = parts[0]
            if "python" in interpreter:
                candidates.append(interpreter)

    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright.sync_api"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def _http(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: Optional[bytes] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any]:
    """One HTTP request against the running server; return ``(status, body)``.

    Every request carries the guard's header — the same header the
    frontend's own API client carries, and the one the server refuses to
    answer without.
    """
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={
            REQUEST_HEADER: REQUEST_HEADER_VALUE,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read() if exc.fp else b""
    try:
        return status, json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, payload


def _phase_of(world: "_World", plan_id: str) -> str:
    """The phase the state machine holds for one plan.

    The panel does not draw a phase any more — the two tables that
    remain are the queue and the running plans, and neither of them
    carries one — so the phase is read where it is written: the state
    machine's own routing table.
    """
    for row in world.routing_rows():
        if str(row["plan_id"]) == plan_id:
            return str(row["current_phase"])
    return ""


#: How many seconds each unit of a duration stands for.
_DURATION_UNITS = {"h": 3600, "m": 60, "s": 1}


def _duration_seconds(text: str) -> Tuple[Optional[int], int]:
    """The panel's duration text, in seconds, and the slack it left out.

    The panel draws how long a plan has been running as a duration —
    ``45s``, ``1m 3s``, ``2h 5m`` — and the read model carries a
    number of seconds.  The two are the same fact, so comparing them
    needs the text turned back into seconds.

    The drawing leaves out what it does not show: an hour and a half is
    drawn as ``1h 30m``, and the seconds are gone.  The second number
    this returns is how many seconds the drawing may have left out —
    the comparison may be that much off and still be right.
    """
    total = 0
    units = set()
    for token in str(text).split():
        if not token or token[-1] not in _DURATION_UNITS:
            return None, 0
        try:
            value = int(token[:-1])
        except ValueError:
            return None, 0
        total += value * _DURATION_UNITS[token[-1]]
        units.add(token[-1])
    if not units:
        return None, 0
    slack = 0
    if "s" not in units:
        slack = 59 if "m" in units else 3599
    return total, slack


# ---------------------------------------------------------------------------
# The world: a server, a database, a plans tree, and the plans in them
# ---------------------------------------------------------------------------


class _World:
    """One end-to-end world.

    Everything in here is real: the server is a child process, the
    state is a SQLite file, the plans are directories, and the executor
    is a child process with a pid the operating system knows.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.plans_dir = self.root / "plans"
        self.db_path = self.root / "state.db"
        self.logs_dir = self.root / "logs"
        self.settings_path = self.root / "scheduler-settings.yaml"
        self.launcher_path = self.root / "launch_server.py"
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: Optional[subprocess.Popen] = None
        self._log_handle: Optional[Any] = None
        #: The order the plans were put in the line in, read from the
        #: plan facts *before* the scheduler ever ran.
        self.backfill_order: List[str] = []
        #: The phase each plan was imported into, read back before the
        #: scheduler ever ran.
        self.imported_phases: Dict[str, str] = {}

        for directory in (self.plans_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self._write_settings()
        self._write_launcher()
        self._import_plans()

    # -- the pieces ---------------------------------------------------------

    def _write_settings(self) -> None:
        """Write the settings file the scheduler is told to read.

        The numbers are the ones the case is about: the scheduler is on,
        it changes what it decides (not a dry run), four plans may run
        at once, a young run is protected for ten minutes, and the tick
        is short.
        """
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

    def _write_launcher(self) -> None:
        """Write the launcher that boots the server on this port."""
        self.launcher_path.write_text(_LAUNCHER_SOURCE, encoding="utf-8")

    def _import_plans(self) -> None:
        """Import the plans: their artifacts, and their place in the machine.

        Two halves, and both are the production ones:

        * the **artifacts** — the plan directory the import lane writes
          (``prd.json``, ``tasks.json``) and the project directory the
          plan works in;
        * the **phase** — the row the state machine holds for a plan
          that has just entered it, written through the repository's own
          bootstrap insert.

        The import lane itself needs a model to write a plan, and this
        case cannot call one.  What the scheduler consumes is the pair
        above, and the pair is what is written here.
        """
        for plan_id in PLAN_IDS:
            plan_dir = self.plans_dir / plan_id
            project_dir = plan_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)
            (plan_dir / "prd.json").write_text(
                json.dumps(
                    {
                        "title": plan_id,
                        "overview": "A plan imported by the scheduler e2e case.",
                        "constraints": "",
                        "acceptance": "",
                        "decision_points": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (plan_dir / "tasks.json").write_text(
                json.dumps(
                    {
                        "requirement": "A plan imported by the scheduler e2e case.",
                        "tasks": [
                            {
                                "id": f"{plan_id}-1",
                                "title": "The one task this plan has",
                                "description": "The e2e plan waits in the line.",
                                "test_command": "true",
                                "project_dir": str(project_dir),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))
        from plan_state import PlanState
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import RoutingRepository

        connection = open_db(self.db_path)
        try:
            migrate(connection)
            routing = RoutingRepository(connection)
            for plan_id in PLAN_IDS:
                routing.insert(plan_id, QUEUED_PHASE)
        finally:
            connection.close()

        # The state machine's own transition, the one the import lane
        # makes right after it has seeded the routing row.
        for plan_id in PLAN_IDS:
            PlanState(self.plans_dir / plan_id).transition_to(QUEUED_PHASE)

        # The order the plans will be put in the line in, read from the
        # plan facts while they are still pristine: the backfill sorts
        # the waiting plans by their own moment and then by their id,
        # and this is that order.
        routing_rows = self.routing_rows()
        self.imported_phases = {
            str(row["plan_id"]): str(row["current_phase"]) for row in routing_rows
        }
        self.backfill_order = [
            str(row["plan_id"])
            for row in sorted(
                routing_rows, key=lambda row: (row["updated_at"], row["plan_id"])
            )
        ]

    # -- the server ---------------------------------------------------------

    def _server_env(self) -> Dict[str, str]:
        """The environment the server is booted with."""
        env = dict(os.environ)
        env.update(
            {
                "PDT_STATE_DB_PATH": str(self.db_path),
                "PDT_PLANS_DIR": str(self.plans_dir),
                # The one substitution this directory is allowed to make:
                # the executor, through the production seam.
                "PDT_EXECUTOR_CMD": "{0} -u {1}".format(
                    shlex.quote(str(VENV_PYTHON)), shlex.quote(str(EXECUTOR_FIXTURE))
                ),
                # The stand-in forks one more process into its own group,
                # so the teardown's claim ("the group goes down with it")
                # is a claim about a group that really has more than one
                # member.
                "PDT_E2E_SPAWN_GRANDCHILD": "1",
                # The provider chain is CC Switch's to serve, and CC Switch
                # is another application: nothing in this repository starts
                # it.  Without this the server's lifespan asks
                # ``127.0.0.1:4000`` for a chain nobody serves, the request
                # is refused, and the child exits before it ever listens —
                # which is what the container CI runner sees, because it has
                # no CC Switch either.  The plans here are run by the
                # deterministic stand-in, so no provider is ever asked for
                # anything and "inherit" is the honest setting: the same
                # answer the sibling end-to-end suites give their servers
                # for the same reason (see
                # ``tests/e2e/test_stop_resume_ui_e2e.py``).
                "PDT_PROVIDER_MODE": "inherit",
                # A capacity file that does not exist = "no rules configured",
                # so the operator's own capacity file is never read.
                "PDT_PROVIDER_CAPACITY_FILE": str(
                    self.root / "absent-provider-capacity.yaml"
                ),
                "PYTHONPATH": os.pathsep.join(
                    [str(REPO_ROOT), str(BACKEND_DIR), env.get("PYTHONPATH", "")]
                ),
            }
        )
        # A stale secret descriptor must never be closed by a process
        # that never owned it — see the sibling end-to-end cases.
        for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
            env.pop(key, None)
        return env

    def start(self) -> None:
        """Boot the server and wait until it answers."""
        log_handle = (self.logs_dir / "server.log").open("wb")
        self._log_handle = log_handle
        self.proc = subprocess.Popen(
            [
                str(VENV_PYTHON),
                str(self.launcher_path),
                str(BACKEND_DIR),
                str(self.settings_path),
                str(self.port),
            ],
            cwd=str(BACKEND_DIR),
            env=self._server_env(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._wait_for_server()

    def _wait_for_server(self, timeout: float = 60.0) -> None:
        """Block until the server answers on ``/health``."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                status, _ = _http(self.base_url, "/health", timeout=2.0)
                if 200 <= status < 300:
                    return
                last = f"status {status}"
            except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
                last = repr(exc)
            time.sleep(0.2)
        raise AssertionError(
            f"the server on {self.base_url} did not come up within {timeout}s; "
            f"the last answer was {last}"
        )

    def kill9(self) -> None:
        """Kill the server the way a crash kills it: ``SIGKILL``, no cleanup.

        The signal is deliberately brutal.  A shutdown that runs would
        give the scheduler the chance to put its state in order, and the
        question this case asks is what happens when it gets **no**
        chance.
        """
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self.proc = None
        self._wait_for_port_free()

    def _wait_for_port_free(self, timeout: float = 30.0) -> None:
        """Wait until the port can be bound again."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _port_is_free(self.port):
                return
            time.sleep(0.1)
        raise AssertionError(f"port {self.port} was still taken after {timeout}s")

    # -- what the scheduler says -------------------------------------------

    def scheduler_state(self) -> Dict[str, Any]:
        """The read model, as the API answers it."""
        status, payload = _http(self.base_url, "/api/scheduler/state")
        assert status == 200, f"the read model was answered with {status}: {payload!r}"
        assert isinstance(payload, dict), f"the read model is not a document: {payload!r}"
        return payload

    def listing(self) -> List[Dict[str, Any]]:
        """The plans the API lists, terminal ones included."""
        status, payload = _http(self.base_url, "/api/plans?include_terminal=true")
        assert status == 200, f"the listing was answered with {status}: {payload!r}"
        rows = payload if isinstance(payload, list) else payload.get("plans", [])
        return list(rows)

    def queue_rows(self) -> List[Dict[str, Any]]:
        """The line, as the scheduler's own table holds it."""
        return _rows(
            self.db_path,
            "SELECT plan_id, seq, priority, promote_order FROM scheduler_queue "
            "ORDER BY seq",
        )

    def slot_rows(self) -> List[Dict[str, Any]]:
        """The slots, as the scheduler's own table holds them."""
        return _rows(
            self.db_path,
            "SELECT plan_id, acquired_at FROM scheduler_slot ORDER BY plan_id",
        )

    def exec_rows(self) -> List[Dict[str, Any]]:
        """The execution rows: the pid the spawn recorded, and its status."""
        return _rows(
            self.db_path,
            "SELECT plan_id, exec_pid, exec_status FROM plan_execution "
            "ORDER BY plan_id",
        )

    def routing_rows(self) -> List[Dict[str, Any]]:
        """The plans' phases, and when they were last written."""
        return _rows(
            self.db_path,
            "SELECT plan_id, current_phase, updated_at FROM plan_routing "
            "ORDER BY plan_id",
        )

    def queue_plan_ids(self) -> List[str]:
        """The line, as the server orders it, with the plan ids on it.

        The read model carries the line in the production order but does
        not name the plans on it; the table does.  The two are joined
        here on the number the line and the table share.
        """
        by_seq = {int(row["seq"]): str(row["plan_id"]) for row in self.queue_rows()}
        order: List[str] = []
        for item in self.scheduler_state()["queue"]:
            plan_id = by_seq.get(int(item["seq"]))
            assert plan_id is not None, (
                f"the line names the number {item['seq']}, which no row holds: "
                f"{self.queue_rows()!r}"
            )
            order.append(plan_id)
        return order

    # -- the panel ----------------------------------------------------------

    def drive(self, steps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Drive the panel through *steps* and report what it did.

        A step names an action and a plan, and says how the action is
        issued: ``"via": "panel"`` presses the button the panel drew
        (the queue row's own button), and ``"via": "api"`` issues the
        command the panel draws no button for.  Nothing is simulated —
        the browser clicks the panel's own buttons and the report is
        what the panel then shows.
        """
        interpreter = _playwright_interpreter()
        if interpreter is None:
            pytest.skip(
                "no Playwright driver is installed in this environment; the "
                "panel cannot be driven, and a panel that was not driven "
                "proves nothing about the panel"
            )

        script = self.root / "drive_panel.py"
        script.write_text(_PANEL_DRIVER_SOURCE, encoding="utf-8")
        result = subprocess.run(
            [
                interpreter,
                str(script),
                f"{self.base_url}/",
                json.dumps(list(steps), ensure_ascii=False),
                str(SCREEN_DIR),
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

    # -- the teardown -------------------------------------------------------

    def teardown(self) -> None:
        """Kill everything this world started, and prove nothing is left.

        The server first, then the executors: an executor whose parent
        is still alive becomes a zombie the moment it is killed, and a
        zombie is a process that is still in the table.

        The server is killed by process group (it was given a session of
        its own), and each executor by the group it leads — the group
        the grandchild is in, so the grandchild goes down with it.
        """
        errors: List[str] = []

        executor_pids = self._exec_pids_from_db()
        groups: Dict[int, str] = {}
        for plan_id, pid in executor_pids.items():
            pgid = _process_pgid(pid)
            if pgid is not None and pgid > 1 and pgid != os.getpgid(0):
                groups[pgid] = plan_id

        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None

        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in executor_pids.values():
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        # The processes need a moment to leave the table: a killed
        # process is reaped by the system, and a reaped process is gone
        # only after the system has done it.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not any(_group_members(pgid) for pgid in groups):
                break
            time.sleep(0.2)

        for pgid, plan_id in groups.items():
            members = _group_members(pgid)
            if members:
                errors.append(
                    f"the executor of {plan_id} left {members} in process "
                    f"group {pgid}"
                )
        for plan_id, pid in executor_pids.items():
            if _process_command(pid):
                errors.append(f"the executor of {plan_id} (pid {pid}) is still alive")

        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:  # noqa: BLE001 — closing a log must not fail a test
                pass
            self._log_handle = None

        try:
            if not _port_is_free(self.port):
                errors.append(
                    f"port {self.port} was still taken after the teardown"
                )
        except OSError as exc:  # pragma: no cover — the check reports, not raises
            errors.append(f"port {self.port} was still taken after the teardown: {exc}")

        assert not errors, "the teardown left something behind:\n  " + "\n  ".join(errors)

    def _exec_pids_from_db(self) -> Dict[str, int]:
        """The pids the scheduler recorded, read out of the database."""
        if not self.db_path.exists():
            return {}
        pids: Dict[str, int] = {}
        for row in self.exec_rows():
            pid = row.get("exec_pid")
            if pid:
                pids[str(row["plan_id"])] = int(pid)
        return pids


# ---------------------------------------------------------------------------
# The seam: is the process the scheduler recorded really the fixture?
# ---------------------------------------------------------------------------


def _assert_seam(world: _World) -> Dict[str, int]:
    """The seam self-check; return the pid each plan's executor has.

    The scheduler records the pid of the process it spawned, and the
    facts reader trusts that pid: a plan is "working" because the
    process behind it is still there.  So the pid has to be *right*.

    The check is deliberately blunt: the command line must name the
    stand-in this suite installed, and must not name the production
    executor.  A run that fails this has not tested what it claims to
    test, so it fails everywhere and not only here.
    """
    rows = world.exec_rows()
    assert rows, "the scheduler recorded no executor at all"

    pids: Dict[str, int] = {}
    for row in rows:
        plan_id = str(row["plan_id"])
        pid = row.get("exec_pid")
        assert pid, f"{plan_id} has an execution row without a pid: {row!r}"
        pid = int(pid)
        command = _process_command(pid)
        assert command, (
            f"the pid {pid} the scheduler recorded for {plan_id} is not a "
            f"live process; the run it describes is not running"
        )
        assert "deterministic_executor.py" in command, (
            f"the process behind {plan_id} (pid {pid}) is not the stand-in "
            f"this case installed: {command!r}"
        )
        assert "cli.py" not in command, (
            f"the process behind {plan_id} (pid {pid}) is the production "
            f"executor, not the stand-in: {command!r}"
        )
        pids[plan_id] = pid
    return pids


def _wait_for_executors(world: _World, timeout: float = 90.0) -> Dict[str, int]:
    """Wait until every running plan has a recorded executor pid."""
    deadline = time.monotonic() + timeout
    last: List[Dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = world.exec_rows()
        recorded = {
            str(row["plan_id"]): int(row["exec_pid"])
            for row in last
            if row.get("exec_pid")
        }
        if len(recorded) >= CAPACITY:
            return recorded
        time.sleep(0.2)
    raise AssertionError(
        f"the scheduler recorded fewer than {CAPACITY} executor pids within "
        f"{timeout}s; the execution rows were {last!r}"
    )


def _wait_for_settle(world: _World, timeout: float = 120.0) -> Dict[str, Any]:
    """Wait until the scheduler has filled the capacity and settled the line."""
    deadline = time.monotonic() + timeout
    state: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = world.scheduler_state()
        if (
            state.get("capacity", {}).get("used") == CAPACITY
            and state.get("queue_length") == len(PLAN_IDS) - CAPACITY
        ):
            return state
        time.sleep(0.2)
    raise AssertionError(
        f"the scheduler did not settle within {timeout}s; the last read model "
        f"was {state!r}"
    )


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    """A running server, its own database, its own plans, and its plans in them."""
    the_world = _World(tmp_path)
    try:
        the_world.start()
        _wait_for_settle(the_world)
        yield the_world
    finally:
        the_world.teardown()


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_seam_selfcheck(world):
    """The pid the scheduler recorded is the stand-in this case installed.

    Everything else in this file rests on this: the facts reader decides
    that a plan is working by probing the pid the spawn recorded, so a
    pid that names the wrong process makes every later observation a
    description of something else.
    """
    pids = _wait_for_executors(world)
    checked = _assert_seam(world)

    assert set(checked) == set(pids), (
        f"the seam check and the recorded pids disagree: {checked!r} vs {pids!r}"
    )
    assert len(checked) == CAPACITY, (
        f"expected {CAPACITY} executors to be running, found {sorted(checked)}"
    )

    # The pid is the *executor's* own, and the executor leads a process
    # group of its own — the group the teardown signals.
    for plan_id, pid in checked.items():
        pgid = _process_pgid(pid)
        assert pgid is not None, f"{plan_id}'s executor (pid {pid}) has no group"
        assert pgid == pid, (
            f"{plan_id}'s executor (pid {pid}) leads the group {pgid} rather "
            f"than being its own leader"
        )


def test_operator_full_flow(world):
    """The eight phases, in order, against one running deployment.

    Every phase is asserted on what the system *did* — the read model,
    the scheduler's own tables, and the panel the operator looks at —
    and never on the server's memory.
    """
    _assert_seam(world)
    _wait_for_executors(world)

    # -- phase 1: the import ------------------------------------------------
    #
    # The pair the scheduler consumes: the plan's artifacts on disk, and
    # the plan's place in the state machine.
    for plan_id in PLAN_IDS:
        plan_dir = world.plans_dir / plan_id
        assert (plan_dir / "prd.json").is_file(), f"{plan_id} has no prd.json"
        assert (plan_dir / "tasks.json").is_file(), f"{plan_id} has no tasks.json"
        assert (plan_dir / "project").is_dir(), f"{plan_id} has no project directory"

    # Every plan entered the state machine in the phase the import puts
    # a plan in — read back before the scheduler ever ran.
    assert world.imported_phases == {
        plan_id: QUEUED_PHASE for plan_id in PLAN_IDS
    }, f"the plans were imported into {world.imported_phases!r}"

    routing = {
        str(row["plan_id"]): str(row["current_phase"])
        for row in world.routing_rows()
    }
    for plan_id in PLAN_IDS:
        assert plan_id in routing, f"{plan_id} has no row in the state machine"
        assert routing[plan_id] in (QUEUED_PHASE, EXECUTING_PHASE), (
            f"{plan_id} is in the phase {routing[plan_id]!r}, which is neither "
            f"waiting nor working"
        )

    phases = {
        str(row.get("id")): str(row.get("current_phase") or "")
        for row in world.listing()
    }
    for plan_id in PLAN_IDS:
        assert plan_id in phases, f"{plan_id} is not in the listing: {phases!r}"

    # -- phase 2: the enqueue -----------------------------------------------
    #
    # The line is the scheduler's own: these rows were written by the
    # scheduler, because this case never writes them.
    queue_rows = world.queue_rows()
    waiting = [str(row["plan_id"]) for row in queue_rows]
    assert len(waiting) == len(PLAN_IDS) - CAPACITY, (
        f"expected {len(PLAN_IDS) - CAPACITY} plans to be waiting, {waiting!r} are"
    )
    assert set(waiting) == {
        plan_id for plan_id, phase in routing.items() if phase == QUEUED_PHASE
    }, f"the line holds {waiting!r}, which is not the set of waiting plans"
    seqs = [int(row["seq"]) for row in queue_rows]
    assert len(set(seqs)) == len(seqs), f"the line's numbers are not unique: {seqs!r}"
    assert all(seq > 0 for seq in seqs), f"the line's numbers are not positive: {seqs!r}"

    state = world.scheduler_state()
    # The snapshot's capacity carries a source field (the configured
    # capacity or a runtime override), so the comparison takes that
    # field into account -- the shape is still compared for exact
    # equality, so one key too many or too few turns the gate red.
    assert state["capacity"] == {
        "total": CAPACITY,
        "used": CAPACITY,
        "source": "config",
    }, (
        f"the capacity is {state['capacity']!r}"
    )
    assert state["queue_length"] == len(waiting), (
        f"the read model says the line holds {state['queue_length']} plans, "
        f"the table holds {len(waiting)}"
    )
    assert len(world.slot_rows()) == CAPACITY, (
        f"{len(world.slot_rows())} plans hold a slot, {CAPACITY} should"
    )

    # -- phase 3: the backfill ----------------------------------------------
    #
    # A plan that was waiting without a line joins the tail, in the order
    # the plan facts give: the plan's own moment, then its id.  The plans
    # that are still waiting are the tail of that order, and they are in
    # it.
    assert world.backfill_order, "the import recorded no order to backfill in"
    line_order = world.queue_plan_ids()
    assert line_order == world.backfill_order[-len(waiting):], (
        f"the line holds {line_order!r}; the plan facts order the plans "
        f"{world.backfill_order!r}, so the tail should be "
        f"{world.backfill_order[-len(waiting):]!r}"
    )

    # -- phases 4 to 7: the four actions, through the panel -----------------
    #
    # The promotion is applied to the plan at the *back* of the line: a
    # promotion applied to the plan that is already first cannot show
    # that a promotion moves a plan at all.
    head, tail = line_order[0], line_order[1]
    tail_seq = int(
        next(row for row in queue_rows if str(row["plan_id"]) == tail)["seq"]
    )
    head_seq = int(
        next(row for row in queue_rows if str(row["plan_id"]) == head)["seq"]
    )
    before_capacity = world.scheduler_state()["capacity"]

    # The four actions happen in two sessions, in the order the operator
    # pressed them, so each action is judged by what the panel drew
    # right after that action.  The sessions are split where the phase
    # has to be read: the phase the pause parks the plan in is gone by
    # the time the resume has put the plan back, so it is read between
    # the two, from the state machine's own routing table.
    first = world.drive(
        [
            {
                "action": "promote",
                "plan": tail,
                "expect_outcome": APPLIED,
                "expect_queue_head_seq": tail_seq,
            },
            {
                # The panel draws a pause for a plan that holds a slot;
                # this plan is waiting, so the command is issued rather
                # than pressed.
                "action": "pause",
                "plan": head,
                "via": "api",
                "expect_outcome": APPLIED,
                "expect_queue_absent": head,
            },
        ]
    )
    paused_phase = _phase_of(world, head)
    second = world.drive(
        [
            {
                # Nothing in the panel draws a resume at all, so this
                # command is issued too.
                "action": "resume",
                "plan": head,
                "via": "api",
                "expect_outcome": APPLIED,
                "expect_queue_head_plan": head,
            },
            {
                "action": "start",
                "plan": tail,
                "expect_outcome": BLOCKED_BY_CAPACITY,
            },
        ]
    )
    resumed_phase = _phase_of(world, head)

    # -- phase 4: the promotion ---------------------------------------------
    #
    # The scheduler's own tables are read afterwards, for what the four
    # actions together left behind.
    promoted = first["steps"][0]
    assert promoted["outcome"] == APPLIED, (
        f"the promotion was answered with {promoted['outcome']!r} "
        f"({promoted['detail']!r})"
    )
    # Before the promotion the plan to promote stood second in the line...
    assert len(first["queue"]) >= 2, (
        f"the line the panel drew holds {len(first['queue'])} plans, not two: "
        f"{first['queue']!r}"
    )
    assert first["queue"][1][2] == str(tail_seq), (
        f"the panel's second row is {first['queue'][1]!r}, which is not the "
        f"plan the promotion was meant for (number {tail_seq})"
    )
    # ...and after it, it stands first.
    assert promoted["queue"] and promoted["queue"][0][2] == str(tail_seq), (
        f"the panel's first row is "
        f"{promoted['queue'][0] if promoted['queue'] else None!r}, which is not "
        f"the promoted plan's row (number {tail_seq})"
    )
    assert promoted["queue"][0][0].strip() == "1", (
        f"the promoted plan is drawn at position {promoted['queue'][0][0]!r}, "
        f"not first"
    )
    # A promotion moves a plan in the line; it takes no slot.
    assert promoted["capacity"]["used"] == str(before_capacity["used"]), (
        f"the promotion changed the number of running plans: "
        f"{before_capacity['used']} -> {promoted['capacity']['used']}"
    )
    assert promoted["capacity"]["total"] == str(before_capacity["total"]), (
        f"the promotion changed the capacity: {before_capacity['total']} -> "
        f"{promoted['capacity']['total']}"
    )

    # -- phase 5: the pause --------------------------------------------------
    #
    # The paused plan leaves the line: the panel's line is one plan
    # shorter, and the plan's own row is the one that is gone.  The
    # phase is read where it is written — the state machine's own
    # routing table — because the panel draws no phase any more.
    paused = first["steps"][1]
    assert paused["outcome"] == APPLIED, (
        f"the pause was answered with {paused['outcome']!r} ({paused['detail']!r})"
    )
    assert paused_phase == STOPPED_PHASE, (
        f"the paused plan is in the phase {paused_phase!r}"
    )
    assert str(head_seq) not in [row[2] for row in paused["queue"]], (
        f"the paused plan still holds a place in the line the panel drew: "
        f"{paused['queue']!r}"
    )
    assert len(paused["queue"]) == len(first["queue"]) - 1, (
        f"the line holds {len(paused['queue'])} plans after the pause, "
        f"{len(first['queue']) - 1} should"
    )

    # -- phase 6: the resume -------------------------------------------------
    #
    # The resume puts the plan back into the line and moves it to the
    # front.  The number it carries in the line is read from the
    # scheduler's own table, which the refused start did not change: a
    # start that is refused only promotes.
    resumed = second["steps"][0]
    assert resumed["outcome"] == APPLIED, (
        f"the resume was answered with {resumed['outcome']!r} ({resumed['detail']!r})"
    )
    assert resumed_phase == QUEUED_PHASE, (
        f"the resumed plan is in the phase {resumed_phase!r}"
    )
    resumed_row = next(
        row for row in world.queue_rows() if str(row["plan_id"]) == head
    )
    assert resumed["queue"] and resumed["queue"][0][2] == str(resumed_row["seq"]), (
        f"the panel's first row is "
        f"{resumed['queue'][0] if resumed['queue'] else None!r}, which is not "
        f"the resumed plan's row {resumed_row!r}"
    )
    assert resumed["queue"][0][0].strip() == "1", (
        f"the resumed plan is drawn at position {resumed['queue'][0][0]!r}, "
        f"not first"
    )

    # -- phase 7: the refused start -----------------------------------------
    #
    # The capacity is full and every run is young, so nobody may be
    # interrupted: the start is refused, and nobody is evicted.
    refused = second["steps"][1]
    assert refused["outcome"] == BLOCKED_BY_CAPACITY, (
        f"the start was answered with {refused['outcome']!r} ({refused['detail']!r})"
    )
    assert refused["capacity"]["used"] == str(before_capacity["used"]), (
        f"the refused start changed the number of running plans: "
        f"{before_capacity['used']} -> {refused['capacity']['used']}"
    )
    running_before = [row[0] for row in resumed["running"]]
    running_after = [row[0] for row in refused["running"]]
    assert running_before == running_after, (
        f"the refused start changed who is running: {running_before!r} -> "
        f"{running_after!r}"
    )

    # What the four actions together left behind in the scheduler's own
    # tables: both plans are waiting again, the four that were running
    # are still running, and the refused start evicted nobody.
    final_rows = world.queue_rows()
    assert {str(row["plan_id"]) for row in final_rows} == {head, tail}, (
        f"the line holds {final_rows!r}"
    )
    assert len(world.slot_rows()) == CAPACITY, (
        f"{len(world.slot_rows())} plans hold a slot, {CAPACITY} should"
    )
    final_routing = {
        str(row["plan_id"]): str(row["current_phase"]) for row in world.routing_rows()
    }
    assert final_routing[head] == QUEUED_PHASE, (
        f"the resumed plan is in the phase {final_routing[head]!r}"
    )
    after_start = world.scheduler_state()
    assert after_start["capacity"]["used"] == CAPACITY, (
        f"the capacity is {after_start['capacity']!r} after the refused start"
    )
    assert all(
        not row["evictable"] for row in after_start["running"]
    ), "a run that has not had its turn is not evictable"

    # Every request the panel's buttons issued carried the guard's
    # header — and so did the two commands the case issued itself.
    requests = first["requests"] + second["requests"]
    assert len(requests) == 2, (
        f"the panel issued {len(requests)} action requests, not two: "
        f"{requests!r}"
    )
    for request in requests:
        assert request["method"] == "POST", f"the request was not a POST: {request!r}"
        assert request["guard"] == REQUEST_HEADER_VALUE, (
            f"the request did not carry the guard's header: {request!r}"
        )
    actions = sorted(request["url"].rsplit("/", 1)[-1] for request in requests)
    assert actions == ["promote", "start"], (
        f"the panel issued {actions!r} rather than the two actions it draws "
        f"buttons for"
    )

    # The two actions the panel draws no button for went to the same
    # endpoints, on the same session, with the same header.
    commands = first["api_actions"] + second["api_actions"]
    assert len(commands) == 2, (
        f"the case issued {len(commands)} commands, not two: "
        f"{commands!r}"
    )
    for entry in commands:
        assert entry["method"] == "POST", f"the command was not a POST: {entry!r}"
        assert entry["guard"] == REQUEST_HEADER_VALUE, (
            f"the command did not carry the guard's header: {entry!r}"
        )
        assert entry["status"] == 200, f"the command was refused: {entry!r}"
    issued = sorted(entry["url"].rsplit("/", 1)[-1] for entry in commands)
    assert issued == ["pause", "resume"], (
        f"the case issued {issued!r} rather than the two actions the panel "
        f"draws no button for"
    )
    assert sorted(actions + issued) == ["pause", "promote", "resume", "start"], (
        f"the four actions came out as {sorted(actions + issued)!r}"
    )

    # -- phase 8: the restart -----------------------------------------------
    #
    # The line is used as it stands, and the running plans are adopted
    # and timed from the moment of the restart.
    before_rows = world.queue_rows()
    before_slots = {
        str(row["plan_id"]): str(row["acquired_at"]) for row in world.slot_rows()
    }
    restart_at = _utcnow_iso()
    world.kill9()
    world.start()
    _wait_for_settle(world)
    observed_at = _utcnow_iso()

    # The line is used as it stands: every row, every number, every
    # promotion.
    assert world.queue_rows() == before_rows, (
        f"the line changed across the restart: {before_rows!r} -> "
        f"{world.queue_rows()!r}"
    )
    for row in world.queue_rows():
        plan_id = str(row["plan_id"])
        was = next(
            old for old in before_rows if str(old["plan_id"]) == plan_id
        )
        assert int(row["seq"]) == int(was["seq"]), (
            f"{plan_id}'s number changed across the restart: "
            f"{was['seq']!r} -> {row['seq']!r}"
        )
        assert row["priority"] == was["priority"], (
            f"{plan_id}'s priority changed across the restart: "
            f"{was['priority']!r} -> {row['priority']!r}"
        )
        assert row["promote_order"] == was["promote_order"], (
            f"{plan_id}'s promotion changed across the restart: "
            f"{was['promote_order']!r} -> {row['promote_order']!r}"
        )

    # The running plans are adopted, and their slots are timed from the
    # moment of the restart.
    for row in world.slot_rows():
        plan_id = str(row["plan_id"])
        acquired = str(row["acquired_at"])
        assert acquired != before_slots.get(plan_id), (
            f"{plan_id} kept its old slot moment ({acquired!r}) across the "
            f"restart"
        )
        assert restart_at <= acquired <= observed_at, (
            f"{plan_id}'s slot was taken at {acquired!r}, which is not the "
            f"moment of the restart ({restart_at!r} to {observed_at!r})"
        )


def test_panel_and_api_parity(world):
    """The panel and the read model say the same thing, field by field.

    The panel draws one read model, and the read model is what the API
    answers with.  A panel that draws its own numbers is a second answer
    to the same question, and the operator has no way of telling which
    of the two the scheduler is using.
    """
    _assert_seam(world)
    _wait_for_executors(world)

    report = world.drive([])
    state = report["api"]
    assert isinstance(state, dict) and "capacity" in state, (
        f"the API did not answer with a read model: {state!r}"
    )

    # The home page's capacity widget.
    home = report["home"]
    assert home["used"] == str(state["capacity"]["used"]), (
        f"the home widget says {home['used']!r} plans are running; the read "
        f"model says {state['capacity']['used']}"
    )
    assert home["total"] == str(state["capacity"]["total"]), (
        f"the home widget says the capacity is {home['total']!r}; the read "
        f"model says {state['capacity']['total']}"
    )
    assert home["queue"] == str(state["queue_length"]), (
        f"the home widget says the line holds {home['queue']!r} plans; the "
        f"read model says {state['queue_length']}"
    )
    assert bool(home["over"]) == bool(state["over_capacity"]), (
        f"the home widget's over-capacity flag is {home['over']!r}; the read "
        f"model says {state['over_capacity']!r}"
    )
    assert bool(home["over_visible"]) == bool(state["over_capacity"]), (
        f"the home widget's over-capacity flag is shown as {home['over_visible']!r}; "
        f"the read model says {state['over_capacity']!r}"
    )

    # The scheduler view's own capacity widget.
    view = report["view"]
    for field in ("used", "total", "queue"):
        assert view[field] == home[field], (
            f"the two capacity widgets disagree about {field!r}: the home "
            f"says {home[field]!r}, the scheduler view says {view[field]!r}"
        )

    # The line, row by row: position, number, priority and promotion.
    panel_rows = report["queue"]
    assert len(panel_rows) == len(state["queue"]), (
        f"the panel draws {len(panel_rows)} rows, the read model carries "
        f"{len(state['queue'])}"
    )
    for drawn, carried in zip(panel_rows, state["queue"]):
        assert drawn[0] == str(carried["position"]), (
            f"the panel draws the position {drawn[0]!r}, the read model says "
            f"{carried['position']}"
        )
        assert drawn[2] == str(carried["seq"]), (
            f"the panel draws the number {drawn[2]!r}, the read model says "
            f"{carried['seq']}"
        )
        assert drawn[3] == str(carried["priority"]), (
            f"the panel draws the priority {drawn[3]!r}, the read model says "
            f"{carried['priority']}"
        )
        expected_promotion = (
            "—" if carried["promote_order"] is None else str(carried["promote_order"])
        )
        assert drawn[4] == expected_promotion, (
            f"the panel draws the promotion {drawn[4]!r}, the read model says "
            f"{carried['promote_order']!r}"
        )

    # The running plans, row by row: the plan, and whether it may be
    # interrupted.  The elapsed time is read twice — once by each side —
    # so it is compared with the tolerance of a clock, not exactly.
    # The panel draws it as a duration and the read model carries
    # seconds, so the drawing is turned back into seconds first.
    panel_running = report["running"]
    assert len(panel_running) == len(state["running"]), (
        f"the panel draws {len(panel_running)} running plans, the read model "
        f"carries {len(state['running'])}"
    )
    for drawn, carried in zip(panel_running, state["running"]):
        assert drawn[0] == str(carried["plan_id"]), (
            f"the panel draws {drawn[0]!r} as running, the read model says "
            f"{carried['plan_id']!r}"
        )
        assert drawn[2] == ("是" if carried["evictable"] else "否"), (
            f"the panel says {drawn[0]!r} may be interrupted is {drawn[2]!r}; "
            f"the read model says evictable={carried['evictable']!r}"
        )
        drawn_elapsed, slack = _duration_seconds(drawn[1])
        assert drawn_elapsed is not None, (
            f"the panel draws {drawn[1]!r} for {drawn[0]!r}, which is not a "
            f"duration"
        )
        assert abs(drawn_elapsed - int(carried["elapsed_sec"])) <= 2 + slack, (
            f"the panel says {drawn[0]!r} has been running for {drawn_elapsed}s, "
            f"the read model says {carried['elapsed_sec']}s"
        )

    # The two readings must also be the readings of the same server: the
    # case's own answer has to agree with the one the browser got.
    own = world.scheduler_state()
    assert own["capacity"] == state["capacity"], (
        f"the capacity the browser read is {state['capacity']!r}, the case "
        f"read {own['capacity']!r}"
    )
    assert own["queue_length"] == state["queue_length"], (
        f"the line the browser read holds {state['queue_length']} plans, the "
        f"case read {own['queue_length']}"
    )

    # The evidence: the two screenshots the case was asked to keep.
    for name in ("home.png", "scheduler.png"):
        screenshot = SCREEN_DIR / name
        assert screenshot.is_file(), f"{screenshot} was not written"
        assert screenshot.stat().st_size > 0, f"{screenshot} is empty"


def test_queue_survives_kill9(world):
    """A ``kill -9`` loses neither the line nor the running plans.

    The line is used as it stands — not renumbered, not reordered — and
    the running plans are adopted, timed from the moment of the restart.
    An adopted plan has not had its turn yet, so it is protected for a
    whole M; carrying an old timestamp over would put it into the
    eviction candidates the moment it was taken over.
    """
    _assert_seam(world)
    _wait_for_executors(world)

    before_rows = world.queue_rows()
    before_order = world.queue_plan_ids()
    before_slots = {
        str(row["plan_id"]): str(row["acquired_at"]) for row in world.slot_rows()
    }
    assert before_rows, "there was no line to lose"
    assert before_slots, "there was no plan to adopt"

    restart_at = _utcnow_iso()
    world.kill9()
    world.start()
    _wait_for_settle(world)

    after_rows = world.queue_rows()
    assert after_rows == before_rows, (
        f"the line changed across the restart:\n  before {before_rows!r}\n"
        f"  after  {after_rows!r}"
    )
    assert world.queue_plan_ids() == before_order, (
        f"the order of the line changed across the restart: {before_order!r} -> "
        f"{world.queue_plan_ids()!r}"
    )

    after_slots = {
        str(row["plan_id"]): str(row["acquired_at"]) for row in world.slot_rows()
    }
    assert set(after_slots) == set(before_slots), (
        f"the plans holding a slot changed across the restart: "
        f"{sorted(before_slots)} -> {sorted(after_slots)}"
    )
    for plan_id, acquired in after_slots.items():
        assert acquired != before_slots[plan_id], (
            f"{plan_id} kept its old slot moment ({acquired!r}) across the "
            f"restart; an adopted plan is timed from now"
        )
        assert restart_at <= acquired, (
            f"{plan_id}'s slot was taken at {acquired!r}, before the restart "
            f"at {restart_at!r}"
        )
        assert acquired <= _utcnow_iso(), (
            f"{plan_id}'s slot was taken at {acquired!r}, in the future"
        )

    # The adopted plans are still the ones the fixtures are running: the
    # executor of each of them is the same process as before the crash.
    for plan_id in after_slots:
        command = _process_command(_exec_pid(world, plan_id))
        assert "deterministic_executor.py" in command, (
            f"{plan_id}'s executor is not the stand-in after the restart: "
            f"{command!r}"
        )


def _exec_pid(world: _World, plan_id: str) -> int:
    """The pid the scheduler recorded for *plan_id*."""
    for row in world.exec_rows():
        if str(row["plan_id"]) == plan_id:
            return int(row["exec_pid"])
    raise AssertionError(f"no execution row for {plan_id}")
