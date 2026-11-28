"""End-to-end: the panel's 「停止中」 row and the 「待你审核」 queue.

What this file is for
---------------------
The scheduler's pause work is spread over four layers, and each layer
can be green while the **panel** is broken:

* the read model has to *name* the plans that are stopping
  (``GET /api/scheduler/state`` → ``stopping[]``);
* the panel has to **draw** that name as a 「停止中」 row
  (``tr[data-stopping-plan]``);
* the panel has to keep the plan **in the queue** while the command is
  in flight — a plan that is stopping is in neither the running line nor
  the queue, and the panel must not contradict itself about it;
* the 「待你审核」 block has to fold three sources into the three groups
  it draws (``pr_merge`` / ``hitl`` / ``approval``).

So this file asks the question the operator asks, against something
that is really running: *open the panel, press 暂停, and read what the
page says.*

What is real here
-----------------
* the page is **served by the backend** (the frontend is a static
  bundle the backend mounts), opened in a **real Chromium** driven by
  Playwright;
* the button is found the way a user's eye finds it — by its
  ``data-action`` and its ``data-plan`` — and **clicked**, not called;
* the pause travels through the page's own ``fetch``, so it carries the
  request guard's header the way the operator's mouse does;
* the state, the plans and the decisions are a **SQLite file** and
  **directories** in a temporary data root — nothing is written into
  the repository.

What the world holds
--------------------
Three plans, and each one exists because a later step reads it:

1. **the plan the operator pauses** — queued, in the scheduler's line,
   and carrying a command the worker has **taken but not answered**
   (``scheduler_command.outcome = 'accepted'``, action ``pause``).
   That row is what the read model's ``stopping[]`` names and what the
   panel has to draw as 「停止中」.
2. **the plan with a pending decision** — a ``plan_decisions`` row that
   is still ``pending``.  It is the 「待你审核」 block's ``hitl`` group.
3. **the plan with a pending approval** — a durable wait record under
   the finishing stage's own ``postprocess_approval`` source.  It is
   the ``approval`` group.

No pull request is seeded, so the ``pr_merge`` group has nothing in it
— and the panel has to **hide** that group rather than draw an empty
heading.

Why the browser runs in its own process
---------------------------------------
Playwright is not importable from the interpreter that runs the suite
(it is not a dependency of the backend), and the backend's virtualenv
does not carry a browser.  So the driver is a script run by an
interpreter that *can* import Playwright (:func:`_playwright_interpreter`
finds one by asking the environment, never by hard-coding a path), and
it reports what it saw as JSON on its last stdout line.  When no such
interpreter exists, or the browser binaries are missing, the case
**skips** and says so — a browser that is not there is not a pass, and
the skip leaves a marker behind (``artifact_skipped.json``).

What this file produces
-----------------------
The screenshots behind the assertions and a KPI document, under
``/tmp/pdt-e2e/screens`` (or ``$PDT_E2E_ARTIFACTS_DIR``):

* ``stopping-row.png`` — the panel at the moment 暂停 was pressed: the
  「停止中」 row and the plan still in the queue;
* ``review-queue.png`` — the 「待你审核」 block with its two groups;
* ``report.json`` — the KPI numbers the acceptance is judged on
  (``pause_response_ms``, ``stopping_row_ms``, ``review_rows``,
  ``page_errors``) and the assertions behind them;
* ``stopping-timeout.png`` — written when the 「停止中」 row did not
  arrive within the hard limit, so a failure leaves evidence behind.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from .test_failed_plan_actions_api_e2e import BACKEND_DIR, FailedPlanWorld

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The plans this file's panel case looks at
# ---------------------------------------------------------------------------

#: The plan the operator pauses.  It is queued, it is in the
#: scheduler's line, and its pause command is **in flight**: the worker
#: wrote the row down when it took the command and has not answered it
#: yet.  That row is the read model's ``stopping[]`` entry, so it is
#: what the panel has to draw.
STOPPING_PLAN_ID: str = "pdt-e2e-pause-stopping"

#: The plan that carries the pending human decision — the 「待你审核」
#: block's ``hitl`` group.
HITL_PLAN_ID: str = "pdt-e2e-pause-hitl"

#: The plan that carries the pending approval — the 「待你审核」 block's
#: ``approval`` group.
APPROVAL_PLAN_ID: str = "pdt-e2e-pause-approval"

#: The item the approval wait is about.  It is the wait's own key, so
#: it has to be in the record as well as in the progress file.
APPROVAL_ITEM_ID: str = "approval-item-1"

#: The kind of decision the HITL plan asked under.  It is not the
#: approval source — the approval stands on its wait, and a decision
#: that claimed to be one would be counted twice.
HITL_KIND: str = "failure_review"

#: The question the pending decision asks.
HITL_QUESTION: str = "这一步要重跑吗？"

#: The identity of that decision.  It is written down rather than
#: minted, so a run's evidence names the same decision every time.
HITL_DECISION_ID: str = "pdt-e2e-pause-decision-1"

#: The question the pending approval asks.
APPROVAL_QUESTION: str = "批准这次收尾吗？"


# ---------------------------------------------------------------------------
# The numbers the acceptance is judged on
# ---------------------------------------------------------------------------

#: How long the pause request may take.  The panel must not hang on a
#: scheduler that is busy.
PAUSE_RESPONSE_BUDGET_MS: int = 2000

#: How long the 「停止中」 row may take to appear after the panel was
#: opened.
STOPPING_ROW_BUDGET_MS: int = 5000

#: The hard limit: past it the row did not arrive, the case fails and
#: the screenshot of the panel is kept.
STOPPING_ROW_HARD_TIMEOUT_MS: int = 30000


# ---------------------------------------------------------------------------
# Where the artifacts land
# ---------------------------------------------------------------------------

#: The artifacts' directory.  The brief's path, overridable because a
#: runner may want them somewhere else — and because a path written
#: into a test file should be a *default*, not a law.
ARTIFACTS_DIR: Path = Path(
    os.environ.get("PDT_E2E_ARTIFACTS_DIR", "/tmp/pdt-e2e/screens")
)

#: The screenshot of the panel at the moment 暂停 was pressed.
STOPPING_SCREEN: str = "stopping-row.png"

#: The screenshot of the 「待你审核」 block.
REVIEW_SCREEN: str = "review-queue.png"

#: The screenshot written when the 「停止中」 row did not arrive.
TIMEOUT_SCREEN: str = "stopping-timeout.png"

#: The KPI document.
REPORT_FILE: str = "report.json"

#: The marker written when the browser was not available.  A skip that
#: leaves nothing behind is a skip nobody can tell from a pass.
SKIP_MARKER: str = "artifact_skipped.json"


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class PauseAndReviewQueueWorld(FailedPlanWorld):
    """The world of this file: one server, three plans, and their facts.

    It is the sibling case's world — the same server, the same
    database, the same settings seam — with the seed replaced.  The
    seed is the whole point of this file, so it is written out here
    rather than inherited: every row below is read by a later step, and
    the order is a dependency.

    1. The tables (``open_db`` + ``migrate``) — without them the
       legacy migration is swallowed by its own ``except Exception`` and
       every row after it is luck rather than evidence.
    2. The **routing rows** — the phase.  ``/api/plans`` reads them,
       and the panel's terminal list (the block that carries the
       buttons) is drawn from it.
    3. The **plan state files** — the same phase in the file the
       state machine keeps for the reconciliation copy.
    4. The **queue row** — the plan the operator pauses is in the
       scheduler's line, so the panel draws it as a card.
    5. The **command row** — the pause the worker has taken and not
       answered.  This is the row the read model's stopping list names.
       It is written after the server is up (see ``start``), because a
       command that is still open when the scheduler starts is one a
       dead process left behind, and the startup fails it.
    6. The **decision** — a pending question, in the HITL table.
    7. The **progress file** — a pending approval, in the plan's own
       durable wait record.
    """

    def _write_settings(self) -> None:
        """Write the settings file the scheduler is told to read.

        The scheduler is on (the operator's action needs it), it changes
        nothing it decides (``dry_run``), one plan may run at a time, a
        young run is protected for ten minutes, the tick is an hour
        away, and a command may take five seconds — long enough for a
        stuck worker to be noticed, short enough that the panel is not
        left waiting.
        """
        self.settings_path.write_text(
            "scheduler:\n"
            "  enabled: true\n"
            "  dry_run: true\n"
            "  capacity: 1\n"
            "  min_runtime_seconds: 600\n"
            "  tick_seconds: 3600\n"
            "  command_timeout_seconds: 5\n",
            encoding="utf-8",
        )

    def start(self) -> None:
        """Boot the server, and say why it did not come up if it did not.

        A server that will not start is a **failure**, not a skip: the
        case has nothing to say about the product, and skipping would
        hide that behind a green run.  So the refusal carries the
        server's own log — the tail of it — because that is where the
        reason is.

        The command the worker has taken and not answered yet is
        written **after** the server came up: a command that is still
        open when the scheduler starts is one a dead process left
        behind, and the startup fails it.  The row of a command the
        *running* worker has taken is written after the scheduler is
        serving, which is what this does.
        """
        try:
            super().start()
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}\n--- the server's log (the tail) ---\n{self._server_log_tail()}"
            ) from exc
        self._seed_open_command()

    def _seed_open_command(self) -> None:
        """Write the in-flight command, and wait until it is in flight.

        The scheduler's startup reconciles the commands it finds open,
        so a row written before the scheduler is up is a row the
        startup fails.  The row is written after the server is up, and
        the wait makes sure it is still open when the driver looks: if
        the startup ran after the row was written, the row is written
        again.
        """
        for _ in range(40):
            self._write_open_command()
            status, document = self.http("/api/scheduler/state")
            if status == 200 and any(
                str(row.get("plan_id") or "") == STOPPING_PLAN_ID
                for row in ((document or {}).get("stopping") or [])
            ):
                return
            time.sleep(0.25)
        raise AssertionError(
            "the in-flight command never reached the stopping list of "
            "the running scheduler"
        )

    def _write_open_command(self) -> None:
        """Write the command the worker has taken and not answered yet.

        This row is the whole reason the plan is drawn as 「停止中」: it
        is the read model's own fact, and the panel has to draw the
        name it carries.
        """
        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))

        from plan_scheduler.store import PENDING_OUTCOME, SqliteSchedulerStore
        from state_machine.db.connection import open as open_db

        connection = open_db(self.db_path)
        try:
            SqliteSchedulerStore().record_command(
                connection,
                STOPPING_PLAN_ID,
                "pause",
                outcome=PENDING_OUTCOME,
            )
        finally:
            connection.close()

    def _server_log_tail(self, lines: int = 40) -> str:
        """The tail of the server's log, or a word about its absence."""
        log_path = self.logs_dir / "server.log"
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(the server's log could not be read: {exc!r})"
        tail = text.splitlines()[-lines:]
        return "\n".join(tail) if tail else "(the server's log is empty)"

    def _seed_plans(self) -> None:
        """Write the three plans and the four facts the panel reads."""
        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))

        from hitl.models import Decision
        from hitl.store import HitlStore
        from plan_scheduler.store import SqliteSchedulerStore
        from plan_state import PLAN_STATE_FILENAME
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
        from state_machine.repositories.routing_repository import RoutingRepository

        connection = open_db(self.db_path)
        try:
            migrate(connection)
            routing = RoutingRepository(connection)
            execution = ExecutionRepository(connection)
            store = SqliteSchedulerStore()

            for plan_id in (STOPPING_PLAN_ID, HITL_PLAN_ID, APPROVAL_PLAN_ID):
                plan_dir = self.plans_dir / plan_id
                project_dir = plan_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)

                # The phase.  The listing reads this row, and the
                # panel's terminal list — the block that carries the
                # buttons — is drawn from it.
                routing.insert(plan_id, "queued")

                # The same phase in the file the state machine keeps
                # for the reconciliation copy.
                (plan_dir / PLAN_STATE_FILENAME).write_text(
                    json.dumps({"current_phase": "queued"}, ensure_ascii=False),
                    encoding="utf-8",
                )

                execution.insert(plan_id, project_dir=str(project_dir))

            # The plan the operator pauses is in the scheduler's line,
            # so the panel draws it as a queue card.
            store.enqueue(connection, STOPPING_PLAN_ID)
        finally:
            connection.close()

        # The pending decision.  Its own store, because the HITL table
        # is written through the store that also keeps the audit
        # mirror — and the mirror belongs in this world's plans tree,
        # not in the repository's.
        stamp = datetime.now(timezone.utc).isoformat()
        with HitlStore(self.db_path, audit_dir=self.plans_dir) as hitl:
            hitl.insert(
                Decision(
                    decision_id=HITL_DECISION_ID,
                    plan_id=HITL_PLAN_ID,
                    source=HITL_KIND,
                    question=HITL_QUESTION,
                    options=["重跑", "跳过"],
                    created_at=stamp,
                    updated_at=stamp,
                )
            )

        _write_approval_wait(self.plans_dir / APPROVAL_PLAN_ID)


