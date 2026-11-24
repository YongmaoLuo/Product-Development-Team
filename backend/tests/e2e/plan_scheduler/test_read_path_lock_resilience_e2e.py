"""End-to-end: the read path does not wait for the write lock.

The scheduler's worker writes on one connection to the state database
and the panel's read model reads on **another** one.  A connection that
holds the database's write lock — ``BEGIN IMMEDIATE`` — must not be
able to make the panel wait: a reader in WAL mode reads the last
committed snapshot and never waits for a writer.

This case proves it on the real thing: a real server, a real browser, a
real database file.

* The read is read **from the page**, the way the operator's panel
  reads it, and it is timed twice — in the browser's own clock and in
  the case's wall clock — because a clock that does not move would
  make the measurement say nothing.
* The lock is held by a **second connection** to the same database,
  opened by the driver itself, and the driver *proves* the lock is
  really held before it measures anything: another connection has to
  fail to take it.
* The answer is proved to be the server's own: the URL carries a probe
  the browser cannot have cached, and the model carries the moment the
  server generated it.

**The worker is put to work while the lock is held.**  A read only
proves something about a reader that has to wait for a writer, and a
worker with nothing to do writes nothing: so a promotion is asked for
and left in flight, and the worker goes into its write and cannot get
the lock.  The panel's model is read **while that write is waiting** —
which is the moment the read path is about.  The case knows the write
is waiting because the promotion has not been answered: the command is
still in the worker's hands.

The second half of the case is what happens **after** the lock is
given back: the operator's own button is pressed, the command is
answered, and the panel draws the new order.

Nothing here is a double.  The server is a child process, the browser
is a real browser, the lock is a real SQLite lock, and the panel is
the panel the operator opens.

**The gap this case found, and the fix that closed it.**

The budget in the first case did not hold on the tree this case was
written against, and the reason was a defect in the product and not a
defect in the case.

``PlanSchedulerEngine._read_state`` (backend/plan_scheduler/engine.py)
takes the connection, the facts and the ledger **the caller reads on**,
and hands them to every part of the read model — ``_rebuild_line``,
``_parked_plans``.  ``_running_view`` was left out of that change: it
read ``self._facts.active_plans()``, which is the **worker's** facts
reader and therefore the worker's own connection.  So the document
waited for the worker, however the rest of the read path was wired:
with the worker inside a write that cannot get the lock, the read took
seconds where the budget is half a second.

The gap is **closed**: ``_running_view`` now reads the working plans
through the facts the caller reads on, the way its siblings in the read
model have them, so the document does not wait for the worker.  The
case therefore asserts the budget **directly** — there is no
known-failure marker left to hide behind, and a read path that starts
depending on the write connection again turns this file red.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from tests.e2e.plan_scheduler import test_scheduler_operator_e2e as operator
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (
    MIN_RUNTIME_SECONDS,
    SCREEN_DIR,
    TICK_SECONDS,
    _driver_env,
    _playwright_interpreter,
    _wait_for_settle,
    _World,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: How many plans may run at the same time.  **One**: the case is
#: about the plans that are waiting, and with a capacity of one the
#: line always holds five of the six plans whatever else happens — so
#: the line is never empty and the read model always has something to
#: say.
CAPACITY: int = 1

#: How long a read may take while another connection holds the write
#: lock.  The old behaviour was the connection's own busy timeout —
#: five seconds — so the bound is a tenth of that: a read that waits
#: for the writer is not a read.
READ_BUDGET_SECONDS: float = 0.5

#: How long the panel may take to draw the new order after the command
#: was answered.
HEAD_BUDGET_SECONDS: float = 5.0

#: How old the answer the page got may be.  The read model carries the
#: moment the **server** generated it, so a stale answer — one the
#: browser had cached — is caught by this bound and not by the timing.
FRESHNESS_BUDGET_SECONDS: float = 30.0

#: How long the worker is given to get into its write before the read
#: is measured.  The worker takes a command out of its mailbox at once,
#: so this is generous: it only has to cover the round trip of a
#: command finding its way to the worker.
BUSY_WAIT_SECONDS: float = 0.8

#: How long the lock is held **after** the read was measured.  A
#: reader that waits for the writer is only caught while the writer is
#: still waiting, so the lock stays held long enough for a waiting
#: read to have shown itself.  It also stays inside the database's own
#: busy timeout, so that the worker's write lands the moment the lock
#: is given back instead of failing on the timeout.
HOLD_AFTER_READ_SECONDS: float = 1.0

#: The screenshot the case leaves behind: the panel while the write
#: lock is held.
SCREENSHOT: Path = SCREEN_DIR / "read-path-lock.png"

#: The screenshot the case leaves behind: the panel after the command
#: was carried out.
SCREENSHOT_AFTER: Path = SCREEN_DIR / "read-path-lock-after-action.png"


# ---------------------------------------------------------------------------
# The ways a case can fail
# ---------------------------------------------------------------------------


class _DriverFailure(RuntimeError):
    """The driver itself failed, not the thing it was measuring.

    A driver that could not run at all — no browser, a crash in the
    middle, a server that never came up — is a broken measurement, not
    a red about the product, and it says so in its own words.
    """


def _require(condition: bool, message: str) -> None:
    """Fail the case with the reason spelled out.

    ``pytest.fail`` raises a ``Failed`` rather than an
    ``AssertionError``, so the failure is reported as the case's own
    verdict and carries the message that says which of the report's
    facts was not what it had to be.
    """
    if not condition:
        pytest.fail(message)


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class _LockWorld(_World):
    """A world whose capacity is one.

    Everything else is the world's own: the same server, the same
    launcher, the same plans.  Only the number of plans that may run
    at the same time is different, because the case is about the line
    that is left over.
    """

    def _write_settings(self) -> None:
        """Write the settings file the scheduler is told to read.

        The capacity is one and the tick is short, so the worker keeps
        the line full of waiting plans and reacts to a command within
        a tick.
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


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A running server, its own database, and a line that is never empty."""
    if _playwright_interpreter() is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "panel cannot be driven, and a panel that was not driven "
            "proves nothing about the panel"
        )

    # The shared settle gate asks whether the capacity is used and the
    # line is short — and it asks about **the** capacity, the one the
    # world runs with.  The two are told the same number here, so the
    # gate means the same thing in this case as it means in the case it
    # was written for.
    monkeypatch.setattr(operator, "CAPACITY", CAPACITY)

    the_world = _LockWorld(tmp_path)
    try:
        try:
            the_world.start()
            _wait_for_settle(the_world)
        except AssertionError as exc:
            # The shared harness says "this world never came up" with a
            # bare ``AssertionError``.  This is made a driver failure
            # of its own: a measurement that never took place is not a
            # red about the product, and it must not be able to wear
            # its face.
            raise _DriverFailure(
                "the world this case measures in never came up: {0}".format(exc)
            ) from exc
        yield the_world
    finally:
        the_world.teardown()


# ---------------------------------------------------------------------------
# The driver — the panel, as the operator uses it, under a write lock
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = r'''
"""Hold the write lock, read from the page, then act and watch the panel.

