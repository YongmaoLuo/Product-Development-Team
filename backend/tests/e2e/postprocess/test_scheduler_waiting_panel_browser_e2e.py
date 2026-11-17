"""浏览器端到端验收：调度面板的"等待中"表格与三个操作。

这一份是收尾阶段等待链路的**最后一道验收**（test 决策点 9，E2E 决策点 B）：
前面所有用例都在进程内断言读模型、进度文件、报告和状态库，这一份换成一个
**真的浏览器**去点面板上的按钮 —— 问题原文在单元格里看得见吗？延长点下去
剩余时间动了吗？终止点下去行消失了吗、plan 到终态了吗？在行里填一个答案
提交出去，plan 会继续吗？

链路上没有一处是假的
--------------------
* 服务端是**真的进程**（一个子进程，端口是空的那个才用，先等就绪探针）；
* 三个等待是**生产注册表**登记的（``postprocess.waits.WaitRegistry``），
  落在 plan 自己的进度文件里；
* 面板读的是**生产的读模型**（``plan_scheduler.snapshot.build``），由
  ``GET /api/scheduler/state`` 端点端出去；
* 三个操作走的是**生产的路由**：延长与终止是调度器自己的等待端点，回答
  复用既有 HITL 的回答端点；
* 浏览器是**真的 Chromium**（Playwright），点的是操作员点的那些按钮。

两处生产缺口在这一份里是**绕过去**而不是**改掉**的（改它们不在本任务的
文件范围内），绕法都写在各自的注释里：

1. 服务端自己的收尾流程没有把等待登记进 HITL（``verification_loop`` 构造
   编排器时没有给 ``waits=``），所以等待由测试用生产注册表登记；登记之后
   服务端**看得见**它 —— 延长期限与终止的路由读的是同一条持久化记录；
2. 回答端点**不结算**收尾阶段的等待记录：``resolve_human_wait`` 在生产里
   没有调用者。面板的回答走了真的 HITL 路由（HITL 表里的那一行变成
   ``answered``），等待记录的结算由本文件用生产注册表完成 —— 行因此从
   表格里消失，而收尾阶段继续往下走。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

#: ``backend/`` —— 仓库自己的布局，不写死任何机器上的路径。
BACKEND_DIR = Path(__file__).resolve().parents[3]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 兄弟用例的浏览器发现与驱动环境：仓库里已经有的那一份，不重写。
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (  # noqa: E402
    _driver_env,
    _playwright_interpreter,
)


def _load_module(name: str, filename: str):
    """Load one of the sibling E2E modules by path.

    The modules are loaded by path rather than copied: the boot
    contract (task 24) and the requirement book's item shape (task 25)
    have to be **the same** contract, and a copy of one would be a
    second contract that could drift away from the first.

    Args:
        name: The name the module is loaded under.
        filename: The file's name, inside ``tests/e2e/postprocess/``.

    Returns:
        The loaded module.
    """
    path = BACKEND_DIR / "tests" / "e2e" / "postprocess" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, (
        f"the {filename} module could not be loaded from {path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Task 24's module —— 真服务器的启动契约。
lifecycle = _load_module(
    "pdt_postprocess_lifecycle_e2e", "test_postprocess_lifecycle_e2e.py"
)

#: Task 25's module —— 要求书条目形状。
waiting = _load_module(
    "pdt_postprocess_waiting_e2e", "test_postprocess_waiting_e2e.py"
)


#: 截图存档的根目录。  这是证据的落点，不是任何输入：仓库里没有任何东西
#: 依赖它，路径本身也可以由环境变量改到别处。
E2E_ROOT: Path = Path(os.environ.get("PDT_PP_E2E_ROOT", "/tmp/pp-e2e"))

#: 等一个人的 plan —— 操作员在面板上回答它的那一条。
HUMAN_PLAN = "p1"
HUMAN_ITEM = "approve"
HUMAN_QUESTION = "批准合入？"

#: 等外部系统的 plan —— 没有人能回答它，所以面板不给它回答框。
EXTERNAL_PLAN = "p2"
EXTERNAL_ITEM = "await-delivery"
EXTERNAL_QUESTION = "等待外部系统确认交付已经完成"

#: 即将到期的 plan —— 它的截止时间被放得很近，面板画的是"分"而不是"小时"。
EXPIRING_PLAN = "p3"
EXPIRING_ITEM = "confirm"
LONG_QUESTION = (
    "请确认本次收尾的要求书是否全部满足：所有指标回归通过、审计三元组完整、"
    "失败通知恰好发出一次，并且在裸仓里真的多出被批准的那条分支，"
    "另外还要确认收尾阶段的报告里每一条都带着它的证据引用。"
)

#: 即将到期的那个等待离截止还有多少秒。  面板按分钟画它，所以剩下的时间
#: 必须是"分"级的 —— 一周的等待画出来是天级的，两者是两种形状。
EXPIRING_REMAINING_SECONDS = 300

#: 操作员在等待行里填的那个答案。
ANSWER = "可以合入"

#: 等待回答被结算之后，收尾阶段最多花多久跑到终态。
STAGE_TIMEOUT_SECONDS = 180.0

#: 面板重新读一次读模型的间隔（前端自己的 5 秒）。  轮询一次之后的读数
#: 才是"轮询没有把行弄丢"的那一份。
POLL_SECONDS = 6.5

#: 面板画"分钟"时用的形状（``schedulerRemainingText``）：``4分59秒`` 或者
#: ``4分``。  一周的等待画出来是天级的 —— 两种形状不一样，所以"即将到期"
#: 那一行是可以认出来的。
re_minutes = re.compile(r"^\d+(分\d+秒|分)$")


# ---------------------------------------------------------------------------
# The requirement books
# ---------------------------------------------------------------------------


def _book_human() -> Dict[str, Any]:
    """等一个人的 plan 的要求书。

    等待的那一条的命令是一条**普通的对外动作**（把被批准的分支推进裸仓），
    不是执行器的待批准形状 —— 所以人回答之后它真的能跑，而它推进的那条
    分支就是"回答唤醒了收尾阶段"的证据。
    """
    return {
        "items": [
            waiting._item(
                HUMAN_ITEM,
                "publish the approved branch",
                ["git", "push", "origin", "HEAD:refs/heads/approved"],
                "approved",
                wait={"type": "human"},
            ),
            waiting._item(
                "read-head",
                "read the head commit",
                ["git", "rev-parse", "--verify", "HEAD"],
                "HEAD",
            ),
        ]
    }


def _book_external() -> Dict[str, Any]:
    """等外部系统的 plan 的要求书。

    外部等待必须说清楚自己复查什么（``postprocess/spec.py`` 拒绝一个没有
    ``check`` 的外部等待）。  这里复查的东西一直不来，而它背后的命令读的
    是一个不存在的引用 —— 所以这个 plan 永远等不到，也永远完不成。
    """
    return {
        "items": [
            waiting._item(
                EXTERNAL_ITEM,
                "wait for the delivery to arrive",
                ["git", "rev-parse", "--verify", "refs/heads/delivered"],
                "refs/heads/delivered",
                wait={"type": "external", "check": "delivery"},
            )
        ]
    }


def _book_expiring() -> Dict[str, Any]:
    """即将到期的 plan 的要求书。

    这一条的命令读的是一个**不存在的引用**：操作员等的是"交付分支已经
    发布"这件事，而它没有发布。所以终止这一条之后，收尾阶段重试它一次
    仍然失败，plan 的终态是 ``postprocess_failed`` —— 一个被放弃的动作
    不可能让 plan 完成。
    """
    return {
        "items": [
            waiting._item(
                EXPIRING_ITEM,
                "confirm the delivery branch was published",
                ["git", "rev-parse", "--verify", "refs/heads/never-published"],
                "refs/heads/never-published",
                wait={"type": "human"},
            )
        ]
    }


# ---------------------------------------------------------------------------
# The world: three plans, one real server, three waits
# ---------------------------------------------------------------------------


class _PanelWorld(lifecycle._Harness):
    """三个 plan、一个真服务器、三条等待。

    启动契约是任务 24 的那一份（同样的变量、同样的就绪探针、同样的进程
    形状）；唯一被改写的是子进程里 ``PYTEST_CURRENT_TEST`` 写的名字 ——
    那是产品自己回答"这个进程可以碰这台机器吗"的凭据，它得写着启动它的
    这个文件。
    """

    def __init__(self, work: Path) -> None:
        super().__init__(work, _book_human())
        self._stores: List[Any] = []
        self._registries: Dict[str, Any] = {}

    # -- setup ---------------------------------------------------------

    def _books(self) -> Dict[str, Dict[str, Any]]:
        """三个 plan 各自的要求书。"""
        return {
            HUMAN_PLAN: _book_human(),
            EXTERNAL_PLAN: _book_external(),
            EXPIRING_PLAN: _book_expiring(),
        }

    def prepare(self) -> None:
        """写出三个 plan 的目录、它们的状态行，以及它们共用的 git 工作区。

        三个 plan 共用一个工作区（一个裸仓）：它们各自的命令都是只读或者
        推一条**别的**分支，所以它们不会互相踩到。
        """
        for plan_id, book in self._books().items():
            self.plan_id = plan_id
            self.plan_dir = self.work / "plans" / plan_id
            self.book = book
            self.delivery_file = self.work / f"channel-delivery-{plan_id}.jsonl"
            self.plan_dir.mkdir(parents=True, exist_ok=True)
            self._write_baseline_files()
            self._write_requirement_book()
            self._write_notification_channel()
            self._seed_state()
        self._make_git_workspace()

        # 默认的 plan 是等一个人的那一个：收尾阶段的断言读的是它。
        self.plan_id = HUMAN_PLAN
        self.plan_dir = self.work / "plans" / HUMAN_PLAN
        self.book = _book_human()

    def start(self) -> None:
        """启动真服务器 —— 任务 24 的契约，写着这个文件的名字。

        契约本身是任务 24 的：同样的变量（plans 树、状态库、端口、provider
        模式、孤儿回收、验证档位、``gh`` 桩的 PATH、``backend/`` 的
        PYTHONPATH），同样的就绪探针（``GET /api/health`` 返回 200 才算
        起来），同样的进程形状（``start_new_session=True``）。
        """
        self.prepare()

        port = lifecycle._free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        env = dict(os.environ)
        env.update(
            {
                "PDT_PLANS_DIR": str(self.work / "plans"),
                "PDT_STATE_DB_PATH": str(self.work / "state.db"),
                "PDT_PORT": str(port),
                "PDT_PROVIDER_MODE": "inherit",
                "PDT_ORPHAN_REAPER": "0",
                "PDT_DEBUG_ROUTES": "1",
                "VERIFICATION_PROFILE": "dry_run",
                "PATH": (
                    f"{lifecycle.GH_STUB_BIN}{os.pathsep}{env.get('PATH', '')}"
                ),
                "PYTHONPATH": (
                    f"{lifecycle.BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}"
                ),
                "PYTEST_CURRENT_TEST": (
                    "backend/tests/e2e/postprocess/"
                    "test_scheduler_waiting_panel_browser_e2e.py (setup)"
                ),
                "GH_CALLS_JSONL": str(self.gh_calls_file),
            }
        )
        # 一个陈旧的秘密描述符不能被一个从来没有拥有它的进程关掉。
        for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
            env.pop(key, None)
        env.pop("ANTHROPIC_BASE_URL", None)

        with self.log_path.open("wb") as handle:
            self.proc = subprocess.Popen(
                [str(lifecycle.VENV_PYTHON), "-m", "backend.server"],
                cwd=str(lifecycle.PROJECT_ROOT),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._wait_ready()

    # -- the waiting lifecycle -----------------------------------------

    def registry(self, plan_id: str) -> Any:
        """一个 plan 的生产等待生命周期。

        注册表是生产的那一个：同一个类、同一个 HITL 表（服务端回答端点
        写的就是这张表）、同一个通道（plan 自己的 ``hitl_config.json``
        绑定）。

        Args:
            plan_id: 等待属于哪个 plan。

        Returns:
            注册表。
        """
        from config_paths import resolve_state_db_path
        from hitl.store import HitlStore
        from postprocess.waits import WaitRegistry

        store = HitlStore(resolve_state_db_path())
        self._stores.append(store)
        registry = WaitRegistry(
            plan_id,
            store=store,
            deliver=self._deliverer(plan_id),
        )
        self._registries[plan_id] = registry
        return registry

    def _deliverer(self, plan_id: str):
        """把一个问题送进某个 plan 自己的通道。

        这是生产的投递：既有的 hook dispatcher、plan 自己的
        ``hitl_config.json`` 绑定、既有 HITL 出路携带的同一个事件。
        """
        from hitl.service import EVENT_DECISION_CREATED
        from hooks.dispatcher import dispatch

        plan_dir = self.work / "plans" / plan_id

        def _deliver(payload: Dict[str, Any]) -> None:
            dispatch(EVENT_DECISION_CREATED, payload, plan_dir)

        return _deliver

    def pin_fingerprint(self, plan_id: str, item_id: str, status: str) -> None:
        """在进度文件里写下要求书的规范化指纹。

        收尾阶段只在进度文件里的指纹等于它冻结的那本书的指纹时才读这个
        文件（``orchestrator._progress_statuses``），而注册表写进去的是
        要求书**字节**的指纹（``progress._spec_fingerprint``）—— 两者不是
        同一个值。所以指纹由测试用生产的 ``progress.mark`` 写进去，写法
        与收尾阶段自己写它时一模一样。

        Args:
            plan_id: 等待属于哪个 plan。
            item_id: 哪一条条目。
            status: 这一条的状态。
        """
        from postprocess.progress import mark
        from postprocess.spec import freeze, load_spec

        frozen = freeze(load_spec(self.work / "plans" / plan_id))
        mark(plan_id, item_id, status, spec_sha256=frozen.spec_sha256)

    def register_waits(self) -> Dict[str, Dict[str, Any]]:
        """登记三条等待，并把即将到期的那一条的截止时间放近。

        登记走的是生产注册表；截止时间走的是生产路由（面板自己调整截止
        时间用的就是它），所以面板读到的剩余时间是服务端算出来的那一份。

        Returns:
            每条等待的记录，按 plan 分。
        """
        human = self.registry(HUMAN_PLAN)
        human_record = human.create_human_wait(
            {"id": HUMAN_ITEM, "wait": {"type": "human"}}, HUMAN_QUESTION
        )
        self.pin_fingerprint(HUMAN_PLAN, HUMAN_ITEM, "waiting")

        external = self.registry(EXTERNAL_PLAN)
        external_record = external.create_external_wait(
            {
                "id": EXTERNAL_ITEM,
                "action": "wait for the delivery to arrive",
                "wait": {"type": "external", "check": "delivery"},
            },
            lambda: False,
            question=EXTERNAL_QUESTION,
        )
        self.pin_fingerprint(EXTERNAL_PLAN, EXTERNAL_ITEM, "waiting")

        expiring = self.registry(EXPIRING_PLAN)
        expiring_record = expiring.create_human_wait(
            {"id": EXPIRING_ITEM, "wait": {"type": "human"}}, LONG_QUESTION
        )
        self.pin_fingerprint(EXPIRING_PLAN, EXPIRING_ITEM, "waiting")

        status, body = self.http(
            "PATCH",
            f"/api/scheduler/{EXPIRING_PLAN}/waits/{EXPIRING_ITEM}/deadline",
            {"remaining_seconds": EXPIRING_REMAINING_SECONDS},
        )
        assert status == 200, (
            f"the deadline could not be set on the wait: {status} {body!r}"
        )

        return {
            HUMAN_PLAN: human_record,
            EXTERNAL_PLAN: external_record,
            EXPIRING_PLAN: expiring_record,
        }

    # -- driving -------------------------------------------------------

    def http(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """对跑着的服务端发一次 HTTP 请求。

        Args:
            method: HTTP 方法。
            path: 路径。
            body: 请求体（有的时候）。

        Returns:
            ``(状态码, 解码后的回答)``。
        """
        return lifecycle._http_json(method, f"{self.base_url}{path}", body)

    def scheduler_state(self) -> Dict[str, Any]:
        """面板读的那份读模型。"""
        status, body = self.http("GET", "/api/scheduler/state")
        assert status == 200, f"the state endpoint answered {status}: {body!r}"
        assert isinstance(body, dict), f"the read model is not an object: {body!r}"
        return body

    def waiting_rows(self) -> List[Dict[str, Any]]:
        """读模型里的等待行。"""
        rows = self.scheduler_state().get("waiting")
        assert isinstance(rows, list), f"the read model carries no waiting list: {rows!r}"
        return rows

    def start_verification(self, plan_id: str) -> Dict[str, Any]:
        """像操作员那样，通过 API 启动一个 plan 的验证轮次。

        Args:
            plan_id: 启动哪个 plan。

        Returns:
            启动的回答。
        """
        status, body = self.http(
            "POST", f"/api/verification/{plan_id}/start", {"auto_fix": True}
        )
        assert status == 200, f"the start was refused: {status} {body!r}"
        assert isinstance(body, dict), f"the start answered {body!r}"
        return body

    def wait_for_report(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """等收尾阶段写下终态报告。

        Args:
            plan_id: 等哪个 plan。

        Returns:
            报告，或者预算用完之后还是 ``None``。
        """
        report_path = self.work / "plans" / plan_id / "postprocess_report.json"

        def _report() -> Optional[Dict[str, Any]]:
            report = lifecycle._read_json(report_path)
            if report and report.get("terminal_status"):
                return report
            return None

        return lifecycle._wait_for(_report, STAGE_TIMEOUT_SECONDS)

    def report(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """某个 plan 的终态报告。"""
        report = lifecycle._read_json(
            self.work / "plans" / plan_id / "postprocess_report.json"
        )
        return report if isinstance(report, dict) else None

    def close(self) -> None:
        """把这份 harness 开过的东西都还回去。"""
        for store in self._stores:
            try:
                store.close()
            except Exception:  # noqa: BLE001 — 这个 store 是我们的
                pass
        self._stores.clear()


# ---------------------------------------------------------------------------
# The browser driver — the panel, as an operator uses it
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = r'''
"""Drive the panel's waiting table the way an operator drives it.

