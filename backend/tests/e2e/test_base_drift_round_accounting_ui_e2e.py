"""端到端：被 base drift 挡住的轮次不吃轮号 —— 页面仍然显示 Round 1/4。

背景
----
任务 1--4 只证明了"行"与"判据"：``mark_round_blocked`` 把阻塞原因写进
``plan_verification``（任务 1），``_plan_next_round`` 认出被挡住的轮次并
**复用同一个轮号**（任务 2），交接前把轮次行写下来并记审计事件（任务 3）。
用户看到的却不是这些行 —— 用户看的是计划页上的 Verification 卡片：

    frontend/app.js 的 ``renderVerificationPanel(data)`` 把
    ``GET /api/verification/{id}/status`` 里的 ``verification_round`` /
    ``verification_max_rounds`` 写进 ``#verification-round``，文本形如
    ``Round 1/4``。

所以这一份是**端到端验收**：真的起一个 ``backend.server``，真的用真的
Playwright 浏览器点开计划页，读页面上那个用户真正会读的字符串。

两个计划，同一份状态库
----------------------
* **A（阻塞）**：``plan_routing(current_phase="verification_running")`` +
  ``plan_verification(round=1, max_rounds=4, verification_status="running")``；
  测试内先 ``mark_round_blocked(reason="base_drift_conflicts")``，再走**生产
  函数** ``_plan_next_round(current())`` 算下一个轮号，最后
  ``init_round(round_n=next)``。A 的轮号不是本文件写死的 —— 它是生产函数
  算出来的，所以"撤掉任务 2 的修复"会让 A 变成 Round 2/4，本文件转红。
* **B（对照）**：形状完全相同，只是没有阻塞原因；同样走
  ``_plan_next_round(current())`` → ``init_round(round_n=next)``。B 的轮号
  因此是 2，页面显示 ``Round 2/4``。

A 与 B 的差别只有"有没有被 base drift 挡住"这一条，其余完全同构 —— 这
才是对照的意义：两个页面之间的差异只能来自轮次的算法。

负控
----
撤掉任务 2 的修复（``_plan_next_round`` 不再复用被挡住轮次的轮号）之后：

* ``_plan_next_round(blocked_row)[0]`` 从 1 变成 2；
* 于是 A 的 ``plan_verification.round`` 变成 2；
* 于是页面显示 ``Round 2/4`` 而不是 ``Round 1/4``；
* ``test_blocked_round_stays_round_one`` 与
  ``test_blocked_round_comes_from_production_math`` 同时转红。

这正是本文件要钉住的东西：**轮次不能被 base drift 吃掉**。

浏览器不可用怎么办
------------------
``_playwright_interpreter()`` 返回 ``None``（这台机器上没有 Playwright，
或者装了驱动但没装浏览器二进制）时，本文件**跳过**，绝不静默通过 ——
一份没有真浏览器的端到端验收不是验收。

两个计划分开看
--------------
每个计划都在**一次全新的页面加载**上看：``frontend/app.js`` 的
``fetchVerificationStatus()`` 不检查回来的响应是不是当前计划的响应，谁后
到谁渲染。所以先看的那个计划如果还有响应在飞，它会在后看的计划的卡片上
落一次 —— 卡片会闪出上一个计划的轮号。重新加载一次页面，两个计划各自的
观察就互不干扰了（这是被测页面的既有性质，不在本文件要修的范围里）。

复用说明
--------
``backend/tests/e2e/test_execution_status_restart_e2e.py`` 的
``_free_port`` / ``_boot`` / ``_wait_ready`` / ``_teardown`` /
``_http_json``，以及 ``backend/tests/e2e/test_fastfail_no_ghost_state_ui.py``
的 ``_playwright_interpreter`` / ``_driver_env``，都按**文件路径**
importlib 加载进来用（不改动那两个文件的任何内容）：跨模块 import 要靠
pytest 的 rootdir gymnastics，太脆；按路径加载不需要 ``__init__.py``，也
不依赖收集顺序。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]

# ``server`` has to be imported FIRST: it pulls in ``verification_loop``
# itself, and ``server`` reads names out of ``verification_loop`` at its
# own import time — so importing ``verification_loop`` first is a
# circular import.  Same ordering the sibling round-resolution tests
# use.
import server as _server_module  # noqa: F401
from verification_loop import _plan_next_round


# ---------------------------------------------------------------------------
# Paths + the sibling modules this file borrows its harness from
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
E2E_DIR = Path(__file__).resolve().parent

# ``backend/`` has to be importable: the production module this file leans
# on (``verification_loop``) imports its siblings by their flat names.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_sibling(module_name: str, filename: str):
    """Load a sibling e2e module by FILE PATH.

    ``importlib.util.spec_from_file_location`` reads the file and executes
    it under a private module name.  Nothing in the sibling is modified,
    and nothing here depends on the sibling being importable as a package
    (this directory's import story is pytest's, not ours).
    """
    path = E2E_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load the sibling module at {path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RESTART_E2E = _load_sibling(
    "_pdt_e2e_execution_status_restart", "test_execution_status_restart_e2e.py"
)
_GHOST_UI_E2E = _load_sibling(
    "_pdt_e2e_fastfail_no_ghost_state_ui", "test_fastfail_no_ghost_state_ui.py"
)

# The harness: boot a real server on a free port, wait for it, talk to it,
# stop it.
_free_port = _RESTART_E2E._free_port
_boot = _RESTART_E2E._boot
_wait_ready = _RESTART_E2E._wait_ready
_teardown = _RESTART_E2E._teardown
_http_json = _RESTART_E2E._http_json
_server_output = _RESTART_E2E._server_output

# The browser harness: find a Playwright interpreter, and run the driver
# under the home the run was launched with (Playwright resolves the
# browser binaries it launches under the home it is handed).
_playwright_interpreter = _GHOST_UI_E2E._playwright_interpreter
_driver_env = _GHOST_UI_E2E._driver_env


# ---------------------------------------------------------------------------
# The scenario's constants
# ---------------------------------------------------------------------------

#: The plan whose round was blocked by the base-drift check.
BLOCKED_PLAN_ID = "e2e-blocked-round"

#: The control plan: the same shape, without the block.
NORMAL_PLAN_ID = "e2e-normal-round"

#: The reason ``base_drift`` writes when the merge left conflicts.
BLOCKED_REASON = "base_drift_conflicts"

#: The plan's immutable round budget.
MAX_ROUNDS = 4

#: What the two pages must say.  ``Round 1/4`` for the blocked plan (the
#: blocked round is re-run under the SAME number) and ``Round 2/4`` for the
#: control (a round that really ran advances).
EXPECTED_BLOCKED_ROUND_TEXT = "Round 1/4"
EXPECTED_NORMAL_ROUND_TEXT = "Round 2/4"

#: The badge's "nothing has happened yet" label.  A page that still says
#: this has not rendered the round at all.
IDLE_BADGE_TEXT = "未开始"


# ---------------------------------------------------------------------------
# The seed: two plans, one state database, one plans tree
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass
class SeededPlan:
    """What the seed put into the database, and what the planner decided."""

    plan_id: str
    #: The row as it looked when the planner saw it (round 1; for the
    #: blocked plan it carries the block reason).
    row: Dict[str, Any]
    #: The round the PRODUCTION planner picked for the next round.
    next_round: int
    #: The cap the planner carried over.
    max_rounds: int


def _seed_plan(work: Path, plan_id: str, *, blocked: bool) -> SeededPlan:
    """Seed one plan and let the production planner choose its round.

    The shape is the one the plan page reads:

    * a ``plan_routing`` row in ``verification_running`` — the phase the
      Verification card is opened from;
    * a ``plan_verification`` row: round 1, cap 4, status ``running``.

    For the blocked plan the block is recorded the way production records
    it (``mark_round_blocked`` — the reason only, the status untouched),
    and then the round number is decided by **``_plan_next_round``**, the
    production function.  The number is therefore not this test's
    opinion: it is what the framework would do.  ``init_round`` then
    writes that number, exactly as the repair hand-off does.

    The plan directory is created too: the page reads
    ``GET /api/plan/{id}/state`` and ``GET /api/verification/{id}/status``,
    and both refuse a plan whose directory does not exist.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_dir = work / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )

    conn = open_db(work / "state.db")
    try:
        migrate(conn)
        routing = RoutingRepository(conn)
        verification = VerificationRepository(conn)

        routing.insert(plan_id, "verification_running")
        verification.insert(
            plan_id,
            verification_status="running",
            round=1,
            max_rounds=MAX_ROUNDS,
            verification_stop_reason=None,
            results=None,
            verdicts=[],
            started_at=_utcnow_iso(),
        )
        if blocked:
            # The block is recorded the way production records it: the
            # reason only.  The round is BLOCKED, not finished, so the
            # status stays ``running`` and the round keeps its number.
            verification.mark_round_blocked(plan_id, reason=BLOCKED_REASON)

        # The row the planner sees — the blocked plan's row carries the
        # reason; the control's does not.
        row = verification.current(plan_id)
        next_round, max_rounds, _exhausted = _plan_next_round(row)
        verification.init_round(
            plan_id, round_n=next_round, max_rounds=max_rounds
        )
    finally:
        conn.close()

    return SeededPlan(
        plan_id=plan_id,
        row=dict(row or {}),
        next_round=int(next_round),
        max_rounds=int(max_rounds),
    )


