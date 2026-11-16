"""End-to-end: an open card's detail catches up **in place** when the plan moves.

What this case is for
---------------------
The panel redraws itself every five seconds, and an open card is a
window on one plan's progress.  Two things have to be true of that
window at the same time, and they pull in opposite directions:

* the window must **follow** the plan — what it says has to be what
  the plan is doing *now*, not what it was doing when the operator
  opened it;
* the window must **stay** the window — the card the operator opened is
  the card they are looking at, and it is still open.

A panel that satisfies the first by throwing the card away and drawing
a new one has lost the second: the operator's fold is gone, the node
their click referred to is gone, and the answer they were reading is
gone with it.  A panel that satisfies the second by freezing the card
has lost the first: the answer is a picture of the past.

So this case drives a real browser against a real server, changes the
plan's own state under the panel, and asks both questions of the card
that was open while it happened:

* is the card on the screen **the same node** the operator opened, and
  is it still open?
* does its detail now say what the **server** says?

The state change is made the way a plan really changes: the plan's own
task list is rewritten, with a write that is atomic, so the reader on
the other side of it — the server, the panel — never sees a
half-written list.  Nothing is written to the server's database and no
double is installed: the plan's file is the plan's file.

What the percentages are compared against
-----------------------------------------
The card draws the **server's** counts, not its own: the percentage is
``round(completed * 100 / total)`` over the counts the server reports
for the plan, and the fraction is ``(completed/total)``.  So the
expected numbers are read back out of the server's own status endpoint
after the change, and the card is asked to agree with them.  A card
that shows a percentage nobody asked for is a card that is counting on
its own, and this case would not be able to tell that apart from a
card that is right.

How the case is run
-------------------
The browser is driven by a separate process, the way the sibling
end-to-end cases in this directory drive it: the driver script is
written into the world's own directory and run by an interpreter that
can import Playwright.  A machine without a Playwright driver, or
without a browser for it to drive, **skips** the case — a panel that
was not driven proves nothing about the panel, and a case that quietly
passes without a browser would be a green lie.

Nothing is left behind either: the world is torn down at the end of the
case, and the browser is closed by the driver whether the flow
finished or raised.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.e2e.plan_scheduler.test_scheduler_card_node_reuse_e2e import (
    _shutdown,
)
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

#: ``<repo>`` — the checkout the screenshots are written into.
REPO_ROOT: Path = BACKEND_DIR.parent

#: Where the evidence is kept.  It is the suite's own ``artifacts``
#: tree, which ``.gitignore`` keeps out of the index on purpose: a
#: screenshot is a runtime output, and a binary blob in a commit stops
#: the commit-range privacy scan (see
#: ``tests/static_gates/test_e2e_artifacts_are_not_tracked.py``).
ARTIFACT_DIR: Path = BACKEND_DIR / "tests" / "e2e" / "artifacts" / "e2e"

#: The screenshot of the card, open, after the plan's state changed.
LIVE_SHOT: Path = ARTIFACT_DIR / "scheduler-card-live-update.png"

#: The sample record — what the driver saw, in the order it saw it.
LIVE_RECORD: Path = ARTIFACT_DIR / "scheduler-card-live-update.json"


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: How long the panel is given to notice the change, in seconds.  The
#: panel reads its read model every five seconds and refreshes a card
#: whose answer has grown old, so seven seconds is more than one of
#: the panel's own intervals: the change has been seen, not waited
#: for.
SETTLE_SECONDS: float = 7.0

#: How long the second reading waits, in seconds, when the first one
#: did not catch up.  One re-reading is allowed — and one only: a card
#: that is still behind after it has been given two of the panel's own
#: intervals is a card that is behind, and the case says so.
RESAMPLE_SECONDS: float = 7.0


# ---------------------------------------------------------------------------
# The driver — the panel, watched across a change of state
# ---------------------------------------------------------------------------

_LIVE_DRIVER_SOURCE = r'''
"""Open one running card, change the plan's state, and watch the card catch up.

Arguments:
  1. the base url of the running server
  2. the directory the plans live in
  3. the directory the screenshot and the sample record are written into

What the driver does
--------------------
It opens the panel, opens the **first running card**, and reads that
card: whether it is the node the driver put on the window, whether the
card says it is open, and what its detail says.  That is the baseline.

