"""End-to-end: an open card is still the same card three polls later.

What this case is for
---------------------
The panel redraws itself every five seconds.  A card that was drawn
again on every redraw is a **new** card each time: the operator's fold
is gone, the answer they were reading is gone, and the node their
scroll position and their click referred to is a node that is no longer
on the page.  The panel looks the same either way — a folded card and
an open card are both "a card" — so the only thing that tells the two
apart is the **node**: whether the card on the screen is the node that
was there before.

So this case drives a real browser against a real server and watches
one open card across three of the panel's own polls.  Four things are
checked, and each of them is a claim the operator would make:

* the card the operator opened is **still the same node** — the one
  they are looking at, still in the document, not a copy of it;
* the card **says it is open** at every sample — the fold did not close
  itself under the operator's eyes;
* the card's detail **is not empty** at any sample — a card that is
  open and blank is a card that lost its answer;
* the panel really did poll three times in between — the samples are
  spread across the panel's own intervals and not across a stopwatch.

Why the node is watched and not a picture
-----------------------------------------
A screenshot cannot answer the question.  Two cards that look the same
are two cards, and a picture of them proves only that the panel draws
cards.  So the node is put on the page's ``window`` under a name of the
driver's own, once, right after the card has been opened; every sample
then asks whether *that* node is the node the panel is drawing now.
A panel that draws its cards again answers no, and the sample says so.

How the samples are spread
--------------------------
The panel reads its read model every five seconds.  The samples are
taken at the baseline and then once every six seconds — more than one
of the panel's own intervals — so that the samples cover three of the
panel's polls.  The polls are counted off the requests the page
actually made, so "three polls" is a fact about the wire and not a
guess about a stopwatch.

The card is waited for, never slept for
----------------------------------------
A card that never arrives is a failure, and it is reported as one: the
driver waits for the card with a deadline and a timeout is a failed
run.  A fixed sleep would turn "the card never arrived" into a green
run, which is the one thing this case must not be.

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
import os
import signal
import subprocess
import time
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

#: ``<repo>`` — the checkout the screenshots are written into.
REPO_ROOT: Path = BACKEND_DIR.parent

#: Where the evidence is kept.  It is the suite's own ``artifacts``
#: tree, which ``.gitignore`` keeps out of the index on purpose: a
#: screenshot is a runtime output, and a binary blob in a commit stops
#: the commit-range privacy scan (see
#: ``tests/static_gates/test_e2e_artifacts_are_not_tracked.py``).
ARTIFACT_DIR: Path = BACKEND_DIR / "tests" / "e2e" / "artifacts" / "e2e"

#: The screenshot of the card, open, after the last sample.
STABLE_SHOT: Path = ARTIFACT_DIR / "scheduler-card-stable-expanded.png"

#: The sample record — what the driver saw, in the order it saw it.
STABLE_RECORD: Path = ARTIFACT_DIR / "scheduler-card-stable-expanded.json"


# ---------------------------------------------------------------------------
# The numbers this case runs against
# ---------------------------------------------------------------------------

#: How many samples are taken **after** the baseline one.  The baseline
#: is the sample of the card as it was opened; three more samples are
#: taken, one per poll.
SAMPLE_COUNT: int = 3

#: The gap between two samples, in seconds.  The panel reads its read
#: model every five seconds, so six seconds is more than one of the
#: panel's own intervals: every sample but the first is taken after a
#: poll has gone out.
SAMPLE_INTERVAL: float = 6.0

#: How many of the panel's own polls the samples have to cover.  The
#: samples span three intervals of six seconds, and the panel polls
#: every five, so three polls is the floor — and the number of polls
#: is counted off the wire, not assumed.
MIN_POLL_COVERAGE: int = 3


# ---------------------------------------------------------------------------
# The driver — the panel, watched through its own polls
# ---------------------------------------------------------------------------

_STABLE_DRIVER_SOURCE = r'''
"""Open one running card and watch it through three of the panel's polls.

Arguments:
  1. the base url of the running server
  2. the directory the screenshot and the sample record are written into

What the driver does
--------------------
It opens the panel, opens the **first running card**, and then samples
that card four times: once as the baseline, and then once every six
seconds — three more times.  Six seconds is more than one of the
panel's own five-second intervals, so the samples cover three of the
panel's polls.

Each sample is read **out of the page**, not out of a picture of it:
whether the card the operator is looking at is still the same node,
what the card says about itself, and how much detail it is showing.
The node is the thing a redraw used to throw away, so the node's
identity is the sample that matters.

How the node is watched
-----------------------
The driver puts the card on the page's ``window`` under a name of its
own (``__probeCard``) once, right after the card has been opened.
Every sample then asks two questions about it: is it still *the same
node* as the card the panel is drawing now, and is that node still in
the document?  A card that was drawn again answers neither.

What the report holds
---------------------
The samples, the number of the panel's own polls that were seen
between them, and where the screenshot was written.  Everything in it
was read off the page or off the wire.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

#: The panel's running plans, as the panel draws them: one card per
#: plan.  The card is asked for by the plan it is about, so a card
#: that moved in the list is still the card that was watched.
RUNNING_CARD_SELECTOR = "#scheduler-running-cards .scheduler-plan-card"

#: The card's own two parts.
HEADER_SELECTOR = ".scheduler-plan-header"
DETAIL_SELECTOR = ".scheduler-plan-detail"

#: The read model the panel polls.  The polls are counted from the
#: requests the page makes, so "three polls" is a fact about the wire
#: and not about a stopwatch.
STATE_PATH = "/api/scheduler/state"

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

