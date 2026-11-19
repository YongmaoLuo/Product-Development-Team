"""端到端验收：页面上的验证状态来自权威行，不是镜像列。

背景
----
2026-11-04 之前，``GET /api/plan/{id}/summary`` 的 ``state.verification``
读的是 ``plan_routing.verification`` —— 一列**镜像**，验证循环路过时顺手
写下的副本。权威记录是 ``plan_verification`` 表，也就是
``GET /api/verification/{id}/status`` 读的那一张。两个答案在真实计划上已经
漂移过：同一个计划，两个端点各说各话。

``backend/tests/api/test_summary_verification_authority.py`` 已经在**进程内**
（``TestClient``）证明了 summary 读的是权威行。那一份到不了浏览器：它没有
起过真的后端进程，也没有开过真的浏览器，所以"页面上给人看的那句话"仍然
没有任何断言覆盖 —— 而页面正是人唯一读的那个答案。

这份补上那一段：

* 真的 ``backend.server`` 子进程（自己的 plans 树、自己的 state.db）；
* 真的 Chromium（Playwright 驱动）；
* 真的点击（用户点开计划卡，验证面板自己打开）；
* 种子里的**冲突**是刻意造的：权威行写着 ``passed / round 2 / max 3``，
  镜像列里写着 ``failed / round 9 / max 3``。两者不可能同时对，所以
  "页面上显示的是哪一个" 是可以判定的 —— 而不是"两个值恰好一样"。

被钉住的东西
------------
* ``/api/plan/{id}/summary`` 的 ``state.verification`` == 权威行 —— **整个
  块**逐字相等（四个生命周期键，包括 ``stop_reason``）；
* ``/api/verification/{id}/status`` 的 ``verification_status`` /
  ``verification_round`` / ``verification_max_rounds`` /
  ``stop_reason`` == 权威行；
* 两者**互相一致**（同一个问题，两个答案）；
* 页面上 ``#verification-status-badge`` == 「已通过」，
  ``#verification-round`` == 「Round 2/3」；
* 镜像里的 ``failed`` / ``Round 9/3`` 一个字都不能出现在页面上；
* 点开的计划卡是**这个**计划的卡（``data-plan``）；
* 后端起在产品自己的解释器上（``VENV_PYTHON``），就绪以 ``/api/plans``
  回答为准，超时的失败消息带着服务端自己的 stdout/stderr；
* 跑完没有孤儿进程：服务器死了，它的进程组也空了；
* 截图落在 pytest 的临时目录里，**不在**仓库工作区里。

复用
----
``tests/e2e/test_dashboard_plan_and_verification_e2e.py`` 的浏览器发现、
报告解析、进程内/子进程两种驱动方式，以及
``tests/e2e/test_execution_status_restart_e2e.py`` 的真后端进程
（``_free_port`` / ``VENV_PYTHON`` / ``_boot`` / ``_wait_ready`` /
``_teardown`` / ``_server_output`` / ``_http_json``）。
**只读引用，不改动那两个文件。**

边界条件
--------
* 没装 Chromium → 整份文件跳过（``chromium is not installed``）；
* 当前解释器 import 不到 ``playwright.sync_api`` → 用能找到的解释器
  以子进程跑同一段驱动；
* 后端起不来 → 失败，并把服务端自己的输出带进断言消息；
* 失败路径也必须把后端停掉（``_stop_backend``）。

跑法::

    cd backend && .venv/bin/python3 -m pytest \\
        tests/e2e/test_summary_verification_authority_ui_e2e.py -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

#: 这份文件是端到端验收：它起服务、开浏览器、走完用户的那条路。
#: **不带** ``time_sensitive`` —— 那条 marker 会被 addopts 的
#: ``-m "not time_sensitive"`` 取消选择，测试会被静默地不收集。
pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
#: 仓库根 —— 截图必须落在它外面。
REPO_ROOT = PROJECT_ROOT

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: 这个 plan 的 id。计划目录、``plan_routing`` 行、``plan_verification``
#: 行都以它为键。
PLAN_ID = "e2e-verification-authority"

#: 权威行（``plan_verification``）里的那一轮 —— 写进表里的值。
SEED_AUTHORITATIVE_STATUS = "passed"
SEED_AUTHORITATIVE_ROUND = 2
SEED_AUTHORITATIVE_MAX_ROUNDS = 3

#: 镜像列（``plan_routing.verification``）里的那一轮 —— **刻意冲突**。
#: 读侧一旦读走它，下面每一个断言都会红。
SEED_MIRROR_STATUS = "failed"
SEED_MIRROR_ROUND = 9
SEED_MIRROR_MAX_ROUNDS = 3

#: 读侧**必须报出来**的值。这三个常量独立写死，不从前两组推导 ——
#: 推导出来的话，「种子里写什么」与「断言什么」就成了同一件事，改一处
#: 就跟着改另一处，那份冲突也就没了。这份文件的价值全在那份冲突上：
#: 它让「读的是哪一个」成为一个可以判定的问题，而不只是「两个值恰好
#: 一样，所以看不出来」。
EXPECTED_VERIFICATION_STATUS = "passed"
EXPECTED_VERIFICATION_ROUND = 2
EXPECTED_VERIFICATION_MAX_ROUNDS = 3

#: summary 的 ``state.verification`` 块，**整个块**的期望值。四个生命周期
#: 键（``status`` / ``round`` / ``max_rounds`` / ``stop_reason``），四个
#: 权威值，此外什么也没有：多一个键（镜像还赖着的键、或者没人要的键）
#: 在这里也是红的。
EXPECTED_SUMMARY_VERIFICATION_BLOCK = {
    "status": "passed",
    "round": 2,
    "max_rounds": 3,
    "stop_reason": None,
}

#: 页面上必须出现的两个词（前端自己的 label 映射，
#: ``frontend/app.js`` 的 ``setVerificationBadge`` 与 ``renderVerificationPanel``）。
EXPECTED_BADGE_TEXT = "已通过"
EXPECTED_ROUND_TEXT = "Round 2/3"

#: 镜像若被读走，页面上会出现的两个词（用来证明它们**没有**出现）。
MIRROR_BADGE_TEXT = "验证失败"
MIRROR_ROUND_TEXT = "Round 9/3"

#: 种子里的两条任务 —— 计划卡点开后要有东西可看。
SEEDED_TASKS = (
    {"id": "T1", "title": "实现导出按钮", "description": "d",
     "files_to_modify": [], "depends_on": []},
    {"id": "T2", "title": "补导出测试", "description": "d",
     "files_to_modify": [], "depends_on": []},
)

#: 整段浏览器过程的预算（秒）。浏览器在等各个页面元素时的预算在驱动
#: 源码里（``timeout_ms``）—— 没有固定 sleep：一个等不到的等待就是一次
#: 带原因的失败。
DRIVER_TIMEOUT_SECONDS = 240

#: 后端起来之后，``/api/plans`` 要在多久之内开始回答。超时算失败，而且
#: 失败消息里带着服务端自己的输出。
PLANS_READY_TIMEOUT_SECONDS = 90.0

#: 截图的文件名。它落在 pytest 的临时目录里 —— **不在**仓库工作区里。
SCREENSHOT_NAME = "summary-verification-authority.png"


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E files
# ---------------------------------------------------------------------------


def _load_sibling(module_name: str, filename: str):
    """Load a sibling E2E module **by file path**, without modifying it.

    ``backend/tests/e2e/`` 的包结构会变，跨模块 import 依赖 pytest 的
    rootdir gymnastics；按文件路径加载避开那一份脆弱性。这个加载器与
    ``test_dashboard_plan_and_verification_e2e.py`` 里的同名函数是同一份
    做法 —— 那一份住在被借用的模块里，而要借它就得先把它加载进来，所以
    这里必须自带一个。
    """
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the borrowed helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    # The module is registered **before** it is executed: ``dataclasses``
    # looks the defining module up in ``sys.modules`` by name, and a module
    # that was never registered makes every ``@dataclass`` in it raise.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


#: 浏览器助手 + 真后端助手 + 报告解析 —— 全部只读借用。
_DASHBOARD = _load_sibling(
    "pdt_dashboard_plan_and_verification_helpers",
    "test_dashboard_plan_and_verification_e2e.py",
)
_RESTART = _DASHBOARD._RESTART
_TOKEN = _DASHBOARD._TOKEN

_free_port = _RESTART._free_port
_http_json = _RESTART._http_json
_boot = _RESTART._boot
_wait_ready = _RESTART._wait_ready
_teardown = _RESTART._teardown
_server_output = _RESTART._server_output
#: 跑后端的那个解释器（``backend/.venv/bin/python3``，没有就退回当前
#: 解释器）。它是「后端是产品自己的解释器起的」这句话的凭据。
VENV_PYTHON = _RESTART.VENV_PYTHON

_upstream_browser_executable = _DASHBOARD._upstream_browser_executable
_current_interpreter_has_playwright = _DASHBOARD._current_interpreter_has_playwright
_driver_env = _DASHBOARD._driver_env
_parse_report = _DASHBOARD._parse_report
_playwright_interpreter = _TOKEN._playwright_interpreter


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page the way an operator does, and report what it shows.

argv: <base_url> <plan_id> <screenshot_path> <chromium_executable>

Every step is a click on the page, so the frontend's own handlers run:
``resumePlan`` -> ``showTasksView`` -> ``resumeVerificationPanel``.  Nothing
here reaches into the application's internals to make a panel show up.

The two API answers are read from **inside the page**, over the same origin
the page itself talks to and with the same guard header the frontend's
``api()`` wrapper sends.  A summary and a status endpoint answered from
outside the browser would not be the thing the page saw.

The report is one JSON object on stdout.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


base_url, plan_id, screenshot_path, chromium = sys.argv[1:5]

#: How long the browser waits for each of the things it needs.
timeout_ms = 30000

report = {"page_errors": [], "console_errors": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=chromium)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

    def _on_console(message):
        if message.type != "error":
            return
        text = message.text or ""
        # A resource that 404s is reported by the browser as a console
        # error; the dashboard asks for a handful of optional assets.  That
        # is not a page error.  Everything else is.
        if "Failed to load resource" in text:
            return
        report["console_errors"].append(text)

    page.on("console", _on_console)

    page.goto(base_url, wait_until="load")
    page.wait_for_selector("#app", timeout=timeout_ms)

    # --- 1: the plan card ---------------------------------------------
    # The dashboard renders one ``.plan-card`` per plan, keyed by the
    # plan id in ``data-plan`` (``frontend/app.js``).  The card that
    # carries OUR id is the one the user clicks -- a card that is
    # merely present would not prove the click landed on the right one.
    card = '.plan-card[data-plan="%s"]' % plan_id
    page.wait_for_selector(card, timeout=timeout_ms)
    report["plan_card_visible"] = page.is_visible(card)
    report["plan_card_plan_id"] = (
        page.get_attribute(card, "data-plan") or ""
    ).strip()

    # --- 2: the user clicks it -----------------------------------------
    page.click(card)

    # The task cards are the point of the click.
    page.wait_for_selector(".task-card", timeout=timeout_ms)

    # --- 3: the verification panel opens -------------------------------
    page.wait_for_selector(
        "#verification-panel", state="visible", timeout=timeout_ms
    )
    report["verification_panel_visible"] = page.is_visible(
        "#verification-panel"
    )

    # The panel is un-hidden one step BEFORE the badge is written, so the
    # panel being visible is not yet "the badge has been painted".  Wait
    # for the badge to leave its static initial value -- the assertions
    # below then still have teeth: it must be exactly the authoritative
    # verdict, not the mirror's.
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            if (!badge) return false;
            const text = (badge.textContent || '').trim();
            return text !== '' && text !== '未开始';
        }""",
        timeout=timeout_ms,
    )

    report["verification_badge"] = (
        page.inner_text("#verification-status-badge") or ""
    ).strip()
    report["verification_round"] = (
        page.inner_text("#verification-round") or ""
    ).strip()
    report["verification_panel_text"] = (
        page.inner_text("#verification-panel") or ""
    ).strip()

    # --- 4: the two API answers, read from inside the page ------------
    report["api"] = page.evaluate(
        """async (planId) => {
            const headers = {
                "X-PDT-Request": "1",
                "Content-Type": "application/json",
            };
            const read = async (url) => {
                const response = await fetch(url, {headers: headers});
                let body = null;
                try { body = await response.json(); } catch (err) { body = null; }
                return {http: response.status, body: body};
            };
            const summary = await read("/api/plan/" + planId + "/summary");
            const status = await read("/api/verification/" + planId + "/status");
            const summaryState = (summary.body || {}).state || {};
            const statusBody = status.body || {};
            return {
                summary_http: summary.http,
                status_http: status.http,
                summary_verification: summaryState.verification || null,
                verification_status: statusBody.verification_status,
                verification_round: statusBody.verification_round,
                verification_max_rounds: statusBody.verification_max_rounds,
                verification_stop_reason: statusBody.stop_reason,
            };
        }""",
        plan_id,
    )

    # --- 5: the screenshot ---------------------------------------------
    shot = Path(screenshot_path)
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot), full_page=True)
    report["screenshot"] = str(shot)
    report["screenshot_size"] = shot.stat().st_size if shot.exists() else 0

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# Running the driver
# ---------------------------------------------------------------------------


