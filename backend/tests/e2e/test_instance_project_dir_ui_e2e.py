"""端到端验收：真浏览器打开真页面，实例身份按 ``project_dir`` 认人。

背景
----
前置（任务 5，``test_instance_project_dir_e2e.py``）已经在**机制层**证明了：
真后端进程在 ``/health/data`` 的 ``instance`` 块里自报**仓库根**
（``project_dir``），而这个值独立于它正在服务的计划树（``plans_dir``）。

机制层证明不了的是**用户看得见的那一环**：这次改动动了后端的进程身份语义，
前端由**同一个** FastAPI 进程从 ``frontend/`` 提供（没有独立的 dev server），
所以「后端还活着」与「页面还能用」在这里是同一件事。本文件走的就是这一环：

* **真页面** —— Playwright 驱动真 Chromium 打开真地址；
* **真用户输入** —— 用户**点击**计划卡片，页面走它自己的事件、自己的路由、
  自己的 ``fetch``；
* **真核对** —— 在**页面里**用页面自己的 ``fetch`` 读 ``/health/data``，
  断言它自报的 ``project_dir`` 就是仓库根，且与 ``plans_dir`` **不同**。

为什么要「在页面里」读 ``/health/data``
------------------------------------
页面读到的那份答案，是**这个浏览器**从这个源拿到的答案 —— 和用户看到的那份
页面是同一条链路。测试进程自己去 curl 只能证明「端口后面有个服务」；页面里
读，才能证明「这个页面用的就是那个自报 ``project_dir`` 的实例」。

不许 skip
----------
浏览器起不来、后端起不来、卡片不出现 —— 都是**失败**（并把 ``server.log`` 的
尾巴打出来），不是 skip：一条 skip 的退出码是 0，和「验证通过」没法区分。
这个文件存在的意义就是产出证据。

产物
----
* 截图 ``/tmp/instance-project-dir-e2e/plan-view.png``
* 浏览器报告 ``/tmp/instance-project-dir-e2e/ui-report.json``（供人工复核）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The siblings are reused **as they are**: task 5's session fixture (the real
# backend process, the plan it imported, the facts about both), the Playwright
# interpreter that can drive a real Chromium, and the helper that reads the
# tail of a log file when something has to be explained.
from tests.e2e.test_instance_identity_baseline_e2e import (  # noqa: E402
    _stop_server,
    _tail,
)
from tests.e2e.test_instance_project_dir_e2e import (  # noqa: E402
    EXPECTED_PROJECT_DIR,
    project_dir_backend,  # noqa: F401 - the fixture is used by name
)
from tests.e2e.test_plan_lifecycle_ui_e2e import (  # noqa: E402
    _boot_server,
    _playwright_interpreter,
    _write_scripted_llm,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

#: Where the artifacts have to land.  The brief names this path, so it is
#: this path — not a ``tmp_path`` that vanishes with the run.
#:
#: ``nosec B108`` — the hard-coded temporary directory **is** the point: the
#: artifact has to be at a path the reviewer can open by hand after the run.
ARTIFACTS_DIR = Path("/tmp/instance-project-dir-e2e")  # nosec B108

#: The screenshot's file name inside :data:`ARTIFACTS_DIR`.
SCREENSHOT_NAME = "plan-view.png"

#: The browser report's file name inside :data:`ARTIFACTS_DIR` — the raw
#: observation a human reads when the assertion below needs a second pair of
#: eyes.
REPORT_NAME = "ui-report.json"

#: The two phrases that must not be anywhere on the page.  「环境阻断」 is the
#: phrase the 2026-10-26 production incident was reported under; 「循环停止」
#: is the badge the frontend renders for a loop-stopped round.  Neither is a
#: string this plan's page may show.
BLOCKED_PHRASES = ("环境阻断", "循环停止")

#: What the frontend renders in the task list when the plan has **no** task
#: (``frontend/app.js`` 的 ``renderTasks``).  It is text, so "the task list
#: has text" would pass on it — the empty state is named here so the
#: assertion can rule it out.
EMPTY_TASK_TEXT = "暂无任务"

#: The plan the numeric-id walk serves.  Its ``tasks.json`` carries
#: **number** ids — a legal shape for a task id (the plan directory is
#: written by hand, by a person, and JSON numbers are numbers), and the
#: one shape the frontend's legacy ``esc()`` helper could not survive.
NUMERIC_ID_PLAN_ID = "vp-project-dir-fixture"

#: The two tasks of :data:`NUMERIC_ID_PLAN_ID`.  Their ids are numbers on
#: purpose — see the note above.
NUMERIC_ID_TASKS = (
    {"id": 1, "title": "instance 块发布 project_dir", "status": "pending"},
    {"id": 2, "title": "身份第三项按 project_dir 比较", "status": "pending"},
)

#: Where the numeric-id walk puts its artifacts.  A sub-directory of its
#: own, so it does not overwrite the plan-view screenshot / report that
#: the walk above leaves for a human to read.
NUMERIC_ID_ARTIFACTS = ARTIFACTS_DIR / "numeric-task-ids"


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

#: The driver, run as its own process.  It is written to disk and executed
#: with an interpreter that can import ``playwright``, so that a browser that
#: cannot start is a **failed subprocess** (a loud failure the test turns into
#: an error) rather than a skip.  The driver prints one JSON object on stdout.
_DRIVER = r'''
"""Open the served page, click the plan card, report what the page shows.

