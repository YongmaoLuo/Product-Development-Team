"""端到端：框架修复分区在浏览器里看得见，徽章是中文，忽略要走确认。

这份文件证明什么
----------------
任务 29/31 证明了**接口**答得对，任务 40 证明了**前端代码**里有那几行
渲染。但这两件事都答不了操作者真正会问的那句话：

    *「屏幕上真的有这一列吗？徽章上写的是我能看懂的中文吗？我点了忽略，它
    真的消失了吗？」*

API 层的证据证明不了「用户看得见」—— 读模型里有一行 ``framework_fixes``，
和操作者的屏幕上有一列「框架修复」，是两件事。所以这一份起一个**真的**
``backend.server`` 进程（真的 uvicorn、真的 HTTP、真的 SQLite 文件），用
**真的 Chromium**（Playwright）把操作者的动作做一遍，然后断言**页面**上
读到的字。

四条判据
--------
* ``test_partition_visible`` —— 注入一条框架修复数据之后，分区在页面上
  **看得见**（不是「接口里有」，是「面板上画出来了」）；
* ``test_badge_text`` —— 徽章上写的是**中文**（「等待合并」），不是后端
  的状态原词（``waiting_on_user_merge``）；
* ``test_dismiss_confirm_flow`` —— 点「忽略」→ 弹层问原因 → 填了确认 →
  条目**从页面上消失**；
* ``test_screenshot_saved`` —— 截图落在
  ``plans/<plan_id>/screenshots/framework_fix_ui.png``。

外加三条边界
------------
* **没有框架修复数据 → 分区隐藏。** 空分区不占地方，调度器看上去和这条
  lane 存在之前一模一样；
* **弹层取消 → 条目仍在**，而且**一个请求都没发出去**（取消就是取消）；
* **失败时截图与 DOM 照样落盘** —— 一次失败的验收也得留下证据，否则失败
  之后没人知道页面当时长什么样。这一条是**故意失败**的一跑：驱动把页面
  画出来，然后去等一个永远不会来的条目，等它放弃；放弃之后盘上仍然要有
  一张真的图和一份真的 DOM。

边界条件
--------
* **端口**：随机取一个空闲端口，**跳过 8000** —— 8000 上跑着这台机器上
  别的服务，抢它会把别的进程踢掉。端口由 ``PDT_PORT`` 交给后端；
* **状态库**：隔离到本次的 ``tmp_path``（``PDT_STATE_DB_PATH``），运营者
  的真库一个字节都不写；
* **等待**：有界轮询（60 秒上限），**没有固定 sleep** —— 等的是「条件成立」
  （面板画出来了 / 弹层开了 / 条目没了），不是「等一会儿再说」；
* **浏览器不可用**：这台机器上没有 Playwright 驱动（或装了驱动没装浏览器
  二进制）时**跳过**，绝不静默通过 —— 一份没有真浏览器的端到端验收不是验收。

数据是真的
----------
注入的那一行走的是读模型真正读的那几张表（``plan_routing`` 的
``plan_kind='framework_fix'``、``framework_fix_registry``、
``framework_fix_attach``），落在后端真正读的那份 SQLite 文件里。页面上
那一列是 ``GET /api/scheduler/state`` 的答案经 ``renderScheduler()`` 画
出来的 —— 中间没有替身。

复用说明
--------
起服务的脚手架（``_boot`` / ``_wait_ready`` / ``_teardown`` /
``_server_output``）按**文件路径** importlib 加载
``backend/tests/e2e/test_execution_status_restart_e2e.py`` 里的那一份用；
浏览器那一对（``_playwright_interpreter`` / ``_driver_env``）按同样的方式
加载 ``backend/tests/e2e/test_fastfail_no_ghost_state_ui.py`` 里的那一份。
本仓库的 e2e 目录带 ``__init__.py``，跨模块 import 能不能成要看 rootdir 和
import mode，还要看收集顺序；按**路径**加载两样都不依赖。那两个兄弟文件
**一个字节都不改**。
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# 路径 + 借来的脚手架
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
E2E_DIR = Path(__file__).resolve().parent

# ``backend/`` 必须可导入：生产模块之间按平铺名字互相 import
# （``from config_paths import ...``）。
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_sibling(module_name: str, filename: str):
    """按**文件路径**加载一个兄弟 e2e 模块。

    ``importlib.util.spec_from_file_location`` 读文件、以私有名义执行它。
    兄弟文件一个字节都不改，这里也不依赖它能作为包被导入。
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

