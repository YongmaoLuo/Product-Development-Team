"""End-to-end: a plan parked for quota is visible on the real page, and
comes back on its own.

Background
----------
``waiting_for_quota`` is what a plan's phase becomes when its run stops
because the provider quota window is shut.  Two things have to be true
for that state to be useful, and neither of them shows up in the state
itself:

* **Somebody can see it.**  A parked plan nobody can see is a plan
  nobody knows to wait for.  The scheduler's panel draws the plans an
  operator can act on, and the parked plan is one of them:
  ``/plans?include_terminal=true`` does not treat ``waiting_for_quota``
  as a terminal phase (``routes/plans.py::list_plans`` says so in its
  own docstring), and the frontend's ``SCHEDULER_ACTIONS_BY_PHASE``
  gives the phase a ``start`` action — so the row survives the panel's
  "actionable" filter.
* **It comes back by itself.**  The quota window opens again minutes
  later, and the plan has to resume without anybody clicking anything.
  The heartbeat drives ``quota_resume.run_resume_sweep()`` on every
  tick; the sweep reads the quota window and starts the parked plan
  again when the window is open.

This file is the acceptance for both, on a real server and — for the
first one — in a real browser.

The cases
---------
* ``test_parked_plan_visible_over_http`` — the parked plan is in the
  read model the panel reads (``GET /api/scheduler/state``), with a
  phase and a phase label of its own.  This one needs no browser, so it
  runs in any environment.
* ``test_quota_recovery_resumes_without_manual_start`` — the quota
  window opening brings the plan back, and no human pressed anything:
  the server's own access log carries no start request, and the plan's
  own verdict has moved on from ``waiting_for_quota``.
* ``test_operator_sees_parked_plan_in_browser`` — the row is on the
  page, in the terminal list, with the parked phase in its own column.

The seams this file drives
--------------------------
* the quota window: ``PDT_QUOTA_FIXTURE`` names a JSON file whose
  ``exhausted`` flag *is* the reading (``quota_resume._fixture_reading``);
* the scheduler's settings: ``PDT_SCHEDULER_FILE`` names a YAML file
  whose ``scheduler.enabled`` says whether the scheduler is bound at all.
  It is off by default — that is the rollback switch — and a scheduler
  nobody enabled answers with the idle snapshot, which has no parked
  plans in it;
* the executor: ``PDT_EXECUTOR_CMD``, installed by the sibling E2E
  helper, stands in for the real one, so no model is ever called.

The helpers are **read**, never modified: ``_free_port``, ``_boot``,
``_wait_ready``, ``_teardown`` and ``_http_json`` come from
``test_execution_status_restart_e2e.py``; ``_playwright_interpreter``
and ``_driver_env`` come from ``test_fastfail_no_ghost_state_ui.py``.
Both are loaded by path — ``backend/tests/e2e`` is not a package — which
is what their own module comments ask a sibling file to do.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List, Optional

import pytest

pytestmark = [pytest.mark.e2e]

BACKEND_DIR = Path(__file__).resolve().parents[2]
E2E_DIR = BACKEND_DIR / "tests" / "e2e"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from pause_state import write_pause_checkpoint  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.execution_repository import (  # noqa: E402
    ExecutionRepository,
)
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)


# ---------------------------------------------------------------------------
# The sibling helpers, loaded by path
# ---------------------------------------------------------------------------


def _load_sibling(path: Path, alias: str) -> ModuleType:
    """Load a sibling E2E module by its path.

    ``backend/tests/e2e`` has no ``__init__.py``, so a sibling cannot be
    imported by name — it is loaded out of its own file instead, which
    is what the sibling modules' own comments say a copying file should
    do.  The module is read; nothing in it is changed.
    """
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken tree
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


_restart = _load_sibling(
    E2E_DIR / "test_execution_status_restart_e2e.py", "pdt_e2e_restart_helpers"
)
_ui = _load_sibling(
    E2E_DIR / "test_fastfail_no_ghost_state_ui.py", "pdt_e2e_ui_helpers"
)

_free_port = _restart._free_port
_http_json = _restart._http_json
_boot = _restart._boot
_wait_ready = _restart._wait_ready
_teardown = _restart._teardown
_server_output = _restart._server_output
_playwright_interpreter = _ui._playwright_interpreter
_driver_env = _ui._driver_env


# ---------------------------------------------------------------------------
# The scene
# ---------------------------------------------------------------------------

#: The plan this file parks.  A leaf name and nothing exotic: every
#: route that takes a plan id validates it before it resolves a path.
PLAN_ID = "pq-quota"

#: The phase a plan is parked in.
PARKED_PHASE = "waiting_for_quota"

#: How long the browser may take to show what it is asked to show.  A
#: page that cannot show it in this budget is a failure, and the
#: failure says why — there is no fixed sleep anywhere in the driver.
BROWSER_TIMEOUT_MS = 20_000

#: How long the quota window may stay shut before the case gives up on
#: the revival.  The heartbeat ticks every thirty seconds, so this is
#: three ticks and one spare.
RECOVERY_TIMEOUT_SECONDS = 90.0

#: How long the case waits while the window is *still* shut, so that the
#: heartbeat has ticked at least once with nothing to revive.  It is
#: what makes the revival the window's doing and not the boot's.
SHUT_WINDOW_SECONDS = 35.0

#: Where the screenshot is written.  The directory is gitignored — the
#: picture is an artefact of a run, not a source file.
ARTIFACTS_DIR = E2E_DIR / "artifacts"

#: The requests a **human** start would leave in the access log.  The
#: sweep starts the run in process (``quota_resume.resume_execution``
#: calls ``routes.execution.start_execution`` directly), so a run that
#: came back on its own leaves no such line behind.  A line here would
#: mean something else started it.
MANUAL_START_REQUESTS = (
    '"POST /api/execution/',
    '"/scheduler/start"',
    '"/scheduler/resume"',
    '"/scheduler/promote"',
)


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def _write_quota_reading(path: Path, exhausted: bool) -> None:
    """Write the quota reading the sweep will read.

    The file is the seam the product itself offers for this case: a
    JSON object with an ``exhausted`` key is one reading.  ``True`` is a
    shut window — every provider is out — and ``False`` is an open one.
    """
    path.write_text(json.dumps({"exhausted": exhausted}), encoding="utf-8")


def _write_scheduler_settings(path: Path) -> None:
    """Write the scheduler's own settings.

    The read model this file reads the parked plans out of is the
    scheduler's, and a scheduler nobody enabled stays unbound: its
    snapshot is the idle document, which has no parked plans in it.  The
    file says "enabled" and nothing else — every other setting keeps
    its default.
    """
    path.write_text(
        "scheduler:\n  enabled: true\n  dry_run: false\n  capacity: 4\n",
        encoding="utf-8",
    )


def _seed_parked_plan(work: Path, plan_id: str, project_dir: Path) -> None:
    """Write everything a plan parked for quota really has.

    The phase lives in the routing table and the run's verdict lives in
    the execution table; the two are separate rows and both have to say
    the same thing, because the read model joins them.

    The working directory the run continues in is on disk in three
    places, and all three are written:

    * the plan's **pause checkpoint** (``pause_state.json``) — this is
      the one the start path reads.  The sweep knows only a plan id,
      so the directory has to already be on disk when the sweep fires;
    * the ``plan_execution.project_dir`` column — the durable record of
      where the run works;
    * an ``execution.json`` sidecar — the plan's own record of the same
      thing.
    """
    plan_dir = work / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    # The work that is still owed — an empty table: the run is waiting
    # for the window to open, not for a task.  The stop reason is the
    # quota marker, and the marker is what keeps the hand-off to the
    # verification round from firing a model when the run ends.
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [], "stop_reason": PARKED_PHASE}), encoding="utf-8"
    )
    (plan_dir / "execution.json").write_text(
        json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
    )
    write_pause_checkpoint(
        plan_dir,
        kind="execution",
        project_dir=str(project_dir),
        reason=PARKED_PHASE,
    )

    conn = open_db(work / "state.db")
    try:
        migrate(conn)
        routing = RoutingRepository(conn)
        routing.insert(plan_id, "executing")
        routing.try_mark_phase(plan_id, ("executing",), PARKED_PHASE)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            exec_status=PARKED_PHASE,
            stop_reason=PARKED_PHASE,
            project_dir=str(project_dir),
        )
    finally:
        conn.close()


def _exec_status(work: Path, plan_id: str) -> Optional[str]:
    """The run's own verdict, read out of the state database.

    Read from the same file the server writes, in a separate connection:
    the row is the durable truth about the run, and it is the thing the
    "nobody pressed anything" case has to see change.
    """
    conn = open_db(work / "state.db")
    try:
        row = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    if row is None:
        return None
    return str(row.get("exec_status") or "")


def _parked_rows(base_url: str) -> List[Dict[str, Any]]:
    """The parked rows the read model answers with."""
    status, body = _http_json("GET", f"{base_url}/api/scheduler/state")
    if status != 200 or not isinstance(body, dict):
        return []
    rows = body.get("parked")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _revival_lines(proc: subprocess.Popen, plan_id: str) -> List[str]:
    """Every line in the server's own log that revived this plan."""
    out = []
    for line in _server_output(proc, limit=1_000_000).splitlines():
        if "quota_recovered_resume" in line and f"plan={plan_id}" in line:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parked_server(tmp_path, monkeypatch) -> Iterator[Dict[str, Any]]:
    """A real backend with one plan parked for quota.

    The plan is parked before the server starts, so the server finds a
    plan that is already waiting — which is what a backend that was
    running when the window shut looks like.

    The three environment variables go in **before** the boot: the boot
    helper starts the server from the environment it is running in, so a
    variable set afterwards would never reach the child.
    """
    work = tmp_path / "work"
    (work / "plans").mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    quota_reading = work / "quota.json"
    _write_quota_reading(quota_reading, True)

    scheduler_settings = work / "scheduler.yaml"
    _write_scheduler_settings(scheduler_settings)

    monkeypatch.setenv("PDT_QUOTA_FIXTURE", str(quota_reading))
    monkeypatch.setenv("PDT_SCHEDULER_FILE", str(scheduler_settings))
    # The stand-in executor lives until it is signalled; the number is
    # a ceiling, so that a teardown that failed to reach it still could
    # not leave it running forever.
    monkeypatch.setenv("PDT_E2E_EXIT_AFTER", "600")

    _seed_parked_plan(work, PLAN_ID, project_dir)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = _boot(work, port)
    try:
        _wait_ready(base_url, proc)
        yield {
            "base_url": base_url,
            "proc": proc,
            "plan_id": PLAN_ID,
            "work": work,
            "quota_reading": quota_reading,
        }
    finally:
        _teardown(proc)


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