Arguments:
  1. the url of the running server
  2. the path of the state database
  3. the file the screenshot under the lock is written to
  4. the file the screenshot after the command is written to
  5. how long the panel may take to draw the new order, in seconds
  6. how long the worker is given to get into its write, in seconds
  7. how long the lock is held after the read, in seconds

What it does, in this order:

1. open the page the operator opens, and open the scheduler view, so
   the panel draws the line;
2. take the database's **write lock** on a connection of its own, and
   prove it is really held — a second connection must fail to take
   it;
3. ask for a promotion and **leave it in flight**, so that the worker
   goes into its write and cannot get the lock; the case waits until
   the command has not been answered, which says the worker is inside
   that write;
4. read the read model **from the page** and time it — the URL
   carries a probe the browser cannot have cached, so the answer is
   the server's and not the cache's;
5. take the screenshot: the panel, alive, while the lock is held;
6. give the lock back — and wait for the worker's write to land;
7. press the promotion button on the **last** card of the line, and
   watch the answer the server sends back;
8. wait for the panel to draw the new order, and say how long that
   took.

The lock is given back whatever happens: the driver wraps the whole
of it in a ``finally``, so a failure in the middle of the case cannot
leave the database locked.

The report is one JSON document on stdout.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.environ["PDT_CASE_BACKEND_DIR"])
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (
    QUEUE_CARD_SELECTOR,
    read_cards,
)