_boot = _RESTART_E2E._boot
_wait_ready = _RESTART_E2E._wait_ready
_teardown = _RESTART_E2E._teardown
_server_output = _RESTART_E2E._server_output

#: 找不到 Playwright 驱动时，验收跳过 —— 一份没有真浏览器的验收不是验收。
_playwright_interpreter = _GHOST_UI_E2E._playwright_interpreter
#: 浏览器驱动跑在哪个 home 下（Playwright 在那里找浏览器二进制）。
_driver_env = _GHOST_UI_E2E._driver_env


#: 绝不抢的端口。8000 上跑着这台机器上别的服务。
_RESERVED_PORT = 8000


def _free_port_excluding(reserved: int) -> int:
    """一个空闲端口，且不是 ``reserved``。

    让内核挑一个端口 —— 那一刻它必然是空的。挑到 ``reserved`` 就再挑一次
    （概率约等于零，但「约等于零」不是「不会」）。
    """
    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != reserved:
            return port
    raise AssertionError(f"could not find a free port other than {reserved}")


# ---------------------------------------------------------------------------
# 这条 lane 的常量
# ---------------------------------------------------------------------------

#: 注入的那条框架修复计划。
FX_PLAN_ID = "fx-ui-e2e"

#: 它修的缺陷的指纹。
FX_FINGERPRINT = "sha256:" + "d" * 64

#: 缺陷签名 —— 面板上那一行摘要读的就是它。
FX_SIGNATURE = {
    "framework_files": ["backend/framework_fix/registry.py"],
    "error_class": "AssertionError",
    "first_error_line": "assert 0 == 1",
}

#: 修复计划所在的仓。
FX_REPO = "app"

#: 行上的时间戳。
FX_STAMP = "2026-11-24T00:00:00Z"

#: 后端的状态原词 —— 徽章上**不该**出现它。
WAITING_MERGE_STATE = "waiting_on_user_merge"

#: 徽章上**该**出现的中文。
WAITING_MERGE_BADGE = "等待合并"

#: 忽略的原因 —— 一次没人能解释的忽略不是忽略。
DISMISS_REASON = "e2e：这个缺陷已经不在了"

#: 截图的名字。截图落在计划自己的 ``screenshots/`` 目录里 —— 那棵树下的
#: 计划目录就是后端读的那棵。
SCREENSHOT_NAME = "framework_fix_ui.png"

#: DOM 快照的名字 —— 失败时页面当时长什么样。
DOM_DUMP_NAME = "framework_fix_ui.html"

#: 等待的上限（毫秒）。面板的读模型是秒级的，60 秒足够；再久就是坏了。
WAIT_TIMEOUT_MS = 60_000

#: 故意失败的那一跑用的等待上限（毫秒）。那一跑的**要点**就是那个等待
#: 放弃，所以它短：页面画出来要一两秒，等一个永远不会来的条目再给它五秒
#: 就够了 —— 剩下的时间是在浪费机器。
FAILING_SCENARIO_TIMEOUT_MS = 5_000