Arguments:
  1. the base url of the running server
  2. the path the screenshot is written to
  3. a JSON object: the mode, the row to act on, the answer, the budget

What it does, in the operator's own order: open the scheduling view,
read the waiting table, act on one row (延长 / 终止 / 回答), read the
table again, and leave a screenshot behind.

Every request the panel's own client makes is watched on its way out,
and the browser's own complaints — a page error, an unhandled promise
rejection — are collected as they happen.

One JSON document is printed on stdout: what the table drew, what the
panel's requests carried, and what the browser complained about.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GUARD_HEADER = "x-pdt-request"

#: The table the panel draws the waiting rows in.
BODY = "#scheduler-waiting-body"


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


def read_rows(page):
    """The waiting rows, read in one go.

    The panel redraws its table whenever it reads the model again, so a
    row that was located a moment ago may be gone by the time it is
    read.  Reading the whole table in one evaluation avoids that race:
    what is read is what is on the page at that moment.

    The rows are the table's own ``tr`` elements — the ones that carry
    the plan and the item they belong to.  A table that has been
    emptied reads as no rows at all, which is what an operator sees.
    """
    return page.evaluate(
        """(selector) => Array.from(document.querySelectorAll(selector)).map(row => {
            const cells = Array.from(row.querySelectorAll('td')).map(
                (cell) => (cell.innerText || '').trim()
            );
            const question = row.querySelector('.scheduler-waiting-question');
            const input = row.querySelector('input.scheduler-waiting-answer');
            return {
                plan_id: row.getAttribute('data-plan-id') || '',
                item_id: row.getAttribute('data-item-id') || '',
                cells: cells,
                remaining: cells.length > 3 ? cells[3] : '',
                question_text: question ? (question.innerText || '').trim() : '',
                question_title: question ? (question.getAttribute('title') || '') : '',
                actions: Array.from(row.querySelectorAll('button')).map(
                    (button) => button.getAttribute('data-action') || ''
                ),
                has_answer_input: Boolean(input),
            };
        })""",
        BODY + " tr",
    )