GUARD_HEADER = "x-pdt-request"
GUARD_VALUE = "1"
STATE_PATH = "/api/scheduler/state"

#: The read, timed in the browser's own clock.  The clock is the
#: browser's, so it says how long the *page* waited — the network
#: round trip and the server's own work included.
FETCH_SCRIPT = """async (arg) => {
    const started = performance.now();
    const response = await fetch(arg.path, {
        cache: 'no-store',
        headers: {[arg.header]: arg.value},
    });
    const text = await response.text();
    const elapsed = performance.now() - started;
    let body = {};
    try { body = JSON.parse(text); } catch (err) { body = {raw: text}; }
    const queue = (body && Array.isArray(body.queue)) ? body.queue : [];
    return {
        ms: elapsed,
        status: response.status,
        generated_at: (body && body.generated_at) ? String(body.generated_at) : "",
        queue_length: (body && typeof body.queue_length === "number")
            ? body.queue_length
            : -1,
        queue_ids: queue.map(row => String((row && row.plan_id) || "")),
        head: queue.length ? String((queue[0] && queue[0].plan_id) || "") : "",
    };
}"""

#: The promotion that keeps the worker busy.  The fetch is **not**
#: awaited: it is left in flight on purpose, so that the worker is
#: inside its write while the panel reads.
FIRE_SCRIPT = """(arg) => {
    window.__busy = {settled: false, status: 0};
    window.__busyPromise = fetch(arg.path, {
        method: 'POST',
        cache: 'no-store',
        headers: {
            [arg.header]: arg.value,
            'Content-Type': 'application/json',
        },
        body: '{}',
    }).then((response) => {
        window.__busy.settled = true;
        window.__busy.status = response.status;
        return response.status;
    }).catch((err) => {
        window.__busy.settled = true;
        window.__busy.status = 0;
        return 0;
    });
    return true;
}"""

#: Whether the busy promotion has been answered yet.  An answer that
#: has not come says the worker is still inside its write.
BUSY_SCRIPT = """() => ({
    settled: Boolean(window.__busy && window.__busy.settled),
    status: (window.__busy && Number(window.__busy.status)) || 0,
})"""

#: The answer of the busy promotion, once it has come.  The wait is
#: bounded: an answer that never comes is an answer of zero, not a
#: hang.
AWAIT_BUSY_SCRIPT = """async () => {
    if (!window.__busyPromise) return {status: 0};
    const timeout = new Promise((resolve) => setTimeout(() => resolve(-1), 30000));
    const status = await Promise.race([window.__busyPromise, timeout]);
    return {status: Number(status) || 0};
}"""


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


def take_write_lock(db_path):
    """Take the database's write lock on a connection of our own.

    The lock is taken with a short busy timeout and is retried: the
    worker takes the same lock for the length of its own transaction,
    and a case that lost the race would fail for a reason that has
    nothing to do with what it is about.
    """
    connection = sqlite3.connect(str(db_path), isolation_level=None, timeout=0.2)
    connection.execute("PRAGMA busy_timeout = 200")
    last = None
    for _ in range(20):
        try:
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except sqlite3.OperationalError as exc:
            last = exc
            time.sleep(0.1)
    raise AssertionError("the write lock could not be taken: {0!r}".format(last))


def lock_is_held(db_path):
    """Whether another connection cannot take the write lock.

    The probe asks for the lock with a short busy timeout: a lock that
    is held answers with "database is locked", and a lock that is not
    held answers with a lock of its own — which the probe gives back
    at once.
    """
    probe = sqlite3.connect(str(db_path), isolation_level=None, timeout=0.2)
    probe.execute("PRAGMA busy_timeout = 200")
    try:
        probe.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return True
    else:
        probe.execute("ROLLBACK")
        return False
    finally:
        probe.close()