#: PNG 文件的开头八个字节。一张截图必须**是**一张图：落盘的路径对了、文件
#: 也在，但里面是空的或者一段文本，那不叫截图。
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 注入：读模型真正读的那几张表
# ---------------------------------------------------------------------------
def _insert(db_path: Path, statement: str, parameters: tuple) -> None:
    """Run one statement against the state database."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(statement, parameters)
        conn.commit()
    finally:
        conn.close()


def _seed_framework_fix_entry(work: Path) -> None:
    """把一条框架修复数据写进状态库 —— 读模型真正读的那几张表。

    三行：

    * ``plan_routing`` —— 路由行。``plan_kind='framework_fix'`` 是这一列
      归入框架修复分区的**唯一**依据；``framework_fix`` 块里记着状态
      （``waiting_on_user_merge``）、指纹、来源计划与两个挂接计数；
    * ``framework_fix_registry`` —— 这个修复计划修的是哪个缺陷。状态写
      ``fixed``：修复本身做完了，只差合入 —— 这正是面板说「等待合并」的
      那种情形；
    * ``framework_fix_attach`` —— 两个挂接（一挂一登记），条目上那两个计数
      读的就是它。

    状态库是后端读的那一份（``PDT_STATE_DB_PATH`` 指的就是它），所以这一列
    是**真的**从库里读出来的，不是测试进程自己攒的。
    """
    db_path = work / "state.db"

    _insert(
        db_path,
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at, plan_kind, framework_fix) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            FX_PLAN_ID,
            "executing",
            FX_STAMP,
            "framework_fix",
            json.dumps(
                {
                    "fingerprint": FX_FINGERPRINT,
                    "status": WAITING_MERGE_STATE,
                    "source_plan_id": "p-source",
                    "source_round": 3,
                    "attach_blocked_count": 1,
                    "attach_registered_count": 2,
                    "repo": FX_REPO,
                    "branch": "fix/fx-ui-e2e",
                    "created_at": FX_STAMP,
                },
                ensure_ascii=False,
            ),
        ),
    )

    _insert(
        db_path,
        "INSERT OR REPLACE INTO framework_fix_registry "
        "(fingerprint, fix_plan_id, repo, status, key_json, "
        "framework_files_json, evidence_summary_json, "
        "first_seen_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            FX_FINGERPRINT,
            FX_PLAN_ID,
            FX_REPO,
            "fixed",
            json.dumps(FX_SIGNATURE, ensure_ascii=False),
            json.dumps(FX_SIGNATURE["framework_files"], ensure_ascii=False),
            json.dumps({}),
            FX_STAMP,
            FX_STAMP,
        ),
    )

    for attach_id, plan_id, status in (
        ("attach-blocked", "p-blocked", "blocked"),
        ("attach-registered", "p-registered", "registered"),
    ):
        _insert(
            db_path,
            "INSERT OR REPLACE INTO framework_fix_attach "
            "(attach_id, fingerprint, plan_id, vp_id, round, source, "
            "status, held_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attach_id,
                FX_FINGERPRINT,
                plan_id,
                "VP-1",
                3,
                "hit",
                status,
                FX_STAMP,
            ),
        )


# ---------------------------------------------------------------------------
# 真服务：真的进程、真的库、真的计划树
# ---------------------------------------------------------------------------
@dataclass
class World:
    """这一轮跑出来的事实。

    Attributes:
        base_url: 真实服务的根地址。
        work: 本次的临时目录（状态库与计划树都在里面）。
        plans_dir: 计划树 —— 后端读的那一棵，截图也落在它里面。
        proc: 后端进程。
    """

    base_url: str
    work: Path
    plans_dir: Path
    proc: subprocess.Popen

    def screenshot_path(self) -> Path:
        """截图落在哪儿 —— ``plans/<plan_id>/screenshots/<name>.png``。"""
        return self.plans_dir / FX_PLAN_ID / "screenshots" / SCREENSHOT_NAME

    def dom_path(self) -> Path:
        """DOM 快照落在哪儿 —— 失败时页面当时长什么样。"""
        return self.plans_dir / FX_PLAN_ID / "screenshots" / DOM_DUMP_NAME

    def server_output(self, limit: int = 2000) -> str:
        """后端自己说过的话。"""
        return _server_output(self.proc, limit=limit)


def _boot_world(work: Path, *, seed_entry: bool) -> World:
    """起一个真的后端，按需注入一条框架修复数据。

    ``seed_entry=False`` 起的是「空」的调度器：库里没有框架修复数据，面板
    上那一列必须**不出现**。
    """
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    # 迁移先行：表还不存在时插入会失败。
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(work / "state.db")
    try:
        migrate(conn)
    finally:
        conn.close()

    if seed_entry:
        _seed_framework_fix_entry(work)

    port = _free_port_excluding(_RESERVED_PORT)
    proc = _boot(work, port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base_url, proc)
    except AssertionError:
        # 一个没起来的端得说为什么：它自己的输出在失败传下去之前打出来。
        print(
            "the backend never became ready; its output:\n" + _server_output(proc),
            file=sys.stderr,
        )
        raise
    return World(
        base_url=base_url,
        work=work,
        plans_dir=plans_dir,
        proc=proc,
    )


@pytest.fixture
def world(tmp_path) -> World:
    """一个真的后端，库里有一条框架修复数据。"""
    the_world = _boot_world(tmp_path, seed_entry=True)
    try:
        yield the_world
    finally:
        _teardown(the_world.proc)


@pytest.fixture
def empty_world(tmp_path) -> World:
    """一个真的后端，库里**没有**框架修复数据。"""
    the_world = _boot_world(tmp_path, seed_entry=False)
    try:
        yield the_world
    finally:
        _teardown(the_world.proc)


@pytest.fixture
def browser() -> str:
    """一个能开真浏览器的解释器 —— 或者一次跳过。

    仪器在**服务起来之前**就要：没有浏览器的机器什么都不用付 —— 不起服务、
    不开浏览器，这份验收就当众说一声「面板没有被驱动过」。
    """
    found = _playwright_interpreter()
    if found is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "framework-fix column cannot be accepted end-to-end without a "
            "browser"
        )
    return found


# ---------------------------------------------------------------------------
# 浏览器驱动：操作者怎么用这一列，就怎么驱动它
# ---------------------------------------------------------------------------
_BROWSER_DRIVER = r'''
"""Drive the framework-fix column the way an operator drives it.