def _write_approval_wait(plan_dir: Path) -> None:
    """Write the approval wait into the plan's own progress file.

    The wait is the **durable** one: the finishing stage keeps it under
    the item's ``wait`` field of the plan's progress document, and the
    panel's read model is built from those records.  A wait written
    anywhere else would be a wait the panel cannot see.

    The record names no pull request, so it lands in the ``approval``
    group — and the ``pr_merge`` group stays empty, which is what the
    panel has to hide.
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))

    from postprocess.progress import PROGRESS_FILENAME
    from postprocess.waits import POSTPROCESS_APPROVAL_SOURCE

    now = datetime.now(timezone.utc)
    deadline = (now + timedelta(hours=1)).isoformat()
    document = {
        "spec_sha256": "0" * 64,
        "started_at": now.isoformat(),
        "items": {
            APPROVAL_ITEM_ID: {
                "status": "waiting",
                "attempts": 1,
                "last_evidence_ref": None,
                "wait": {
                    "item_id": APPROVAL_ITEM_ID,
                    "wait_type": "human",
                    "question": APPROVAL_QUESTION,
                    "deadline": deadline,
                    "extensions": [],
                    "status": "pending",
                    "source": POSTPROCESS_APPROVAL_SOURCE,
                    "pr_number": None,
                    "pr_url": None,
                },
            }
        },
    }
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / PROGRESS_FILENAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Finding a Playwright
# ---------------------------------------------------------------------------


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


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with (see
    ``$PDT_TEST_REAL_HOME``) so that Playwright can find the browser
    binaries it installed there — the suite itself runs with a
    redirected home.
    """
    env = dict(os.environ)
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        env["HOME"] = real_home
    return env