url = sys.argv[1]
db_path = sys.argv[2]
screen_locked = sys.argv[3]
screen_after = sys.argv[4]
head_budget = float(sys.argv[5])
busy_wait = float(sys.argv[6])
hold_after_read = float(sys.argv[7])

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {
    "lock_proven": False,
    "worker_busy": False,
    "state_ms": -1.0,
    "page_ms": -1.0,
    "wall_ms": -1.0,
    "status": 0,
    "queue_len": -1,
    "queue_ids": [],
    "generated_at": "",
    "busy_plan": "",
    "busy_status": 0,
    "busy_seconds": -1.0,
    "promoted_plan": "",
    "action_status": 0,
    "lock_released_at": 0.0,
    "clicked_at": 0.0,
    "action_after_unlock": False,
    "head_plan": "",
    "head_position": "",
    "head_seconds": -1.0,
    "screenshot": screen_locked,
}


with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page()

    # The answers the server gave to **the operator's own button** are
    # watched by their own listener: the panel polls the state as well,
    # and a poll's answer is not the answer to the button.
    action_answers = []

    lock = None
    try:
        page.goto(url, wait_until="load")
        page.wait_for_selector("#app", timeout=30000)

        # The scheduler view, the way the operator opens it.
        page.click("#btn-open-scheduler")
        page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)
        page.wait_for_selector(QUEUE_CARD_SELECTOR, timeout=30000)
        page.wait_for_timeout(500)

        # --- the line the panel draws --------------------------------------
        cards = read_cards(page, QUEUE_CARD_SELECTOR)
        if len(cards) < 2:
            raise AssertionError(
                "the line held fewer than two plans, so there is no plan "
                "to keep the worker busy with and another to promote: "
                "{0!r}".format(cards)
            )
        # The plan whose promotion keeps the worker busy, and the plan
        # the operator promotes with the button afterwards.  They are
        # two different plans, so the order the panel draws at the end
        # is the order the operator's own button made.
        busy_plan = str(cards[0]["plan_id"])
        target = str(cards[-1]["plan_id"])
        report["busy_plan"] = busy_plan
        report["promoted_plan"] = target
        busy_path = "/api/plan/{0}/scheduler/promote".format(busy_plan)
        target_path = "/api/plan/{0}/scheduler/promote".format(target)

        def watch_action(response):
            if target_path in response.url:
                action_answers.append(response.status)

        page.on("response", watch_action)

        # --- the lock ------------------------------------------------------
        lock = take_write_lock(db_path)
        report["lock_proven"] = lock_is_held(db_path)

        # --- the worker is put to work while the lock is held --------------
        # The promotion is asked for and left in flight: the worker takes
        # it out of its mailbox at once and goes into its write, and that
        # write cannot get the lock this connection holds.
        page.evaluate(
            FIRE_SCRIPT,
            {"path": busy_path, "header": GUARD_HEADER, "value": GUARD_VALUE},
        )
        page.wait_for_timeout(int(busy_wait * 1000))
        busy = page.evaluate(BUSY_SCRIPT)
        # A command that has not been answered is a command the worker
        # is still inside: that is the write the read has to get past.
        report["worker_busy"] = not bool(busy.get("settled"))

        # --- the read, from the page, under the lock -----------------------
        probe = STATE_PATH + "?probe=" + str(int(time.time() * 1000))
        started = time.time()
        answer = page.evaluate(
            FETCH_SCRIPT,
            {"path": probe, "header": GUARD_HEADER, "value": GUARD_VALUE},
        )
        wall_ms = round((time.time() - started) * 1000.0, 3)
        report["page_ms"] = round(float(answer.get("ms", -1.0)), 3)
        report["wall_ms"] = wall_ms
        # The honest number is the larger of the two: a browser clock
        # that does not move would report a read that took no time at
        # all, and the case's own clock does not move on its own.
        report["state_ms"] = max(report["page_ms"], wall_ms)
        report["status"] = int(answer.get("status", 0))
        report["queue_len"] = int(answer.get("queue_length", -1))
        report["queue_ids"] = list(answer.get("queue_ids") or [])
        report["generated_at"] = str(answer.get("generated_at") or "")

        # The evidence of the moment: the panel, alive, under the lock.
        page.screenshot(path=screen_locked, full_page=True)

        # The lock stays held for a moment yet: a reader that waits for
        # the writer only shows itself while the writer is still
        # waiting.
        page.wait_for_timeout(int(hold_after_read * 1000))

        # --- the lock is given back ----------------------------------------
        lock.execute("ROLLBACK")
        lock.close()
        lock = None
        report["lock_released_at"] = time.time()

        # --- the worker's write lands --------------------------------------
        busy_started = time.time()
        busy_answer = page.evaluate(AWAIT_BUSY_SCRIPT)
        report["busy_status"] = int(busy_answer.get("status", 0))
        report["busy_seconds"] = round(time.time() - busy_started, 3)

        # --- the operator's own button -------------------------------------
        selector = (
            "#scheduler-queue-cards button.scheduler-action"
            "[data-action='promote'][data-plan='{0}']"
        ).format(target)
        page.wait_for_selector(selector, timeout=30000)

        before = len(action_answers)
        clicked_at = time.time()
        report["clicked_at"] = clicked_at
        # The order the case is about: the command goes out **after** the
        # lock is given back.  The two moments are recorded, so the
        # order is a fact and not a claim.
        report["action_after_unlock"] = clicked_at >= report["lock_released_at"]
        page.click(selector, timeout=30000)

        deadline = time.time() + 30
        while len(action_answers) == before and time.time() < deadline:
            page.wait_for_timeout(50)
        report["action_status"] = (
            int(action_answers[-1]) if len(action_answers) > before else 0
        )

        # --- the panel draws the new order ----------------------------------
        deadline = time.time() + head_budget
        while time.time() < deadline:
            drawn = read_cards(page, QUEUE_CARD_SELECTOR)
            if drawn:
                report["head_plan"] = str(drawn[0]["plan_id"])
                report["head_position"] = str(drawn[0]["facts"].get("position", ""))
                if report["head_plan"] == target and report["head_position"] == "1":
                    report["head_seconds"] = round(time.time() - clicked_at, 3)
                    break
            page.wait_for_timeout(100)

        page.screenshot(path=screen_after, full_page=True)
    finally:
        if lock is not None:
            try:
                lock.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            lock.close()
        browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