Arguments:
  1. the base url of the running server
  2. the path the screenshot is written to
  3. the path the DOM snapshot is written to
  4. the scenario: ``observe`` / ``cancel`` / ``dismiss`` / ``empty``
  5. the plan id the entry is about
  6. a JSON object: the wait budget in milliseconds

Every wait is a wait for a **condition** — the panel drew the entry, the
question layer opened, the entry went away — and every one of them is
bounded.  There is no fixed sleep anywhere in this file: a fixed wait
asserts nothing, and it asserts it slowly.

One JSON document is printed on stdout: what the page drew, what the
panel's own requests carried, and where the evidence landed.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

#: The request guard's header.  Every ``/api/*`` call has to carry it.
GUARD_HEADER = "x-pdt-request"

#: The path the dismissal is written through.
DISMISS_PATH = "/framework-fix/dismiss"


def browser_executable():
    """A Chromium already installed under the Playwright cache.

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


#: Read the column as the page draws it.
#
# The badge is read twice: its **text** (what the operator reads) and its
# **class** (the state the backend named).  They are two different
# questions, and the second one is the control for the first: a badge
# that carries the state token as its text would satisfy "the badge says
# something" while telling the operator nothing.
_READ_PANEL = """
() => {
    const panel = document.querySelector('#framework-fix-panel');
    const list = document.querySelector('#framework-fix-list');
    const modal = document.querySelector('#framework-fix-confirm');
    const entries = Array.from(
        document.querySelectorAll('#framework-fix-list .ff-entry')
    );
    const textOf = (node) => ((node || {}).textContent || '').trim();
    return {
        panel_present: !!panel,
        panel_hidden_class: panel ? panel.classList.contains('hidden') : null,
        list_entry_count: entries.length,
        entries: entries.map((entry) => {
            const badge = entry.querySelector('.ff-badge');
            return {
                plan_id: (entry.dataset.planId || '').trim(),
                badge_text: textOf(badge),
                badge_class: (badge ? badge.className : '').trim(),
                summary: textOf(entry.querySelector('.ff-summary')),
                source: textOf(entry.querySelector('.ff-source')),
                blocked: textOf(entry.querySelector('.ff-blocked')),
                dismiss_button: !!entry.querySelector('.ff-dismiss'),
            };
        }),
        notice: textOf(document.querySelector('#framework-fix-notice')),
        modal_present: !!modal,
        modal_hidden_class: modal ? modal.classList.contains('hidden') : null,
    };
}
"""


base_url = sys.argv[1]
screenshot_path = Path(sys.argv[2])
dom_path = Path(sys.argv[3])
scenario = sys.argv[4]
plan_id = sys.argv[5]
options = json.loads(sys.argv[6])
timeout_ms = int(options.get("timeout_ms", 60000))
reason = str(options.get("reason", ""))

ENTRY = '.ff-entry[data-plan-id="%s"]' % plan_id

report = {
    "scenario": scenario,
    "panel_visible": False,
    "panel_hidden_class": None,
    "entry_count": 0,
    "entries": [],
    "notice": "",
    "modal_visible": False,
    "modal_hidden_class": None,
    "entry_after_cancel": None,
    "entry_after_confirm": None,
    "dismiss_requests": [],
    "api_requests": [],
    "page_errors": [],
    "unhandled_rejections": [],
    "screenshot": str(screenshot_path),
    "dom_dump": str(dom_path),
    "failure": None,
}


def save_evidence(page):
    """Leave the screenshot and the DOM behind.

    Called on the way out of every path — the one that worked and the one
    that did not.  A failed acceptance that leaves no evidence behind
    leaves nobody any way to see what the page looked like.
    """
    try:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        pass
    try:
        dom_path.parent.mkdir(parents=True, exist_ok=True)
        dom_path.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

failure = None

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})

    page.on(
        "pageerror",
        lambda exc: report["page_errors"].append(str(exc)),
    )
    page.add_init_script(
        """
        window.__pdtUnhandled = [];
        window.addEventListener('unhandledrejection', function (event) {
            const reason = event.reason;
            const text = (reason && (reason.stack || reason.message)) || String(reason);
            window.__pdtUnhandled.push(text);
        });
        """
    )

    def on_request(request):
        if "/api/" not in request.url:
            return
        report["api_requests"].append(
            {
                "url": request.url,
                "method": request.method,
                "guard": request.headers.get(GUARD_HEADER),
            }
        )

    def on_response(response):
        if "/api/" not in response.url:
            return
        if DISMISS_PATH not in response.url:
            return
        report["dismiss_requests"].append(
            {"url": response.url, "status": response.status}
        )

    page.on("request", on_request)
    page.on("response", on_response)

    def read_page():
        """Read the page as it stands — and fold it into the report.

        Called on the way out of **every** path, the one that worked and
        the one that did not: a run that died waiting for the column has
        to say what the column looked like when it died, otherwise the
        report says only "timed out" and the reader has to guess.
        """
        try:
            panel = page.evaluate(_READ_PANEL)
        except Exception:
            return
        report["panel_hidden_class"] = panel["panel_hidden_class"]
        report["entry_count"] = panel["list_entry_count"]
        report["entries"] = panel["entries"]
        report["notice"] = panel["notice"]
        report["modal_hidden_class"] = panel["modal_hidden_class"]
        try:
            report["panel_visible"] = page.locator(
                "#framework-fix-panel"
            ).is_visible()
            report["modal_visible"] = (
                False
                if panel["modal_present"] is False
                else page.locator("#framework-fix-confirm").is_visible()
            )
            report["entry_after_cancel"] = (
                page.locator(ENTRY).count() if scenario == "cancel" else None
            )
            report["entry_after_confirm"] = (
                page.locator(ENTRY).count() if scenario == "dismiss" else None
            )
        except Exception:
            pass
        try:
            report["unhandled_rejections"] = page.evaluate(
                "() => window.__pdtUnhandled || []"
            )
        except Exception:
            pass

    try:
        page.goto(base_url + "/", wait_until="load")
        page.wait_for_selector("#app", timeout=timeout_ms)

        # The operator opens the scheduling view.
        page.click("#btn-open-scheduler", timeout=timeout_ms)
        page.wait_for_selector(
            "#scheduler-view:not(.hidden)", timeout=timeout_ms
        )

        # The read model has been read AND drawn: the panel's own stamp
        # is on the screen.  The stamp is written by the same
        # synchronous render that draws (or hides) the column, so once
        # it is there the column's state is settled.
        page.wait_for_function(
            """
            () => {
                const el = document.querySelector('#scheduler-view-generated-at');
                if (!el) return false;
                const text = (el.textContent || '').trim();
                const placeholder = String.fromCharCode(0x2014);
                return text !== '' && text !== placeholder;
            }
            """,
            timeout=timeout_ms,
        )

        if scenario in ("observe", "cancel", "dismiss", "fail"):
            # The column is drawn with the entry in it.
            page.wait_for_selector(ENTRY, state="visible", timeout=timeout_ms)

        if scenario == "empty":
            # The column drew nothing, so the panel must not be on the
            # screen.  The wait is for the panel to be *hidden* — the
            # condition the empty state is supposed to reach.
            page.wait_for_selector(
                "#framework-fix-panel", state="hidden", timeout=timeout_ms
            )

        if scenario == "fail":
            # A run that is **supposed** to fail.  The column is drawn
            # first — so the evidence this run leaves behind is a page
            # with something on it — and then the driver waits for an
            # entry that is never going to be there.  The point of this
            # scenario is not the wait: it is what is still on the disk
            # after the wait gives up.
            page.wait_for_selector(
                '.ff-entry[data-plan-id="never-there"]',
                state="visible",
                timeout=timeout_ms,
            )

        if scenario == "cancel":
            # The operator asks to dismiss it, then changes their mind.
            page.click(ENTRY + " .ff-dismiss", timeout=timeout_ms)
            page.wait_for_selector(
                "#framework-fix-confirm:not(.hidden)",
                state="visible",
                timeout=timeout_ms,
            )
            page.click("#framework-fix-confirm-cancel", timeout=timeout_ms)
            page.wait_for_selector(
                "#framework-fix-confirm", state="hidden", timeout=timeout_ms
            )

        if scenario == "dismiss":
            # The operator asks to dismiss it, says why, and confirms.
            page.click(ENTRY + " .ff-dismiss", timeout=timeout_ms)
            page.wait_for_selector(
                "#framework-fix-confirm:not(.hidden)",
                state="visible",
                timeout=timeout_ms,
            )
            page.fill("#framework-fix-reason", reason)
            page.click("#framework-fix-confirm-ok", timeout=timeout_ms)
            # The entry is gone from the page: the wait is for the
            # element to disappear, not for a number of seconds to pass.
            page.wait_for_selector(
                ENTRY, state="hidden", timeout=timeout_ms
            )

        read_page()
    except Exception as exc:  # noqa: BLE001 — the report carries the reason
        failure = exc
        report["failure"] = "%s: %s" % (type(exc).__name__, exc)
        read_page()
        save_evidence(page)
    else:
        save_evidence(page)
    finally:
        try:
            browser.close()
        except Exception:
            pass

print(json.dumps(report, ensure_ascii=False))

if failure is not None:
    raise failure
'''


# ---------------------------------------------------------------------------
# 驱动一次浏览器，读回它的报告
# ---------------------------------------------------------------------------
def _drive(
    interpreter: str,
    world: World,
    scenario: str,
    *,
    reason: str = DISMISS_REASON,
    timeout_ms: int = WAIT_TIMEOUT_MS,
    allow_failure: bool = False,
) -> Dict[str, Any]:
    """Drive the page in a real browser and return what it drew.

    The driver runs in its own process: a browser that dies takes the
    driver down with it, and it must not take the test session with it.
    Its report is read from stdout — and when the driver failed, the
    report (which carries the failure and the paths the evidence landed
    at) is attached to the failure, so the artifacts are named in the
    error rather than left on disk unmentioned.

    ``allow_failure`` is for the one scenario that is **meant** to fail
    (the evidence run): there the report is handed back with its
    ``failure`` field filled in, instead of the failure being raised.
    ``timeout_ms`` is the wait budget handed to the driver — the failing
    scenario gets a short one, because the wait is the thing that is
    supposed to give up.
    """
    driver_script = world.work / f"drive_{scenario}.py"
    driver_script.write_text(_BROWSER_DRIVER, encoding="utf-8")

    screenshot = world.screenshot_path()
    dom_dump = world.dom_path()

    result = subprocess.run(
        [
            interpreter,
            str(driver_script),
            world.base_url,
            str(screenshot),
            str(dom_dump),
            scenario,
            FX_PLAN_ID,
            json.dumps(
                {
                    "timeout_ms": timeout_ms,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=_driver_env(),
    )

    payload = _last_document(result.stdout)
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or (
            "playwright install" in combined
        ):
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not; the framework-fix column cannot be "
                "accepted end-to-end without a browser"
            )
        if allow_failure:
            return payload
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"report={json.dumps(payload, ensure_ascii=False)}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}; "
            f"the screenshot and the DOM are at "
            f"{screenshot} and {dom_dump}; "
            f"the backend said: {world.server_output(limit=1200)!r}"
        )
    return payload


def _last_document(text: str) -> Dict[str, Any]:
    """The last JSON document in *text*.

    The driver prints one document; the browser's own chatter may sit
    around it, so the last line that parses is the report.
    """
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            return document
    return {}


def _summary(report: Dict[str, Any]) -> str:
    """A one-line summary of what the page drew — for the failure text."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 1. 分区在页面上看得见