Then it **changes the plan's state**.  The plan's own task list is
rewritten -- one task done, one task waiting -- and the write is
atomic: the new list goes into a temporary file beside the old one and
is then renamed over it.  A reader that caught the file half-written
would read a plan that does not exist, and a panel that showed that
would be reporting on a state nobody is in.

Then it asks the server what it now says the plan's counts are, waits
for the panel's own poll to come round, and reads the card again.

How the node is watched
-----------------------
The driver puts the card on the page's ``window`` under a name of its
own (``__probeCard``) once, right after the card has been opened.
Every reading from here on asks whether *that* node is the node the
panel is drawing now, and whether that node is still in the document.

What the report holds
---------------------
The two readings of the card, the counts the server reported after the
change, the percentage those counts make, and where the screenshot was
written.  Everything in it was read off the page or off the wire.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

#: The panel's running plans, as the panel draws them: one card per
#: plan.  The card is asked for by the plan it is about, so a card
#: that moved in the list is still the card that was watched.
RUNNING_CARD_SELECTOR = "#scheduler-running-cards .scheduler-plan-card"

#: The card's own two parts, and the one that carries the counts.
HEADER_SELECTOR = ".scheduler-plan-header"
DETAIL_SELECTOR = ".scheduler-plan-detail"
PCT_SELECTOR = ".scheduler-detail-pct"

#: The status endpoint the expected counts are read from.  It is the
#: same reading the panel's own card is built from, so the card and
#: this case are asked about the same facts.
STATUS_PATH = "/api/plan/{0}/status"

#: The name the opened card is put on the page's ``window`` under.  It
#: is the driver's own name: the page is not asked to remember anything.
PROBE_NAME = "__probeCard"

#: How long the running card is waited for, in milliseconds.  The card
#: is waited for rather than slept for: a card that never arrives is a
#: failure, and a fixed sleep would hide it behind a green run.
CARD_WAIT_MS = 60000

#: How long the detail's text is waited for, in seconds.  The detail
#: becomes visible the moment the card is opened; its text arrives
#: with the answer, which is a separate moment.
TEXT_TIMEOUT = 30.0

#: How long the panel is given to notice the change, in seconds.
SETTLE_SECONDS = 7.0

#: How long the second reading waits, in seconds.
RESAMPLE_SECONDS = 7.0

#: The screenshot's name.
SHOT_NAME = "scheduler-card-live-update.png"

#: The sample record's name.
RECORD_NAME = "scheduler-card-live-update.json"


def browser_candidates():
    """The chromium builds under the Playwright cache, newest first.

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
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    )

    def revision(path):
        """The build number in the cache directory's name."""
        for part in path.parts:
            if part.startswith("chromium") and "-" in part:
                tail = part.split("-")[-1]
                if tail.isdigit():
                    return int(tail)
        return 0

    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in root.glob(pattern):
                if candidate.exists():
                    found.append(candidate)
    found.sort(key=revision, reverse=True)
    return [str(candidate) for candidate in found]


def launch(driver):
    """The browser this driver drives, or ``None`` when there is none.

    Playwright's own browser is used when it is installed: it is the
    revision the driver was written against, and any other build is a
    guess.  When it is not installed, the chromium builds that *are*
    under the cache are tried, newest first.
    """
    try:
        return driver.chromium.launch()
    except Exception:  # noqa: BLE001 — any launch failure falls back
        pass
    for candidate in browser_candidates():
        try:
            return driver.chromium.launch(executable_path=candidate)
        except Exception:  # noqa: BLE001 — any launch failure falls back
            continue
    return None