#: The gap between two samples, in seconds.
SAMPLE_INTERVAL = 6.0

#: How many samples are taken after the baseline one.
SAMPLE_COUNT = 3

#: The screenshot's name.
SHOT_NAME = "scheduler-card-stable-expanded.png"

#: The sample record's name.
RECORD_NAME = "scheduler-card-stable-expanded.json"


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


def state_polls(log):
    """How many reads of the read model the page has made."""
    return sum(1 for url in log if url.split("?")[0].endswith(STATE_PATH))


#: The sample, read out of the page.  ``same_node`` is the claim the
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
  const text = detail ? String(detail.innerText || "") : "";
  return {
    plan_id: planId,
    same_node: Boolean(probe && card && probe === card && document.contains(card)),
    identical: Boolean(probe && card && probe === card),
    contained: Boolean(card && document.contains(card)),
    expanded: card ? String(card.dataset.expanded || "") : "",
    detail_len: text.trim().length,
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


base = sys.argv[1]
shots = Path(sys.argv[2])
shots.mkdir(parents=True, exist_ok=True)

#: Every request the page made.
log = []

samples = []

with sync_playwright() as driver:
    browser = launch(driver)
    if browser is None:
        # No browser for the driver to drive: the panel cannot be shown,
        # so there is nothing this case could say about it.
        print(json.dumps({"browser_unavailable": True}))
        sys.exit(3)
    try:
        page = browser.new_page()

        def on_request(request):
            log.append(request.url)

        page.on("request", on_request)

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
        # Every sample from here on asks about *this* node.
        if not page.evaluate(PROBE_SCRIPT, plan_id):
            raise AssertionError(
                "the card to watch could not be put on the window"
            )

        # The samples: the baseline, and then one per poll.
        for index in range(SAMPLE_COUNT + 1):
            if index:
                page.wait_for_timeout(int(SAMPLE_INTERVAL * 1000))
            sample = page.evaluate(SAMPLE_SCRIPT)
            sample["state_polls"] = state_polls(log)
            samples.append(sample)

        shot = shots / SHOT_NAME
        page.screenshot(path=str(shot), full_page=True)

        record = {
            "plan_id": plan_id,
            "samples": samples,
            "state_polls": state_polls(log),
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


# ---------------------------------------------------------------------------
# Driving the panel
# ---------------------------------------------------------------------------


def _drive(world: _World) -> Dict[str, Any]:
    """Open a card, watch it through three polls, and report.

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

    script = world.root / "drive_card_stability.py"
    script.write_text(_STABLE_DRIVER_SOURCE, encoding="utf-8")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/",
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

    STABLE_RECORD.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _samples(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The samples of a report, in the order they were taken."""
    samples = report.get("samples")
    assert isinstance(samples, list), (
        f"the report holds no samples: {report!r}"
    )
    return samples


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_expanded_card_node_survives_three_polls(card_world):
    """The card the operator opened is still the same node three polls later.

    The node is the thing a redraw used to throw away, so the node's
    identity is the claim: the card on the screen is the card that was
    opened, and it is still in the document.  A panel that draws its
    cards again answers no, and this case says so.
    """
    report = _drive(card_world)
    samples = _samples(report)

    assert len(samples) == SAMPLE_COUNT + 1, (
        f"the card was sampled {len(samples)} times, not "
        f"{SAMPLE_COUNT + 1}: {samples!r}"
    )

    assert report["plan_id"] in PLAN_IDS, (
        f"the card that was watched is about {report['plan_id']!r}, which "
        f"is not one of the plans this world imported: {PLAN_IDS!r}"
    )

    for index, sample in enumerate(samples):
        assert sample["same_node"] is True, (
            "the card the operator opened is no longer the card the panel "
            f"is drawing (sample {index}): {sample!r}"
        )

    # The samples have to span the panel's own polls — otherwise the
    # case would be about a card that never had to survive anything.
    polls = int(report["state_polls"]) - int(samples[0]["state_polls"])
    assert polls >= MIN_POLL_COVERAGE, (
        "the samples covered "
        f"{polls} of the panel's polls, not {MIN_POLL_COVERAGE}: "
        f"{report!r}"
    )


def test_expanded_flag_never_flips(card_world):
    """The card says it is open at every sample.

    A repaint that folded the card would close it under the operator's
    eyes, and the fold is the one thing the operator did.
    """
    report = _drive(card_world)
    samples = _samples(report)

    for index, sample in enumerate(samples):
        assert sample["expanded"] == "true", (
            f"the card was not open at sample {index}: {sample!r}"
        )


def test_detail_text_never_empty(card_world):
    """The card's detail is never empty at any sample.

    An open card that says nothing is a card that lost its answer to a
    repaint — which is exactly what the operator was reading.
    """
    report = _drive(card_world)
    samples = _samples(report)

    for index, sample in enumerate(samples):
        assert sample["detail_len"] > 0, (
            f"the detail of the open card was empty at sample {index}: "
            f"{sample!r}"
        )


def test_screenshot_archived(card_world):
    """The evidence of the run is on disk.

    The screenshot is what the panel looked like with the card open
    after the last sample, and the sample record is what the driver
    saw.  A run that left neither has told nobody anything.
    """
    report = _drive(card_world)

    shot = REPO_ROOT / report["screenshot"]
    assert shot.is_file(), (
        f"the screenshot of the open card is not there: {shot}"
    )
    assert shot.stat().st_size > 0, (
        f"the screenshot of the open card is empty: {shot}"
    )