# ---------------------------------------------------------------------------
def test_partition_visible(browser: str, world: World):
    """注入一条框架修复数据之后，那一列在页面上画出来了。

    判据是**屏幕上**：面板不是隐藏的，列表里有一条，条目说的是那条计划。
    「接口里有这一行」不是这个判据 —— 那一行在接口里的时候，面板也可能
    什么都没画。
    """
    report = _drive(browser, world, "observe")

    assert report.get("panel_hidden_class") is False, (
        f"the framework-fix column is still hidden on the page: "
        f"{_summary(report)}"
    )
    assert report.get("panel_visible") is True, (
        f"the framework-fix column is not on the screen: {_summary(report)}"
    )
    assert report.get("entry_count") == 1, (
        f"the column drew {report.get('entry_count')!r} entries; it must "
        f"draw exactly the one that was injected: {_summary(report)}"
    )

    entries = report.get("entries") or []
    assert entries[0].get("plan_id") == FX_PLAN_ID, (
        f"the entry is about another plan: {_summary(report)}"
    )
    assert entries[0].get("dismiss_button") is True, (
        f"the entry carries no 忽略 button: {_summary(report)}"
    )


# ---------------------------------------------------------------------------
# 2. 徽章上是中文
# ---------------------------------------------------------------------------
def test_badge_text(browser: str, world: World):
    """徽章上写的是中文，不是后端的状态原词。

    徽章有两样东西：**文字**（操作者读的那句）和 **class**（后端命名的
    那个状态）。这一份断言文字是「等待合并」，并且断言文字**不是**
    ``waiting_on_user_merge`` —— 只断言「徽章上有字」的话，把状态原词
    直接印上去也能过，而那对操作者毫无意义。
    """
    report = _drive(browser, world, "observe")

    entries = report.get("entries") or []
    assert entries, f"the column drew no entry: {_summary(report)}"
    badge_text = entries[0].get("badge_text") or ""
    badge_class = entries[0].get("badge_class") or ""

    assert badge_text == WAITING_MERGE_BADGE, (
        f"the badge does not read {WAITING_MERGE_BADGE!r}: "
        f"{_summary(report)}"
    )
    assert badge_text != WAITING_MERGE_STATE, (
        f"the badge shows the backend's state token instead of a word the "
        f"operator can read: {_summary(report)}"
    )
    assert WAITING_MERGE_STATE in badge_class, (
        f"the badge does not carry the state it is showing: "
        f"{_summary(report)}"
    )