# ---------------------------------------------------------------------------
# The browser driver: click a plan in the sidebar, read the round
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page the way an operator does.

For each plan: load the page, click the plan's entry in the sidebar, wait
for the Verification card to render, read the round it shows, and archive
a screenshot.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead —
    nothing is downloaded, and nothing is hard-coded: the cache lives
    under the home the run was launched with.
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


# The page's own shell carries the placeholder "Round 0/3", and the card
# of the PREVIOUS plan stays on screen until the next plan's status has
# been fetched.  So "the round is rendered" is not a question about the
# shape of the text — it is a question about whether THIS plan's number
# is the one on screen.  The wait below asks exactly that; when it does
# not come true the text is read anyway, and the caller reports what the
# page actually showed.
#
# The predicate hands back the text it matched, so the recorded value is
# the one the card showed AT THE INSTANT it matched — not a later
# reading of a card that may have been re-rendered since.
_ROUND_IS_EXPECTED = """
(expected) => {
    const el = document.querySelector('#verification-round');
    if (!el) return false;
    const text = (el.textContent || '').trim();
    return text === expected ? text : false;
}
"""

base_url, blocked_id, normal_id = sys.argv[1:4]
expected_blocked, expected_normal = sys.argv[4:6]
blocked_shot, normal_shot = sys.argv[6:8]

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

