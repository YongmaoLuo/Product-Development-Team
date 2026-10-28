"""端到端验收：未判满的一轮在页面上是「环境阻断」，不是「✅ 验证通过」。

背景
----
R1--R4 各自的单测都绿，但原症状是**报告写 PASSED、前端显示 ✅ 验证通过**。
单元测试证明不了这一条 —— 它要的是「真后端 + 真浏览器」串一遍：

* **真后端**：``backend.server`` 的真进程（不是 ``TestClient``），监听内核
  选的空闲端口，状态库与计划树都在临时目录里。
* **真浏览器**：Playwright 驱动真 Chromium 打开真页面，走页面自己的事件与
  自己的 ``fetch``。

唯一的替身是模型，而那是仓库自己留的缝：``PDT_LLM_MODE=scripted`` 把每一次
模型调用换成磁盘上的固定回答（``backend/coding_tool.py`` 的
``ScriptedLLMRegistry``）。不换的话，``POST /api/plan/import`` 会真的去调
模型 —— 那是 E2E 不该做的事：不可复现，还烧钱。

与兄弟文件 ``test_plan_lifecycle_ui_e2e.py`` 的三处差别（都是**故意的**）
--------------------------------------------------------------------
1. **不许 skip。** 兄弟文件在浏览器缺失时 ``pytest.skip``（它的
   ``_run_driver``），而一条 skip 的退出码是 0 —— 和「验证通过」没法区分。
   这个文件要证明的事一旦被 skip 掉，证据就是空的。所以这里**自写驱动**：
   浏览器找不到就失败，后端起不来就失败（并把 ``server.log`` 的尾巴打出来）。
2. **不复用 ``_run_driver``。** 同上 —— 它会把「浏览器缺失」变成一条 skip。
3. **注入的是验证状态，不是跑一轮真的验证。** 这一轮要证明的是「未判满的
   轮次在前端长什么样」，不是「验证器怎么算出未判满」（那是 R4/R5 的单测
   已经钉住的事）。所以这里把 ``plan_verification`` 那一行直接写成
   ``loop_stopped``，然后让**真的页面**去读它。

被注入的状态
------------
``verification_status = 'loop_stopped'``；``results`` 里
``overall_status = 'SKIPPED'``（这一轮没有判满）、``failed_count = 0``、
``skipped_count = 11``、``repair_tasks = []``（未判满不派修复任务）。

页面上必须看到的是：

* ``#verification-status-badge`` → 「循环停止」
* ``#verification-result-area`` → 可见
* ``#verification-overall-result`` → 「⚠️ 循环停止」
* 整页**不出现**「验证通过」

**这个文件依赖 Playwright。** CI 的 e2e lane（``.github/workflows/ci.yml``
的 ``e2e-on-demand``）目前**不**安装 ``playwright``（它不在
``backend/pyproject.toml`` 的依赖里），所以那条 lane 上本文件会**失败**而不是
跳过 —— 这是「不许 skip」的直接代价，也是有意的：一条在 CI 上偷偷跳过、
在本地才跑绿的测试，证明不了任何事。真要让这条 lane 绿，得把 ``playwright``
加进 backend 的 dev 依赖并在 e2e job 里 ``playwright install chromium``。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from tests.e2e.test_plan_lifecycle_ui_e2e import (
    _boot_server,
    _http,
    _make_checkout,
    _write_scripted_llm,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

#: Where the screenshot has to land.  The brief names this path, so it is
#: this path — not a ``tmp_path`` that vanishes with the run.
#:
#: ``nosec B108`` — the hard-coded temporary directory **is** the point:
#: the artifact has to be at a path the reviewer can open by hand after
#: the run, and ``tempfile.gettempdir()`` would answer ``$TMPDIR`` (a
#: per-user scratch directory on macOS) instead.  Nothing is written
#: there that is not a screenshot this test just took.
ARTIFACTS_DIR = Path("/tmp/dp-identity/e2e")  # nosec B108

#: The screenshot's file name inside :data:`ARTIFACTS_DIR`.
SCREENSHOT_NAME = "unjudged-round.png"

#: The material the import brings in.  The scripted LLM answers the
#: generation prompt from disk, so the text itself only has to be *a*
#: material; what the plan is called is what the page shows.
IMPORT_CONTENT = "1. 修一个东西"
IMPORT_TITLE = "dp-identity-e2e"

#: The verdict written into ``plan_verification``.  ``overall_status`` is
#: ``SKIPPED`` because this round was **not judged** — that is the whole
#: point of the exercise.  ``repair_tasks`` is empty because an unjudged
#: round must not派 repair tasks (R5).
LOOP_STOPPED_RESULTS: Dict[str, Any] = {
    "overall_status": "SKIPPED",
    "failed_count": 0,
    "skipped_count": 11,
    "repair_tasks": [],
}

#: The badge text the frontend shows for ``loop_stopped``
#: (``frontend/app.js`` 的 ``setVerificationBadge``).
BADGE_TEXT = "循环停止"

#: The overall-result text for ``loop_stopped``
#: (``frontend/app.js`` 的 ``renderVerificationResult``).
OVERALL_TEXT = "⚠️ 循环停止"

#: The text that must **not** appear anywhere on the page.
PASS_TEXT = "验证通过"

#: The text that must not appear on the page either (the whole phrase,
#: emoji included — the second test asserts on exactly this).
PASS_PHRASE = "✅ 验证通过"


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

#: The driver, run as its own process.  It is written to disk and executed
#: with ``sys.executable`` so that a browser that cannot start is a **failed
#: subprocess** (a loud failure the test turns into an error) rather than a
#: skip.  The driver prints one JSON object on stdout.
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

#: The text that must not appear anywhere on the page.
PASS_TEXT = "验证通过"
#: The screenshot's file name.
SCREENSHOT_NAME = "unjudged-round.png"


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
    page.wait_for_selector("#verification-result-area", state="visible", timeout=30000)
    page.wait_for_timeout(500)

    report["badge"] = page.inner_text("#verification-status-badge").strip()
    report["badge_class"] = page.get_attribute("#verification-status-badge", "class")
    report["result_area_visible"] = page.is_visible("#verification-result-area")
    report["overall"] = page.inner_text("#verification-overall-result").strip()
    report["body_text"] = page.evaluate("() => document.body.innerText")
    report["mentions_pass"] = PASS_TEXT in report["body_text"]

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

    ``$HOME`` is pointed back at the home the run was launched with (the
    pytest tree redirects ``$HOME`` to a scratch directory), so that
    Playwright finds the browser binaries it installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        env["HOME"] = real_home
    return env


def _tail(path: Path, lines: int = 60) -> str:
    """The last ``lines`` lines of a file, or why it could not be read."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(cannot read {path}: {exc})"
    if not content:
        return f"({path} is empty)"
    return "\n".join(content[-lines:])