# ---------------------------------------------------------------------------
# 3. 忽略要走确认，确认之后条目消失
# ---------------------------------------------------------------------------
def test_dismiss_confirm_flow(browser: str, world: World):
    """点「忽略」→ 弹层问原因 → 填了确认 → 条目从页面上消失。

    三件事都要成立：弹层**问过**（取消与确认是两条路），服务端**答了**
    200（请求是面板自己发的、带了请求守卫的头），条目**没了**。
    """
    report = _drive(browser, world, "dismiss")

    # The entry is gone from the page.
    assert report.get("entry_after_confirm") == 0, (
        f"the entry is still on the page after the dismissal: "
        f"{_summary(report)}"
    )
    assert report.get("entry_count") == 0, (
        f"the column still draws the dismissed entry: {_summary(report)}"
    )

    # The panel's own client wrote the dismissal, and the server took it.
    dismiss_requests = report.get("dismiss_requests") or []
    assert dismiss_requests, (
        f"the page never wrote the dismissal: {_summary(report)}"
    )
    assert dismiss_requests[-1].get("status") == 200, (
        f"the server refused the dismissal: {_summary(report)}"
    )

    # The panel's own requests carried the request guard's header — the
    # header the server refuses to answer without.
    api_requests = report.get("api_requests") or []
    assert api_requests, f"the page made no request at all: {_summary(report)}"
    unguarded = [
        entry for entry in api_requests if entry.get("guard") != "1"
    ]
    assert not unguarded, (
        f"the page made a request without the request guard's header: "
        f"{unguarded!r}"
    )

    # The page tells the operator what happened.
    notice = report.get("notice") or ""
    assert notice, (
        f"the panel said nothing after the dismissal: {_summary(report)}"
    )