def find_row(rows, plan_id, item_id):
    """The row of one waiting item, or None."""
    for row in rows:
        if row.get("plan_id") == plan_id and row.get("item_id") == item_id:
            return row
    return None


def row_selector(plan_id, item_id):
    """The CSS selector of one row."""
    return (
        BODY + ' tr[data-plan-id="' + plan_id + '"][data-item-id="' + item_id + '"]'
    )


def action_selector(plan_id, item_id, action):
    """The CSS selector of one action button in one row."""
    return row_selector(plan_id, item_id) + ' button[data-action="' + action + '"]'


url = sys.argv[1]
screenshot = Path(sys.argv[2])
options = json.loads(sys.argv[3])
mode = options.get("mode", "read")
plan_id = options.get("plan_id", "")
item_id = options.get("item_id", "")
answer = options.get("answer", "")
budget = float(options.get("timeout", 120))
settle = float(options.get("settle", 0))

screenshot.parent.mkdir(parents=True, exist_ok=True)

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

page_errors = []
requests = []

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1400, "height": 1000})

    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.add_init_script(
        """
        window.__pdtUnhandledRejections = [];
        window.addEventListener('unhandledrejection', function (event) {
            const reason = event.reason;
            window.__pdtUnhandledRejections.push(
                reason && reason.message ? String(reason.message) : String(reason)
            );
        });
        """
    )

    def record(route, request):
        """Watch every request the panel's own client makes."""
        requests.append({
            "url": request.url,
            "method": request.method,
            "guard": request.headers.get(GUARD_HEADER),
        })
        route.continue_()

    page.route("**/api/**", record)

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)

    # The operator opens the scheduling view.
    page.click("#btn-open-scheduler", timeout=30000)
    page.wait_for_selector("#scheduler-view:not(.hidden)", timeout=30000)

    # The waiting table draws its rows.
    page.wait_for_selector(BODY + " tr", timeout=int(budget * 1000))
    rows_before = read_rows(page)

    remaining_before = ""
    if mode != "read":
        row = find_row(rows_before, plan_id, item_id)
        if row is None:
            raise AssertionError(
                "the row to act on is not on the page: {0!r}".format(rows_before)
            )
        remaining_before = row.get("remaining", "")
        if mode == "answer":
            page.fill(
                row_selector(plan_id, item_id) + " input.scheduler-waiting-answer",
                answer,
            )
        page.click(action_selector(plan_id, item_id, mode), timeout=30000)

    # The wait: what the operator expects to see after the action.
    #
    # A plain reading has nothing to wait for — the second reading is
    # taken after the settle below, once the panel has polled once.  The
    # three actions have something to wait for, and each of them has its
    # own "it has happened" reading: the remaining time moved, or the
    # row is gone.
    deadline = time.time() + budget
    rows_after = rows_before
    while mode != "read":
        if mode == "extend":
            row = find_row(rows_after, plan_id, item_id)
            if row is not None and row.get("remaining", "") != remaining_before:
                break
        elif mode in ("terminate", "answer"):
            if find_row(rows_after, plan_id, item_id) is None:
                break
        if time.time() >= deadline:
            break
        page.wait_for_timeout(300)
        if mode == "answer":
            # The row leaves the table when the wait is settled, and the
            # settle happens outside the browser — so the panel is asked
            # to read its model again rather than waiting for the tick.
            page.click("#btn-refresh-scheduler", timeout=30000)
            page.wait_for_timeout(300)
        rows_after = read_rows(page)

    # A plain reading waits out one poll, so the second reading is the
    # one that says the panel's own timer did not lose the rows.
    if settle:
        page.wait_for_timeout(int(settle * 1000))
        rows_after = read_rows(page)

    body_text = page.inner_text(BODY)
    unhandled = page.evaluate("window.__pdtUnhandledRejections || []")
    page.screenshot(path=str(screenshot), full_page=True)

    print(json.dumps({
        "rows": rows_before,
        "rows_after": rows_after,
        "body_text": body_text,
        "requests": requests,
        "page_errors": page_errors,
        "unhandled_rejections": unhandled,
        "screenshot": str(screenshot),
    }, ensure_ascii=False))
    browser.close()