def _drive(world: _LockWorld) -> Dict[str, Any]:
    """Run the driver and return what it reported."""
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "panel cannot be driven, and a panel that was not driven "
            "proves nothing about the panel"
        )

    script = world.root / "drive_read_path.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            interpreter,
            str(script),
            world.base_url + "/",
            str(world.db_path),
            str(SCREENSHOT),
            str(SCREENSHOT_AFTER),
            str(HEAD_BUDGET_SECONDS),
            str(BUSY_WAIT_SECONDS),
            str(HOLD_AFTER_READ_SECONDS),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = "{0}\n{1}".format(result.stdout, result.stderr)
        if (
            "Executable doesn't exist" in combined
            or "playwright install" in combined
            or "BrowserType.launch" in combined
        ):
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not; the panel cannot be driven"
            )
        raise _DriverFailure(
            "the browser driver failed (exit {0}); stdout={1!r} "
            "stderr={2!r}".format(result.returncode, result.stdout, result.stderr)
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _say(report: Dict[str, Any]) -> Dict[str, Any]:
    """Write the report the driver produced, and hand it back.

    The report is the evidence: the milliseconds the read took, the
    status the command was answered with, and the plans the panel drew
    — none of which a pass/fail line carries.
    """
    print("read-path-lock report: " + json.dumps(report, ensure_ascii=False))
    return report


def _age_of(generated_at: str) -> Optional[float]:
    """How many seconds ago the server generated the answer, or ``None``.

    The read model carries the moment the **server** wrote it, so this
    is the age of the answer as the server sees it — and an answer the
    browser had cached is old by exactly as much as the cache is.
    """
    if not generated_at:
        return None
    from datetime import datetime, timezone

    text = generated_at.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - moment).total_seconds()


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_scheduler_state_fast_while_write_lock_held(world):
    """The panel reads the scheduler state while another connection writes.

    Another connection holds the database's write lock, the worker is
    inside its own write and cannot get that lock — and the panel's
    read model still comes back at once.  The old behaviour was the
    connection's own busy timeout — five seconds — because the read
    model shared the worker's connection and waited for the worker's
    transaction.

    The budget check is the one the case is really about, and it comes
    **last** so that the checks before it are proven even while the
    budget is not met.
    """
    report = _say(_drive(world))

    _require(
        report["lock_proven"],
        "the write lock was not held while the read was measured, so the "
        "measurement says nothing: {0!r}".format(report),
    )
    _require(
        report["worker_busy"],
        "the worker was not inside a write while the read was measured, "
        "so there was nothing for the read to wait for: {0!r}".format(report),
    )
    _require(
        report["status"] == 200,
        "the page's read of the read model was answered with "
        "{0}: {1!r}".format(report["status"], report),
    )
    _require(
        report["queue_len"] > 0,
        "the line was empty, so the fast read was a fast read of "
        "nothing: {0!r}".format(report),
    )

    age = _age_of(report["generated_at"])
    _require(
        age is not None,
        "the answer carried no moment the server generated it, so it "
        "cannot be told apart from a cached one: {0!r}".format(report),
    )
    _require(
        age < FRESHNESS_BUDGET_SECONDS,
        "the answer the page got was generated {0}s ago, so it was not "
        "the server's own answer to this read: {1!r}".format(age, report),
    )

    # The worker's write was waiting for the lock while the panel read,
    # and it landed the moment the lock was given back.
    _require(
        200 <= report["busy_status"] < 300,
        "the promotion the worker was busy with was answered with {0} "
        "after the lock was given back: {1!r}".format(
            report["busy_status"], report
        ),
    )

    _require(
        SCREENSHOT.exists(),
        "the case left no screenshot behind: {0}".format(SCREENSHOT),
    )

    # The budget, and the last word: the check the case is really
    # about.  It comes last so that the checks before it are proven
    # even while the budget is not met.
    state_ms = report["state_ms"]
    budget_ms = READ_BUDGET_SECONDS * 1000.0
    assert state_ms < budget_ms, (
        "the read took {0}ms while another connection held the write "
        "lock; the budget is {1}ms: {2!r}".format(
            state_ms, budget_ms, report
        )
    )