# ---------------------------------------------------------------------------
# 4. 截图落在计划自己的目录里
# ---------------------------------------------------------------------------
def test_screenshot_saved(browser: str, world: World):
    """截图落在 ``plans/<plan_id>/screenshots/framework_fix_ui.png``。

    截图是**证据**：一次没有被拍下来的验收，等于没有验收过。所以这张图
    不在临时目录里凑合 —— 它落在计划自己的 ``screenshots/`` 目录里，
    和后端读的那棵计划树是同一棵。
    """
    report = _drive(browser, world, "observe")

    screenshot = world.screenshot_path()
    assert screenshot.exists(), (
        f"the panel left no screenshot at {screenshot}: {_summary(report)}"
    )
    assert screenshot.stat().st_size > 0, (
        f"the screenshot is empty: {screenshot}"
    )
    assert screenshot.read_bytes()[: len(PNG_MAGIC)] == PNG_MAGIC, (
        f"the file at {screenshot} is not a PNG: a screenshot has to be a "
        f"picture of the page, not a file that merely sits at that path"
    )
    assert screenshot.parent == (
        world.plans_dir / FX_PLAN_ID / "screenshots"
    ), (
        f"the screenshot landed outside the plan's own screenshots "
        f"directory: {screenshot}"
    )
    assert screenshot.name == SCREENSHOT_NAME, (
        f"the screenshot has another name: {screenshot}"
    )


