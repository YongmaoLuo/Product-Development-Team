"""End-to-end: a **failed** plan's two buttons, clicked in a real browser.

What this file is for
---------------------
Tasks 1-3 of this plan made three layers agree with each other: the
lifecycle facade accepts the ``failed`` phase, the panel draws two
*different* buttons on a failed plan, and the request layer posts the
action each of those buttons means.  Task 4 proved the chain over HTTP.

This file proves the part a user actually touches: **the button**.  The
question is not "does the route accept the request" but "does a person
who cannot read the source code see two different buttons, and does the
one they press do the thing its label says".

So the browser is the point:

* the page is **served by the backend** (the frontend is a static bundle
  the backend mounts), opened in a **real Chromium** driven by
  Playwright;
* the buttons are found the way a user's eye finds them — by their
  label and their ``data-action`` — and **clicked**, not called;
* the assertion is made against **the server's own answer** after the
  click, not against the DOM.

What is real here
-----------------
The server is a child process (the world from task 4), the state is a
SQLite file, the plans are directories, and the click travels through
the page's own ``fetch`` — the same request the operator's mouse
produces.

What is a double
----------------
The **model**, and only the model: the verification round asks a model
what to verify, so the answers come from a fixture directory
(``PDT_LLM_MODE=scripted`` with ``PDT_LLM_SCRIPT_DIR``) instead of from
a provider.  This is the repository's own seam — the same one
``tests/e2e/test_plan_lifecycle_ui_e2e.py`` uses — and it is not
optional here: without a fixture directory the verification round dies
in milliseconds (``orchestrator_init_failed``), and a round that is
already over cannot be observed *running*, which is the whole point of
the re-run case.

Why the browser runs in its own process
---------------------------------------
Playwright is not importable from the interpreter that runs the suite
(it is not a dependency of the backend), and it is not installed into
it either — the backend's virtualenv does not carry a browser.  So the
driver is a script run by an interpreter that *can* import Playwright
(:func:`_playwright_interpreter` finds one by asking the environment,
never by hard-coding a path), and it reports what it saw as JSON on its
last stdout line.  When no such interpreter exists, or the browser
binaries are missing, the case **skips** and says so — a browser that
is not there is not a pass.

What this file produces
-----------------------
Three assertions and the screenshots behind them, under
``/tmp/pdt-e2e/screens`` (or ``$PDT_E2E_ARTIFACTS_DIR``):

* ``failed-panel.png`` — the failed row with its two buttons;
* ``failed-start.png`` — the panel after "re-queue" was pressed;
* ``failed-rerun-verification.png`` — the panel after "re-run
  verification" was pressed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from .test_failed_plan_actions_api_e2e import (
    BACKEND_DIR,
    FAILED_PHASE,
    PLAN_A_ID,
    PLAN_B_ID,
    QUEUED_PHASE,
    FailedPlanWorld,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The plan this file's panel case looks at
# ---------------------------------------------------------------------------

#: The plan the *panel* case reads its buttons from.
#:
#: A third plan, on purpose.  The other two plans are moved by the other
#: two cases — the start case takes plan A out of ``failed`` and the
#: re-run case takes plan B into a verification round — so a panel case
#: that read either of them would depend on the order the cases ran in.
#: This one is only ever looked at.
PANEL_PLAN_ID: str = "pdt-e2e-failed-panel"

#: The two labels the failed row must carry.
START_LABEL: str = "重新排队执行"
RERUN_LABEL: str = "重跑验证"


# ---------------------------------------------------------------------------
# Where the screenshots land
# ---------------------------------------------------------------------------

#: The screenshots' directory.  The brief's path, overridable because a
#: runner may want them somewhere else — and because a path written
#: into a test file should be a *default*, not a law.
ARTIFACTS_DIR: Path = Path(
    os.environ.get("PDT_E2E_ARTIFACTS_DIR", "/tmp/pdt-e2e/screens")
)


# ---------------------------------------------------------------------------
# The world: task 4's world, plus the two things the browser pass needs
# ---------------------------------------------------------------------------


def _seed_failed_plan(world: "FailedPlanWorld", plan_id: str) -> None:
    """Seed one more **failed** plan into the world's state and plans tree.

    The same five steps the world seeds its own plans with, and for the
    same reasons: the tables, the routing row (the phase), the legacy
    file, the project directory and one task.
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    connection = open_db(world.db_path)
    try:
        migrate(connection)
        routing = RoutingRepository(connection)
        execution = ExecutionRepository(connection)

        plan_dir = world.plans_dir / plan_id
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        routing.insert(plan_id, FAILED_PHASE)
        (plan_dir / "plan_state.json").write_text(
            json.dumps({"current_phase": FAILED_PHASE}, ensure_ascii=False),
            encoding="utf-8",
        )
        execution.insert(plan_id, project_dir=str(project_dir))
        (plan_dir / "tasks.json").write_text(
            json.dumps(
                {
                    "requirement": "A plan that failed, waiting to be re-run.",
                    "tasks": [
                        {
                            "id": f"{plan_id}-1",
                            "title": "The one task this plan has",
                            "description": "The e2e plan waits for the operator.",
                            "test_command": "true",
                            "project_dir": str(project_dir),
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    finally:
        connection.close()


class BrowserFailedPlanWorld(FailedPlanWorld):
    """Task 4's world, with a scripted model and a third failed plan.

    Two additions, and both of them are about *what the round does*,
    not about what the button does:

    * **a scripted model.**  The verification round asks a model what to
      verify.  The world's own environment switches the model to
      scripted mode but points at no fixture directory, so that ask
      fails in milliseconds and the round is over before anyone can
      look at it.  A round that is already over is not a round that is
      *running*, so the re-run case would be measuring the missing
      fixture instead of the button.  The fixture directory is the
      repository's own seam (``PDT_LLM_MODE=scripted`` with
      ``PDT_LLM_SCRIPT_DIR``), used the same way by
      ``tests/e2e/test_plan_lifecycle_ui_e2e.py``.
    * **a third failed plan**, so the panel case does not depend on the
      order the other two cases run in.
    """

    def _server_env(self) -> Dict[str, str]:
        env = super()._server_env()
        env["PDT_LLM_SCRIPT_DIR"] = str(self._write_llm_fixtures())
        return env

    def _write_llm_fixtures(self) -> Path:
        """Write the fixture directory the scripted model answers from.

        One catch-all matcher: every prompt the verification flow asks
        is answered by the same fixture.  The scripted registry has no
        silent fallback — a prompt nothing matches is an error — so the
        catch-all is what keeps the round from dying on a prompt
        nobody thought of in advance.
        """
        script_dir = self.root / "llm-script"
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "registry.json").write_text(
            json.dumps({"prompts": {"catch_all": "e"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (script_dir / "catch_all.txt").write_text(
            json.dumps({"findings": [], "summary": "scripted"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return script_dir

    def _seed_plans(self) -> None:
        super()._seed_plans()
        _seed_failed_plan(self, PANEL_PLAN_ID)


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


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = r'''
"""Drive the served panel in a real browser and report what happened.

One mode per moment of the story:

* ``panel`` -- the failed plan's row: which buttons it offers.
* ``start`` -- the "re-queue" button: the plan's phase afterwards.
* ``rerun`` -- the "re-run verification" button: the round's status.

The page is driven through its own events (``click``), so the
frontend's own code paths run -- including the ``fetch`` that carries
the request guard's header.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"
#: The two names above are the guard's own (see
#: ``request_guard.REQUEST_HEADER``); the driver runs in another
#: interpreter and cannot import this package, so it carries them.


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


mode, root, plan_panel, plan_start, plan_rerun, artifacts_dir = sys.argv[1:7]
artifacts = Path(artifacts_dir)
artifacts.mkdir(parents=True, exist_ok=True)

report = {
    "mode": mode,
    "page_errors": [],
    "console_errors": [],
    "api_responses": [],
    "buttons": [],
}

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}


def on_response(response):
    """Record the API calls the click made."""
    if "/api/" in response.url:
        report["api_responses"].append(
            {"url": response.url, "status": response.status}
        )


def on_console(message):
    if message.type == "error":
        report["console_errors"].append(message.text)


with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))
    page.on("console", on_console)
    page.on("response", on_response)

    page.goto(root + "/", wait_until="load")
    page.wait_for_selector("#btn-open-scheduler", timeout=30000)
    page.click("#btn-open-scheduler")

    if mode == "panel":
        plan_id = plan_panel
    elif mode == "start":
        plan_id = plan_start
    else:
        plan_id = plan_rerun

    # The row's buttons -- found the way a user's eye finds them.
    page.wait_for_selector(
        '.scheduler-action[data-plan="%s"]' % plan_id, timeout=30000
    )

    if mode == "panel":
        report["buttons"] = page.eval_on_selector_all(
            '.scheduler-action[data-plan="%s"]' % plan_id,
            """els => els.map(el => ({
                action: el.dataset.action,
                label: el.textContent.trim(),
            }))""",
        )
        page.screenshot(
            path=str(artifacts / "failed-panel.png"), full_page=True
        )

    elif mode == "start":
        with page.expect_response(
            lambda r: (
                "/api/plan/%s/scheduler/start" % plan_id in r.url
                and r.request.method == "POST"
            ),
            timeout=30000,
        ) as answered:
            page.click(
                '.scheduler-action[data-plan="%s"][data-action="start"]' % plan_id
            )
        response = answered.value
        report["start_response_status"] = response.status
        report["start_response_body"] = response.text()
        page.wait_for_timeout(500)
        page.screenshot(
            path=str(artifacts / "failed-start.png"), full_page=True
        )

    else:
        with page.expect_response(
            lambda r: (
                "/api/verification/%s/start" % plan_id in r.url
                and r.request.method == "POST"
            ),
            timeout=30000,
        ) as answered:
            page.click(
                '.scheduler-action[data-plan="%s"][data-action="rerun-verification"]'
                % plan_id
            )
        response = answered.value
        report["start_response_status"] = response.status
        report["start_response_body"] = response.text()

        # The round is written "running" *before* that answer is
        # produced, so this is the first read that can see it -- and
        # the round stays running for seconds, so a read that arrives
        # a millisecond later still sees a running round.
        http_status, document = api_get(
            root, "/api/verification/%s/status" % plan_id
        )
        report["verification_status_http"] = http_status
        report["verification_status_document"] = document
        page.wait_for_timeout(500)
        page.screenshot(
            path=str(artifacts / "failed-rerun-verification.png"),
            full_page=True,
        )

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright_interpreter() -> str:
    """An interpreter that can drive a browser, or a skip.

    A browser that is not there is not a pass: the case says so and
    stops.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "playwright unavailable: no interpreter on this machine can "
            "import playwright, so no browser can be driven"
        )
    return interpreter


@pytest.fixture(scope="session")
def failed_browser_world(tmp_path_factory, playwright_interpreter):
    """A running server, its own database, and three failed plans."""
    the_world = BrowserFailedPlanWorld(
        tmp_path_factory.mktemp("failed-plan-browser")
    )
    try:
        the_world.start()
        yield the_world
    finally:
        the_world.stop()


@pytest.fixture(scope="session")
def driver_script(tmp_path_factory) -> Path:
    """The browser driver, written out as a script of its own."""
    script = tmp_path_factory.mktemp("failed-plan-browser-driver") / "drive.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    return script


@pytest.fixture(scope="session")
def artifacts_dir() -> Path:
    """Where the screenshots land."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


# ---------------------------------------------------------------------------
# Running the driver
# ---------------------------------------------------------------------------


def _run_driver(
    interpreter: str,
    script: Path,
    mode: str,
    world: FailedPlanWorld,
    artifacts: Path,
) -> Dict[str, Any]:
    """Run one browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            mode,
            world.base_url,
            PANEL_PLAN_ID,
            PLAN_A_ID,
            PLAN_B_ID,
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


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_panel_offers_two_distinct_actions(
    failed_browser_world, playwright_interpreter, driver_script, artifacts_dir
):
    """A failed plan offers two buttons, and they say different things.

    The operator has to be able to tell "put this plan back in the
    queue" from "run its verification again" — those are two different
    commands, and a button that says "启动" for both of them sends the
    operator to the wrong one.
    """
    report = _run_driver(
        playwright_interpreter,
        driver_script,
        "panel",
        failed_browser_world,
        artifacts_dir,
    )

    buttons = report.get("buttons") or []
    assert len(buttons) == 2, (
        f"the failed row offers {len(buttons)} buttons, not two: {buttons!r}"
    )
    assert {button.get("action") for button in buttons} == {
        "start",
        "rerun-verification",
    }, f"the two buttons are not the two actions: {buttons!r}"
    assert {button.get("label") for button in buttons} == {
        START_LABEL,
        RERUN_LABEL,
    }, f"the two buttons do not say two different things: {buttons!r}"

    _assert_no_page_errors(report)


def test_click_start_requeues_plan(
    failed_browser_world, playwright_interpreter, driver_script, artifacts_dir
):
    """Pressing "re-queue" puts the failed plan back in the line.

    Not "starts it" and not "runs its verification" — back in the
    *queue*, which is what the label says.  The phase is read from the
    API the panel itself reads, after the click.
    """
    world = failed_browser_world

    report = _run_driver(
        playwright_interpreter,
        driver_script,
        "start",
        world,
        artifacts_dir,
    )

    assert report.get("start_response_status") == 200, (
        "the click was not answered with a success: "
        f"{report.get('start_response_status')!r} "
        f"{report.get('start_response_body')!r} "
        f"(requests: {report.get('api_responses')!r})"
    )

    # The listing is cached for a couple of seconds, so the phase is
    # polled rather than read once.
    phase = world.wait_for_phase(PLAN_A_ID, QUEUED_PHASE)
    assert phase == QUEUED_PHASE, (
        f"the plan is in the phase {phase!r} after the click, not "
        f"{QUEUED_PHASE!r}"
    )

    _assert_no_page_errors(report)


def test_click_rerun_verification_starts_round(
    failed_browser_world, playwright_interpreter, driver_script, artifacts_dir
):
    """Pressing "re-run verification" starts a verification round.

    The round is written ``running`` before the start request is
    answered, so the status read that follows the click sees a running
    round.  If the round is already over by then, the case fails and
    prints the whole status document — the round's own account of what
    happened to it is the evidence, and a loosened assertion would hide
    it.
    """
    world = failed_browser_world

    report = _run_driver(
        playwright_interpreter,
        driver_script,
        "rerun",
        world,
        artifacts_dir,
    )

    assert report.get("start_response_status") == 200, (
        "the click was not answered with a success: "
        f"{report.get('start_response_status')!r} "
        f"{report.get('start_response_body')!r} "
        f"(requests: {report.get('api_responses')!r})"
    )
    assert report.get("verification_status_http") == 200, (
        f"the verification status was not readable: "
        f"{report.get('verification_status_http')!r}"
    )

    document = report.get("verification_status_document")
    assert isinstance(document, dict), (
        f"the verification status is not a document: {document!r}"
    )
    assert document.get("verification_status") == "running", (
        "the verification round is not running right after the click; "
        f"the whole status document says: {json.dumps(document, ensure_ascii=False)}"
    )

    _assert_no_page_errors(report)