argv: <base_url> <plan_id> <artifacts_dir>

Everything it reports is read off the **rendered** page or out of the page's
own ``fetch`` — nothing is reconstructed from the API by the test process.
The point of this file is what a *user* sees, and what the page itself
learns about the backend it is talking to.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

#: The screenshot's file name.
SCREENSHOT_NAME = "plan-view.png"

#: The phrases that must not be anywhere on the page.
BLOCKED_PHRASES = ("环境阻断", "循环停止")


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache, so the
    binaries that *are* installed are found here instead: nothing is
    downloaded and nothing is hard-coded.  The cache lives under the home
    the run was launched with — the driver is started with ``$HOME`` pointed
    back at it, because the pytest tree redirects ``$HOME`` to a scratch
    directory.
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


base_url, plan_id, artifacts_dir = sys.argv[1:4]

executable = browser_executable()
if not executable:
    # Fail, do not skip: a skipped test proves nothing, and this file's
    # whole job is to prove what the page shows.
    raise SystemExit(
        "no chromium binary under the playwright cache -- this test refuses "
        "to skip; install one with `playwright install chromium`"
    )

card = '.plan-card[data-plan="%s"]' % plan_id
report = {"page_errors": [], "console_errors": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=executable)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

    def _on_console(message):
        if message.type == "error":
            report["console_errors"].append(message.text)

    page.on("console", _on_console)

    page.goto(base_url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)

    # The listing is served from a two-second cache, so a page that loads
    # right after the plan appeared shows a list without it.  Reloading is
    # what a user does (the sidebar polls), and it is the only way the
    # cache gets out of the way.
    found = False
    for _ in range(30):
        try:
            page.wait_for_selector(card, timeout=3000)
            found = True
            break
        except Exception:
            page.reload(wait_until="load")
            page.wait_for_timeout(1000)
    if not found:
        raise SystemExit("the plan card %s never appeared on the page" % card)

    # The user clicks the card: the page routes to the plan view and reads
    # the plan's own tasks through its own API calls.
    page.click(card)
    page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
    page.wait_for_timeout(1200)

    report["tasks_view_visible"] = page.is_visible("#tasks-view")
    report["tasks_list_visible"] = page.is_visible("#tasks-list")
    report["tasks_list_text"] = page.inner_text("#tasks-list")
    # The **cards**, not the text: a plan without tasks renders an empty
    # state whose text is 「暂无任务」 — non-empty, and a false pass for a
    # "the task list has text" assertion.  The count is the truth.
    report["task_card_count"] = page.evaluate(
        "() => document.querySelectorAll('#tasks-list .task-card').length"
    )

    body_text = page.evaluate("() => document.body.innerText")
    report["body_text"] = body_text
    report["blocked_text"] = any(
        phrase in body_text for phrase in BLOCKED_PHRASES
    )

    # The page reads its own backend -- the very road the UI travels.  The
    # guard header is the one the frontend itself sends.
    health = page.evaluate(
        """async () => {
            const res = await fetch('/health/data', {
                headers: {'X-PDT-Request': '1'},
            });
            let body = null;
            try { body = await res.json(); } catch (e) { body = null; }
            return {http_status: res.status, body: body};
        }"""
    )
    report["health_http_status"] = (health or {}).get("http_status")
    instance = ((health or {}).get("body") or {}).get("instance") or {}
    report["project_dir"] = instance.get("project_dir")
    report["plans_dir"] = instance.get("plans_dir")
    report["boot_id"] = instance.get("boot_id")

    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    screenshot = artifacts / SCREENSHOT_NAME
    page.screenshot(path=str(screenshot), full_page=True)
    report["screenshot"] = str(screenshot)
    report["screenshot_exists"] = screenshot.exists()
    report["screenshot_size"] = (
        screenshot.stat().st_size if screenshot.exists() else 0
    )

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


