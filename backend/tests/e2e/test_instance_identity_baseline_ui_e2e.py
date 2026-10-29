"""端到端验收：实例身份基线不再误判时，页面上这一轮是「已通过」。

背景
----
前置已经证明了两件事：

* **机制层**（``test_instance_identity_baseline_e2e.py``）—— 真后端进程下，
  记录了实例身份的一轮，预检**不**把被测服务判成「别人」；
* **单元层**（``tests/unit/test_service_instance_identity_baseline.py``）——
  三项比较一律按**记录值**比，没有记录就不拦（R1/R2）。

剩下的最后一环是**用户看得见的那一环**：这一轮跑完了，页面必须显示「已通过」，
而且**整页**不得出现「环境阻断」/「循环停止」——那正是 2026-10-26 生产上
「每一轮都被判成环境阻断」的用户可见形状。

本文件证明的因果链
------------------
页面显示「已通过」这件事本身，只说明前端会渲染一个 ``passed`` 的状态；它
**说明不了**这一轮为什么没有被打断。所以顺序是反过来的：

1. 先用**真**后端（真 ``backend.server`` 子进程、真 ``/health/data``、真
   ``verification_agent``）跑一次**真实预检**：记录一次实例身份，然后问
   「 answering 的还是我记录的那个实例吗」。
2. 断言这个预检**返回 None**（不拦），并先断言记录**确实发生**
   （``boot_id`` 非空）——否则 R2 的「无记录不拦」会让这条因果前提空过：
   ``None`` 只会说明「我不知道它是谁」，页面判通过就说明不了任何事。
3. 只有在这个前提成立之后，才把这轮的终态写成 ``passed``，再让**真浏览器**
   打开**真页面**去读它。

唯一的替身是模型，而那是仓库自己留的缝：``PDT_LLM_MODE=scripted`` 把每一次
模型调用换成磁盘上的固定回答（``backend/coding_tool.py`` 的
``ScriptedLLMRegistry``）。不换的话，``POST /api/plan/import`` 会真的去调模型
—— 不可复现，还烧钱。

不许 skip
----------
浏览器起不来、后端起不来、卡片不出现 —— 都是**失败**（并把 ``server.log`` 的
尾巴打出来），不是 skip：一条 skip 的退出码是 0，和「验证通过」没法区分。
这个文件存在的意义就是产出证据。

产物
----
* 截图 ``/tmp/instance-identity-baseline/round-passed.png``
* 浏览器报告 ``/tmp/instance-identity-baseline/round-passed-report.json``
  （供人工复核）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Tuple

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The siblings are reused **as they are**: the real ``backend.server``
# subprocess, the scripted LLM, the git checkout, the one HTTP client the
# whole E2E lane speaks through, and Task 4's round agent (which is the
# one that records the instance block before the preflight asks about it).
from tests.e2e.test_instance_identity_baseline_e2e import (  # noqa: E402
    _boot_or_fail,
    _import_plan,
    _round_agent,
    _server_port,
    _stop_server,
    _tail,
)
from tests.e2e.test_plan_lifecycle_ui_e2e import (  # noqa: E402
    _http,
    _make_checkout,
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
#: artifact has to be at a path the reviewer can open by hand after the run,
#: and ``tempfile.gettempdir()`` would answer ``$TMPDIR`` (a per-user scratch
#: directory on macOS) instead.  Nothing is written there that is not an
#: artifact this test just produced.
ARTIFACTS_DIR = Path("/tmp/instance-identity-baseline")  # nosec B108

#: The screenshot's file name inside :data:`ARTIFACTS_DIR`.
SCREENSHOT_NAME = "round-passed.png"

#: The browser report's file name inside :data:`ARTIFACTS_DIR` — the raw
#: observation a human reads when the assertion below needs a second pair of
#: eyes.
REPORT_NAME = "round-passed-report.json"

#: The verdict written into ``plan_verification``.  ``overall_status`` is
#: ``PASSED`` because this round **was** judged — that is the whole point of
#: the exercise.  ``passed_count`` is 11 (the number of verification points a
#: real round has), ``failed_count`` and ``skipped_count`` are 0, and there
#: are no ``repair_tasks`` (a passing round must not派 repair tasks).
PASSED_RESULTS: Dict[str, Any] = {
    "overall_status": "PASSED",
    "passed_count": 11,
    "failed_count": 0,
    "skipped_count": 0,
    "repair_tasks": [],
}

#: The badge text the frontend shows for ``passed``
#: (``frontend/app.js`` 的 ``setVerificationBadge``).
BADGE_TEXT = "已通过"

#: The overall-result text for a passing round
#: (``frontend/app.js`` 的 ``renderVerificationResult``).
OVERALL_TEXT = "✅ 验证通过"

#: The badge text of the **blocked** verdict — the symptom this plan exists
#: to remove.  It must not be anywhere on the page.
BLOCKED_BADGE_TEXT = "循环停止"

#: The phrase the production incident was reported under.  It is not a
#: string the frontend renders, so this assertion is a guard against a new
#: renderer putting it there.
BLOCKED_PHRASE = "环境阻断"


# ---------------------------------------------------------------------------
# The verdict that goes into the state database
# ---------------------------------------------------------------------------


def _inject_passed(state_db: Path, plan_id: str) -> str:
    """Write a **passed** round's verdict into the state DB the server reads.

    The row is written the way the orchestrator writes it: through
    :class:`VerificationRepository` when the plan has no row yet, through a
    plain ``UPDATE`` when it has one.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(state_db)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        if repo.current(plan_id) is None:
            repo.insert(
                plan_id,
                "passed",
                round=1,
                max_rounds=3,
                results=dict(PASSED_RESULTS),
                verification_stop_reason=None,
            )
            return "inserted"
        conn.execute(
            "UPDATE plan_verification SET verification_status='passed', "
            "results=? WHERE plan_id=?",
            (json.dumps(PASSED_RESULTS, ensure_ascii=False), plan_id),
        )
        return "updated"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