#: The driver runs in its own process: Playwright's synchronous API
#: wants the event loop it was started with, and a server subprocess
#: has none of that to give.
_BROWSER_DRIVER = r'''
"""Open the panel, refresh it, and report what the operator can see."""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BROWSER_TIMEOUT_MS = __BROWSER_TIMEOUT_MS__


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache, and
    a driver whose expected revision is not the one installed cannot
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


def run():
    url, plan_id, shot = sys.argv[1], sys.argv[2], sys.argv[3]

    executable = browser_executable()
    launch_kwargs = {"executable_path": executable} if executable else {}

    with sync_playwright() as driver:
        browser = driver.chromium.launch(**launch_kwargs)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="load")

            # The panel is behind a button; the operator opens it.
            page.wait_for_selector(
                "#btn-open-scheduler", timeout=BROWSER_TIMEOUT_MS
            )
            page.click("#btn-open-scheduler")
            page.wait_for_selector(
                "#scheduler-view", state="visible", timeout=BROWSER_TIMEOUT_MS
            )
            page.click("#btn-refresh-scheduler")

            # The list is filled by the refresh's own read, so the row is
            # waited for rather than slept on: a panel that never shows
            # the plan must fail, not wait out the clock.
            page.wait_for_function(
                """(planId) => {
                    const body = document.querySelector('#scheduler-terminal-list');
                    return !!body && body.textContent.indexOf(planId) >= 0;
                }""",
                arg=plan_id,
                timeout=BROWSER_TIMEOUT_MS,
            )

            text = page.inner_text("#scheduler-terminal-list")
            rows = page.eval_on_selector_all(
                "#scheduler-terminal-list tr",
                "els => els.map(el => Array.from(el.querySelectorAll('td'))"
                ".map(td => td.textContent.trim()))",
            )
            page.screenshot(path=shot, full_page=True)
            return {
                "terminal_text": text,
                "rows": rows,
                "executable": executable,
            }
        finally:
            browser.close()


try:
    payload = run()
except Exception as exc:
    # The reason travels out on stderr: a page that never showed the
    # plan has to say which wait ran out.
    print(
        json.dumps({"error": type(exc).__name__, "detail": str(exc)}),
        file=sys.stderr,
    )
    sys.exit(1)

print(json.dumps(payload))
'''


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_parked_plan_visible_over_http(parked_server):
    """The parked plan is in the read model the panel reads.

    No browser is involved: the panel draws what this answer carries, so
    the answer is the thing that has to be right.  The row has to name
    the plan, and it has to carry a phase and a phase label of its own —
    a row with an empty phase is a row the panel cannot draw.
    """
    base_url = parked_server["base_url"]
    plan_id = parked_server["plan_id"]

    deadline = time.monotonic() + 30.0
    rows: List[Dict[str, Any]] = []
    while True:
        rows = _parked_rows(base_url)
        if any(row.get("plan_id") == plan_id for row in rows):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    row = next((r for r in rows if r.get("plan_id") == plan_id), None)
    assert row is not None, (
        f"the parked plan {plan_id!r} is not in the read model of "
        f"{base_url}/api/scheduler/state; the answer carried {rows!r}"
    )
    assert row.get("phase"), f"the parked row carries no phase: {row!r}"
    assert row.get("phase_label"), (
        f"the parked row carries no phase label: {row!r}"
    )