def test_action_applied_and_panel_updates_after_unlock(world):
    """The operator's own button works once the lock is given back.

    The lock is held, the worker is inside its write, the panel reads
    through it — the lock is given back, and then the operator presses
    the promotion button on the last plan of the line.  The command is
    answered, and the panel draws the new order: the promoted plan
    stands at the head.
    """
    report = _say(_drive(world))

    assert report["lock_proven"], (
        "the write lock was not held while the panel read, so the case "
        "did not exercise the lock at all: {0!r}".format(report)
    )
    assert 200 <= report["action_status"] < 300, (
        "the promotion was answered with {0}: {1!r}".format(
            report["action_status"], report
        )
    )
    assert report["action_after_unlock"], (
        "the command was issued before the lock was given back, so the "
        "case did not exercise the unlock: {0!r}".format(report)
    )
    assert report["promoted_plan"], (
        "the line held no plan to promote: {0!r}".format(report)
    )
    assert report["head_plan"] == report["promoted_plan"], (
        "the panel does not show the promoted plan at the head of the "
        "line: {0!r}".format(report)
    )
    assert report["head_position"] == "1", (
        "the panel draws the promoted plan at position "
        "{0!r} and not at the head: {1!r}".format(
            report["head_position"], report
        )
    )
    assert 0 <= report["head_seconds"] <= HEAD_BUDGET_SECONDS, (
        "the panel drew the new order after {0}s, and the budget is "
        "{1}s: {2!r}".format(
            report["head_seconds"], HEAD_BUDGET_SECONDS, report
        )
    )

    assert SCREENSHOT_AFTER.exists(), (
        "the case left no screenshot behind: {0}".format(SCREENSHOT_AFTER)
    )