def _drive_in_process(argv: List[str]) -> Dict[str, Any]:
    """Run the driver inside this interpreter (it has playwright)."""
    buffer = io.StringIO()
    previous_argv = list(sys.argv)
    # The driver reads its arguments the way a script does -- ``argv[1:]``
    # -- so the program name has to be there too.
    sys.argv = ["<summary-verification-browser-driver>", *argv]
    try:
        with redirect_stdout(buffer):
            exec(  # noqa: S102  # nosec B102 - the driver, run verbatim
                compile(_BROWSER_DRIVER, "<summary-verification-browser-driver>", "exec"),
                {"__name__": "__main__"},
            )
    finally:
        sys.argv = previous_argv
    return _parse_report(buffer.getvalue())


def _drive_in_subprocess(
    interpreter: str, argv: List[str], work: Path
) -> Dict[str, Any]:
    """Run the driver as its own process, with an interpreter that has playwright."""
    script = work / "drive_summary_verification.py"
    script.write_text(_BROWSER_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [interpreter, str(script), *argv],
        capture_output=True,
        text=True,
        timeout=DRIVER_TIMEOUT_SECONDS,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return _parse_report(result.stdout)


def _drive(
    base_url: str, plan_id: str, screenshot_path: Path, work: Path
) -> Dict[str, Any]:
    """Drive the page and return what it showed.

    The plan id is an **argument**, not a module constant: the sibling
    acceptance file hard-codes its own plan id in the driver, which makes
    that driver unusable for a second plan.
    """
    chromium = _upstream_browser_executable()
    if not chromium:
        pytest.skip("chromium is not installed")

    argv = [base_url, plan_id, str(screenshot_path), chromium]

    if _current_interpreter_has_playwright():
        return _drive_in_process(argv)

    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no interpreter with playwright is installed in this environment"
        )
    return _drive_in_subprocess(interpreter, argv, work)