def server_counts(base, plan_id):
    """The counts the server reports for this plan.

    The card is drawn from these counts, so they are what the card is
    asked to agree with.  The summary's own counts are the ones the
    card's progress bar is built from; the snapshot's own counts are
    the fallback for a server that answered without one.
    """
    url = base.rstrip("/") + STATUS_PATH.format(urllib.parse.quote(plan_id))
    request = urllib.request.Request(url, headers={"X-PDT-Request": "1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))

    summary = payload.get("summary") if isinstance(payload, dict) else None
    summary = summary if isinstance(summary, dict) else {}
    candidates = [
        summary.get("tasks"),
        payload.get("tasks") if isinstance(payload, dict) else None,
        payload.get("counts") if isinstance(payload, dict) else None,
    ]
    counts = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate:
            continue
        if not counts:
            counts = candidate
        try:
            if int(candidate.get("total") or 0) > 0:
                counts = candidate
                break
        except (TypeError, ValueError):
            continue
    return {
        "total": int(counts.get("total") or 0),
        "completed": int(counts.get("completed") or 0),
        "pending": int(counts.get("pending") or 0),
        "in_progress": int(counts.get("in_progress") or 0),
        "failed": int(counts.get("failed") or 0),
    }


def rewrite_tasks(tasks_file, plan_id):
    """The plan's task list, rewritten: one task done, one task waiting.

    The write is **atomic**: the new list goes into a temporary file
    beside the old one and is then renamed over it, so a reader — the
    server, the panel, this very case — never sees a half-written
    task list.  A plan that is read half-written is a plan that does
    not exist, and a panel that showed that would be reporting on a
    state nobody is in.

    The plan's other tasks are **not** kept: the list the panel shows
    is the list the plan has, and a second list would only make the
    counts mean something else.
    """
    data = json.loads(tasks_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {}
    existing = data.get("tasks")
    existing = existing if isinstance(existing, list) else []

    project_dir = ""
    for task in existing:
        if isinstance(task, dict) and task.get("project_dir"):
            project_dir = str(task["project_dir"])
            break

    data["tasks"] = [
        {
            "id": "{0}-done".format(plan_id),
            "title": "The task this plan has finished",
            "description": "Written by the live-update end-to-end case.",
            "test_command": "true",
            "project_dir": project_dir,
            "status": "completed",
        },
        {
            "id": "{0}-waiting".format(plan_id),
            "title": "The task this plan is waiting for",
            "description": "Written by the live-update end-to-end case.",
            "test_command": "true",
            "project_dir": project_dir,
            "status": "pending",
        },
    ]
    tmp = tasks_file.with_name(tasks_file.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(str(tmp), str(tasks_file))
    return data


#: The reading, taken out of the page.  ``same_node`` is the claim the
#: whole case is about: the card the operator opened is still the node
#: the panel is drawing, and that node is still in the document.
SAMPLE_SCRIPT = """
() => {
  const probe = window.__PROBE__ || null;
  const planId = probe && probe.dataset ? String(probe.dataset.planId || "") : "";
  const cards = Array.from(
    document.querySelectorAll('#scheduler-running-cards .scheduler-plan-card')
  );
  const card = cards.find(c => String(c.dataset.planId || "") === planId) || null;
  const detail = card ? card.querySelector('.scheduler-plan-detail') : null;
  const pct = card ? card.querySelector('.scheduler-detail-pct') : null;
  const text = detail ? String(detail.innerText || "") : "";
  return {
    plan_id: planId,
    same_node: Boolean(probe && card && probe === card && document.contains(card)),
    expanded: card ? String(card.dataset.expanded || "") : "",
    detail_len: text.trim().length,
    detail_text: text.trim(),
    pct_text: pct ? String(pct.innerText || "").trim() : "",
    card_count: cards.length,
  };
}
""".replace("__PROBE__", PROBE_NAME)

#: Putting the card on the window, once.  The card is found by the plan
#: it is about, so the plan's own id is what is handed over — never a
#: selector built out of a string.
PROBE_SCRIPT = """
(planId) => {
  const cards = Array.from(
    document.querySelectorAll('#scheduler-running-cards .scheduler-plan-card')
  );
  const card = cards.find(
    c => String(c.dataset.planId || "") === String(planId || "")
  ) || null;
  if (!card) return false;
  window.__PROBE__ = card;
  return true;
}
""".replace("__PROBE__", PROBE_NAME)

#: The rejections the page never handled.  They are collected from the
#: page itself: a promise nobody caught is not an exception the
#: browser reports, so the page is asked to remember it.
REJECTION_SCRIPT = """
() => {
  const seen = window.__REJECTIONS__ || [];
  return Array.isArray(seen) ? seen.map(String) : [];
}
"""


base = sys.argv[1]
plans_dir = Path(sys.argv[2])
shots = Path(sys.argv[3])
shots.mkdir(parents=True, exist_ok=True)

#: The exceptions the page raised, and the rejections it never handled.
js_errors = []

with sync_playwright() as driver:
    browser = launch(driver)
    if browser is None:
        # No browser for the driver to drive: the panel cannot be shown,
        # so there is nothing this case could say about it.
        print(json.dumps({"browser_unavailable": True}))
        sys.exit(3)
    try:
        page = browser.new_page()

        def on_page_error(error):
            js_errors.append("pageerror: {0}".format(error))

        page.on("pageerror", on_page_error)
        page.add_init_script(
            """
            window.__REJECTIONS__ = [];
            window.addEventListener('unhandledrejection', (event) => {
              const reason = event && event.reason;
              const text = reason && reason.message ? reason.message : String(reason);
              window.__REJECTIONS__.push(text);
            });
            """
        )

        page.goto(base, wait_until="load")
        page.wait_for_selector("#app", timeout=30000)

        # The one click the operator makes to get here.
        page.click("#btn-open-scheduler")
        page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)

        # The panel draws its running plans as cards; the driver waits
        # for one, so that what is read is a panel that has drawn them.
        # A card that never arrives is a failure, not a skip.
        page.wait_for_selector(RUNNING_CARD_SELECTOR, timeout=CARD_WAIT_MS)

        card = page.locator(RUNNING_CARD_SELECTOR).first
        plan_id = (card.get_attribute("data-plan-id") or "").strip()

        # Open it, and wait for the answer to arrive: a card that is
        # open and empty has proved nothing.
        card.locator(HEADER_SELECTOR).click()
        detail = card.locator(DETAIL_SELECTOR)
        detail.wait_for(state="visible", timeout=30000)
        deadline = time.time() + TEXT_TIMEOUT
        while True:
            text = detail.inner_text().strip()
            if text:
                break
            if time.time() >= deadline:
                raise AssertionError("the detail never carried any text")
            page.wait_for_timeout(100)

        # Put the card on the window under a name of the driver's own.
        # Every reading from here on asks about *this* node.
        if not page.evaluate(PROBE_SCRIPT, plan_id):
            raise AssertionError(
                "the card to watch could not be put on the window"
            )

        # The baseline: the card as it was opened.
        before = page.evaluate(SAMPLE_SCRIPT)

        # The change: the plan's own task list, rewritten.
        tasks_file = plans_dir / plan_id / "tasks.json"
        rewrite_tasks(tasks_file, plan_id)
        counts = server_counts(base, plan_id)

        # What the card is expected to say about those counts: the
        # server's percentage and the server's fraction.
        total = counts["total"]
        completed = counts["completed"]
        percent = max(0, min(100, round(completed * 100 / total))) if total else 0
        ratio = "{0}/{1}".format(completed, total)
        expected_text = "{0}% ({1})".format(percent, ratio)

        # The panel's own poll comes round, and the card catches up.
        page.wait_for_timeout(int(SETTLE_SECONDS * 1000))
        after = page.evaluate(SAMPLE_SCRIPT)
        resampled = False
        if expected_text not in after.get("pct_text", ""):
            # One re-reading is allowed: the card may have been read
            # between the panel's poll and the refresh it asks for.
            page.wait_for_timeout(int(RESAMPLE_SECONDS * 1000))
            after = page.evaluate(SAMPLE_SCRIPT)
            resampled = True

        shot = shots / SHOT_NAME
        page.screenshot(path=str(shot), full_page=True)

        rejections = page.evaluate(REJECTION_SCRIPT)
        for rejection in rejections:
            js_errors.append("unhandledrejection: {0}".format(rejection))

        updated = (
            expected_text in after.get("pct_text", "")
            and after.get("detail_text") != before.get("detail_text")
        )
        record = {
            "plan_id": plan_id,
            "before": before.get("detail_text", ""),
            "after": after.get("detail_text", ""),
            "samples": [before, after],
            "counts": counts,
            "expected": {
                "percent": percent,
                "ratio": ratio,
                "text": expected_text,
            },
            "updated": bool(updated),
            "resampled": resampled,
            "js_errors": js_errors,
            "screenshot": str(shot),
        }
        (shots / RECORD_NAME).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(record, ensure_ascii=False))
    finally:
        browser.close()
'''


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Driving the panel
# ---------------------------------------------------------------------------


def _drive(world: _World) -> Dict[str, Any]:
    """Open a card, change the plan's state, and report.

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

    script = world.root / "drive_card_live_update.py"
    script.write_text(_LIVE_DRIVER_SOURCE, encoding="utf-8")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            interpreter,
            str(script),
            world.base_url,
            str(world.plans_dir),
            str(ARTIFACT_DIR),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=_driver_env(),
    )
    if result.returncode == 3:
        pytest.skip(
            "the Playwright driver is installed but no browser is; the "
            "panel cannot be driven"
        )
    if result.returncode != 0:
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    report = json.loads(result.stdout.strip().splitlines()[-1])

    # The screenshot is reported the way a reader would name it: from the
    # repository's root, not from this machine's directory layout.
    shot = Path(report["screenshot"])
    try:
        report["screenshot"] = shot.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        report["screenshot"] = shot.as_posix()

    LIVE_RECORD.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _samples(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The readings of a report, in the order they were taken."""
    samples = report.get("samples")
    assert isinstance(samples, list) and samples, (
        f"the report holds no samples: {report!r}"
    )
    return samples


def _expected(report: Dict[str, Any]) -> Dict[str, Any]:
    """The numbers the card is expected to show, read off the server."""
    expected = report.get("expected")
    assert isinstance(expected, dict), (
        f"the report holds no expectation: {report!r}"
    )
    return expected


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_detail_updates_in_place_after_state_change(card_world):
    """The open card's detail says what the server says, after the change.

    The card is asked for the percentage and the fraction the server
    reports for the plan, and it is asked for them **after** the plan's
    state changed — a card that still shows the old numbers is a card
    that is showing a picture of the past.
    """
    report = _drive(card_world)
    samples = _samples(report)
    expected = _expected(report)

    assert report["plan_id"] in PLAN_IDS, (
        f"the card that was watched is about {report['plan_id']!r}, which "
        f"is not one of the plans this world imported: {PLAN_IDS!r}"
    )

    before = samples[0]
    after = samples[-1]

    # The card's own text must carry the server's percentage and the
    # server's fraction.
    pct_text = str(after.get("pct_text") or "")
    assert "{0}%".format(expected["percent"]) in pct_text, (
        "the card does not show the percentage the server reports "
        f"({expected['percent']}%): {pct_text!r}"
    )
    assert "({0})".format(expected["ratio"]) in pct_text, (
        "the card does not show the fraction the server reports "
        f"({expected['ratio']}): {pct_text!r}"
    )

    # And the card's text must have moved: the same text before and
    # after is a card that did not notice anything.
    assert after.get("detail_text") != before.get("detail_text"), (
        "the card says exactly what it said before the plan's state "
        f"changed: {before.get('detail_text')!r}"
    )
    assert report.get("updated") is True, (
        f"the card did not catch up with the server's counts: {report!r}"
    )


def test_card_node_and_expanded_flag_survive_update(card_world):
    """The card the operator opened is still the card, and still open.

    The node is the thing a redraw used to throw away, so the node's
    identity is the claim: the card on the screen is the card that was
    opened, it is still in the document, and it is still open.
    """
    report = _drive(card_world)
    samples = _samples(report)

    for index, sample in enumerate(samples):
        assert sample["same_node"] is True, (
            "the card the operator opened is no longer the card the panel "
            f"is drawing (sample {index}): {sample!r}"
        )
        assert sample["expanded"] == "true", (
            f"the card was not open at sample {index}: {sample!r}"
        )
        assert int(sample["detail_len"]) > 0, (
            f"the detail of the open card was empty at sample {index}: "
            f"{sample!r}"
        )


def test_acceptance_screenshot_and_no_js_errors(card_world):
    """The evidence of the run is on disk, and the page raised nothing.

    The screenshot is what the panel looked like with the card open
    after the change, and the sample record is what the driver saw.  A
    run that left neither has told nobody anything.

    The page must also have been well behaved: an exception nobody
    caught, or a promise nobody handled, is a failure of the panel
    that no screenshot would show.
    """
    report = _drive(card_world)

    shot = REPO_ROOT / report["screenshot"]
    assert shot.is_file(), (
        f"the screenshot of the open card is not there: {shot}"
    )
    assert shot.stat().st_size > 0, (
        f"the screenshot of the open card is empty: {shot}"
    )

    assert report.get("js_errors") == [], (
        "the page raised something the case did not expect: "
        f"{report.get('js_errors')!r}"
    )