page_errors = []

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    # An uncaught exception in the page.  A rejected promise nobody
    # handles is NOT one of these, so the page is asked to record those
    # itself (see the init script below).
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.add_init_script("""
        window.__pdtUnhandled = [];
        window.addEventListener('unhandledrejection', function (event) {
            const reason = event.reason;
            const text = (reason && (reason.stack || reason.message)) || String(reason);
            window.__pdtUnhandled.push(text);
        });
    """)
    results = {}
    unhandled = []
    for key, plan_id, expected, shot_path in (
        ("blocked", blocked_id, expected_blocked, blocked_shot),
        ("normal", normal_id, expected_normal, normal_shot),
    ):
        # Every plan is inspected on a FRESH page load.  The served page
        # re-renders whichever verification response arrives last and
        # does not check whose response it is, so a response that was
        # still in flight for the PREVIOUSLY viewed plan can land after
        # this plan's own response and put the other plan's round on the
        # card.  A reload per plan keeps the two observations
        # independent of each other.
        page.goto(base_url + "/", wait_until="load")
        page.wait_for_selector("#app", timeout=30000)

        selector = '.sidebar-plan-item[data-plan="' + plan_id + '"]'
        page.wait_for_selector(selector, state="visible", timeout=30000)
        page.click(selector)
        page.wait_for_selector("#verification-round", state="visible", timeout=30000)

        wait_error = ""
        matched_text = ""
        try:
            handle = page.wait_for_function(
                _ROUND_IS_EXPECTED, arg=expected, timeout=20000
            )
            matched_text = str(handle.json_value() or "").strip()
        except Exception as exc:
            # The page never showed this plan's number.  The text is read
            # anyway, so the caller can report what the page ACTUALLY
            # shows — that text is what the assertion is made against.
            wait_error = str(exc)

        # Let the card settle and read it once more: what the operator
        # ends up looking at must be this plan's round.
        page.wait_for_timeout(1500)
        round_text = (page.text_content("#verification-round") or "").strip()
        badge_text = (page.text_content("#verification-status-badge") or "").strip()

        Path(shot_path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=shot_path, full_page=True)

        # The init script re-runs on every navigation, so the rejections
        # of THIS page load are collected before the next load resets
        # the array.
        unhandled.extend(page.evaluate("() => window.__pdtUnhandled || []"))

        results[key] = {
            "plan_id": plan_id,
            "round_text": round_text,
            "matched_text": matched_text,
            "badge_text": badge_text,
            "screenshot": shot_path,
            "wait_error": wait_error,
        }

    browser.close()

