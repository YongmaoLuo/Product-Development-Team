"""真后端关机重启的端到端验收：SIGTERM → 检查点 → 重启 → queued → 续跑。

前面几个任务各自改一处，各自有单测：

* 任务 1/2 —— 关机检查点的**写入器**与**判定**（``backend/shutdown_checkpoint.py``）；
* 任务 3 —— 关机路径在 kill 之前把检查点写到盘上；
* 任务 4 —— 开机恢复扫描把「关机中断」的行落成 ``stopped`` + 相位 ``queued``；
* 任务 5 —— verification 侧同一件事。

它们各自的单测都是**在进程内**跑的：写入器被直接调用，扫描被直接调用。
但真正要成立的那句话是「关机进程写下的检查点，被下一个进程的恢复扫描读到」
—— 这句话只有真起后端才验得了，因为写检查点的是**将要死掉的那个进程**，
读它的是**下一个进程**。两个进程之间唯一的通道是磁盘上的
``plans/<id>/pause_state.json`` 和 ``state.db``。

所以这一轮走的是真的：

* **真后端** —— ``backend.server`` 的真进程（不是 ``TestClient``），两次启动，
  两次都监听内核选的空闲端口，两次共用同一份 ``state.db`` 和同一棵计划树；
* **真信号** —— 第一次的进程收 ``SIGTERM``（``proc.terminate()``），
  走 uvicorn 的优雅关闭，于是 lifespan 的 ``finally`` 里那个
  ``_shutdown_all_executions()`` 真的跑了；
* **真浏览器** —— Playwright 驱动真 Chromium 打开真页面，看操作者看得见
  的是什么：计划列表里**没有**那条被停掉的计划（执行行的裁决是终局的），
  而**调度面板的队列**里列着它，带着可执行的操作。

唯一的替身仍然是模型（``PDT_LLM_MODE=scripted``，磁盘上的固定回答），
这跟同目录的 ``test_plan_lifecycle_ui_e2e.py`` 是同一条缝。

这个用例证明的那条链
--------------------

1. 第一次启动时，计划 ``shutdown-e2e`` 的 ``plan_execution`` 行写着
   ``running``，pid 指向一个**还活着**的占位进程 —— 所以开机扫描不会判它死，
   相位也不动；
2. ``SIGTERM`` 之后，第一段进程在退出前把检查点写下来：
   ``reason='server_shutdown'``、``kind='execution'``，
   ``unfinished_tasks`` 里是 t2（当时在飞）和 t3（还没做）；
3. 占位进程被回收（它不是执行器，服务器不认它，所以得**手动**杀）；
4. 第二次启动读到那份检查点，把行落成 ``stopped`` + ``server_shutdown`` +
   pid 为空，把相位落成 ``queued`` —— 不是 ``failed``；
5. 调度器看得见它（``PlanFactsReader.queued_plans()`` 里有它）；
6. **带终局计划的那份列表**里有它，相位是 ``queued``（带「排队中」这个标签）；
   页面上的**调度面板的队列**里也列着它，带着可执行的操作；
7. 再点一次开始，走的是**续跑**：``resumed=True``，``resume_plan`` 里有 t2，
   **没有 t1** —— 已经做完的任务不重跑。

与简报字面写法的几处偏差（都是**系统当前的真实行为**，不是这里放宽了
断言；改它们要改产品代码，不改测试）
--------------------------------------------------------------------------

1. **执行段的字段名。** 简报写的是 ``execution.status == 'running'``；这个
   接口给执行段的字段名是 ``execution_status``
   （``routes/execution.get_execution_progress`` 的返回体）。断言按接口
   真实的字段名写，两个名字都收（``_execution_status``）。
2. **停在 ``queued`` 的计划不在操作者的计划列表里。** 简报说重启后
   页面上那张写着「排队中」的卡片回来了。系统当前的真实行为不是这样：
   恢复扫描把 ``plan_execution`` 行落成 ``stopped``（这是简报第 5 条
   要求的），而计划列表**按行的终局裁决过滤** ——
   ``routes/plans.py`` 的 ``TERMINAL_EXEC_STATUSES`` 含 ``stopped``，
   ``_plan_is_terminal`` 只要看到行是 ``stopped`` 就把这条计划滤掉，
   哪怕相位是 ``queued``（同一处代码的注释写明了这个取舍：
   「一次跑挂了或被停掉的计划就是结束了，不管工作流相位怎么说」）。
   所以本文件断言的是**两件事**：默认列表**没有**这条计划（连同一条
   无关计划做负控，证明列表本身是好的），而**带终局计划的那份列表**
   （``?include_terminal=true``）里有它，相位 ``queued``、标签「排队中」，
   浏览器里**调度面板的队列**里也有它，带着可执行的操作。
   同一个取舍也让同目录的 ``test_plan_lifecycle_ui_e2e.py`` 在本分支上
   变红（它的 ``requeued`` 模式同样在等一张回不来的卡片）。
3. **续跑计划里只有 t2。** 简报的输出示例写 ``resume_plan_ids`` 是
   ``['t2', 't3']``。系统当前的真实行为是 ``['t2']``：续跑计划是从
   **中断**的任务（``interrupted_tasks``）建的，t3 当时还没开始，
   它在检查点的 ``unfinished_tasks`` 里，但不在这轮续跑计划里。
4. **被 SIGTERM 的服务器退出码是 ``-15``。** uvicorn 优雅关闭之后会把
   信号重新抛给自己，于是进程是「因信号而死」。优雅关闭（也就是写检查点
   的那段 lifespan ``finally``）**已经跑完了** —— 盘上那份检查点就是
   证据，在下一步断言。断言只保证「没有卡住」，不保证退出码是 0。
5. **启动预算与 ``lark_oapi``。** 被复用的 ``_boot_server`` 只给启动
   60 秒，而服务器启动时会构造飞书通知器，它的客户端 ``import lark_oapi``
   —— 一个 10,627 个模块的 SDK，在这台机器上光这一步就要几十秒，启动
   因此在端口打开之前就超时了。本文件在服务器的 ``PYTHONPATH`` 上放一个
   让 ``import lark_oapi`` 立刻失败的桩：客户端对缺失的 SDK 本来就抛
   ``FeishuUnavailable``，通知器于是记一条日志然后**保持惰性**
   （``server.py`` 的 lifespan 把整个构造包在 ``except Exception`` 里）。
   飞书在这台机器上本来就没有配置，桩没有改变任何被测行为。
6. **面板那一遍要一台「调度器开着」的服务器。** 调度面板画的是**队列**
   ——``scheduler_queue`` 表，那是调度器自己的表，写它的是调度器的
   worker。仓库自己的配置把调度器关着（``backend/config.yaml`` 的
   ``scheduler.enabled: false``），所以调度器没开的服务器读到的是空
   文档，队列里没有行，面板也就没有行可读。于是面板那一遍跑在一台
   **自己的**服务器上：状态库、计划树、脚本模型都跟第二次启动那一台
   一样（面板要看见的就是本用例停下来的那条计划），只有调度器的设置
   是自己的 —— 通过服务器暴露的那条缝（``server._scheduler_settings_path``）
   指到自己的文件上，缝是**点名**的，不是替身。容量是 **0**（「什么都不
   跑」是一个自洽的容量，配置自己的注释就是这么写的）：调度器**留着队
   列**，也就是面板要画的东西，但**不准入任何人**，所以本用例停下来的那条
   计划一直停在那儿，剩下的步骤不会被一条自己跑起来的计划打扰。

复跑
----

    pytest backend/tests/e2e/test_shutdown_checkpoint_e2e.py -v

报告落在 ``backend/tests/e2e/artifacts/shutdown-e2e/report.json``。
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    # ``PlanFactsReader`` lives under ``backend/``; the E2E tree has no
    # ``__init__.py``, so the import is made against the path, not
    # against pytest's rootdir juggling.
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E file
# ---------------------------------------------------------------------------


def _load_lifecycle_helpers():
    """Load ``test_plan_lifecycle_ui_e2e.py`` **without** modifying it.

    The helpers this story needs are all there already: the server
    bootstrap (``_boot_server``), the teardown (``_teardown_server``),
    the scripted-LLM writer, the browser driver (``_run_driver``), the
    HTTP client and the plan-state reader.  Loading them **by file
    path** keeps one implementation of "how a real backend is booted
    here" instead of two that drift apart.
    """
    path = Path(__file__).with_name("test_plan_lifecycle_ui_e2e.py")
    spec = importlib.util.spec_from_file_location("pdt_reused_lifecycle", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the reused helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_H = _load_lifecycle_helpers()
_boot_server = _H._boot_server
_teardown_server = _H._teardown_server
_write_scripted_llm = _H._write_scripted_llm
_run_driver = _H._run_driver
_playwright_interpreter = _H._playwright_interpreter
_http = _H._http
_plan_state = _H._plan_state
VENV_PYTHON = _H.VENV_PYTHON

#: The driver script the reused ``_run_driver`` runs — read, never edited.
_BROWSER_DRIVER = _H._BROWSER_DRIVER
#: The label the frontend shows for the waiting phase.
QUEUED_LABEL = _H.QUEUED_LABEL
#: Where the evidence is archived (git-ignored: runtime artifacts).
ARTIFACTS_ROOT = _H.ARTIFACTS_ROOT


#: The plan this story is about.
PLAN_ID = "shutdown-e2e"

#: An unrelated plan, seeded so the operator's list is **not** empty.
#:
#: Without it, "the parked plan is not in the list" would be
#: indistinguishable from "the list is broken" — both look like an
#: empty page.  With it, the page shows one card and *not* the parked
#: plan: the list works, and the parked plan is missing on purpose.
OTHER_PLAN_ID = "other-plan"

#: The three tasks of the plan, with the status each one had when the
#: server was shut down.  ``t1`` was done, ``t2`` was in flight, ``t3``
#: had not started.  The ids are deliberately short and distinguishable
#: so a reader can tell them apart in an assertion message.
_TASKS: List[Dict[str, Any]] = [
    {
        "id": "t1",
        "title": "draw the chart renderer",
        "description": "## 背景\nthe reporting view has no chart\n\n## 目标\ndraw one",
        "files_to_modify": ["backend/chart.py"],
        "test_command": "true",
        "status": "completed",
    },
    {
        "id": "t2",
        "title": "wire the renderer into the view",
        "description": "## 背景\nthe renderer is not mounted\n\n## 目标\nmount it",
        "files_to_modify": ["backend/reporting_view.py"],
        "test_command": "true",
        "depends_on": ["t1"],
        "status": "in_progress",
    },
    {
        "id": "t3",
        "title": "export the daily ledger",
        "description": "## 背景\nthere is no export\n\n## 目标\nexport it",
        "files_to_modify": ["backend/ledger.py"],
        "test_command": "true",
        "depends_on": ["t2"],
        "status": "pending",
    },
]


# ---------------------------------------------------------------------------
# The fixture world
# ---------------------------------------------------------------------------


def _make_project(work: Path) -> Path:
    """A real git checkout the executor is allowed to work in."""
    project = work / "project"
    (project / "backend").mkdir(parents=True, exist_ok=True)
    for name in ("chart.py", "reporting_view.py", "ledger.py"):
        (project / "backend" / name).write_text("# placeholder\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(project), check=False)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.email=e2e@example.invalid",
            "-c",
            "user.name=pdt-e2e",
            "commit",
            "-qm",
            "e2e checkout",
        ],
        check=False,
    )
    return project


def _write_lark_stub(work: Path) -> Path:
    """A directory whose ``lark_oapi`` is a stub, so the boot stays quick.

    The server's lifespan builds the Feishu notifier, and the notifier's
    client imports ``lark_oapi`` — an SDK of 10,627 modules.  On a loaded
    machine that import costs tens of seconds, which is more than the
    60 seconds the reused ``_boot_server`` allows for the whole boot, so
    the boot times out before the port is ever opened.

    The stub raises :class:`ImportError` on import, which is exactly what
    the client does with a missing SDK: the notifier logs that it is
    disabled and stays inert.  It is inert in this test anyway — Feishu
    is not configured on a test machine — and nothing else in the backend
    imports ``lark_oapi``.

    The stub is a **test-side** seam: the product's files are untouched,
    and the server's behaviour is the behaviour of a machine without the
    optional SDK.
    """
    stub_dir = work / "no-lark"
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "lark_oapi.py").write_text(
        'raise ImportError(\n'
        '    "lark-oapi is stubbed out for this E2E run: the real SDK is "\n'
        '    "10,627 modules and importing it costs tens of seconds, which "\n'
        '    "is more than the boot timeout allows. The Feishu notifier is "\n'
        '    "inert in this test either way -- Feishu is not configured here."\n'
        ')\n',
        encoding="utf-8",
    )
    return stub_dir


def _seed_plan_dir(work: Path) -> Path:
    """The plan directory the server will find on disk."""
    plan_dir = work / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": _TASKS}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (plan_dir / "prd.json").write_text(
        json.dumps({"prd": "# PRD\n\nA reporting view with a daily grouped bar chart."}),
        encoding="utf-8",
    )
    (plan_dir / "interview.json").write_text(
        json.dumps(
            {
                "requirement": "给运营后台加一个按天分组的柱状图",
                "dimensions": {"goals": "按天分组的柱状图"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "review.json").write_text(
        json.dumps({"reviewed": True}), encoding="utf-8"
    )
    return plan_dir


def _seed_other_plan_dir(work: Path) -> Path:
    """A second, unrelated plan — the negative control for the listing.

    It sits in the initial phase and is never touched by the shutdown,
    so the operator's list has a card in it.  Without that card, "the
    parked plan is not on the page" would look exactly like "the page
    is broken": both are an empty list.

    It carries an interview document and **no** task list on purpose:
    that is what puts it in the ``interviewed`` bucket, so its card says
    「已访谈」 and not 「排队中」.  A second card labelled 「排队中」 on
    the same page would muddy the very thing this control is for — the
    parked plan's 「排队中」 card must be the *only* one of its kind, and
    it must be missing.
    """
    plan_dir = work / "plans" / OTHER_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        json.dumps(
            {"requirement": "另一条与关机无关的计划", "dimensions": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir


def _seed_state_db(work: Path, *, exec_pid: int, started_at: str) -> None:
    """Write the rows a *running* plan leaves behind.

    Three rows, all of them the ones the product itself writes:

    * ``plan_routing`` —— the phase of the plan (``executing``: it is
      running);
    * ``plan_execution`` —— the run: ``running``, the pid of the stand-in
      process, the moment the run started, the directory the work
      happens in;
    * ``plan_tasks`` —— the per-task runtime state, so the checkpoint
      the shutdown writes knows which task was in flight.

    The unrelated plan gets a routing row of its own (and no execution
    row at all) — it is the plan that stays in the operator's list.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(work / "state.db")
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "executing")
        RoutingRepository(conn).insert(OTHER_PLAN_ID, "interview")
        ExecutionRepository(conn).update_fields(
            PLAN_ID,
            create_if_missing=True,
            exec_status="running",
            exec_pid=exec_pid,
            started_at=started_at,
            project_dir=str(work / "project"),
        )
        tasks = PlanTaskRepository(conn)
        for task in _TASKS:
            tasks.update_task(PLAN_ID, str(task["id"]), {"status": task["status"]})
    finally:
        conn.close()