def test_quota_recovery_resumes_without_manual_start(parked_server):
    """The plan comes back on its own when the quota window opens.

    The window is shut first and the heartbeat is given a tick with
    nothing to revive, so the revival that follows can only be the
    window's doing.  Then the window opens, and the run starts by
    itself: the server's own log says so, the plan's own verdict has
    moved on, and the access log carries no start request — nobody
    pressed anything.
    """
    base_url = parked_server["base_url"]
    plan_id = parked_server["plan_id"]
    proc = parked_server["proc"]
    work = parked_server["work"]

    # The window is shut: the sweep must leave the plan alone.
    time.sleep(SHUT_WINDOW_SECONDS)
    assert not _revival_lines(proc, plan_id), (
        "the plan was revived while the quota window was shut:\n"
        + "\n".join(_revival_lines(proc, plan_id))
    )
    assert _exec_status(work, plan_id) == PARKED_PHASE, (
        "the plan left the parked phase while the quota window was shut: "
        f"{_exec_status(work, plan_id)!r}"
    )

    # The window opens.
    _write_quota_reading(parked_server["quota_reading"], False)

    deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
    lines: List[str] = []
    while time.monotonic() < deadline:
        lines = _revival_lines(proc, plan_id)
        if lines:
            break
        time.sleep(1.0)

    assert lines, (
        f"the plan was not revived within {RECOVERY_TIMEOUT_SECONDS:.0f}s of "
        "the quota window opening; the server's own log says:\n"
        + _server_output(proc, limit=20_000)
    )
    assert any("reason=quota_recovered" in line for line in lines), (
        f"the revival line carries no reason: {lines!r}"
    )

    # The run's own verdict moved on: the plan is not parked any more.
    status = _exec_status(work, plan_id)
    assert status != PARKED_PHASE, (
        f"the plan is still parked after the revival: exec_status={status!r}"
    )

    # And nobody pressed anything: the sweep starts the run in process,
    # so a start *request* would mean a hand had been on it.
    log = _server_output(proc, limit=1_000_000)
    for request in MANUAL_START_REQUESTS:
        assert request not in log, (
            f"the server was asked to start the plan by hand ({request!r}); "
            f"the revival has to be the sweep's own doing:\n{log}"
        )