print(json.dumps({
    "results": results,
    "page_errors": page_errors,
    "unhandled_rejections": unhandled,
}, ensure_ascii=False))
'''


def _drive_browser(interpreter: str, base_url: str, work: Path) -> Dict[str, Any]:
    """Run the driver and return its JSON payload.

    The two screenshots land inside the plans tree, one per plan:
    ``<plans>/<plan>/screenshots/<name>.png``.
    """
    driver_script = work / "drive_page.py"
    driver_script.write_text(_BROWSER_DRIVER, encoding="utf-8")

    blocked_shot = (
        work / "plans" / BLOCKED_PLAN_ID / "screenshots" / "base-drift-round.png"
    )
    normal_shot = (
        work / "plans" / NORMAL_PLAN_ID / "screenshots" / "normal-round.png"
    )

    result = subprocess.run(
        [
            interpreter,
            str(driver_script),
            base_url,
            BLOCKED_PLAN_ID,
            NORMAL_PLAN_ID,
            EXPECTED_BLOCKED_ROUND_TEXT,
            EXPECTED_NORMAL_ROUND_TEXT,
            str(blocked_shot),
            str(normal_shot),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or (
            "playwright install" in combined
        ):
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not; the round accounting cannot be accepted "
                "end-to-end without a browser"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    payload: Optional[Dict[str, Any]] = None
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    assert payload is not None, (
        f"the browser driver printed no JSON payload; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return payload


def _verification_status(base_url: str, plan_id: str) -> Dict[str, Any]:
    """``GET /api/verification/{id}/status`` — what the card renders from."""
    status, body = _http_json(
        "GET", f"{base_url}/api/verification/{plan_id}/status"
    )
    assert status == 200, (
        f"GET /api/verification/{plan_id}/status answered HTTP {status}: "
        f"{body!r}"
    )
    assert isinstance(body, dict), (
        f"the verification status answer is not an object: {body!r}"
    )
    return body


# ---------------------------------------------------------------------------
# The fixture: one server, one browser run, both plans
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """Everything the assertions need, gathered by one server + one browser."""

    work: Path
    base_url: str
    log_path: Optional[Path]
    blocked: SeededPlan
    normal: SeededPlan
    api_status: Dict[str, Dict[str, Any]]
    payload: Dict[str, Any]

    def result(self, key: str) -> Dict[str, Any]:
        """The browser's reading of one of the two plans."""
        return self.payload["results"][key]

    def server_log_tail(self, limit: int = 2000) -> str:
        """The tail of the backend's own output.

        A failing assertion about the page has to carry the server's side
        of the story: what the backend answered, and what it logged.
        """
        if self.log_path is None:
            return "(no server output was captured)"
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(could not read {self.log_path}: {exc})"
        return text[-limit:]


@pytest.fixture(scope="module")
def scenario(tmp_path_factory) -> Scenario:
    """Boot a real server, seed two plans, drive a real browser at them."""
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "round accounting cannot be accepted end-to-end without a "
            "browser"
        )

    work = tmp_path_factory.mktemp("base-drift-round-ui")
    (work / "plans").mkdir(parents=True, exist_ok=True)

    blocked = _seed_plan(work, BLOCKED_PLAN_ID, blocked=True)
    normal = _seed_plan(work, NORMAL_PLAN_ID, blocked=False)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = _boot(work, port)
    try:
        try:
            _wait_ready(base_url, proc)
        except AssertionError:
            # A backend that never came up has to say why: its own output
            # is printed before the failure propagates.
            print(
                "the backend never became ready; its output:\n"
                + _server_output(proc),
                file=sys.stderr,
            )
            raise
        payload = _drive_browser(interpreter, base_url, work)
        api_status = {
            "blocked": _verification_status(base_url, BLOCKED_PLAN_ID),
            "normal": _verification_status(base_url, NORMAL_PLAN_ID),
        }
        yield Scenario(
            work=work,
            base_url=base_url,
            log_path=getattr(proc, "_pdt_log_path", None),
            blocked=blocked,
            normal=normal,
            api_status=api_status,
            payload=payload,
        )
    finally:
        _teardown(proc)


# ---------------------------------------------------------------------------
# 1. The round number comes from the production planner
# ---------------------------------------------------------------------------


def test_blocked_round_comes_from_production_math(scenario: Scenario):
    """The blocked round keeps its number, and the number is production's.

    This is the negative control's anchor: the round the page shows is
    computed by ``_plan_next_round`` — the same function the framework
    calls when the repair hand-off decides which round runs next.  Take
    the base-drift branch out of that function and this assertion (and
    the page's "Round 1/4") both go away.
    """
    row = scenario.blocked.row
    assert row.get("verification_stop_reason") == BLOCKED_REASON, (
        f"the seeded row must carry the block reason, or the planner is "
        f"not being asked the question this test is about; "
        f"verification_stop_reason={row.get('verification_stop_reason')!r}"
    )
    assert int(row.get("round") or 0) == 1, (
        f"the seeded row must be the first round; round={row.get('round')!r}"
    )

    next_round, max_rounds, exhausted = _plan_next_round(row)

    assert next_round == 1, (
        f"a round blocked by base drift must be re-run under the SAME "
        f"number; _plan_next_round said {next_round!r} for a row carrying "
        f"stop_reason={BLOCKED_REASON!r}"
    )
    assert max_rounds == MAX_ROUNDS, (
        f"the cap must not move; max_rounds={max_rounds!r}"
    )
    assert exhausted is False, (
        f"a blocked round has not run, so it cannot exhaust the budget; "
        f"exhausted={exhausted!r}"
    )
    assert scenario.blocked.next_round == next_round, (
        f"the seeded row must carry the number the planner produced; "
        f"seeded={scenario.blocked.next_round!r} planned={next_round!r}"
    )


# ---------------------------------------------------------------------------
# 2. The blocked plan's page says Round 1/4
# ---------------------------------------------------------------------------


def test_blocked_round_stays_round_one(scenario: Scenario):
    """The blocked plan's card still says ``Round 1/4``.

    The user clicked the plan in the sidebar and reads the number off the
    Verification card.  A blocked round never dispatched a verification
    point, so it must not have charged the plan a round: the card says
    the round the plan is still ON, not the round it has finished.
    """
    reading = scenario.result("blocked")
    api = scenario.api_status["blocked"]

    assert reading["matched_text"] == EXPECTED_BLOCKED_ROUND_TEXT, (
        f"the blocked plan's card must have rendered "
        f"{EXPECTED_BLOCKED_ROUND_TEXT!r}; the wait ended with "
        f"matched_text={reading['matched_text']!r} "
        f"(wait_error={reading['wait_error']!r})\n"
        f"the API answered verification_round="
        f"{api.get('verification_round')!r} / "
        f"verification_max_rounds={api.get('verification_max_rounds')!r} / "
        f"stop_reason={api.get('stop_reason')!r}\n"
        f"server log tail:\n{scenario.server_log_tail()}"
    )
    assert reading["round_text"] == EXPECTED_BLOCKED_ROUND_TEXT, (
        f"the blocked plan's card must read "
        f"{EXPECTED_BLOCKED_ROUND_TEXT!r}; the page actually showed "
        f"textContent={reading['round_text']!r} "
        f"(wait_error={reading['wait_error']!r})\n"
        f"the API answered verification_round="
        f"{api.get('verification_round')!r} / "
        f"verification_max_rounds={api.get('verification_max_rounds')!r} / "
        f"stop_reason={api.get('stop_reason')!r}\n"
        f"server log tail:\n{scenario.server_log_tail()}"
    )
    assert api.get("verification_round") == 1, (
        f"the API must report round 1 for the blocked plan; "
        f"verification_round={api.get('verification_round')!r}"
    )


# ---------------------------------------------------------------------------
# 3. The control plan's page says Round 2/4
# ---------------------------------------------------------------------------


def test_normal_round_shows_round_two(scenario: Scenario):
    """The control plan's card says ``Round 2/4``.

    Same shape, no block: the first round really ran, so the next one is
    round 2.  The difference between the two pages is therefore the
    blocked round's accounting and nothing else.
    """
    reading = scenario.result("normal")
    api = scenario.api_status["normal"]

    assert reading["matched_text"] == EXPECTED_NORMAL_ROUND_TEXT, (
        f"the control plan's card must have rendered "
        f"{EXPECTED_NORMAL_ROUND_TEXT!r}; the wait ended with "
        f"matched_text={reading['matched_text']!r} "
        f"(wait_error={reading['wait_error']!r})\n"
        f"the API answered verification_round="
        f"{api.get('verification_round')!r} / "
        f"verification_max_rounds={api.get('verification_max_rounds')!r} / "
        f"stop_reason={api.get('stop_reason')!r}\n"
        f"server log tail:\n{scenario.server_log_tail()}"
    )
    assert reading["round_text"] == EXPECTED_NORMAL_ROUND_TEXT, (
        f"the control plan's card must read "
        f"{EXPECTED_NORMAL_ROUND_TEXT!r}; the page actually showed "
        f"textContent={reading['round_text']!r} "
        f"(wait_error={reading['wait_error']!r})\n"
        f"the API answered verification_round="
        f"{api.get('verification_round')!r} / "
        f"verification_max_rounds={api.get('verification_max_rounds')!r} / "
        f"stop_reason={api.get('stop_reason')!r}\n"
        f"server log tail:\n{scenario.server_log_tail()}"
    )
    assert api.get("verification_round") == 2, (
        f"the API must report round 2 for the control plan; "
        f"verification_round={api.get('verification_round')!r}"
    )


# ---------------------------------------------------------------------------
# 4. The page raised nothing
# ---------------------------------------------------------------------------


def test_no_page_errors(scenario: Scenario):
    """Neither page raised an uncaught exception or an unhandled rejection.

    A card that renders the wrong number because the fetch threw is a
    card that renders nothing at all — and the console is where the
    operator would never look.  Both kinds of failure are counted here.
    """
    page_errors = list(scenario.payload.get("page_errors") or [])
    unhandled = list(scenario.payload.get("unhandled_rejections") or [])

    assert page_errors == [], (
        f"the page raised {len(page_errors)} uncaught exception(s): "
        f"{page_errors!r}\nserver log tail:\n{scenario.server_log_tail()}"
    )
    assert unhandled == [], (
        f"the page left {len(unhandled)} promise rejection(s) unhandled: "
        f"{unhandled!r}\nserver log tail:\n{scenario.server_log_tail()}"
    )


# ---------------------------------------------------------------------------
# 5. The card is not the idle card
# ---------------------------------------------------------------------------


def test_status_badge_is_not_blank(scenario: Scenario):
    """The status badge says something, and it is not 「未开始」.

    A blank (or idle) badge next to a round number means the card was
    never really rendered — the round text would be the shell's
    placeholder, not an answer.
    """
    for key in ("blocked", "normal"):
        badge = (scenario.result(key).get("badge_text") or "").strip()
        assert badge, (
            f"the {key} plan's status badge is empty; the card did not "
            f"render\nserver log tail:\n{scenario.server_log_tail()}"
        )
        assert badge != IDLE_BADGE_TEXT, (
            f"the {key} plan's status badge still reads {IDLE_BADGE_TEXT!r}; "
            f"the Verification card was never rendered\n"
            f"server log tail:\n{scenario.server_log_tail()}"
        )


# ---------------------------------------------------------------------------
# 6. The screenshots are on disk
# ---------------------------------------------------------------------------


def test_screenshot_archived(scenario: Scenario):
    """Both pages were archived as PNGs inside the plans tree."""
    expected = {
        "blocked": "base-drift-round.png",
        "normal": "normal-round.png",
    }
    for key, filename in expected.items():
        path = Path(scenario.result(key)["screenshot"])
        assert path.exists(), (
            f"the {key} plan's screenshot was not written to {path}"
        )
        assert path.stat().st_size > 0, (
            f"the {key} plan's screenshot at {path} is empty"
        )
        assert path.name == filename, (
            f"the {key} plan's screenshot must be archived as {filename}; "
            f"it is at {path}"
        )
        assert path.parent == (
            scenario.work / "plans" / scenario.result(key)["plan_id"]
            / "screenshots"
        ), (
            f"the {key} plan's screenshot must live in the plan's own "
            f"screenshots directory; it is at {path}"
        )