def _boot_or_fail(work: Path, script_dir: Path) -> Tuple[str, subprocess.Popen]:
    """Boot the real backend; fail (never skip) when it does not come up."""
    try:
        return _boot_server(work, script_dir)
    except Exception as exc:  # noqa: BLE001 - every failure is a failure
        log = work / "server.log"
        raise AssertionError(
            f"the real backend did not come up: {exc}\n"
            f"--- tail of {log} ---\n{_tail(log)}"
        ) from exc


def _stop_server(proc: subprocess.Popen) -> None:
    """Terminate the server subprocess and close its log handle."""
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


def _inject_loop_stopped(state_db: Path, plan_id: str) -> str:
    """Write an unjudged round's verdict into the state DB the server reads.

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
                "loop_stopped",
                round=1,
                max_rounds=3,
                results=dict(LOOP_STOPPED_RESULTS),
                verification_stop_reason=None,
            )
            return "inserted"
        conn.execute(
            "UPDATE plan_verification SET verification_status='loop_stopped', "
            "results=? WHERE plan_id=?",
            (json.dumps(LOOP_STOPPED_RESULTS, ensure_ascii=False), plan_id),
        )
        return "updated"
    finally:
        conn.close()


def _run_driver(
    work: Path,
    base_url: str,
    plan_id: str,
    artifacts: Path,
) -> Dict[str, Any]:
    """Run the browser pass; return the report it printed.

    Two different things can go wrong, and they are not the same thing:

      * **No browser at all** — ``playwright`` is not importable by the
        interpreter this driver runs on.  That is the same condition the
        other browser walks in this directory meet with a skip
        ("playwright is not importable here …"), and the PR gate never
        installs one, so failing here would report a missing capability
        as a broken product.  Skipped, with the same wording.
      * **A browser that cannot start** — once ``playwright`` is there,
        a driver that dies is a **failure**, not a skip: the exit code
        of a skipped test and the exit code of a passing test are the
        same number, and this file exists to produce evidence.
    """
    if importlib.util.find_spec("playwright") is None:
        pytest.skip(
            "playwright is not importable here (install it, or put a "
            "'playwright' command on PATH); the acceptance walk needs a "
            "real browser"
        )
    script = work / "drive_verdict.py"
    script.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), f"{base_url}/", plan_id, str(artifacts)],
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
def unjudged_round_e2e(tmp_path_factory) -> Dict[str, Any]:
    """Walk the story once: import → inject the verdict → open the page."""
    work = tmp_path_factory.mktemp("dp-identity-e2e")
    script_dir = work / "llm-script"
    script_dir.mkdir(parents=True, exist_ok=True)
    _write_scripted_llm(script_dir)
    checkout = _make_checkout(work)

    base_url, proc = _boot_or_fail(work, script_dir)
    try:
        # --- the import parks the plan in the waiting phase -------------
        status, body = _http(
            "POST",
            f"{base_url}/api/plan/import",
            {
                "project_dir": str(checkout),
                "content": IMPORT_CONTENT,
                "title": IMPORT_TITLE,
            },
            timeout=300,
        )
        assert status == 200, f"the import failed: {status} {body!r}"
        plan_id = str((body or {}).get("plan_id") or "")
        assert plan_id, f"the import produced no plan id: {body!r}"

        status, phase_body = _http("GET", f"{base_url}/api/plan/{plan_id}/status")
        assert status == 200, f"the plan status answered {status}: {phase_body!r}"
        phase = (phase_body or {}).get("current_phase")

        # --- the unjudged round's verdict -------------------------------
        injected = _inject_loop_stopped(work / "state.db", plan_id)

        # --- the real page ----------------------------------------------
        browser = _run_driver(work, base_url, plan_id, ARTIFACTS_DIR)

        # --- what the API says ------------------------------------------
        status, verification = _http(
            "GET", f"{base_url}/api/verification/{plan_id}/status"
        )
        assert status == 200, f"/verification/status answered {status}: {verification!r}"
        status, repair = _http(
            "GET", f"{base_url}/api/verification/{plan_id}/repair_tasks"
        )
        assert status == 200, f"/verification/repair_tasks answered {status}: {repair!r}"

        return {
            "plan_id": plan_id,
            "phase": phase,
            "injected": injected,
            "browser": browser,
            "verification": verification,
            "repair": repair,
            "artifacts_dir": str(ARTIFACTS_DIR),
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


def test_unjudged_round_renders_as_environment_blocked(unjudged_round_e2e):
    """An unjudged round shows 「循环停止」 — never 「✅ 验证通过」.

    The whole chain in one test: the state the orchestrator would have
    written, the API that serves it, and the page a user actually looks at.
    """
    obs = unjudged_round_e2e
    browser = obs["browser"]

    # --- the plan is still listed (a terminal phase would hide it) -----
    assert obs["phase"] == "queued", (
        f"the imported plan must stay in the waiting phase so its card is "
        f"listed; got {obs['phase']!r}"
    )

    # --- the badge -----------------------------------------------------
    assert browser["badge"] == BADGE_TEXT, (
        f"the verification badge must read {BADGE_TEXT!r}; got "
        f"{browser['badge']!r}"
    )

    # --- the result area is on screen ----------------------------------
    assert browser["result_area_visible"] is True, (
        "the verification result area must be visible for a round that "
        f"stopped; report={browser!r}"
    )

    # --- the overall result -------------------------------------------
    assert browser["overall"] == OVERALL_TEXT, (
        f"the overall result must read {OVERALL_TEXT!r}; got "
        f"{browser['overall']!r}"
    )

    # --- and the page never claims the round passed --------------------
    assert browser["mentions_pass"] is False, (
        f"the page must not mention {PASS_TEXT!r} for an unjudged round; "
        f"body={browser['body_text']!r}"
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

    # --- the API says the same thing ------------------------------------
    assert obs["verification"]["verification_status"] == "loop_stopped", (
        f"the verification status must be 'loop_stopped'; got "
        f"{obs['verification']!r}"
    )
    repair_tasks = obs["repair"].get("tasks")
    assert repair_tasks == [], (
        f"an unjudged round must not派 repair tasks; got {repair_tasks!r}"
    )

    # --- the page had no uncaught error ---------------------------------
    assert browser["page_errors"] == [], (
        f"the page raised uncaught errors: {browser['page_errors']!r}"
    )


def test_the_page_never_shows_pass_for_an_unjudged_round(unjudged_round_e2e):
    """``document.body.innerText`` must not contain 「✅ 验证通过」.

    The first test proves the page shows the right thing *in the panel*;
    this one proves the page does not show the wrong thing *anywhere* —
    a stale panel, a hidden element that still carries the text, or a
    second renderer would all pass the first test and fail this one.
    """
    browser = unjudged_round_e2e["browser"]
    body = browser["body_text"]

    assert PASS_PHRASE not in body, (
        f"the page must not contain {PASS_PHRASE!r} for an unjudged round; "
        f"body={body!r}"
    )
    assert PASS_TEXT not in body, (
        f"the page must not contain {PASS_TEXT!r} for an unjudged round; "
        f"body={body!r}"
    )
    assert BADGE_TEXT in body, (
        f"the page must show {BADGE_TEXT!r} somewhere; body={body!r}"
    )