#: The numeric-id driver: the walk above **plus** a probe that calls the
#: page's own ``renderTasks`` with a numeric-id payload — no pre-coercion,
#: no helper in the test process — and reads what the page made of it.
#:
#: It is a separate driver because ``_DRIVER`` is shared: the acceptance
#: command imports it verbatim and reads the plan's *own* task list out of
#: it.  A probe that overwrites ``#tasks-list`` would replace that
#: evidence with the fixture's, so the probe lives here instead, after the
#: screenshot has already captured the real thing.
_NUMERIC_ID_DRIVER = r'''
"""Open the served page, click the plan card, and probe the page's renderer.

argv: <base_url> <plan_id> <artifacts_dir>

Two observations, in this order:

1. the **user's** path — click the plan card, read the task list the
   page built out of the plan's own ``tasks.json`` (whose ids are
   numbers); and
2. the **renderer's** path — call the page's own ``renderTasks`` with a
   numeric-id payload, exactly as the page would receive it from
   ``/api/tasks``, with nothing coerced on the way in.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

#: The screenshot's file name.
SCREENSHOT_NAME = "plan-view.png"

#: The phrases that must not be anywhere on the page.
BLOCKED_PHRASES = ("环境阻断", "循环停止")


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache, so the
    binaries that *are* installed are found here instead: nothing is
    downloaded and nothing is hard-coded.  The cache lives under the home
    the run was launched with — the driver is started with ``$HOME`` pointed
    back at it, because the pytest tree redirects ``$HOME`` to a scratch
    directory.
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


base_url, plan_id, artifacts_dir = sys.argv[1:4]

executable = browser_executable()
if not executable:
    raise SystemExit(
        "no chromium binary under the playwright cache -- this test refuses "
        "to skip; install one with `playwright install chromium`"
    )

card = '.plan-card[data-plan="%s"]' % plan_id
report = {"page_errors": [], "console_errors": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=executable)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

    def _on_console(message):
        if message.type == "error":
            report["console_errors"].append(message.text)

    page.on("console", _on_console)

    page.goto(base_url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)

    # The listing is served from a two-second cache, so a page that loads
    # right after the plan appeared shows a list without it.  Reloading is
    # what a user does (the sidebar polls), and it is the only way the
    # cache gets out of the way.
    found = False
    for _ in range(30):
        try:
            page.wait_for_selector(card, timeout=3000)
            found = True
            break
        except Exception:
            page.reload(wait_until="load")
            page.wait_for_timeout(1000)
    if not found:
        raise SystemExit("the plan card %s never appeared on the page" % card)

    # The user clicks the card: the page routes to the plan view and reads
    # the plan's own tasks through its own API calls.
    page.click(card)
    page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
    page.wait_for_timeout(1200)

    report["tasks_view_visible"] = page.is_visible("#tasks-view")
    report["tasks_list_visible"] = page.is_visible("#tasks-list")
    report["tasks_list_text"] = page.inner_text("#tasks-list")
    # The **cards**, not the text: a plan without tasks renders an empty
    # state whose text is 「暂无任务」 — non-empty, and a false pass for a
    # "the task list has text" assertion.  The count is the truth.
    report["task_card_count"] = page.evaluate(
        "() => document.querySelectorAll('#tasks-list .task-card').length"
    )

    body_text = page.evaluate("() => document.body.innerText")
    report["body_text"] = body_text
    report["blocked_text"] = any(
        phrase in body_text for phrase in BLOCKED_PHRASES
    )

    # The page reads its own backend -- the very road the UI travels.  The
    # guard header is the one the frontend itself sends.
    health = page.evaluate(
        """async () => {
            const res = await fetch('/health/data', {
                headers: {'X-PDT-Request': '1'},
            });
            let body = null;
            try { body = await res.json(); } catch (e) { body = null; }
            return {http_status: res.status, body: body};
        }"""
    )
    report["health_http_status"] = (health or {}).get("http_status")
    instance = ((health or {}).get("body") or {}).get("instance") or {}
    report["project_dir"] = instance.get("project_dir")
    report["plans_dir"] = instance.get("plans_dir")
    report["boot_id"] = instance.get("boot_id")

    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    screenshot = artifacts / SCREENSHOT_NAME
    page.screenshot(path=str(screenshot), full_page=True)
    report["screenshot"] = str(screenshot)
    report["screenshot_exists"] = screenshot.exists()
    report["screenshot_size"] = (
        screenshot.stat().st_size if screenshot.exists() else 0
    )

    # --- the renderer's own path ----------------------------------------
    # The payload is handed to the page as the page would receive it from
    # ``/api/tasks``: a JSON **number** as the task id.  Nothing is
    # coerced on the way in -- the renderer has to cope with it.
    probe = page.evaluate(
        """() => {
            const payload = {tasks: [
                {id: 1, title: 't1', status: 'pending'},
                {id: 2, title: 't2', status: 'pending'},
            ]};
            let error = null;
            try {
                window.renderTasks(payload);
            } catch (e) {
                error = String(e);
            }
            return {
                error: error,
                card_count: document.querySelectorAll('#tasks-list .task-card').length,
                task_ids: Array.from(
                    document.querySelectorAll('#tasks-list .task-id')
                ).map((el) => (el.textContent || '').trim()),
            };
        }"""
    )
    report["render_tasks_error"] = (probe or {}).get("error")
    report["render_tasks_card_count"] = (probe or {}).get("card_count")
    report["render_tasks_task_ids"] = (probe or {}).get("task_ids")

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with (see
    ``PDT_TEST_REAL_HOME``) so that Playwright finds the browser binaries it
    installed there — the pytest tree redirects ``$HOME`` to a scratch
    directory.
    """
    env = dict(os.environ)
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        env["HOME"] = real_home
    return env


def _run_driver(
    script: Path,
    base_url: str,
    plan_id: str,
    artifacts_dir: Path,
) -> Dict[str, Any]:
    """Run the browser pass; return the report it printed.

    A driver that cannot start is a **failure**, not a skip: the exit code
    of a skipped test and the exit code of a passing test are the same
    number, and this file exists to produce evidence.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        raise AssertionError(
            "no interpreter with playwright was found -- this test refuses to "
            "skip; install one with `pip install playwright`"
        )
    result = subprocess.run(
        [interpreter, str(script), f"{base_url}/", plan_id, str(artifacts_dir)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise AssertionError(
            f"the browser driver printed nothing; stderr={result.stderr!r}"
        )
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def project_dir_ui(project_dir_backend, tmp_path_factory) -> Dict[str, Any]:
    """Drive the real page against the real backend; report what it shows.

    The backend is **task 5's own fixture**: the same real ``backend.server``
    process, the same imported plan, the same facts about both.  Nothing is
    booted twice and nothing is faked — the only thing added here is the
    browser.
    """
    work = tmp_path_factory.mktemp("instance-project-dir-ui-e2e")
    script = work / "drive_page.py"
    script.write_text(_DRIVER, encoding="utf-8")

    base_url = project_dir_backend["base_url"]
    plan_id = project_dir_backend["plan_id"]

    try:
        browser = _run_driver(script, base_url, plan_id, ARTIFACTS_DIR)
    except Exception as exc:  # noqa: BLE001 - the server log is the context
        log = project_dir_backend["work"] / "server.log"
        raise AssertionError(
            f"{exc}\n--- tail of {log} ---\n{_tail(log)}"
        ) from exc

    # The browser report is written next to the screenshot so a human can
    # read the raw observation without re-running the test.
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = ARTIFACTS_DIR / REPORT_NAME
    report_path.write_text(
        json.dumps(browser, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    return {
        "base_url": base_url,
        "work": project_dir_backend["work"],
        "checkout": project_dir_backend["checkout"],
        "plan_id": plan_id,
        "port": project_dir_backend["port"],
        "browser": browser,
        "report_path": str(report_path),
    }


# ---------------------------------------------------------------------------
# The walk with numeric task ids
# ---------------------------------------------------------------------------


def _seed_numeric_id_plan(work: Path) -> None:
    """Write the plan whose task ids are **numbers**, and file it.

    Two pieces:

    * ``<work>/plans/<plan_id>/tasks.json`` — the task list, with numeric
      ids.  This is the legal input the whole walk is about: a task id is
      not required to be a string.
    * a ``plan_routing`` row — the plan's phase, without which the listing
      has no answer to publish and the card never appears.

    The routing row goes into the state database **before** the server is
    booted, so the very first listing already carries the plan.
    """
    plan_dir = work / "plans" / NUMERIC_ID_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        json.dumps({"status": "complete", "chat_history": [], "dimensions": {}}),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {"plan_id": NUMERIC_ID_PLAN_ID, "tasks": list(NUMERIC_ID_TASKS)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(work / "state.db")
    try:
        migrate(conn)
        RoutingRepository(conn).insert(NUMERIC_ID_PLAN_ID, "queued")
    finally:
        conn.close()


@pytest.fixture(scope="session")
def numeric_id_page(tmp_path_factory) -> Dict[str, Any]:
    """Drive the real page against a plan whose task ids are numbers.

    Same page, same server, same browser — the only difference is the
    payload: the task ids are JSON **numbers**.  Everything the user sees
    has to be the same as for a plan whose ids happen to be strings.
    """
    work = tmp_path_factory.mktemp("instance-project-dir-numeric-ids")
    script_dir = work / "llm-script"
    script_dir.mkdir(parents=True, exist_ok=True)
    _write_scripted_llm(script_dir)
    _seed_numeric_id_plan(work)

    base_url, proc = _boot_server(work, script_dir)
    try:
        script = work / "drive_page.py"
        script.write_text(_NUMERIC_ID_DRIVER, encoding="utf-8")
        try:
            browser = _run_driver(
                script, base_url, NUMERIC_ID_PLAN_ID, NUMERIC_ID_ARTIFACTS
            )
        except Exception as exc:  # noqa: BLE001 - the server log is context
            log = work / "server.log"
            raise AssertionError(
                f"{exc}\n--- tail of {log} ---\n{_tail(log)}"
            ) from exc
    finally:
        _stop_server(proc)

    # The browser report is written next to the screenshot so a human can
    # read the raw observation without re-running the test.
    NUMERIC_ID_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    report_path = NUMERIC_ID_ARTIFACTS / REPORT_NAME
    report_path.write_text(
        json.dumps(browser, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    return {
        "base_url": base_url,
        "work": work,
        "plan_id": NUMERIC_ID_PLAN_ID,
        "browser": browser,
        "report_path": str(report_path),
    }


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_the_plan_page_loads_and_shows_the_plan_after_a_user_clicks_its_card(
    project_dir_ui,
):
    """The page opens, the card opens the plan, and the page names the instance.

    Three facts, in the order a user meets them:

    1. **the page opens** and the plan card is on it;
    2. **the user clicks the card** and the plan's task view is there —
       the page is not broken by the identity change;
    3. **the page itself** reads ``/health/data`` and learns that the
       instance answers for **the repository root** (``project_dir``), which
       is a *different place* from the plans tree it is serving
       (``plans_dir``).

    The third one is the point of the whole exercise: the workspace is the
    identity, the tree is not.  If the two ever collapsed into one, the
    semantics this plan exists for would be untested.
    """
    obs = project_dir_ui
    browser = obs["browser"]

    # --- 1. the page opened -------------------------------------------
    assert browser.get("page_errors") == [], (
        f"the page raised uncaught JavaScript exceptions: "
        f"{browser.get('page_errors')!r}"
    )

    # --- 2. the click opened the plan ----------------------------------
    assert browser.get("tasks_view_visible") is True, (
        "the task view did not open after the user clicked the plan card; "
        f"report={browser!r}"
    )
    assert browser.get("tasks_list_visible") is True, (
        f"the task list is not visible in the plan view; report={browser!r}"
    )
    tasks_list_text = str(browser.get("tasks_list_text") or "").strip()
    assert tasks_list_text, (
        "the task list is empty -- the page renders the plan but no task; "
        f"report={browser!r}"
    )
    # 「暂无任务」 is what the page shows when the plan has **no** task.  It
    # is text, so the assertion above would pass on it -- the task cards are
    # what say the plan's tasks are really there.
    assert EMPTY_TASK_TEXT not in tasks_list_text, (
        f"the task list shows the empty state {EMPTY_TASK_TEXT!r} instead of "
        f"the plan's tasks; report={browser!r}"
    )
    assert int(browser.get("task_card_count") or 0) >= 1, (
        "the task list renders no task card at all; report="
        f"{browser!r}"
    )

    # --- 3. the page carries no blocked verdict -------------------------
    assert browser.get("blocked_text") is False, (
        f"the page shows a blocked verdict ({BLOCKED_PHRASES!r}); "
        f"body={browser.get('body_text')!r}"
    )
    body_text = str(browser.get("body_text") or "")
    for phrase in BLOCKED_PHRASES:
        assert phrase not in body_text, (
            f"the page must not contain {phrase!r}; body={body_text!r}"
        )

    # --- 4. the page's own read of /health/data -------------------------
    assert browser.get("health_http_status") == 200, (
        "the page's own fetch of /health/data answered "
        f"{browser.get('health_http_status')!r}; report={browser!r}"
    )
    project_dir = str(browser.get("project_dir") or "")
    assert project_dir, (
        f"the backend the page talks to publishes no project_dir: "
        f"report={browser!r}"
    )
    assert Path(project_dir).resolve() == Path(EXPECTED_PROJECT_DIR).resolve(), (
        f"the instance the page sees must answer for the repository root "
        f"{EXPECTED_PROJECT_DIR!r}; got {project_dir!r}"
    )
    plans_dir = str(browser.get("plans_dir") or "")
    assert plans_dir, (
        f"the instance the page sees publishes no plans_dir: report={browser!r}"
    )
    assert Path(project_dir).resolve() != Path(plans_dir).resolve(), (
        "the workspace and the plans tree must be two places, otherwise "
        f"'认人按 workspace' 这条语义没被测到: project_dir={project_dir!r} "
        f"plans_dir={plans_dir!r}"
    )

    # --- 5. the screenshot is on disk -----------------------------------
    screenshot = Path(str(browser.get("screenshot") or ""))
    assert screenshot == ARTIFACTS_DIR / SCREENSHOT_NAME, (
        f"the screenshot must land at {ARTIFACTS_DIR / SCREENSHOT_NAME}; got "
        f"{screenshot}"
    )
    assert browser.get("screenshot_exists") is True, (
        f"the screenshot was not written to {screenshot}"
    )
    assert screenshot.exists() and screenshot.stat().st_size > 0, (
        f"the screenshot at {screenshot} is missing or empty"
    )

    # --- 6. the browser report is on disk (for a human to read) ----------
    report_path = Path(obs["report_path"])
    assert report_path == ARTIFACTS_DIR / REPORT_NAME, (
        f"the browser report must land at {ARTIFACTS_DIR / REPORT_NAME}; got "
        f"{report_path}"
    )
    assert report_path.exists() and report_path.stat().st_size > 0, (
        f"the browser report at {report_path} is missing or empty"
    )


def test_render_task_card_accepts_numeric_ids(numeric_id_page):
    """The page's own ``renderTasks`` must take a numeric task id.

    The payload is handed to the page the way the page receives it from
    ``/api/tasks``: ``{"id": 1, ...}`` — a JSON **number**.  Nothing is
    coerced on the way in; the renderer has to cope with it.

    Before the fix the call raised ``TypeError: s.replace is not a
    function`` inside the legacy ``esc()`` helper, and the page caught it
    and painted 「任务尚未生成」 over a plan that *had* tasks — silently,
    with a clean console.  After the fix the call returns, and the page
    shows the two cards with the ids as text.
    """
    browser = numeric_id_page["browser"]

    assert browser.get("render_tasks_error") is None, (
        "the page's own renderTasks() threw on a numeric task id: "
        f"{browser.get('render_tasks_error')!r}. The renderer must coerce "
        "the id to a string before escaping it."
    )
    assert int(browser.get("render_tasks_card_count") or 0) == len(
        NUMERIC_ID_TASKS
    ), (
        f"the page rendered {browser.get('render_tasks_card_count')!r} task "
        f"cards for {len(NUMERIC_ID_TASKS)} numeric-id tasks; report={browser!r}"
    )
    assert browser.get("render_tasks_task_ids") == ["1", "2"], (
        "the rendered task ids must be the ids' text — ['1', '2'] — not the "
        f"raw numbers; got {browser.get('render_tasks_task_ids')!r}. "
        f"report={browser!r}"
    )
    assert browser.get("page_errors") == [], (
        "the page raised uncaught JavaScript exceptions: "
        f"{browser.get('page_errors')!r}"
    )


def test_a_plan_with_numeric_task_ids_shows_its_task_cards(numeric_id_page):
    """A numeric task id must render a task card, not an empty list.

    The payload here is legal: ``tasks.json`` says ``{"id": 1, ...}``, and
    a number is a number.  The page used to hand that number straight to
    the legacy ``esc()`` helper, which called ``.replace`` on it — a
    ``TypeError`` the page caught, so the user saw 「任务尚未生成」 over a
    plan that *had* tasks, with a clean console and no error anywhere.

    The user-visible claim this test pins is therefore narrow and exact:
    **the plan's tasks are on the page**.  Two cards (the two tasks), the
    empty state nowhere, and no uncaught exception while it happens.
    """
    browser = numeric_id_page["browser"]

    assert browser.get("page_errors") == [], (
        "the page raised uncaught JavaScript exceptions: "
        f"{browser.get('page_errors')!r}"
    )
    assert browser.get("tasks_view_visible") is True, (
        "the task view did not open after the user clicked the plan card; "
        f"report={browser!r}"
    )
    assert int(browser.get("task_card_count") or 0) == len(NUMERIC_ID_TASKS), (
        f"the task list must show {len(NUMERIC_ID_TASKS)} task cards for a "
        f"plan whose task ids are numbers; it showed "
        f"{browser.get('task_card_count')!r}. report={browser!r}"
    )
    tasks_list_text = str(browser.get("tasks_list_text") or "")
    assert EMPTY_TASK_TEXT not in tasks_list_text, (
        f"the task list shows the empty state {EMPTY_TASK_TEXT!r} instead of "
        f"the plan's tasks; report={browser!r}"
    )
    for task in NUMERIC_ID_TASKS:
        assert str(task["title"]) in tasks_list_text, (
            f"the task {task['title']!r} is missing from the rendered task "
            f"list; report={browser!r}"
        )