def _read_execution_row(work: Path) -> Dict[str, Any]:
    """The durable ``plan_execution`` row, as the database holds it."""
    from state_machine.db.connection import open as db_open

    conn = db_open(work / "state.db")
    try:
        row = conn.execute(
            "SELECT exec_status, exec_pid, stop_reason FROM plan_execution "
            "WHERE plan_id = ?",
            (PLAN_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no plan_execution row for {PLAN_ID}"
    return {
        "exec_status": row[0],
        "exec_pid": row[1],
        "stop_reason": row[2],
    }


def _read_checkpoint(work: Path) -> Dict[str, Any]:
    """The checkpoint the shutdown left behind."""
    path = work / "plans" / PLAN_ID / "pause_state.json"
    assert path.exists(), (
        f"the shutdown left no checkpoint behind: {path} does not exist"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _execution_status(state: Dict[str, Any]) -> Optional[str]:
    """The run's status out of a plan-status snapshot.

    The execution section names it ``execution_status``; ``status`` is
    accepted as well so the assertion does not depend on which of the
    two spellings a given build publishes.
    """
    execution = state.get("execution")
    if not isinstance(execution, dict):
        return None
    for key in ("execution_status", "status"):
        value = execution.get(key)
        if value:
            return str(value)
    return None


def _task_ids(entries: Any) -> List[str]:
    """The task ids of a checkpoint list (``interrupted`` / ``unfinished``)."""
    ids: List[str] = []
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("task_id"):
            ids.append(str(entry["task_id"]))
    return ids


# ---------------------------------------------------------------------------
# A second browser pass: the scheduler panel
# ---------------------------------------------------------------------------


#: The driver for the **scheduler panel**.
#:
#: The reused sibling driver has three modes and every one of them is
#: about the *plan card* in the operator's plan list.  The panel is a
#: different surface: it reads the listing with ``?include_terminal=true``
#: on purpose — the frontend says why: "a stopped plan is the one that
#: can be resumed" — so it is where a plan the shutdown parked is
#: actually shown to the operator.  This driver opens the panel and
#: reports its rows.
_SCHEDULER_DRIVER = r'''
"""Drive the scheduler panel and report the rows it shows.

One question, two surfaces: what the plan list draws on its own, and
what the scheduler panel draws.  The panel draws two tables: the
**queue** and the running plans.  The queue is where a plan that is
waiting is named — with the reason it is waiting and the actions a
waiting plan can be given — so a plan the shutdown parked is named
there, and the actions beside it are the ones an operator may press.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead
    -- nothing is downloaded, and nothing is hard-coded: the cache lives
    under the home the run was launched with.  (Same lookup as the
    reused sibling driver; the two drivers are separate programs, so
    each carries its own copy.)
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


def plan_cards(page):
    """The plan cards the page draws: ``(plan_id, label)``."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('.plan-card')).map(card => {
            const badge = card.querySelector('.plan-status');
            return [card.dataset.plan, badge ? badge.textContent.trim() : ''];
        })"""
    )


def panel_rows(page):
    """The queue the panel draws: ``(plan_id, reason, action buttons)``.

    The queue's cells are the position, the plan, the number, the
    priority, the promotion, the reason and the actions — so the plan
    is the second cell, the reason the sixth, and the buttons are in
    the last one.
    """
    return page.evaluate(
        """() => Array.from(
              document.querySelectorAll('#scheduler-queue-list tr')
           ).map(row => {
              const cells = row.querySelectorAll('td');
              const actions = cells.length > 6 ? cells[6] : null;
              return [
                cells[1] ? cells[1].textContent.trim() : '',
                cells[5] ? cells[5].textContent.trim() : '',
                actions ? actions.querySelectorAll('button').length : 0,
              ];
           })"""
    )


url, plan_id, artifacts_dir = sys.argv[1:4]

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {"plan_cards": [], "rows": [], "page_errors": [], "console_errors": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    def on_page_error(exc):
        report["page_errors"].append(str(exc))

    def on_console(message):
        if message.type == "error":
            report["console_errors"].append(message.text)

    page.on("pageerror", on_page_error)
    page.on("console", on_console)

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    # The plan list is fetched, not drawn with the markup: wait for the
    # cards to be there before reading them.
    page.wait_for_function(
        "() => document.querySelectorAll('.plan-card').length > 0",
        timeout=30000,
    )
    page.wait_for_timeout(300)
    report["plan_cards"] = plan_cards(page)

    # The panel: the queue is the block that names a waiting plan.
    page.click("#btn-open-scheduler")
    page.wait_for_selector("#scheduler-queue-list tr", timeout=30000)
    page.wait_for_timeout(500)
    report["rows"] = panel_rows(page)
    page.screenshot(
        path=str(Path(artifacts_dir) / "scheduler-panel.png"), full_page=True
    )

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# A server whose scheduler is on — the panel draws the scheduler's queue
# ---------------------------------------------------------------------------


#: The launcher of the panel server.
#:
#: The scheduler reads the ``scheduler:`` section of the backend's own
#: ``config.yaml``, and the path it reads is a seam the server exposes
#: (``server._scheduler_settings_path``).  This run points that seam at a
#: file of its own, so the case can say what the scheduler is told
#: without touching the repository's configuration.  The seam is
#: *named*, not replaced: no component of the application is swapped for
#: a double.  (The same shape as the operator case in
#: ``plan_scheduler/test_scheduler_operator_e2e.py``.)
_PANEL_SERVER_LAUNCHER = r'''
"""Boot the backend with the scheduler's settings file pointed elsewhere.

The path the scheduler's settings are read from is a seam the server
exposes (``server._scheduler_settings_path``), so this run points it at
a file of its own.  Nothing is replaced -- the seam is named, exactly
the way the repository's own cases name it.
"""
import sys
from pathlib import Path

backend_dir = Path(sys.argv[1])
settings_path = Path(sys.argv[2])
port = int(sys.argv[3])

sys.path.insert(0, str(backend_dir))
sys.path.insert(0, str(backend_dir.parent))

import server

server._scheduler_settings_path = lambda: settings_path

import uvicorn

uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
'''


def _write_scheduler_settings(path: Path) -> None:
    """Write the settings the panel server's scheduler is told to read.

    The scheduler is **on**: the panel draws the scheduler's own queue,
    and a server whose scheduler is off answers the read model with an
    empty document — there is no queue to draw.

    The capacity is **zero**, and zero is a coherent capacity ("run
    nothing"): the scheduler keeps the line — that is what the panel
    draws — and admits nobody, so the plan this case parked stays
    parked and nothing starts underneath the rest of the case.  A young
    run is protected for ten minutes, as everywhere else: a run that has
    not had its turn is never interrupted for the sake of a number.
    """
    path.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: false\n"
        "  capacity: 0\n"
        "  min_runtime_seconds: 600\n"
        "  tick_seconds: 0.5\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )


def _boot_panel_server(
    work: Path, script_dir: Path, settings_path: Path, launcher_path: Path
) -> Tuple[str, subprocess.Popen]:
    """Boot a server whose scheduler is on; return ``(base_url, proc)``.

    The panel is a **scheduler** surface, so it needs a scheduler: the
    queue it draws is the scheduler's own queue table, and the scheduler
    is what writes that table.  The repository's own configuration
    leaves the scheduler switched off, so this server is pointed at a
    settings file of its own through the seam the server exposes.

    Everything else is the reused boot's environment — the same state
    database, the same plans tree, the same scripted model — because
    the panel has to show the very plan the rest of this case parked.
    """
    port = _H._free_port()
    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PLANS_DIR": str(work / "plans"),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": str(port),
            "PDT_DEBUG_ROUTES": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(work / "absent.yaml"),
            "PDT_LLM_MODE": "scripted",
            "PDT_LLM_SCRIPT_DIR": str(script_dir),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PYTHONPATH": os.pathsep.join(
                [str(_H.BACKEND_DIR), env.get("PYTHONPATH", "")]
            ),
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_handle = (work / "panel-server.log").open("wb")
    proc = subprocess.Popen(
        [
            str(VENV_PYTHON),
            str(launcher_path),
            str(_H.BACKEND_DIR),
            str(settings_path),
            str(port),
        ],
        cwd=str(_H.PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _H._wait_for_api(base_url, "/api/plans", timeout=60.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise
    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc


def _kill_panel_server(proc: subprocess.Popen) -> None:
    """Stop the panel server, without touching the plan it was showing.

    The reused teardown stops the plan on its way out, and this plan
    belongs to the *other* server: it is the one that resumes it, and
    stopping it here would take it out of the phase the rest of the
    case is about.  The scheduler this server ran never started
    anything — its capacity was zero — so there is no executor to reap
    and nothing to stop but the process itself.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, AttributeError):
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _run_scheduler_driver(
    interpreter: str,
    script: Path,
    base_url: str,
    plan_id: str,
    artifacts_dir: Path,
) -> Dict[str, Any]:
    """Run the scheduler-panel browser pass; return the report it printed.

    The same contract as the reused ``_run_driver``: the driver is a
    separate program, its last line of stdout is the report, and a
    missing browser binary is a *skip* (the browserless path carries
    the assertions) while any other failure is a failure.
    """
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{base_url}/",
            plan_id,
            str(artifacts_dir),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=_H._driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or "playwright install" in combined:
            pytest.skip(
                "the Playwright driver is installed but its browser binaries "
                "are not; the default (browserless) path carries the assertions"
            )
        raise AssertionError(
            f"the scheduler browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The story
# ---------------------------------------------------------------------------


def test_shutdown_writes_a_checkpoint_and_restart_parks_queued(tmp_path, monkeypatch):
    """SIGTERM → a checkpoint on disk → a restart that parks the plan.

    The whole chain, through two real backends and a real browser.
    """
    work = Path(tmp_path)
    script_dir = work / "llm-script"
    script_dir.mkdir(parents=True, exist_ok=True)
    _write_scripted_llm(script_dir)
    _make_project(work)
    _seed_plan_dir(work)
    _seed_other_plan_dir(work)

    # The server's boot must fit into the reused ``_boot_server``'s 60
    # second budget.  The Feishu notifier's client imports the
    # ``lark_oapi`` SDK -- 10,627 modules -- and that import alone costs
    # tens of seconds on a loaded machine, so the boot would time out
    # before the port was ever opened.  The stub makes that import fail
    # fast; the notifier then does what it does on a machine without the
    # optional SDK: it logs that it is disabled and stays inert.  Nothing
    # else in the backend imports ``lark_oapi``.
    stub_dir = _write_lark_stub(work)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(stub_dir), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    )

    # The stand-in for the executor: a process that is alive, holds a
    # pid the recovery scan can probe, and does nothing else.  The
    # server never learns about it (it is not the server's child), so
    # the server will not kill it -- the test has to.
    stand_in = subprocess.Popen(
        [str(VENV_PYTHON), "-c", "import time; time.sleep(600)"],
        start_new_session=True,
    )
    started_at = (datetime.now() - timedelta(seconds=60)).isoformat()
    _seed_state_db(work, exec_pid=stand_in.pid, started_at=started_at)

    interpreter = _playwright_interpreter()
    if interpreter is None:
        stand_in.kill()
        stand_in.wait(timeout=10)
        pytest.skip("playwright interpreter not found")

    artifacts = ARTIFACTS_ROOT / PLAN_ID
    artifacts.mkdir(parents=True, exist_ok=True)
    script = work / "drive_page.py"
    script.write_text(_BROWSER_DRIVER, encoding="utf-8")
    panel_script = work / "drive_scheduler_panel.py"
    panel_script.write_text(_SCHEDULER_DRIVER, encoding="utf-8")
    # The panel pass needs a server whose scheduler is on — the queue it
    # draws is the scheduler's own table — so it gets a launcher and a
    # settings file of its own.
    settings_path = work / "scheduler-settings.yaml"
    launcher_path = work / "launch_panel_server.py"
    _write_scheduler_settings(settings_path)
    launcher_path.write_text(_PANEL_SERVER_LAUNCHER, encoding="utf-8")

    report: Dict[str, Any] = {"plan_id": PLAN_ID}
    base_url = ""
    # Every server this test boots, with the URL it answers on: each one
    # is torn down against its own URL, so the teardown of the first
    # server cannot talk to the second one.
    servers: List[Tuple[subprocess.Popen, str]] = []
    try:
        # --- step 1: a real backend, and a plan that is really running --
        base_url, proc = _boot_server(work, script_dir)
        servers.append((proc, base_url))
        state = _plan_state(base_url, PLAN_ID)
        report["phase_before_shutdown"] = state.get("current_phase")
        report["execution_status_before_shutdown"] = _execution_status(state)
        assert _execution_status(state) == "running", (
            "the plan was running before the shutdown; got "
            f"{_execution_status(state)!r}"
        )

        # --- step 2: SIGTERM, then the executor is taken away -----------
        proc.terminate()
        proc.wait(timeout=30)
        # A SIGTERM'd uvicorn exits *because of* the signal: after the
        # graceful shutdown it restores the handler it displaced and
        # re-raises the signal, so the exit status is ``-SIGTERM``.  The
        # graceful shutdown -- the lifespan's ``finally``, which is what
        # writes the checkpoint -- has already run by then; the
        # checkpoint on disk is the evidence of that, and it is asserted
        # in the next step.  What must not happen is a hang.
        assert proc.returncode in (0, -signal.SIGTERM), (
            f"the server did not exit on SIGTERM: {proc.returncode}"
        )
        # The server does not own this process (``_execution_state`` has
        # no ``process`` for it), so it does not kill it either.  A pid
        # that is still alive would make the second boot see a *live*
        # run and judge nothing -- the whole point of the second half of
        # this test is that the pid is gone.
        stand_in.kill()
        stand_in.wait(timeout=10)
        assert stand_in.poll() is not None, "the stand-in executor survived"

        # --- step 3: the checkpoint the shutdown left behind -------------
        checkpoint = _read_checkpoint(work)
        report["checkpoint_reason"] = checkpoint.get("reason")
        report["checkpoint_kind"] = checkpoint.get("kind")
        report["checkpoint_unfinished_task_ids"] = _task_ids(
            checkpoint.get("unfinished_tasks")
        )
        report["checkpoint_interrupted_task_ids"] = _task_ids(
            checkpoint.get("interrupted_tasks")
        )
        assert checkpoint.get("reason") == "server_shutdown", (
            "the checkpoint must say the *server* went down; got "
            f"{checkpoint.get('reason')!r}"
        )
        assert checkpoint.get("kind") == "execution", (
            f"the checkpoint must be an execution checkpoint; got "
            f"{checkpoint.get('kind')!r}"
        )
        unfinished = set(report["checkpoint_unfinished_task_ids"])
        assert {"t2", "t3"} <= unfinished, (
            "the checkpoint must name the tasks the run still owes; got "
            f"{sorted(unfinished)}"
        )

        # --- step 4: the second boot parks the plan in ``queued`` -------
        base_url_after, proc_after = _boot_server(work, script_dir)
        servers.append((proc_after, base_url_after))
        base_url = base_url_after
        state_after = _plan_state(base_url, PLAN_ID)
        report["phase_after_restart"] = state_after.get("current_phase")
        assert state_after.get("current_phase") == "queued", (
            "a plan the shutdown interrupted comes back as ``queued``; got "
            f"{state_after.get('current_phase')!r}"
        )
        assert state_after.get("current_phase") != "failed", (
            "the plan was interrupted, not broken -- it must not be failed"
        )

        # --- step 5: the row carries the parked verdict ------------------
        row = _read_execution_row(work)
        report["exec_status"] = row["exec_status"]
        report["exec_stop_reason"] = row["stop_reason"]
        report["exec_pid"] = row["exec_pid"]
        assert row["exec_status"] == "stopped", (
            f"the run is over for this process; got {row['exec_status']!r}"
        )
        assert row["stop_reason"] == "server_shutdown", (
            f"the row must say why; got {row['stop_reason']!r}"
        )
        assert row["exec_pid"] is None, (
            "a stopped row must not carry a pid: every reader believes a "
            "pid means alive"
        )

        # --- step 6: the scheduler sees it ------------------------------
        from plan_scheduler.facts import PlanFactsReader
        from state_machine.db.connection import open as db_open

        conn = db_open(work / "state.db")
        try:
            queued = PlanFactsReader(conn, work / "plans").queued_plans()
        finally:
            conn.close()
        queue_plan_ids = [str(entry.get("plan_id")) for entry in queued]
        report["queue_plan_ids"] = queue_plan_ids
        assert PLAN_ID in queue_plan_ids, (
            f"the parked plan is not in the scheduler's waiting set: "
            f"{queue_plan_ids}"
        )

        # --- step 7: what the operator sees ---------------------------
        # 7a: the listing that includes the terminal runs — the one the
        #     scheduler panel reads — carries the parked plan, in the
        #     queued phase, under the product's own label 排队中.
        status, body = _http("GET", f"{base_url}/api/plans?include_terminal=true")
        assert status == 200, f"the terminal-inclusive listing failed: {body!r}"
        entries = body if isinstance(body, list) else (body or {}).get("plans", [])
        parked = next(
            (e for e in entries if isinstance(e, dict) and e.get("id") == PLAN_ID),
            None,
        )
        report["terminal_listing_ids"] = [
            e.get("id") for e in entries if isinstance(e, dict)
        ]
        assert parked is not None, (
            "the parked plan is not in the listing that includes the "
            f"terminal runs: {report['terminal_listing_ids']!r}"
        )
        report["parked_phase"] = parked.get("current_phase")
        report["parked_phase_label"] = parked.get("current_phase_label")
        assert parked.get("current_phase") == "queued", (
            f"the parked plan must be queued; got {parked.get('current_phase')!r}"
        )
        assert parked.get("current_phase_label") == QUEUED_LABEL, (
            f"the label must be {QUEUED_LABEL!r}; got "
            f"{parked.get('current_phase_label')!r}"
        )

        # 7b: the operator's *default* list does not carry it: the row's
        #     verdict is ``stopped``, and a terminal run is filtered out
        #     of the list whatever the phase says.  The unrelated plan
        #     is the negative control: it proves the list works.
        status, body = _http("GET", f"{base_url}/api/plans")
        assert status == 200, f"the plan list failed: {body!r}"
        default_entries = (
            body if isinstance(body, list) else (body or {}).get("plans", [])
        )
        default_ids = [e.get("id") for e in default_entries if isinstance(e, dict)]
        report["default_listing_ids"] = default_ids
        assert OTHER_PLAN_ID in default_ids, (
            "the list itself is broken — even the unrelated plan is missing: "
            f"{default_ids!r}"
        )
        assert PLAN_ID not in default_ids, (
            "a plan whose run was stopped is a terminal plan, and the list "
            "filters terminal runs out; the brief expected a card here"
        )

        # 7c: the browser — the plan list on the page shows the
        #     unrelated plan and not the parked one.  (The reused
        #     driver's ``stopped`` mode measures exactly this: a plan
        #     whose run is stopped is not on the list.)
        card_report = _run_driver(
            interpreter, script, "stopped", base_url, PLAN_ID, artifacts
        )
        report["browser_card"] = card_report
        assert not card_report.get("page_errors"), (
            f"the page raised errors: {card_report.get('page_errors')!r}"
        )
        assert card_report.get("stopped_card_count") == 0, (
            "the parked plan must not be on the operator's plan list; got "
            f"{card_report.get('cards')!r}"
        )

        # 7d: the browser — the scheduler panel's **queue** holds it, with
        #     the actions a waiting plan can be given.  This is the
        #     surface where a parked plan is visible to the operator.
        #
        #     The queue is the scheduler's own table, so the panel pass
        #     runs against a server whose scheduler is on — with a
        #     capacity of zero, so it keeps the line and starts nothing.
        panel_url, panel_proc = _boot_panel_server(
            work, script_dir, settings_path, launcher_path
        )
        try:
            panel = _run_scheduler_driver(
                interpreter, panel_script, panel_url, PLAN_ID, artifacts
            )
        finally:
            _kill_panel_server(panel_proc)
        report["browser_panel"] = panel
        assert not panel.get("page_errors"), (
            f"the page raised errors: {panel.get('page_errors')!r}"
        )
        card_ids = [entry[0] for entry in (panel.get("plan_cards") or [])]
        report["browser_plan_cards"] = panel.get("plan_cards")
        assert OTHER_PLAN_ID in card_ids, (
            "the page draws no plan card at all — the page is broken, not "
            f"the list: {panel.get('plan_cards')!r}"
        )
        assert PLAN_ID not in card_ids, (
            "the parked plan must not be on the operator's plan list; got "
            f"{card_ids!r}"
        )
        rows = panel.get("rows") or []
        row = next((r for r in rows if r and r[0] == PLAN_ID), None)
        assert row is not None, (
            f"the scheduler panel's queue does not hold the parked plan: {rows!r}"
        )
        report["panel_row"] = row
        assert row[1], (
            "the panel must say why the plan is waiting; got "
            f"{row[1]!r}"
        )
        assert row[2] >= 1, (
            "the parked plan must be actionable in the panel — that is the "
            f"whole point of parking it; got {row[2]!r} action buttons"
        )

        # --- step 8: the start resumes, and does not redo t1 -------------
        status, body = _http(
            "POST", f"{base_url}/api/execution/{PLAN_ID}/start", {}, timeout=120
        )
        report["start_status"] = status
        report["start_body"] = body
        assert status == 200, f"the start failed: {body!r}"
        assert isinstance(body, dict), f"the start answered {body!r}"
        assert body.get("resumed") is True, (
            f"the start did not resume from the checkpoint; got {body!r}"
        )
        resume_plan_ids = [
            str(entry.get("task_id"))
            for entry in (body.get("resume_plan") or [])
            if isinstance(entry, dict)
        ]
        report["resume_plan_ids"] = resume_plan_ids
        assert "t2" in resume_plan_ids, (
            "the interrupted task must be in the resume plan; got "
            f"{resume_plan_ids}"
        )
        assert "t1" not in resume_plan_ids, (
            "a task that was already done must not be run again; got "
            f"{resume_plan_ids}"
        )

        (artifacts / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        # Both servers go down, and the plan is stopped on the way out so
        # no executor is left behind.
        for a_proc, a_url in servers:
            try:
                _teardown_server(a_proc, a_url, PLAN_ID)
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        if stand_in.poll() is None:
            stand_in.kill()
            try:
                stand_in.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