# ---------------------------------------------------------------------------
# The seed: an authoritative row and a mirror that contradicts it
# ---------------------------------------------------------------------------


def _seed_conflicting_plan(work: Path) -> None:
    """Write the plan the browser is going to open — with a deliberate conflict.

    Four pieces, each on the surface the server reads it from:

    * ``plans/<id>/tasks.json`` —— 任务列表（前端的任务卡就是它渲染的）；
    * ``plan_routing`` 行 —— 计划在哪一步。相位是 ``queued``，**不是**
      ``verification_running``：那个相位会把状态强行按成 ``running``，
      于是"读的是哪一行"就再也分不出来了；
    * ``plan_verification`` 行 —— **权威**的验证记录（``passed``，
      第 2 轮，上限 3 轮）；
    * ``plan_routing.verification`` 列 —— **镜像**，写的是 ``failed``、
      第 9 轮。读侧必须无视它。

    The verification row goes in through the repository, exactly the way
    the product writes it: ``init_round`` marks the round as started,
    ``complete_round`` records its verdict.  ``publish_closed=False``
    because the round is over but the **plan** is not: the plan is still
    on the task view, and a terminal announce would say otherwise.

    The mirror goes in through raw SQL, the way the **old** writer left
    it: no production writer writes that column any more, so there is no
    repository call that could produce it — and a mirror written through
    the surviving writer (the baseline one) would carry the baseline
    instead of the stale verdict this test needs.
    """
    plan_dir = work / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": list(SEEDED_TASKS)}, ensure_ascii=False),
        encoding="utf-8",
    )

    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = db_open(work / "state.db")
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "queued")

        # The mirror: a stale verdict that contradicts the authoritative
        # row on every field a reader could report.
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (
                json.dumps(
                    {
                        "status": SEED_MIRROR_STATUS,
                        "round": SEED_MIRROR_ROUND,
                        "max_rounds": SEED_MIRROR_MAX_ROUNDS,
                    },
                    ensure_ascii=False,
                ),
                PLAN_ID,
            ),
        )
        conn.commit()

        repository = VerificationRepository(conn)
        repository.init_round(
            PLAN_ID,
            SEED_AUTHORITATIVE_ROUND,
            SEED_AUTHORITATIVE_MAX_ROUNDS,
        )
        repository.complete_round(
            PLAN_ID,
            {
                "pytest_summary": "2 passed",
                "llm_findings": "",
                "performance_metrics": {},
            },
            status=SEED_AUTHORITATIVE_STATUS,
            publish_closed=False,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The backend process
# ---------------------------------------------------------------------------


def _stop_backend(proc: Optional[subprocess.Popen]) -> None:
    """Stop the backend — the whole process group, so nothing is left running.

    ``_teardown`` signals the server process; the server was started with
    ``start_new_session=True``, so it is a **group** leader and anything
    it spawned would outlive a plain ``terminate()``.  The group is
    signalled first (while the process is known to be alive), then
    ``_teardown`` does the graceful-then-force escalation.
    """
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, AttributeError):
            pass
    _teardown(proc)