# ---------------------------------------------------------------------------
# 边界一：没有框架修复数据 → 分区隐藏
# ---------------------------------------------------------------------------
def test_partition_hidden_without_data(browser: str, empty_world: World):
    """库里没有框架修复数据时，那一列不占地方。

    空分区必须**看不见** —— 一个空的框在那儿，调度器看上去就和这条 lane
    存在之前不一样了。这一条同时是别几条的负控：数据在的时候列是可见的，
    数据不在的时候列是不可见的，两边都对，才说明「可见」是数据带来的，
    不是页面本来就这样。
    """
    report = _drive(browser, empty_world, "empty")

    assert report.get("entry_count") == 0, (
        f"the column drew an entry out of an empty database: "
        f"{_summary(report)}"
    )
    assert report.get("panel_hidden_class") is True, (
        f"the empty column is on the screen: {_summary(report)}"
    )
    assert report.get("panel_visible") is False, (
        f"the empty column is visible: {_summary(report)}"
    )


# ---------------------------------------------------------------------------
# 边界二：弹层取消 → 条目仍在，而且一个请求都没发
# ---------------------------------------------------------------------------
def test_cancel_keeps_entry(browser: str, world: World):
    """弹层里点「取消」：条目还在，一个请求都没发出去。

    取消就是取消 —— 端点**不被调用**。所以这一条断言两件事：条目还在
    页面上，而且面板**没有**发过那个 POST。
    """
    report = _drive(browser, world, "cancel")

    assert report.get("entry_after_cancel") == 1, (
        f"the entry is not on the page after the cancellation: "
        f"{_summary(report)}"
    )
    assert report.get("entry_count") == 1, (
        f"the column drew {report.get('entry_count')!r} entries after the "
        f"cancellation; it must still draw the one that was there: "
        f"{_summary(report)}"
    )
    assert report.get("dismiss_requests") == [], (
        f"a cancelled question still wrote the dismissal: "
        f"{_summary(report)}"
    )


# ---------------------------------------------------------------------------
# 边界三：失败时截图与 DOM 照样落盘
# ---------------------------------------------------------------------------
def test_failure_leaves_screenshot_and_dom(browser: str, world: World):
    """一次**失败**的验收，也把截图与 DOM 留在盘上。

    这一条钉的是**证据本身**：验收失败的时候也得留下页面当时的样子，
    否则失败之后没有人能知道屏幕上到底有什么 —— 只知道「超时了」。

    所以这一跑是**故意失败**的：驱动把页面画出来（那一列在上面），然后去
    等一个永远不会出现的条目，等它放弃。判据是放弃之后盘上仍然有东西：
    一张真的图、一份真的 DOM —— 而且那两份证据说的是**画出来之后**的页面，
    不是一片空白。
    """
    report = _drive(
        browser,
        world,
        "fail",
        timeout_ms=FAILING_SCENARIO_TIMEOUT_MS,
        allow_failure=True,
    )

    # The run really did fail — otherwise this test would be proving
    # nothing about the failure path.
    assert report.get("failure"), (
        f"the run that was supposed to fail did not fail: "
        f"{_summary(report)}"
    )

    screenshot = world.screenshot_path()
    dom_dump = world.dom_path()

    assert screenshot.exists(), (
        f"a failed run left no screenshot at {screenshot}: "
        f"{_summary(report)}"
    )
    assert screenshot.stat().st_size > 0, (
        f"the screenshot is empty: {screenshot}"
    )
    assert screenshot.read_bytes()[: len(PNG_MAGIC)] == PNG_MAGIC, (
        f"the file at {screenshot} is not a PNG"
    )
    assert dom_dump.exists(), (
        f"a failed run left no DOM snapshot at {dom_dump}: "
        f"{_summary(report)}"
    )
    dom_text = dom_dump.read_text(encoding="utf-8")
    assert dom_text.strip(), f"the DOM snapshot is empty: {dom_dump}"
    # The DOM snapshot is the page itself — the column's own hooks are
    # in it, which is what makes it evidence rather than a file.
    assert 'id="framework-fix-panel"' in dom_text, (
        f"the DOM snapshot does not carry the column: {dom_dump}"
    )
    # The evidence is of the page **as it was drawn**: the entry is in
    # it, so the snapshot is worth having.
    assert f'data-plan-id="{FX_PLAN_ID}"' in dom_text, (
        f"the DOM snapshot does not carry the entry: {dom_dump}"
    )