def test_operator_sees_parked_plan_in_browser(parked_server, tmp_path):
    """The operator sees the parked plan on the real page.

    The page is opened, the panel is opened and refreshed the way the
    operator does it, and the terminal list is read.  The plan's id has
    to be on the screen and its row has to carry the parked phase in
    its own column.

    A missing browser driver is a **failure**, not a skip: this case is
    the acceptance for the page, and a page that was never opened has
    accepted nothing.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.fail(
            "no Playwright driver is installed in this environment, so the "
            "page cannot be driven and the parked plan cannot be seen; "
            "install one with `python -m playwright install chromium` and "
            "re-run this case"
        )

    base_url = parked_server["base_url"]
    plan_id = parked_server["plan_id"]

    shot = ARTIFACTS_DIR / plan_id / "quota-auto-resume.png"
    shot.parent.mkdir(parents=True, exist_ok=True)

    driver_script = tmp_path / "drive_scheduler_panel.py"
    driver_script.write_text(
        _BROWSER_DRIVER.replace("__BROWSER_TIMEOUT_MS__", str(BROWSER_TIMEOUT_MS)),
        encoding="utf-8",
    )

    result = subprocess.run(
        [interpreter, str(driver_script), base_url, plan_id, str(shot)],
        capture_output=True,
        text=True,
        timeout=180,
        env=_driver_env(),
    )
    assert result.returncode == 0, (
        "the browser driver failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    text = str(payload.get("terminal_text") or "")
    rows = payload.get("rows") or []

    assert plan_id in text, (
        f"the terminal list does not carry the plan id {plan_id!r}: {text!r}"
    )
    matching = [row for row in rows if row and str(row[0]) == plan_id]
    assert matching, (
        f"the terminal list has no row for the plan {plan_id!r}: {rows!r}"
    )
    assert matching[0][1] == PARKED_PHASE, (
        f"the row's phase column is {matching[0][1]!r}, not "
        f"{PARKED_PHASE!r}: {matching[0]!r}"
    )

    assert shot.exists() and shot.stat().st_size > 0, (
        f"the screenshot was not written: {shot}"
    )