def _wait_plans_ready(base_url: str, proc: subprocess.Popen) -> None:
    """Wait until the plans listing answers — the endpoint the page needs.

    ``/api/health`` says the process is up; ``/api/plans`` says the
    instance can answer the question the dashboard asks.  This acceptance
    needs the second one, so the second one is what is polled — through
    ``_http_json``, which sends the request guard's header
    (``X-PDT-Request: 1``); without it the route answers 403 and the
    poll would never see a 200.

    A listing that never answers is a failure that carries the backend's
    own stdout/stderr verbatim: a boot that died says why in there, and a
    boot that hung says where it stopped.
    """
    deadline = time.monotonic() + PLANS_READY_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the backend exited with {proc.returncode} before it could "
                f"list its plans:\n--- the backend's own output ---\n"
                f"{_server_output(proc)}"
            )
        try:
            status, body = _http_json("GET", f"{base_url}/api/plans")
            if status == 200:
                return
            last = (status, body)
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"{base_url}/api/plans did not answer within "
        f"{PLANS_READY_TIMEOUT_SECONDS}s; last={last!r}\n"
        f"--- the backend's own output ---\n{_server_output(proc)}"
    )


def _surviving_group_members(pgid: int) -> List[str]:
    """What is still alive of the backend's process group.

    ``killpg(pgid, 0)`` succeeds while **any** member of the group is
    alive; it raises ``ProcessLookupError`` when the group is empty.  The
    group id has to be captured while the leader is still running — once
    the leader is reaped, its pid is gone and its group id cannot be
    asked for any more.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return []
    except OSError:
        return []
    return [f"process group {pgid} is still alive"]


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def verification_authority_ui(tmp_path_factory) -> Dict[str, Any]:
    """A real backend, a real browser, and what the page showed.

    The backend is a real ``backend.server`` process against a plans tree
    and a state database that live in pytest's temporary directory.  The
    browser is a real Chromium driven through the page's own events.
    """
    work = tmp_path_factory.mktemp("summary-verification-authority-e2e")
    _seed_conflicting_plan(work)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    screenshot_path = work / SCREENSHOT_NAME

    proc = _boot(work, port)
    # The backend must be the product's own interpreter, not a system
    # python that happens to have the dependencies.
    assert str(proc.args[0]) == str(VENV_PYTHON), (
        f"the backend was launched by {proc.args[0]!r}, not by the "
        f"product interpreter {str(VENV_PYTHON)!r}"
    )
    # The group id is captured while the leader is alive: once the
    # process is reaped, its pid is gone and its group cannot be asked
    # about any more.
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:  # pragma: no cover - the process is ours
        pgid = proc.pid
    try:
        _wait_ready(base_url, proc)

        # The plans listing is what puts the plan card on the page, so
        # it is the endpoint whose readiness this acceptance waits for
        # (it carries the request guard's header).
        _wait_plans_ready(base_url, proc)

        try:
            report = _drive(base_url, PLAN_ID, screenshot_path, work)
        except Exception as exc:  # noqa: BLE001 - the server log is context
            # ``Exception``, not ``BaseException``: pytest's ``Skipped``
            # inherits from ``BaseException`` (``Skipped -> OutcomeException
            # -> BaseException``), so a wider net here rewrote
            # ``pytest.skip("chromium is not installed")`` into an
            # ``AssertionError`` -- a machine without a browser reported a
            # product failure instead of skipping.  ``AssertionError`` /
            # ``TimeoutExpired`` / a failed subprocess are all ``Exception``
            # subclasses, so they still arrive here with the backend's own
            # output attached.
            raise AssertionError(
                f"{exc}\n--- tail of the backend's own output ---\n"
                f"{_server_output(proc)}"
            ) from exc
    finally:
        # The failure path stops the backend too.
        _stop_backend(proc)

    # Nothing of the backend may outlive it: no zombie server, and no
    # survivor of the process group it led.
    assert proc.poll() is not None, (
        f"the backend is still running after the teardown: pid={proc.pid}"
    )
    survivors = _surviving_group_members(pgid)
    assert survivors == [], f"the backend left orphans behind: {survivors!r}"

    return {
        "base_url": base_url,
        "work": work,
        "report": report,
        "screenshot": Path(str(report.get("screenshot") or screenshot_path)),
    }


# ---------------------------------------------------------------------------
# The assertions
# ---------------------------------------------------------------------------


def test_summary_and_status_agree_end_to_end(verification_authority_ui):
    """两个端点，一个问题，一个答案 —— 而且是权威行的那个答案。

    ``/api/plan/{id}/summary`` 与 ``/api/verification/{id}/status`` 读的
    是同一张表（``plan_verification``）。种子里镜像列写的是
    ``failed / round 9``，与权威行冲突：两个端点都必须报权威行，任何一个
    读走镜像，下面每一个断言都会红。

    这一份读的是**真的后端进程**的回答 —— 走网络、走 JSON 序列化、走
    端点自己的代码路径，而不是进程内的直接调用。
    """
    report = verification_authority_ui["report"]
    api = report.get("api") or {}

    assert api.get("summary_http") == 200, (
        f"the summary endpoint answered {api.get('summary_http')!r}: {report!r}"
    )
    assert api.get("status_http") == 200, (
        f"the status endpoint answered {api.get('status_http')!r}: {report!r}"
    )

    block = api.get("summary_verification") or {}

    assert block.get("status") == EXPECTED_VERIFICATION_STATUS, (
        f"the summary must report the authoritative verdict "
        f"{EXPECTED_VERIFICATION_STATUS!r}; got {block!r} (the mirror says "
        f"{SEED_MIRROR_STATUS!r})"
    )
    assert block.get("round") == EXPECTED_VERIFICATION_ROUND, (
        f"the summary must report the authoritative round "
        f"{EXPECTED_VERIFICATION_ROUND}; got {block!r} (the mirror says "
        f"{SEED_MIRROR_ROUND})"
    )
    assert block.get("max_rounds") == EXPECTED_VERIFICATION_MAX_ROUNDS, (
        f"the summary must report the authoritative cap "
        f"{EXPECTED_VERIFICATION_MAX_ROUNDS}; got {block!r}"
    )

    assert api.get("verification_status") == EXPECTED_VERIFICATION_STATUS, (
        f"the status endpoint must report the authoritative verdict "
        f"{EXPECTED_VERIFICATION_STATUS!r}; got "
        f"{api.get('verification_status')!r} (the mirror says "
        f"{SEED_MIRROR_STATUS!r})"
    )
    assert api.get("verification_round") == EXPECTED_VERIFICATION_ROUND, (
        f"the status endpoint must report the authoritative round "
        f"{EXPECTED_VERIFICATION_ROUND}; got "
        f"{api.get('verification_round')!r} (the mirror says "
        f"{SEED_MIRROR_ROUND})"
    )
    assert api.get("verification_max_rounds") == EXPECTED_VERIFICATION_MAX_ROUNDS, (
        f"the status endpoint must report the authoritative cap "
        f"{EXPECTED_VERIFICATION_MAX_ROUNDS}; got "
        f"{api.get('verification_max_rounds')!r}"
    )

    # The stop reason is the fourth lifecycle key.  The seed closes a
    # round with a verdict and no stop reason, so the authoritative value
    # is "none" -- and "none" is what a mirror would have to fake to
    # pass the other three.
    assert block.get("stop_reason") is None, (
        f"the summary must report the authoritative stop reason (none); "
        f"got {block!r}"
    )
    assert api.get("verification_stop_reason") is None, (
        f"the status endpoint must report the authoritative stop reason "
        f"(none); got {api.get('verification_stop_reason')!r}"
    )

    # The **whole** block, not three fields out of it.  A block that
    # still carries one of the mirror's values under another name is a
    # mirror too, and a field-by-field check would have said nothing
    # about it.
    assert block == EXPECTED_SUMMARY_VERIFICATION_BLOCK, (
        f"the summary's verification block must be exactly "
        f"{EXPECTED_SUMMARY_VERIFICATION_BLOCK!r}; got {block!r}"
    )

    # The mirror is a different verdict on every field; neither endpoint
    # may report it.
    assert block.get("status") != SEED_MIRROR_STATUS, (
        f"the summary reported the mirror's verdict: {block!r}"
    )
    assert block.get("round") != SEED_MIRROR_ROUND, (
        f"the summary reported the mirror's round: {block!r}"
    )

    # Two endpoints, one question, one answer.
    assert block.get("status") == api.get("verification_status"), (
        f"summary and status disagree on the verdict: {block!r} vs {api!r}"
    )
    assert block.get("round") == api.get("verification_round"), (
        f"summary and status disagree on the round: {block!r} vs {api!r}"
    )
    assert block.get("max_rounds") == api.get("verification_max_rounds"), (
        f"summary and status disagree on the cap: {block!r} vs {api!r}"
    )


def test_dashboard_renders_authoritative_badge(verification_authority_ui):
    """页面上给人看的那句话，是权威行的那一句。

    徽标说的是这一轮的结论，轮次说的是这一轮是第几轮。两个都必须来自
    权威行 —— 镜像里的 ``验证失败`` 与 ``Round 9/3`` 一个字都不能出现在
    面板上。

    页面全程没有未捕获异常，也没有 console error：资源 404 不算（浏览器把
    「一个资源没找到」也报成 console error，而仪表盘会去问几个可有可无的
    静态资源）。一个「有错误但不影响断言」的页面不是可用的仪表盘。
    """
    report = verification_authority_ui["report"]

    assert report.get("plan_card_visible") is True, (
        f"the plan card never appeared on the page: {report!r}"
    )
    # The card that was clicked has to be OUR plan's card -- a card that
    # merely exists on the page proves nothing about which one the user
    # opened.
    assert report.get("plan_card_plan_id") == PLAN_ID, (
        f"the card on the page belongs to {report.get('plan_card_plan_id')!r}, "
        f"not to {PLAN_ID!r}: {report!r}"
    )
    assert report.get("verification_panel_visible") is True, (
        f"the verification panel did not open: {report!r}"
    )

    assert report.get("verification_badge") == EXPECTED_BADGE_TEXT, (
        f"the verification badge reads {report.get('verification_badge')!r}, "
        f"not {EXPECTED_BADGE_TEXT!r}: {report!r}"
    )
    assert report.get("verification_round") == EXPECTED_ROUND_TEXT, (
        f"the verification round reads {report.get('verification_round')!r}, "
        f"not {EXPECTED_ROUND_TEXT!r}: {report!r}"
    )

    panel_text = report.get("verification_panel_text") or ""
    assert MIRROR_BADGE_TEXT not in panel_text, (
        f"the panel shows the mirror's verdict {MIRROR_BADGE_TEXT!r}: "
        f"{panel_text!r}"
    )
    assert MIRROR_ROUND_TEXT not in panel_text, (
        f"the panel shows the mirror's round {MIRROR_ROUND_TEXT!r}: "
        f"{panel_text!r}"
    )

    assert report.get("page_errors") == [], (
        f"the page raised uncaught JavaScript exceptions: "
        f"{report.get('page_errors')!r}"
    )
    assert report.get("console_errors") == [], (
        f"the page logged console errors: {report.get('console_errors')!r}"
    )


def test_screenshot_is_archived_outside_repo(
    verification_authority_ui, tmp_path_factory
):
    """截图存在、非空，而且**不在仓库工作区里**。

    证据落在 pytest 的临时目录里 —— 仓库工作区里多一张 PNG、一个日志、
    一个 artifact，都是要跟着提交走的东西。
    """
    screenshot = verification_authority_ui["screenshot"]

    assert screenshot.is_file(), f"the screenshot was not written: {screenshot}"
    assert screenshot.stat().st_size > 0, f"the screenshot is empty: {screenshot}"

    resolved = screenshot.resolve()
    repo_root = REPO_ROOT.resolve()
    assert not resolved.is_relative_to(repo_root), (
        f"the screenshot landed inside the repository workspace: {resolved}"
    )
    base_temp = Path(str(tmp_path_factory.getbasetemp())).resolve()
    assert resolved.is_relative_to(base_temp), (
        f"the screenshot is outside pytest's temporary directory: {resolved}"
    )