def _write_skip_marker(artifacts: Path, reason: str) -> None:
    """Leave a marker behind saying the browser was not there.

    A skip that leaves nothing behind is a skip nobody can tell from a
    pass.  The marker says which run it was and why it did not run.
    """
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / SKIP_MARKER).write_text(
        json.dumps(
            {"artifact_skipped": True, "reason": reason},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = r'''
"""Drive the served panel in a real browser and report what happened.

The page is driven through its own events (``click``), so the
frontend's own code paths run — including the ``fetch`` that carries
the request guard's header.

Every read of the page is **one** ``evaluate`` call, so the numbers in
the report are of the same instant: a panel that is being repainted
under the reader cannot be caught half-drawn.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"
#: The two names above are the guard's own (see
#: ``request_guard.REQUEST_HEADER``); the driver runs in another
#: interpreter and cannot import this package, so it carries them.

STOPPING_SCREEN = "stopping-row.png"
REVIEW_SCREEN = "review-queue.png"
TIMEOUT_SCREEN = "stopping-timeout.png"
REPORT_FILE = "report.json"

STOPPING_ROW_HARD_TIMEOUT_MS = 30000

#: The panel, read in one go.
READ_DOM = """() => {
    const stopping = Array.from(
        document.querySelectorAll('tr[data-stopping-plan]')
    ).map((row) => ({
        plan_id: row.getAttribute('data-stopping-plan'),
        cells: Array.from(row.querySelectorAll('td')).map(
            (cell) => cell.textContent.trim()
        ),
    }));
    const queue = Array.from(
        document.querySelectorAll('#scheduler-queue-cards .scheduler-plan-card')
    ).map((card) => card.getAttribute('data-plan-id'));
    const group = (id) => {
        const box = document.getElementById(id);
        if (!box) return null;
        const rows = Array.from(box.querySelectorAll('.scheduler-review-row'));
        return {
            id: id,
            hidden: box.style.display === 'none',
            rows: rows.length,
            plans: rows.map((row) => row.getAttribute('data-review-plan')),
        };
    };
    return {
        stopping: stopping,
        queue_plans: queue,
        groups: {
            pr_merge: group('review-queue-pr-merge'),
            hitl: group('review-queue-hitl'),
            approval: group('review-queue-approval'),
        },
        unhandled_rejections: (window.__pdtUnhandledRejections || []).slice(),
    };
}"""

#: The page's own record of the rejections nothing handled.  The
#: frontend is supposed to catch its own (``loadInBackground``); a
#: rejection that reaches the window is a rejection the operator's
#: browser would log.
COLLECT_REJECTIONS = """
window.__pdtUnhandledRejections = [];
window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    let text;
    try {
        text = String((reason && reason.message) || reason);
    } catch (err) {
        text = 'unhandled rejection';
    }
    window.__pdtUnhandledRejections.push(text);
});
"""


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot
    launch anything.  The binaries that *are* installed are found here
    instead -- nothing is downloaded, and nothing is hard-coded: the
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
        "chromium-*/chrome-linux*/chromium",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern)):
                if candidate.exists():
                    return str(candidate)
    return None


def api_get(root, path, timeout=30.0):
    """One GET against the running server, with the guard's header."""
    request = urllib.request.Request(
        root + path,
        method="GET",
        headers={
            REQUEST_HEADER: REQUEST_HEADER_VALUE,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


mode, root, stopping_plan, artifacts_dir = sys.argv[1:5]
artifacts = Path(artifacts_dir)
artifacts.mkdir(parents=True, exist_ok=True)

report = {
    "mode": mode,
    "page_errors": [],
    "console_errors": [],
    "api_responses": [],
    "stopping_row_seen": False,
    "pause_response_at": None,
}

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}


def on_response(response):
    """Record the API calls the click made — and when the answer came.

    The answer's own moment is stamped here, in the listener, rather
    than read afterwards: the reading that follows the click (a
    screenshot of the whole page is not quick) would otherwise be
    counted into the answer's time, and the number would be about the
    driver rather than about the product.
    """
    if "/api/" in response.url:
        report["api_responses"].append(
            {"url": response.url, "status": response.status}
        )
    if "/scheduler/pause" in response.url:
        report["pause_response_at"] = time.monotonic()


def on_console(message):
    if message.type == "error":
        report["console_errors"].append(message.text)


with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))
    page.on("console", on_console)
    page.on("response", on_response)
    page.add_init_script(COLLECT_REJECTIONS)

    page.goto(root + "/", wait_until="load")
    page.wait_for_selector("#btn-open-scheduler", timeout=30000)

    # The panel is opened the way an operator opens it, and the read
    # model is waited for: the panel is drawn from that answer, and a
    # screenshot of an empty panel proves nothing.
    opened = time.monotonic()
    with page.expect_response(
        lambda r: "/api/scheduler/state" in r.url, timeout=30000
    ):
        page.click("#btn-open-scheduler")

    # The 「停止中」 row.  It is waited for, not hoped for: past the
    # hard limit the row did not arrive, and the panel is photographed
    # so the failure leaves evidence behind.
    selector = 'tr[data-stopping-plan="%s"]' % stopping_plan
    while True:
        if page.query_selector(selector):
            report["stopping_row_seen"] = True
            break
        if (time.monotonic() - opened) * 1000 > STOPPING_ROW_HARD_TIMEOUT_MS:
            page.screenshot(path=str(artifacts / TIMEOUT_SCREEN), full_page=True)
            break
        time.sleep(0.05)
    report["stopping_row_ms"] = int((time.monotonic() - opened) * 1000)

    # The button the operator presses — found the way a user's eye
    # finds it.
    pause_button = '.scheduler-action[data-action="pause"][data-plan="%s"]' % stopping_plan
    page.wait_for_selector(pause_button, timeout=30000)

    report["before"] = page.evaluate(READ_DOM)

    # The click.  The reading that follows it is taken **inside** this
    # block: the repaint that follows the answer is caused by the
    # answer, so a panel read here is the panel the operator pressed
    # the button on.  The reading before the click (``before``) is the
    # panel the operator found.
    started = time.monotonic()
    with page.expect_response(
        lambda r: (
            "/api/plan/%s/scheduler/pause" % stopping_plan in r.url
            and r.request.method == "POST"
        ),
        timeout=30000,
    ) as answered:
        page.click(pause_button)
        report["after_click"] = page.evaluate(READ_DOM)
        page.screenshot(path=str(artifacts / STOPPING_SCREEN), full_page=True)
    response = answered.value
    # The answer's own moment, stamped by the listener when the
    # response arrived — not the moment the driver got round to
    # reading it, which includes the screenshot above.
    report["pause_response_ms"] = int(
        ((report.get("pause_response_at") or time.monotonic()) - started) * 1000
    )
    report["pause_response_status"] = response.status
    report["pause_response_body"] = response.text()

    # The pause has landed: the plan is taken out of the queue, so its
    # card goes off the page.  The panel is given time to say so — the
    # repaint follows the answer — and the wait is bounded, so a panel
    # that still shows the card is a failure and not a hang.
    card = '.scheduler-plan-card[data-plan-id="%s"]' % stopping_plan
    try:
        page.wait_for_selector(card, state="detached", timeout=15000)
        report["pause_landed"] = True
    except Exception:
        report["pause_landed"] = False

    # The panel after the answer has been painted.
    page.wait_for_timeout(500)
    report["settled"] = page.evaluate(READ_DOM)
    page.screenshot(path=str(artifacts / REVIEW_SCREEN), full_page=True)

    status, document = api_get(root, "/api/scheduler/state")
    report["state_status"] = status
    report["state_document"] = document

    browser.close()

# -- the KPI document --------------------------------------------------------

settled = report.get("settled") or {}
groups = settled.get("groups") or {}
review_rows = sum(
    int((groups.get(name) or {}).get("rows") or 0)
    for name in ("pr_merge", "hitl", "approval")
)
review_groups = sum(
    1 for name in ("pr_merge", "hitl", "approval")
    if int((groups.get(name) or {}).get("rows") or 0) > 0
)
unhandled = settled.get("unhandled_rejections") or []
artifact = {
    "screens": [
        str(artifacts / STOPPING_SCREEN),
        str(artifacts / REVIEW_SCREEN),
    ],
    "kpis": {
        "pause_response_ms": report.get("pause_response_ms"),
        "stopping_row_ms": report.get("stopping_row_ms"),
        "review_rows": review_rows,
        "page_errors": len(report.get("page_errors") or []) + len(unhandled),
    },
    "assertions": {
        "stopping_visible": bool(report.get("stopping_row_seen")),
        "review_groups": review_groups,
    },
}
(artifacts / REPORT_FILE).write_text(
    json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
)
report["artifact"] = artifact

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright_interpreter() -> str:
    """An interpreter that can drive a browser, or a skip.

    A browser that is not there is not a pass: the case says so and
    stops — and it leaves a marker behind saying the run produced no
    artifacts.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        _write_skip_marker(
            ARTIFACTS_DIR,
            "no interpreter on this machine can import playwright, so no "
            "browser can be driven",
        )
        pytest.skip(
            "playwright unavailable: no interpreter on this machine can "
            "import playwright, so no browser can be driven"
        )
    return interpreter


@pytest.fixture(scope="session")
def pause_review_world(tmp_path_factory, playwright_interpreter):
    """A running server, its own database, and the three plans."""
    the_world = PauseAndReviewQueueWorld(
        tmp_path_factory.mktemp("pause-review-queue")
    )
    try:
        the_world.start()
        yield the_world
    finally:
        the_world.stop()


@pytest.fixture(scope="session")
def driver_script(tmp_path_factory) -> Path:
    """The browser driver, written out as a script of its own."""
    script = tmp_path_factory.mktemp("pause-review-queue-driver") / "drive.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    return script


@pytest.fixture(scope="session")
def artifacts_dir() -> Path:
    """Where the screenshots and the KPI document land."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


@pytest.fixture(scope="session")
def browser_report(
    pause_review_world, playwright_interpreter, driver_script, artifacts_dir
) -> Dict[str, Any]:
    """One browser pass; the report it printed.

    One pass, four assertions: the panel is one page, and a second
    browser run would be a second world — a second click on a plan that
    the first click has already paused.
    """
    return _run_driver(
        playwright_interpreter,
        driver_script,
        pause_review_world,
        artifacts_dir,
    )


# ---------------------------------------------------------------------------
# Running the driver
# ---------------------------------------------------------------------------


def _run_driver(
    interpreter: str,
    script: Path,
    world: PauseAndReviewQueueWorld,
    artifacts: Path,
) -> Dict[str, Any]:
    """Run the browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            "pause-and-review",
            world.base_url,
            STOPPING_PLAN_ID,
            str(artifacts),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or "playwright install" in combined:
            _write_skip_marker(
                artifacts,
                "the Playwright driver is installed but its browser binaries "
                "are not",
            )
            pytest.skip(
                "playwright unavailable: the Playwright driver is installed "
                "but its browser binaries are not"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    lines = result.stdout.strip().splitlines()
    assert lines, f"the browser driver printed nothing: {result.stdout!r}"
    return json.loads(lines[-1])


def _assert_no_page_errors(report: Dict[str, Any]) -> None:
    """The page must not have raised anything the user could see."""
    assert not report.get("page_errors"), (
        "the page raised an uncaught exception while the case ran: "
        f"{report.get('page_errors')!r} (console errors: "
        f"{report.get('console_errors')!r})"
    )
    rejections = (report.get("settled") or {}).get("unhandled_rejections") or []
    assert not rejections, (
        "the page left an unhandled promise rejection behind: "
        f"{rejections!r} (console errors: {report.get('console_errors')!r})"
    )


def _group(report: Dict[str, Any], when: str, name: str) -> Dict[str, Any]:
    """One review group of one reading of the panel."""
    groups = ((report.get(when) or {}).get("groups") or {})
    group = groups.get(name) or {}
    return {
        "hidden": bool(group.get("hidden")),
        "rows": int(group.get("rows") or 0),
        "plans": list(group.get("plans") or []),
    }


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_pause_shows_stopping_then_queue(browser_report, artifacts_dir):
    """The panel shows the plan as 停止中, and still has it in the queue.

    The two halves are the same fact seen from two places: the panel
    says the plan is stopping, and the panel still has it in the queue.
    A panel that dropped the plan from the queue while saying it was
    stopping would be telling the operator two different things at
    once.

    **When the overlap is read.**  The two views overlap only while the
    stop command is in flight — the arch decision the panel is built on
    says a plan that is stopping is in neither the queue nor the
    running line, and the queue table agrees the moment the pause
    lands: the plan is taken out of it.  So the overlap is the window
    between "the command was accepted" and "the command was carried
    out".

    The world's accepted command is that window: it is accepted and
    nobody ever answers it, so the panel shows the overlap for as long
    as the case looks.  The two facts are therefore asserted on the
    reading taken **immediately before the click** — the panel the
    operator pressed the button on.

    The click's own pause is a **second** command.  The row is still on
    the panel while that answer is on its way back — the panel does not
    take the plan's word away before the answer arrives — and the last
    assertion here pins the landing down: once the pause has been
    carried out, the card goes off the page.
    """
    report = browser_report

    assert report.get("stopping_row_seen"), (
        "the 「停止中」 row for %s never appeared within the hard limit; "
        "the panel was photographed at %s"
        % (STOPPING_PLAN_ID, artifacts_dir / TIMEOUT_SCREEN)
    )

    before = report.get("before") or {}
    stopping = [row.get("plan_id") for row in (before.get("stopping") or [])]
    assert STOPPING_PLAN_ID in stopping, (
        f"the panel does not draw a 「停止中」 row for {STOPPING_PLAN_ID}: "
        f"{before.get('stopping')!r}"
    )

    queue = list(before.get("queue_plans") or [])
    assert STOPPING_PLAN_ID in queue, (
        f"the plan the panel says is stopping is not in the queue: "
        f"{queue!r}"
    )

    # The click does not take the row away: the panel still says the
    # plan is on its way out while the answer is on its way back.
    after = report.get("after_click") or {}
    assert STOPPING_PLAN_ID in [
        row.get("plan_id") for row in (after.get("stopping") or [])
    ], (
        f"the 「停止中」 row for {STOPPING_PLAN_ID} is gone right after the "
        f"click: {after.get('stopping')!r}"
    )

    # And the pause really was carried out: the plan leaves the queue.
    assert report.get("pause_landed"), (
        "the plan is still in the queue after its pause was answered: the "
        "pause did not land"
    )

    assert (artifacts_dir / STOPPING_SCREEN).is_file(), (
        "the screenshot of the panel at the click was not written"
    )

    _assert_no_page_errors(report)


def test_review_queue_renders_three_groups(browser_report, pause_review_world):
    """The 「待你审核」 block draws the groups it has, and hides the one it has not.

    Two of the three groups have something in them — a pending decision
    and a pending approval — and the third (a pull request waiting to
    be merged) has nothing, so the panel has to **hide** it rather than
    draw a heading over an empty block.

    The row counts are checked against the server's own answer: the
    panel and ``GET /api/scheduler/state`` must not tell the operator
    two different stories about what is waiting.
    """
    report = browser_report

    hitl = _group(report, "settled", "hitl")
    approval = _group(report, "settled", "approval")
    pr_merge = _group(report, "settled", "pr_merge")

    assert hitl["rows"] >= 1, (
        f"the 「待你审核」 block draws no pending decision: {hitl!r}"
    )
    assert approval["rows"] >= 1, (
        f"the 「待你审核」 block draws no pending approval: {approval!r}"
    )
    assert pr_merge["rows"] == 0, (
        f"there is no pull request in this world, but the block draws "
        f"{pr_merge['rows']} of them: {pr_merge!r}"
    )
    assert pr_merge["hidden"], (
        "the group with nothing in it is drawn: an operator must not be "
        f"shown a heading over an empty block ({pr_merge!r})"
    )

    status, document = pause_review_world.http("/api/scheduler/state")
    assert status == 200, f"the read model was answered with {status}"
    queue = (document or {}).get("review_queue") or {}
    for name, seen in (
        ("hitl", hitl),
        ("approval", approval),
        ("pr_merge", pr_merge),
    ):
        assert seen["rows"] == len(queue.get(name) or []), (
            f"the panel draws {seen['rows']} rows of {name!r} and the "
            f"server says {len(queue.get(name) or [])}"
        )

    assert (report.get("state_status") or 0) == 200, (
        "the driver could not read the read model"
    )

    _assert_no_page_errors(report)


def test_pause_endpoint_returns_quickly(browser_report, pause_review_world):
    """The pause is answered fast, and the panel shows the stop at once.

    Two numbers, both bounded:

    * the **answer** — the click must not leave the operator waiting on
      a scheduler that is busy.  The status is one of the two the
      vocabulary has for an action that was taken on: ``200`` (it was
      carried out) or ``202`` (it is on its way).
    * the **row** — the 「停止中」 row must be on the panel within the
      budget, so the operator is not left with a button that did
      nothing visible.

    The plan's own phase is read from the server afterwards: the click
    has to have **paused the plan**, not merely drawn a row.
    """
    report = browser_report

    status = report.get("pause_response_status")
    assert status in (200, 202), (
        "the click was not answered with one of the two statuses the "
        f"vocabulary has: {status!r} "
        f"{report.get('pause_response_body')!r} "
        f"(requests: {report.get('api_responses')!r})"
    )

    elapsed = int(report.get("pause_response_ms") or 0)
    assert elapsed <= PAUSE_RESPONSE_BUDGET_MS, (
        f"the pause was answered in {elapsed}ms, over the "
        f"{PAUSE_RESPONSE_BUDGET_MS}ms budget"
    )

    row_ms = int(report.get("stopping_row_ms") or 0)
    assert row_ms <= STOPPING_ROW_BUDGET_MS, (
        f"the 「停止中」 row took {row_ms}ms to appear, over the "
        f"{STOPPING_ROW_BUDGET_MS}ms budget"
    )

    # The plan has to be **stopped** — a button that was pressed and
    # did nothing is not an answer.  The listing is cached for a moment,
    # so the phase is waited for rather than read once.
    phase = pause_review_world.wait_for_phase(
        STOPPING_PLAN_ID, "stopped", timeout=30.0
    )
    assert phase == "stopped", (
        f"the plan is in the phase {phase!r} after the click, not "
        f"'stopped'"
    )

    _assert_no_page_errors(report)


def test_no_uncaught_page_errors(browser_report):
    """Nothing the page raised reaches the operator's browser.

    Two kinds: an uncaught exception (``pageerror``) and a promise
    rejection nobody handled.  The second one is the harder half to
    see — the page keeps working — and it is the one the frontend is
    supposed to catch itself.
    """
    _assert_no_page_errors(browser_report)