#: The driver, run as its own process.  It is written to disk and executed
#: with an interpreter that can import ``playwright``, so that a browser
#: that cannot start is a **failed subprocess** (a loud failure the test
#: turns into an error) rather than a skip.  The driver prints one JSON
#: object on stdout.
_DRIVER = r'''
"""Open the served page, click the plan card, report what the page says.

argv: <base_url> <plan_id> <artifacts_dir>

Everything it reports is read off the **rendered** page: the badge's text,
whether the result area is visible, the overall result's text, and the
page's own ``document.body.innerText``.  Nothing is reconstructed from the
API — the point of this file is what a *user* sees.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

#: The screenshot's file name.
SCREENSHOT_NAME = "round-passed.png"


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
    # the verification status on its own.
    page.click(card)
    page.wait_for_selector("#verification-status-badge", state="visible", timeout=30000)

    # The overall result is written **after** the result area is shown (the
    # panel unhides the area first, then renders the verdict into it), so
    # "the verdict has text" is what tells us the panel finished rendering.
    page.wait_for_function(
        """() => { const el = document.querySelector('#verification-overall-result');
                  return !!el && el.textContent.trim().length > 0; }""",
        timeout=30000,
    )
    page.wait_for_timeout(300)

    report["badge"] = page.inner_text("#verification-status-badge").strip()
    report["badge_class"] = page.get_attribute("#verification-status-badge", "class")
    report["result_area_visible"] = page.is_visible("#verification-result-area")
    report["overall"] = page.inner_text("#verification-overall-result").strip()
    report["body_text"] = page.evaluate("() => document.body.innerText")

    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    screenshot = artifacts / SCREENSHOT_NAME
    page.screenshot(path=str(screenshot), full_page=True)
    report["screenshot"] = str(screenshot)
    report["screenshot_exists"] = screenshot.exists()

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
    work: Path,
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
    script = work / "drive_page.py"
    script.write_text(_DRIVER, encoding="utf-8")
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
def passed_round_ui(tmp_path_factory) -> Dict[str, Any]:
    """Walk the story once: real backend → real preflight → verdict → page."""
    work = tmp_path_factory.mktemp("instance-identity-ui-e2e")
    script_dir = work / "llm-script"
    script_dir.mkdir(parents=True, exist_ok=True)
    _write_scripted_llm(script_dir)
    checkout = _make_checkout(work)

    base_url, proc = _boot_or_fail(work, script_dir)
    try:
        # --- the plan the round is about -------------------------------
        plan_id = _import_plan(base_url, checkout)
        port = _server_port(base_url)

        # --- the causal premise: a real preflight against a real backend -
        # ``_round_agent`` records the instance block first and asserts it
        # is non-empty; without that record the preflight's ``None`` would
        # only say "no baseline, no verdict" (R2) and the page's verdict
        # would prove nothing.
        agent = _round_agent(work, checkout, plan_id, port)
        recorded = agent._service_instance_identity.get("api") or {}
        recorded_boot_id = str(recorded.get("boot_id") or "")
        assert recorded_boot_id, (
            "这一轮没有真的记录下实例身份，后面的 None 只反映「无记录不拦」："
            f"recorded={recorded!r}"
        )

        mismatch = agent._service_instance_mismatch(
            "api", SimpleNamespace(port=port),
        )
        assert mismatch is None, (
            "真后端进程服务的是它自己的计划树，不得被判成「别人」："
            f"mismatch={mismatch!r}"
        )

        # --- the round's verdict ----------------------------------------
        injected = _inject_passed(work / "state.db", plan_id)

        # --- the real page ----------------------------------------------
        browser = _run_driver(work, base_url, plan_id, ARTIFACTS_DIR)

        # The browser report is written next to the screenshot so a human
        # can read the raw observation without re-running the test.
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = ARTIFACTS_DIR / REPORT_NAME
        report_path.write_text(
            json.dumps(browser, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        # --- what the API says ------------------------------------------
        status, verification = _http(
            "GET", f"{base_url}/api/verification/{plan_id}/status"
        )
        assert status == 200, (
            f"/verification/status answered {status}: {verification!r}"
        )

        return {
            "work": work,
            "checkout": checkout,
            "base_url": base_url,
            "port": port,
            "plan_id": plan_id,
            "recorded_boot_id": recorded_boot_id,
            "mismatch": mismatch,
            "injected": injected,
            "browser": browser,
            "verification": verification,
            "report_path": str(report_path),
        }
    except Exception as exc:  # noqa: BLE001 - the server log is the context
        log = work / "server.log"
        raise AssertionError(
            f"{exc}\n--- tail of {log} ---\n{_tail(log)}"
        ) from exc
    finally:
        _stop_server(proc)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_the_page_shows_the_round_as_passed(passed_round_ui):
    """A round that was not blocked shows 「已通过」 on the page.

    The whole chain in one test: the real preflight that did **not**
    block, the verdict the state DB carries, and the page a user actually
    looks at.
    """
    obs = passed_round_ui
    browser = obs["browser"]

    # --- the identity was really recorded ------------------------------
    assert obs["recorded_boot_id"], (
        "这一轮没有记录下实例身份，页面判通过就说明不了任何事："
        f"recorded={obs['recorded_boot_id']!r}"
    )

    # --- and the preflight really did not block ------------------------
    assert obs["mismatch"] is None, (
        "真后端实例块下的预检不得判成「别人」："
        f"mismatch={obs['mismatch']!r}"
    )

    # --- the badge ------------------------------------------------------
    assert browser["badge"] == BADGE_TEXT, (
        f"the verification badge must read {BADGE_TEXT!r}; got "
        f"{browser['badge']!r}"
    )

    # --- the result area is on screen -----------------------------------
    assert browser["result_area_visible"] is True, (
        "the verification result area must be visible for a round that "
        f"passed; report={browser!r}"
    )

    # --- the overall result ---------------------------------------------
    assert browser["overall"] == OVERALL_TEXT, (
        f"the overall result must read {OVERALL_TEXT!r}; got "
        f"{browser['overall']!r}"
    )

    # --- the screenshot is on disk --------------------------------------
    screenshot = Path(browser["screenshot"])
    assert screenshot == ARTIFACTS_DIR / SCREENSHOT_NAME, (
        f"the screenshot must land at {ARTIFACTS_DIR / SCREENSHOT_NAME}; got "
        f"{screenshot}"
    )
    assert browser["screenshot_exists"] is True, (
        f"the screenshot was not written to {screenshot}"
    )
    assert screenshot.exists() and screenshot.stat().st_size > 0, (
        f"the screenshot at {screenshot} is missing or empty"
    )

    # --- the browser report is on disk (for a human to read) -------------
    report_path = Path(obs["report_path"])
    assert report_path == ARTIFACTS_DIR / REPORT_NAME, (
        f"the browser report must land at {ARTIFACTS_DIR / REPORT_NAME}; got "
        f"{report_path}"
    )
    assert report_path.exists() and report_path.stat().st_size > 0, (
        f"the browser report at {report_path} is missing or empty"
    )

    # --- the API says the same thing ------------------------------------
    assert obs["verification"]["verification_status"] == "passed", (
        f"the verification status must be 'passed'; got "
        f"{obs['verification']!r}"
    )

    # --- the page had no uncaught error ----------------------------------
    assert browser["page_errors"] == [], (
        f"the page raised uncaught errors: {browser['page_errors']!r}"
    )


def test_the_page_never_mentions_environment_blocked(passed_round_ui):
    """``document.body.innerText`` must not contain 「环境阻断」/「循环停止」.

    The first test proves the page shows the right thing *in the panel*;
    this one proves the page does not show the wrong thing *anywhere* — a
    stale panel, a hidden element that still carries the text, or a second
    renderer would all pass the first test and fail this one.
    """
    body = passed_round_ui["browser"]["body_text"]

    assert BLOCKED_PHRASE not in body, (
        f"the page must not contain {BLOCKED_PHRASE!r} for a round that was "
        f"not blocked; body={body!r}"
    )
    assert BLOCKED_BADGE_TEXT not in body, (
        f"the page must not contain {BLOCKED_BADGE_TEXT!r} for a round that "
        f"was not blocked; body={body!r}"
    )
    assert BADGE_TEXT in body, (
        f"the page must show {BADGE_TEXT!r} somewhere; body={body!r}"
    )