'''


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def interpreter():
    """一个能开真浏览器的解释器 —— 或者一次跳过。

    这台机器有浏览器才付得起后面的钱：没有浏览器就不启动服务端、不登记
    等待、用例明说面板没有被驱动过。一个没有被驱动的面板什么都证明不了，
    而假装驱动过的用例比没有用例更糟。
    """
    found = _playwright_interpreter()
    if found is None:
        pytest.skip(
            "这个环境里没有装 Playwright 驱动；面板无法被驱动，"
            "没有被驱动的面板什么都证明不了"
        )
    return found


@pytest.fixture
def world(tmp_path):
    """一个真服务器、三个 plan、三条等待。

    服务器在用例结束的时候被停掉：端口还回来了，一个中途失败的用例也
    没有把端口留在外面。
    """
    the_world = _PanelWorld(tmp_path)
    try:
        the_world.start()
        the_world.register_waits()
        yield the_world
    finally:
        the_world.stop()
        the_world.close()


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _last_document(text: str) -> Dict[str, Any]:
    """``text`` 里的最后一个 JSON 文档。

    驱动脚本打印一份文档；浏览器自己的 chatter 可能待在它旁边，所以最后
    一行能解析的就是那份报告。
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
    raise AssertionError(f"the driver printed no report: {text!r}")


def _drive(
    world: _PanelWorld,
    interpreter: str,
    *,
    mode: str,
    screenshot: Path,
    plan_id: str = "",
    item_id: str = "",
    answer: str = "",
    settle: float = 0.0,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """在真浏览器里驱动面板，把它的报告带回来。

    Args:
        world: 跑着的服务端。
        interpreter: 能开浏览器的解释器。
        mode: ``read`` / ``extend`` / ``terminate`` / ``answer``。
        screenshot: 截图写到哪里。
        plan_id: 操作的是哪个 plan 的行。
        item_id: 操作的是哪一条。
        answer: 填进回答框的字。
        settle: 读完之后的等待（秒）—— 让面板自己轮询一次。
        timeout: 这一次驱动的预算（秒）。

    Returns:
        驱动的报告。
    """
    script = world.work / "drive_waiting_panel.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{world.base_url}/",
            str(screenshot),
            json.dumps(
                {
                    "mode": mode,
                    "plan_id": plan_id,
                    "item_id": item_id,
                    "answer": answer,
                    "settle": settle,
                    "timeout": timeout,
                }
            ),
        ],
        capture_output=True,
        text=True,
        timeout=timeout + 120,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if (
            "Executable doesn't exist" in combined
            or "playwright install" in combined
            or "BrowserType.launch" in combined
        ):
            pytest.skip(
                "Playwright 驱动装上了，但它的浏览器二进制没装；面板无法被驱动"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return _last_document(result.stdout)


def _row_of(
    rows: List[Dict[str, Any]],
    plan_id: str,
    item_id: str,
) -> Optional[Dict[str, Any]]:
    """某一行的记录，或者 ``None``。"""
    for row in rows:
        if row.get("plan_id") == plan_id and row.get("item_id") == item_id:
            return row
    return None


def _remaining_seconds(
    world: _PanelWorld,
    plan_id: str,
    item_id: str,
) -> int:
    """读模型里某一条等待还剩多少秒。

    面板画的是这个数字的另一种写法；这里读的是秒，所以"延长了一小时"
    是可以被量出来的，而不只是一格字换了说法。
    """
    for row in world.waiting_rows():
        if row.get("plan_id") == plan_id and row.get("item_id") == item_id:
            return int(row.get("remaining_seconds") or 0)
    raise AssertionError(f"there is no waiting row for {plan_id}/{item_id}")


def _settle_answered_wait(
    plan_id: str,
    item_id: str,
    timeout: float,
) -> bool:
    """把被回答的那条等待结算掉。

    面板的回答走的是真的 HITL 路由，所以 HITL 表里的那一行变成
    ``answered``。  但**回答端点不结算收尾阶段自己的等待记录** ——
    ``resolve_human_wait`` 在生产里没有调用者（这是本文件绕过去的第二处
    生产缺口）。

    所以结算在这里做，用的是生产注册表：等到决策行说"回答了"，就把那个
    回答交给注册表。  注册表看到决策已经被回答，就**采纳**它（而不是写第
    二次），然后把结算过的等待写回进度文件 —— 面板的行正是从那条记录
    建出来的，所以它下一次读的时候，这一行就不在了。

    这条等待的注册表和 HITL 表都是这条线程自己开的：SQLite 的连接不能
    跨线程用。

    注册表是**重建**出来的：等待活在内存里，而一条等待比问出它的那个进程
    活得久 —— 所以注册表先从磁盘上的记录里放回去，放的方式和调度器自己
    的端点一样（``routes.scheduler`` 的 ``_wait_records`` + ``_registry_of``），
    然后才谈得上结算它。

    Args:
        plan_id: 等待属于哪个 plan。
        item_id: 哪一条等待。
        timeout: 最多等多久（秒）。

    Returns:
        等待有没有被结算掉。
    """
    from config_paths import resolve_state_db_path
    from hitl.store import HitlStore
    from routes.scheduler import _registry_of, _wait_records

    store = HitlStore(resolve_state_db_path())
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for decision in store.list_by_plan(plan_id):
                if decision.task_id != item_id:
                    continue
                if decision.status != "answered":
                    continue
                registry = _registry_of(
                    plan_id, _wait_records(plan_id), store=store
                )
                registry.resolve_human_wait(item_id, decision.answer)
                return True
            time.sleep(0.2)
        return False
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_waiting_table_shows_question_text(interpreter, world):
    """三个等待各画成一行，问题原文看得见，长问题在 ``title`` 里是完整的。

    这一条钉的是面板的**呈现契约**（arch 决策点 6）：行是行、问题是问题、
    类型是类型、剩余时间是剩余时间。  断言读的是页面上的文字，不是 HTML
    字符串 —— 截断后的那一格也得是真的看得见的字。

    长问题被截断时 ``title`` 里仍然是完整的原文：一条操作员读不全的问题
    是没有人能回答的问题。
    """
    document = _drive(
        world,
        interpreter,
        mode="read",
        screenshot=E2E_ROOT / "waiting-table.png",
        settle=POLL_SECONDS,
    )

    rows = document["rows"]
    assert len(rows) == 3, f"the table drew {len(rows)} rows instead of three: {rows!r}"

    keys = [(row["plan_id"], row["item_id"]) for row in rows]
    assert len(set(keys)) == 3, f"the table drew a row twice: {keys!r}"
    assert (HUMAN_PLAN, HUMAN_ITEM) in keys, f"the human wait is missing: {keys!r}"
    assert (EXTERNAL_PLAN, EXTERNAL_ITEM) in keys, (
        f"the external wait is missing: {keys!r}"
    )
    assert (EXPIRING_PLAN, EXPIRING_ITEM) in keys, (
        f"the expiring wait is missing: {keys!r}"
    )

    body_text = document["body_text"]

    # --- the question is on the page, in the operator's own words ---------
    human = _row_of(rows, HUMAN_PLAN, HUMAN_ITEM)
    assert human is not None
    assert HUMAN_QUESTION in human["question_text"], (
        f"the question is not in the cell: {human!r}"
    )
    assert HUMAN_QUESTION in body_text, (
        f"the question is not on the page: {body_text!r}"
    )

    # --- the three operations are on the row ------------------------------
    assert set(human["actions"]) == {"extend", "terminate", "answer"}, (
        f"the row does not carry the three operations: {human!r}"
    )
    assert human["has_answer_input"] is True, (
        f"a human wait has no answer box: {human!r}"
    )

    # --- an external wait has nobody to answer it -------------------------
    external = _row_of(rows, EXTERNAL_PLAN, EXTERNAL_ITEM)
    assert external is not None
    assert external["has_answer_input"] is False, (
        f"an external wait was given an answer box: {external!r}"
    )
    assert "answer" not in external["actions"], (
        f"an external wait was given an answer button: {external!r}"
    )

    # --- the long question: shortened for the eye, whole in the title -----
    assert len(LONG_QUESTION) > 80, "the long question is not long any more"
    expiring = _row_of(rows, EXPIRING_PLAN, EXPIRING_ITEM)
    assert expiring is not None
    assert expiring["question_text"] != LONG_QUESTION, (
        "the cell was supposed to shorten the question"
    )
    assert expiring["question_title"] == LONG_QUESTION, (
        f"the title does not carry the whole question: {expiring!r}"
    )
    assert LONG_QUESTION[:40] in body_text, (
        f"the start of the long question is not on the page: {body_text!r}"
    )

    # --- the expiring wait is drawn in minutes, not in days ---------------
    assert re_minutes.match(expiring["remaining"]), (
        f"the expiring wait is not drawn in minutes: {expiring!r}"
    )
    assert not re_minutes.match(human["remaining"]), (
        f"a week-long wait is drawn in minutes: {human!r}"
    )

    # --- a poll does not lose the rows ------------------------------------
    after = document["rows_after"]
    assert len(after) == 3, f"the table lost rows after a poll: {after!r}"
    assert {(row["plan_id"], row["item_id"]) for row in after} == set(keys), (
        f"the table changed after a poll: {after!r}"
    )

    # --- the screenshot is on disk -----------------------------------------
    assert Path(document["screenshot"]).is_file(), (
        f"the screenshot was not written: {document['screenshot']!r}"
    )


def test_extend_button_updates_remaining_time(interpreter, world):
    """点"延长"之后，行里的剩余时间动了。

    断言读的是**面板画出来的那一格**：操作员看到的是那一格，不是读模型里
    的秒数。  顺带把读模型也读一次 —— 延长确实把截止时间推远了整整一个
    小时，而不只是让某一格换了个说法。
    """
    before_seconds = _remaining_seconds(world, HUMAN_PLAN, HUMAN_ITEM)
    assert before_seconds > 0, "the wait had no time left to extend"

    document = _drive(
        world,
        interpreter,
        mode="extend",
        plan_id=HUMAN_PLAN,
        item_id=HUMAN_ITEM,
        screenshot=E2E_ROOT / "extend.png",
    )

    before = _row_of(document["rows"], HUMAN_PLAN, HUMAN_ITEM)
    after = _row_of(document["rows_after"], HUMAN_PLAN, HUMAN_ITEM)
    assert before is not None and after is not None, (
        f"the row is not on the page: {document['rows']!r}"
    )
    assert after["remaining"] != before["remaining"], (
        f"the remaining time did not move: {before['remaining']!r} -> "
        f"{after['remaining']!r}"
    )

    # The read model says the same thing the panel drew, and says it in
    # seconds: the extension is one hour.
    after_seconds = _remaining_seconds(world, HUMAN_PLAN, HUMAN_ITEM)
    assert after_seconds - before_seconds >= 3500, (
        f"the deadline moved by {after_seconds - before_seconds}s instead of "
        "an hour"
    )

    # The panel's own request went out with the request guard's header.
    assert any(
        entry["method"] == "POST" and entry["url"].endswith("/extend")
        for entry in document["requests"]
    ), f"the extension never went out: {document['requests']!r}"


def test_terminate_removes_row_and_sets_terminal(interpreter, world):
    """点"终止"之后，这一行不见了，plan 走到了终态。

    被放弃的动作不会让 plan 完成：它背后的命令读的是一个不存在的引用，
    所以收尾阶段重试它一次仍然失败，终态是 ``postprocess_failed``。
    """
    document = _drive(
        world,
        interpreter,
        mode="terminate",
        plan_id=EXPIRING_PLAN,
        item_id=EXPIRING_ITEM,
        screenshot=E2E_ROOT / "terminate.png",
    )

    assert _row_of(document["rows"], EXPIRING_PLAN, EXPIRING_ITEM) is not None, (
        f"the row was not on the page to begin with: {document['rows']!r}"
    )
    assert _row_of(document["rows_after"], EXPIRING_PLAN, EXPIRING_ITEM) is None, (
        f"the row is still on the page: {document['rows_after']!r}"
    )

    # The other two waits are untouched: giving up on one is not giving up
    # on all of them.
    assert _row_of(document["rows_after"], HUMAN_PLAN, HUMAN_ITEM) is not None, (
        f"the human wait disappeared too: {document['rows_after']!r}"
    )
    assert _row_of(document["rows_after"], EXTERNAL_PLAN, EXTERNAL_ITEM) is not None, (
        f"the external wait disappeared too: {document['rows_after']!r}"
    )

    # --- the plan reaches a terminal state ---------------------------------
    world.start_verification(EXPIRING_PLAN)
    report = world.wait_for_report(EXPIRING_PLAN)
    assert report is not None, (
        f"the finishing stage wrote no terminal report:\n{world.server_output()}"
    )
    assert report.get("terminal_status") == "postprocess_failed", (
        f"the stage did not end at postprocess_failed: {report!r}"
    )


def test_answer_flow_resumes_plan(interpreter, world):
    """在行里填一个答案提交出去：这一行不见了，plan 继续往下走。

    回答走的是真的 HITL 路由（HITL 表里那一行变成 ``answered``），等待记录
    的结算是生产注册表做的 —— 生产里回答端点不结算收尾阶段的等待记录，
    这是本文件绕过去的第二处生产缺口，注释在 ``_settle_answered_wait``
    上面。

    plan 继续往下走，是**服务端自己的流程**走出来的：验证轮次由服务端启动，
    收尾阶段跑完之后裸仓里多出被批准的那条分支。
    """
    settler = threading.Thread(
        target=_settle_answered_wait,
        args=(HUMAN_PLAN, HUMAN_ITEM, 120.0),
        daemon=True,
    )
    settler.start()

    document = _drive(
        world,
        interpreter,
        mode="answer",
        plan_id=HUMAN_PLAN,
        item_id=HUMAN_ITEM,
        answer=ANSWER,
        screenshot=E2E_ROOT / "answer.png",
    )

    assert _row_of(document["rows"], HUMAN_PLAN, HUMAN_ITEM) is not None, (
        f"the row was not on the page to begin with: {document['rows']!r}"
    )
    assert _row_of(document["rows_after"], HUMAN_PLAN, HUMAN_ITEM) is None, (
        f"the row is still on the page: {document['rows_after']!r}"
    )

    # --- the answer really went through the panel's own client -------------
    assert any(
        entry["method"] == "POST" and entry["url"].endswith("/answer")
        for entry in document["requests"]
    ), f"the answer never went out: {document['requests']!r}"

    # --- the answer is on the record ---------------------------------------
    status, decisions = world.http("GET", f"/api/hitl/{HUMAN_PLAN}/decisions")
    assert status == 200, f"the history was refused: {status} {decisions!r}"
    answered = [
        decision
        for decision in decisions
        if decision.get("task_id") == HUMAN_ITEM
    ]
    assert len(answered) == 1, f"the history does not hold the question: {decisions!r}"
    assert answered[0].get("status") == "answered", (
        f"the decision is not answered: {answered[0]!r}"
    )
    assert answered[0].get("answer") == ANSWER, (
        f"the answer was not kept: {answered[0]!r}"
    )

    # --- the plan continues, on the server's own flow ----------------------
    world.start_verification(HUMAN_PLAN)
    report = world.wait_for_report(HUMAN_PLAN)
    assert report is not None, (
        f"the finishing stage wrote no terminal report:\n{world.server_output()}"
    )
    assert report.get("terminal_status") == "completed", (
        f"the stage did not complete: {report!r}"
    )

    # The branch the waiting item publishes is the proof that the answer
    # woke the stage up: nothing else could have put it there.
    assert "approved" in world.bare_branches(), (
        f"the bare repo holds {world.bare_branches()!r}, not the approved branch"
    )


def test_no_pageerror_or_unhandledrejection(interpreter, world):
    """浏览器没有报过错：一个页面级异常、一个未处理的 promise 拒绝都没有。

    面板自己会发出一些 404（例如某个页面在 plan 页面上没有的端点）——
    那是控制台里的一条记录，不是页面级的失败，而面板自己的客户端把它们
    都接住了。这里断言的是**浏览器报告的那两类失败**。

    面板开着的时候会自己轮询（每 5 秒一次），所以这一次读数是跨过一次
    轮询之后拿到的。
    """
    document = _drive(
        world,
        interpreter,
        mode="read",
        screenshot=E2E_ROOT / "no-pageerror.png",
        settle=POLL_SECONDS,
    )

    assert document["page_errors"] == [], (
        f"the browser reported page errors: {document['page_errors']!r}"
    )
    assert document["unhandled_rejections"] == [], (
        f"the browser reported unhandled rejections: "
        f"{document['unhandled_rejections']!r}"
    )

    # The panel really was talking to the server: it read the model at
    # least once, and every request it made carried the guard's header.
    state_requests = [
        entry for entry in document["requests"] if entry["url"].endswith("/state")
    ]
    assert state_requests, f"the panel never read the read model: {document['requests']!r}"
    assert all(entry["guard"] == "1" for entry in state_requests), (
        f"a request went out without the guard's header: {state_requests!r}"
    )
