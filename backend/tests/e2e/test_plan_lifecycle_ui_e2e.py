"""真实后端 + Playwright：``queued`` 相位与统一生命周期门面的端到端验收。

这一轮改了三件事 —— 相位词汇（``ready`` 并进 ``queued``）、前端标签、
统一生命周期门面（``POST /api/plan/{id}/lifecycle/start|stop``）。三件事
各自的单测都绿，但**串起来走一遍**才算验收：单测各自绿、用户打开页面却
看到「就绪」或者点了没反应，是本类改动最典型的失败。

走的就是那一遍：

* **真后端** —— ``backend.server`` 的真进程（不是 ``TestClient``），监听
  内核选的空闲端口，状态库与计划树都在临时目录里。
* **真浏览器** —— Playwright 驱动真 Chromium 打开真页面，走页面自己的
  事件与自己的 ``fetch``。

唯一的替身是模型，而那是仓库自己留的缝：``PDT_LLM_MODE=scripted`` 把每
一次模型调用换成磁盘上的固定回答（``backend/coding_tool.py`` 的
``ScriptedLLMRegistry``）。不换的话，``POST /api/plan/import`` 与访谈都会
真的去调模型 —— 那是 E2E 不该做的事：不可复现，还烧钱。

与简报字面写法的三处偏差（都是**系统当前的真实行为**，不是这里放宽了
断言；改它们要改产品代码，不改测试）
--------------------------------------------------------------

1. **「停」要停得住东西。** 门面按**实时状态**分派：没有在飞的东西就
   什么都不派发（``detail='idle'``），相位原样不动。所以
   ``final_phase == 'stopped'`` 只在计划**真的在飞**时成立 —— 简报里
   「先点一次没有目录的开始（被拒），再停」的那条路径停的是 idle 的计划，
   门面会答 ``final_phase='queued'``。这里改成：先用门面把计划启动起来
   （带着一个真的 checkout），等它真的在飞，再停。
2. **终态的计划不进计划列表。** ``stopped`` 是终态相位，列表接口默认把
   终态计划过滤掉（``routes/plans.py`` 的 ``include_terminal``），前端
   的计划卡片里**没有**「已停止」这个标签（``frontend/app.js`` 的卡片
   映射只有 ``new / interviewed / prd / reviewed / ready / queued``）。
   所以刷新之后，停掉的计划**不在卡片列表里** —— 这正是要断言的事实：
   用户不会看见一张还写着「排队中」或者「就绪」的过期卡片。
3. **访谈建出来的计划在 ``interview`` 相位**，卡片标签是「已访谈」，不是
   「排队中」。「排队中」说的是**等待相位**，而访谈中的计划还没到那一步。
   断言是：新卡片出现、标签不是「就绪」（旧词汇），并且排队中那张卡片
   的标签**仍然是**「排队中」。

手工复跑（不跑 pytest 的时候）
--------------------------------

用 ``mcp__playwright__`` 手工走一遍，顺序如下（每一步的结果都在页面上
看得见）：

1. ``mcp__playwright__browser_navigate`` 打开 ``http://127.0.0.1:<port>/``；
   ``mcp__playwright__browser_wait_for`` 等
   ``.plan-card[data-plan="<plan_id>"]`` 出现。
2. ``mcp__playwright__browser_find``（或 ``browser_snapshot``）读
   ``[data-plan="<plan_id>"] .plan-status`` —— 必须是「排队中」，不能是
   「就绪」；``mcp__playwright__browser_take_screenshot`` 存成
   ``queued.png``。
3. ``mcp__playwright__browser_click`` 点 ``#btn-new-plan``，
   ``mcp__playwright__browser_fill_form`` 在 ``#interview-input`` 里写需求，
   再 ``browser_click`` 点 ``#btn-send-interview``。
4. ``mcp__playwright__browser_click`` 点 ``#btn-sidebar-home`` 回到计划
   列表：新卡片出现，标签不是「就绪」；排队中那张卡片的标签仍是「排队中」。
5. 点排队中那张卡片 → 任务视图：``#tasks-list`` 与 ``#btn-start-execution``
   都可见。
6. 点 ``#btn-start-execution``（展开执行面板）→ ``#btn-confirm-execute``
   （**不填**工作目录）→ 后端 400 拒绝，页面弹 alert，没有未捕获异常；
   相位仍是 ``queued``。
7. ``POST /api/plan/<plan_id>/lifecycle/stop {"requeue": false}`` →
   ``final_phase`` 与 ``berth_phase`` 都是 ``stopped``；刷新后停掉的计划
   不在卡片列表里（终态不进列表）；``browser_take_screenshot`` 存成
   ``stopped.png``。
8. ``POST /api/plan/<plan_id>/lifecycle/stop {"requeue": true}`` →
   ``final_phase == 'queued'``；刷新后卡片回到「排队中」。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from framework.ids import derive_plan_id

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

# Prefer the project venv: the backend subprocess this test boots must run
# on the interpreter this suite was installed into.  A checkout with no
# ``backend/.venv`` (fresh worktree, container) falls back to the
# interpreter already running pytest, so the server really boots.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: Every ``/api/*`` request must carry this header (``backend/request_guard.py``).
#: The frontend carries it too; the test carries it for the same reason.
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}

#: Where the screenshots go: ``<repo>/backend/tests/e2e/artifacts/<plan_id>/``.
ARTIFACTS_ROOT = Path(__file__).resolve().parent / "artifacts"

#: The label the frontend shows for the waiting phase
#: (``plan_state.PHASE_LABELS['queued']``).
QUEUED_LABEL = "排队中"
#: The label of the **old** vocabulary.  The waiting phase must never show
#: it again -- that is the whole point of the phase merge.
LEGACY_READY_LABEL = "就绪"
#: The label of the interview phase (a plan created through the interview
#: has not reached the waiting phase yet).
INTERVIEW_LABEL = "已访谈"

#: The material the import brings in.  It must differ from the interview's
#: requirement: the scripted LLM matches on substrings, and two identical
#: requirements would cross the wires.
IMPORT_CONTENT = "把报表页的折线图换成按天分组的柱状图"
INTERVIEW_REQUIREMENT = "给运营后台加一个按天分组的柱状图"

#: It carries a per-flow token, and it has to: the suite runs this module
#: on several workers at once, and two flows that share a plan id share
#: everything that is keyed by that id.  The teardown kills "this plan's"
#: leftover executor by matching the id in the command line -- which is
#: also the *other* flow's executor and its browser driver, so one copy
#: of this module used to kill the other copy mid-flight.  The artifacts
#: directory is keyed by the plan id too, so the two copies also wrote
#: each other's screenshots.
#:
#: The token is short enough that the derived slug still fits the
#: 20-character limit ``derive_plan_id`` enforces -- a longer one would
#: be truncated and could collide again.
IMPORT_TITLE_PREFIX = "e2e-ui"


def _flow_title() -> str:
    """A title that derives a plan id unique to this flow."""
    return f"{IMPORT_TITLE_PREFIX}-{uuid.uuid4().hex[:12]}"


def _import_title() -> str:
    """The title this run's import is filed under.

    The title becomes the plan id (``framework.ids.derive_plan_id``),
    and the plan id reaches outside the run: it is an argument of the
    browser driver, and it is the needle the teardown uses to find an
    executor that outlived the server.  A title that is the same on
    every run gives every run on the machine the same id -- and two runs
    that share an id have one run's teardown killing the other run's
    driver (pytest-xdist gives every worker its own server and its own
    import, so this happened *inside* one run of this file: the second
    worker's teardown SIGTERMed the first worker's driver, which is the
    ``exit -15`` with an empty stdout and a node ``EPIPE``).

    The token keeps the id unique per run.  It has to survive the
    backend's slug rule -- the id is ``<date>-<slug truncated to 20
    characters>`` -- so it goes in front of the title.
    """
    return f"{os.getpid()}-{uuid.uuid4().hex[:6]}-{IMPORT_TITLE_PREFIX}"


#: The home the run was *launched* with.  ``install_hermetic_env()``
#: redirects ``$HOME`` to a temp dir, and Playwright resolves the browser
#: binaries it launches under the home it is handed -- so the driver
#: subprocess is given the real home.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E file
# ---------------------------------------------------------------------------


def _load_reused_helpers():
    """Load ``test_fastfail_no_ghost_state_ui.py`` **without** modifying it.

    Two helpers are reused as they are: ``_free_port()`` (the kernel picks
    a free port) and ``_playwright_interpreter()`` (the interpreter that
    can import playwright).  ``backend/tests/e2e/`` **is** a package now
    (``backend/tests/__init__.py`` and ``backend/tests/e2e/__init__.py``
    are tracked since 720ceb5), so a sibling could be imported as
    ``tests.e2e.<module>`` -- but that name is only resolvable from the
    paths pytest happens to put on ``sys.path`` (the repository root and
    ``backend/``), and this file is also imported *as* a module by the
    sibling files that reuse it.  Loading by **file path** keeps the
    dependency independent of both: the helper module is read once, under
    a name of its own, without copying a single line of it.
    """
    path = Path(__file__).with_name("test_fastfail_no_ghost_state_ui.py")
    spec = importlib.util.spec_from_file_location("pdt_reused_ui_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the reused helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REUSED = _load_reused_helpers()

#: Bind an ephemeral loopback port (the kernel picks it).
_free_port = _REUSED._free_port
#: An interpreter that can import ``playwright``, or ``None``.
_playwright_interpreter = _REUSED._playwright_interpreter


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> Any:
    """JSON when the body is JSON, the text otherwise."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _http(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Tuple[int, Any]:
    """One HTTP request against the test server; ``(status, body)``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in REQUEST_HEADERS.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _wait_for_api(base_url: str, path: str, timeout: float = 60.0) -> int:
    """Poll ``GET base_url + path`` until it answers 200; return the status.

    The brief's first step: the backend is up when the API answers, not
    when the process exists.
    """
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            status, body = _http("GET", f"{base_url}{path}", timeout=5)
            if status == 200:
                return status
            last = (status, body)
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"{base_url}{path} did not answer 200 within {timeout}s; last={last!r}"
    )


def _plan_state(base_url: str, plan_id: str) -> Dict[str, Any]:
    """``GET /api/plan/{id}/status`` as a dict."""
    status, body = _http("GET", f"{base_url}/api/plan/{plan_id}/status")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"plan status for {plan_id} answered {status}: {body!r}")
    return body


#: The run verdicts that end a plan, whatever the workflow's phase says.
#: The listing filters on both answers (``routes/plans.py``,
#: ``_plan_is_terminal``): the phase is the workflow's own verdict, and
#: this is the run's.  Kept in step with ``TERMINAL_EXEC_STATUSES``
#: there -- a run that failed or was stopped is over.
TERMINAL_EXEC_VERDICTS = frozenset({"failed", "stopped"})


def _persisted_exec_verdict(state_db_path: str, plan_id: str) -> Optional[str]:
    """The run verdict **as persisted** (``plan_execution.exec_status``).

    This is the surface the listing filters on (``routes/plans.py``,
    ``_persisted_exec_status``), and it is not the same as what
    ``/api/plan/{id}/summary`` reports: that endpoint prefers the
    server's in-memory record of the process, which keeps saying
    "stopped" after a requeue while the row no longer does.

    Read straight from the SQLite file the server was given
    (``PDT_STATE_DB_PATH``), because there is no endpoint that reports
    the row.
    """
    conn = sqlite3.connect(state_db_path, timeout=15)
    try:
        row = conn.execute(
            "SELECT exec_status FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError(
            f"no plan_execution row for {plan_id} in {state_db_path}"
        )
    return str(row[0]) if row[0] is not None else None


def _listed_plan_ids(base_url: str) -> List[str]:
    """The plan ids ``GET /api/plans`` currently lists."""
    status, body = _http("GET", f"{base_url}/api/plans")
    if status != 200:
        raise RuntimeError(f"/api/plans answered {status}: {body!r}")
    entries = body if isinstance(body, list) else (body or {}).get("plans", [])
    return [str(entry.get("id")) for entry in entries if isinstance(entry, dict)]


def _wait_for_plan_in_list(
    base_url: str, plan_id: str, timeout: float = 30.0
) -> bool:
    """Wait until the plan shows up in the listing; return whether it did.

    The listing is **cached** (``server._PLANS_CACHE_TTL_SECONDS``, two
    seconds) and the import does not drop the cache, so the plan a moment
    after the import is not in the list yet.  That is the system's
    behaviour, not a flake: the wait is what a user does (the page polls
    the list), and it is why the browser pass below reloads until the
    card is there.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if plan_id in _listed_plan_ids(base_url):
            return True
        time.sleep(0.5)
    return False


def _wait_for_plan_absent_from_list(
    base_url: str, plan_id: str, timeout: float = 30.0
) -> bool:
    """Wait until the plan leaves the listing; return whether it did."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if plan_id not in _listed_plan_ids(base_url):
            return True
        time.sleep(0.5)
    return False


def _wait_in_flight(base_url: str, plan_id: str, timeout: float = 60.0) -> bool:
    """Wait until the plan's execution is really running; return whether it was.

    The facade dispatches on the **live** state, so "the plan is running"
    is a fact that has to be observed before a stop means anything.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _plan_state(base_url, plan_id)
        if state.get("execution_in_flight"):
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# The scripted LLM double
# ---------------------------------------------------------------------------

#: The task list the import is answered with.  ``sleep 300`` is the
#: executor's pre-flight command: the pre-flight caps at 60 s, so the plan
#: stays **in flight** long enough for a stop to have something to stop.
#: (An idle plan cannot be stopped -- the facade answers ``detail='idle'``.)
_TASKS_PAYLOAD = {
    "requirement": "add a daily grouped bar chart to the reporting view",
    "tasks": [
        {
            "id": "T1",
            "title": "add the chart renderer",
            "description": "## 背景\nthe reporting view has no chart",
            "files_to_modify": ["backend/chart.py"],
            "test_command": "sleep 300",
        },
        {
            "id": "T2",
            "title": "wire the renderer into the view",
            "description": "## 目标\nmount the renderer",
            "files_to_modify": ["backend/reporting_view.py"],
            "test_command": "sleep 300",
            "depends_on": ["T1"],
        },
    ],
}


def _write_scripted_llm(script_dir: Path) -> None:
    """Write the scripted answers the server will read instead of a model.

    The registry maps a **matcher** (a substring of the prompt) to a
    response file.  The rules the double itself enforces: an exact match
    wins, otherwise the most specific fragment wins, and a prompt nothing
    matches is an error (there is no silent fallback -- that is the point
    of the double).

    * ``import.json`` -- the import path's task-generation prompt names the
      file it is supposed to read.
    * ``资深任务列表审校员 / 第二轮生成器`` -- the mandatory self-review of
      the task list.
    * the requirement itself -- the interview.  The scope pre-check's
      prompt *is* the requirement, so the exact match lands here.
    * ``e`` -- the catch-all for the executor's coding turns.  It is one
      character long, so every other (longer, more specific) matcher beats
      it; it exists so that a turn nobody scripted is answered with
      *something* instead of raising.
    """
    registry = {
        "prompts": {
            "tasks_generation": "import.json",
            "tasks_self_review": "资深任务列表审校员 / 第二轮生成器",
            "interview": INTERVIEW_REQUIREMENT,
            "agent_turn": "e",
        }
    }
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (script_dir / "tasks_generation.txt").write_text(
        json.dumps(_TASKS_PAYLOAD, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (script_dir / "tasks_self_review.txt").write_text(
        json.dumps(
            {"findings": [], "fixed_content": _TASKS_PAYLOAD}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    (script_dir / "interview.txt").write_text(
        json.dumps(
            {
                "product_form": "software",
                "in_scope": [],
                "out_of_scope": [],
                "open_questions": [],
                "questions": ["先做哪一块？"],
                "complete": False,
                "dimensions": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (script_dir / "agent_turn.txt").write_text(
        json.dumps({"ok": True}, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The server + the checkout it works on
# ---------------------------------------------------------------------------


def _make_checkout(work: Path) -> Path:
    """A real git checkout for the imported plan's tasks to work on."""
    checkout = work / "checkout"
    (checkout / "backend").mkdir(parents=True, exist_ok=True)
    for name in ("chart.py", "reporting_view.py"):
        (checkout / "backend" / name).write_text("# placeholder\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(checkout), check=False)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
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
    return checkout


def _boot_server(work: Path, script_dir: Path) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` subprocess; return ``(base_url, proc)``."""
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": str(port),
            "PDT_DEBUG_ROUTES": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            # "inherit" and not a provider: this server exists to prove the
            # lifecycle, not to exercise provider routing.
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(work / "absent.yaml"),
            # The model is the one thing that is replaced: a real model call
            # would be neither reproducible nor free.
            "PDT_LLM_MODE": "scripted",
            "PDT_LLM_SCRIPT_DIR": str(script_dir),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never owned
    # it (see the sibling E2E file for the full account).
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_handle = (work / "server.log").open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_api(base_url, "/api/plans", timeout=60.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise
    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc


def _teardown_server(proc: subprocess.Popen, base_url: str, plan_id: str) -> None:
    """Stop the plan, terminate the server, and leave no executor behind.

    The executor runs in its own session, so terminating the server does
    not reach it -- it is stopped through the same facade the test used,
    and anything that survived is killed by the shape of its own command
    line (see :func:`_kill_leftover_executors`).
    """
    if plan_id:
        try:
            _http(
                "POST",
                f"{base_url}/api/plan/{plan_id}/lifecycle/stop",
                {"requeue": False},
                timeout=30,
            )
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass
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
    _kill_leftover_executors(plan_id)


def _kill_leftover_executors(plan_id: str) -> None:
    """Kill the plan's executor if it outlived the server -- and only it.

    The executor is the ``cli.py --recover`` subprocess the plan owns,
    and the plan id is in its command line (it was given the plan's
    ``--tasks-file``).  It used to be found with ``pgrep -f <plan id>``,
    which matches *every* command line that mentions the id -- and the
    browser driver takes the id as an argument.  Two workers of the
    same run shared the id (it was derived from a constant title), so
    one worker's teardown killed the other worker's driver: the driver
    was SIGTERMed (``exit -15``) while the playwright node driver was
    still writing to it (the node ``EPIPE``).

    So the executor is found by *its own* shape -- the recovery CLI and
    the plan's tasks file -- and not by "mentions the plan id".

    ``ps -ax`` and not a bare ``ps``: the executor is started in its own
    session (no controlling terminal), so a bare ``ps`` does not even
    list it.
    """
    if not plan_id:
        return
    try:
        listing = subprocess.run(
            ["ps", "-ax", "-ww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if listing.returncode != 0:
        return
    for line in listing.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not command:
            continue
        if "cli.py" not in command or "--recover" not in command:
            continue
        if plan_id not in command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page and report what it shows.

Three modes, one per moment of the story:

* ``queued``   -- the plan is waiting: the card, the interview, the task
  view, and the refused start.
* ``stopped``  -- the plan has been stopped: what the list shows now.
* ``requeued`` -- the plan is waiting again: what the list shows now.

The page is driven through its own events (``click``, ``fill``), so the
frontend's own code paths run -- including the ``alert`` it raises when a
start is refused.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead --
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


def card_labels(page):
    """Every plan card on the page: ``(plan_id, label)``."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('.plan-card')).map(card => {
            const badge = card.querySelector('.plan-status');
            return [card.dataset.plan, badge ? badge.textContent.trim() : ''];
        })"""
    )


def wait_for_card(page, card, attempts=30):
    """Wait for a plan card, reloading while the served list is stale.

    The listing is served from a two-second cache, so a page that loads
    right after a plan appeared shows a list without it.  Reloading is
    what a user does (the sidebar polls), and it is the only way the
    cache gets out of the way.
    """
    for _ in range(attempts):
        try:
            page.wait_for_selector(card, timeout=3000)
            return True
        except Exception:
            page.reload(wait_until="load")
            page.wait_for_timeout(1000)
    return False


def wait_for_card_gone(page, card, attempts=30):
    """Wait until a plan card is not on the page any more."""
    for _ in range(attempts):
        page.wait_for_timeout(1000)
        if page.locator(card).count() == 0:
            return True
        page.reload(wait_until="load")
    return False


mode, url, plan_id, artifacts_dir = sys.argv[1:5]
requirement = sys.argv[5] if len(sys.argv) > 5 else ""

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {
    "mode": mode,
    "page_errors": [],
    "console_errors": [],
    "dialogs": [],
}

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    def on_page_error(exc):
        report["page_errors"].append(str(exc))

    def on_console(message):
        if message.type == "error":
            report["console_errors"].append(message.text)

    def on_dialog(dialog):
        report["dialogs"].append(dialog.message)
        dialog.dismiss()

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("dialog", on_dialog)

    card = '.plan-card[data-plan="%s"]' % plan_id

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_timeout(800)

    if mode == "queued":
        wait_for_card(page, card)
        page.wait_for_timeout(300)
        report["queued_label"] = page.inner_text(card + " .plan-status")
        report["queued_class"] = page.get_attribute(card + " .plan-status", "class")
        page.screenshot(path=str(Path(artifacts_dir) / "queued.png"), full_page=True)

        # --- the interview creates a second plan -------------------------
        page.click("#btn-new-plan")
        page.wait_for_selector("#interview-input", state="visible", timeout=15000)
        page.fill("#interview-input", requirement)
        page.click("#btn-send-interview")
        page.wait_for_function(
            "() => { const id = (typeof currentPlanId !== 'undefined') ? currentPlanId : null; return !!id; }",
            timeout=60000,
        )
        report["interview_plan_id"] = page.evaluate(
            "() => (typeof currentPlanId !== 'undefined') ? currentPlanId : null"
        )
        # The list is served from a two-second cache, so the card of a plan
        # that was created a moment ago is not in the first answer.  Go
        # home again until it is -- that is what the sidebar poll does.
        for _ in range(30):
            page.click("#btn-sidebar-home")
            page.wait_for_timeout(1000)
            if page.evaluate(
                "() => document.querySelectorAll('.plan-card').length >= 2"
            ):
                break
        page.wait_for_function(
            "() => document.querySelectorAll('.plan-card').length >= 2",
            timeout=30000,
        )
        page.wait_for_timeout(300)
        report["cards_after_interview"] = card_labels(page)
        for entry in report["cards_after_interview"]:
            if entry[0] == plan_id:
                report["queued_label_after_interview"] = entry[1]
            else:
                report["interview_label"] = entry[1]
                report["interview_card_id"] = entry[0]

        # --- the task view -----------------------------------------------
        page.click(card)
        page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
        page.wait_for_timeout(1200)
        report["tasks_view_visible"] = page.is_visible("#tasks-view")
        report["tasks_list_visible"] = page.is_visible("#tasks-list")
        report["tasks_list_text"] = page.inner_text("#tasks-list")
        report["start_button_visible"] = page.is_visible("#btn-start-execution")

        # --- a start without a working directory is refused ---------------
        page.click("#btn-start-execution")
        page.wait_for_timeout(300)
        report["confirm_button_visible"] = page.is_visible("#btn-confirm-execute")
        page.fill("#project-dir-input", "")
        page.click("#btn-confirm-execute")
        page.wait_for_timeout(2500)
        report["dialogs"] = report["dialogs"]
        report["confirm_button_visible_after"] = page.is_visible("#btn-confirm-execute")

    elif mode == "stopped":
        wait_for_card_gone(page, card)
        page.wait_for_timeout(300)
        report["cards"] = card_labels(page)
        report["stopped_card_count"] = page.locator(card).count()
        page.screenshot(path=str(Path(artifacts_dir) / "stopped.png"), full_page=True)

    elif mode == "requeued":
        wait_for_card(page, card)
        page.wait_for_timeout(300)
        report["requeued_label"] = page.inner_text(card + " .plan-status")
        report["cards"] = card_labels(page)
        report["requeued_card_count"] = page.locator(card).count()

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with (see
    :data:`REAL_HOME_ENV`) so that Playwright can find the browser
    binaries it installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get(REAL_HOME_ENV)
    if real_home:
        env["HOME"] = real_home
    return env


def _run_driver(
    interpreter: str,
    script: Path,
    mode: str,
    base_url: str,
    plan_id: str,
    artifacts_dir: Path,
    requirement: str = "",
) -> Dict[str, Any]:
    """Run one browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            mode,
            f"{base_url}/",
            plan_id,
            str(artifacts_dir),
            requirement,
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
                "the Playwright driver is installed but its browser binaries "
                "are not; the default (browserless) path carries the assertions"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def lifecycle_ui_flow(tmp_path_factory) -> Dict[str, Any]:
    """Walk the whole story once and hand the observations to the tests.

    One walk, many assertions: the story has a beginning (import), a
    middle (a refused start, a real start, a stop) and an end (a requeue),
    and re-running it per assertion would make the suite slow without
    making it stricter.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip("playwright interpreter not found")

    work = tmp_path_factory.mktemp("plan-lifecycle-ui-e2e")
    script_dir = work / "llm-script"
    script_dir.mkdir(parents=True, exist_ok=True)
    _write_scripted_llm(script_dir)
    checkout = _make_checkout(work)

    base_url, proc = _boot_server(work, script_dir)
    plan_id = ""
    observations: Dict[str, Any] = {}
    observations["state_db_path"] = str(work / "state.db")
    try:
        # --- step 1: the backend answers -------------------------------
        observations["plans_list_status"] = _wait_for_api(base_url, "/api/plans")

        # --- step 2: the import parks the plan in the waiting phase ----
        status, body = _http(
            "POST",
            f"{base_url}/api/plan/import",
            {
                "project_dir": str(checkout),
                "content": IMPORT_CONTENT,
                "title": _flow_title(),
            },
            timeout=300,
        )
        observations["import_status"] = status
        observations["import_body"] = body
        plan_id = str((body or {}).get("plan_id") or "") if isinstance(body, dict) else ""
        if not plan_id:
            raise RuntimeError(
                f"the import did not produce a plan (status {status}): {body!r}"
            )
        state = _plan_state(base_url, plan_id)
        observations["plan_id"] = plan_id
        observations["phase_after_import"] = state.get("current_phase")
        observations["phase_label_after_import"] = state.get("current_phase_label")
        observations["listed_after_import"] = _wait_for_plan_in_list(base_url, plan_id)

        artifacts_dir = ARTIFACTS_ROOT / plan_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        observations["artifacts_dir"] = str(artifacts_dir)
        script = work / "drive_page.py"
        script.write_text(_BROWSER_DRIVER, encoding="utf-8")

        # --- steps 3-6: the browser -------------------------------------
        observations["browser_queued"] = _run_driver(
            interpreter, script, "queued", base_url, plan_id, artifacts_dir,
            INTERVIEW_REQUIREMENT,
        )
        observations["phase_after_refused_start"] = _plan_state(
            base_url, plan_id
        ).get("current_phase")

        # --- step 6: the facade refuses a start without a directory -----
        status, body = _http(
            "POST", f"{base_url}/api/plan/{plan_id}/lifecycle/start", {}, timeout=60
        )
        observations["facade_start_without_dir_status"] = status
        observations["facade_start_without_dir_body"] = body

        # --- step 7: start it for real, then stop it --------------------
        status, body = _http(
            "POST",
            f"{base_url}/api/plan/{plan_id}/lifecycle/start",
            {"project_dir": str(checkout)},
            timeout=120,
        )
        observations["facade_start_with_dir_status"] = status
        observations["facade_start_with_dir_body"] = body
        observations["in_flight_before_stop"] = _wait_in_flight(base_url, plan_id)

        status, body = _http(
            "POST",
            f"{base_url}/api/plan/{plan_id}/lifecycle/stop",
            {"requeue": False},
            timeout=120,
        )
        observations["facade_stop_status"] = status
        observations["facade_stop_body"] = body
        observations["phase_after_stop"] = _plan_state(base_url, plan_id).get(
            "current_phase"
        )
        observations["exec_verdict_after_stop"] = _persisted_exec_verdict(
            observations["state_db_path"], plan_id
        )
        observations["listed_after_stop"] = _wait_for_plan_absent_from_list(
            base_url, plan_id
        )

        # --- step 7: what the list shows now ----------------------------
        observations["browser_stopped"] = _run_driver(
            interpreter, script, "stopped", base_url, plan_id, artifacts_dir
        )

        # --- step 8: resume, then stop with a requeue -------------------
        status, body = _http(
            "POST",
            f"{base_url}/api/plan/{plan_id}/lifecycle/start",
            {"project_dir": str(checkout)},
            timeout=120,
        )
        observations["facade_resume_status"] = status
        observations["facade_resume_body"] = body
        observations["in_flight_before_requeue_stop"] = _wait_in_flight(
            base_url, plan_id
        )

        status, body = _http(
            "POST",
            f"{base_url}/api/plan/{plan_id}/lifecycle/stop",
            {"requeue": True},
            timeout=120,
        )
        observations["facade_requeue_stop_status"] = status
        observations["facade_requeue_stop_body"] = body
        observations["phase_after_requeue"] = _plan_state(base_url, plan_id).get(
            "current_phase"
        )
        observations["exec_verdict_after_requeue"] = _persisted_exec_verdict(
            observations["state_db_path"], plan_id
        )
        observations["listed_after_requeue"] = _wait_for_plan_in_list(base_url, plan_id)

        # --- step 8: the card is back ------------------------------------
        observations["browser_requeued"] = _run_driver(
            interpreter, script, "requeued", base_url, plan_id, artifacts_dir
        )
        return observations
    finally:
        _teardown_server(proc, base_url, plan_id)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_end_to_end_queued_then_stop_and_requeue(lifecycle_ui_flow):
    """The whole story, through the real backend and the real page.

    Every step of the brief's eight steps is asserted here, against what
    the system actually does (see the module docstring for the three
    places where the brief's literal wording and the product disagree).
    """
    obs = lifecycle_ui_flow
    plan_id = obs["plan_id"]

    # --- step 2: the import parks the plan in the waiting phase -------
    assert obs["import_status"] == 200, f"the import failed: {obs['import_body']!r}"
    assert obs["phase_after_import"] == "queued", (
        f"an imported plan is parked in the waiting phase; got "
        f"{obs['phase_after_import']!r}"
    )
    assert obs["phase_label_after_import"] == QUEUED_LABEL, (
        f"the waiting phase is labelled {QUEUED_LABEL!r}; got "
        f"{obs['phase_label_after_import']!r}"
    )
    assert obs["listed_after_import"] is True, (
        "the imported plan never showed up in the plan listing"
    )

    # --- step 3: the card says 排队中, and never 就绪 -------------------
    queued = obs["browser_queued"]
    assert queued["queued_label"] == QUEUED_LABEL, (
        f"the plan card of a waiting plan reads {QUEUED_LABEL!r}; got "
        f"{queued['queued_label']!r}"
    )
    assert LEGACY_READY_LABEL not in queued["queued_label"], (
        f"the plan card still carries the old vocabulary "
        f"{LEGACY_READY_LABEL!r}: {queued['queued_label']!r}"
    )
    assert "queued" in (queued["queued_class"] or ""), (
        f"the card's status class names the phase; got "
        f"{queued['queued_class']!r}"
    )

    # --- step 4: the interview creates a second plan -------------------
    assert queued.get("interview_plan_id"), (
        "the interview did not create a plan"
    )
    assert queued.get("interview_label"), (
        "the plan card of the new plan carries no label"
    )
    assert queued["interview_label"] != LEGACY_READY_LABEL, (
        f"a plan created through the interview is labelled with the old "
        f"vocabulary {LEGACY_READY_LABEL!r}: {queued['interview_label']!r}"
    )
    assert queued["interview_label"] == INTERVIEW_LABEL, (
        f"a plan created through the interview is in the interview phase, "
        f"labelled {INTERVIEW_LABEL!r}; got {queued['interview_label']!r}"
    )
    assert queued.get("queued_label_after_interview") == QUEUED_LABEL, (
        f"the waiting plan's card still reads {QUEUED_LABEL!r}; got "
        f"{queued.get('queued_label_after_interview')!r}"
    )

    # --- step 5: the task view -----------------------------------------
    assert queued["tasks_view_visible"] is True, "the task view did not open"
    assert queued["tasks_list_visible"] is True, "the task list is not visible"
    assert "chart" in (queued.get("tasks_list_text") or ""), (
        f"the task list does not show the imported tasks: "
        f"{queued.get('tasks_list_text')!r}"
    )
    assert queued["start_button_visible"] is True, (
        "the start button is not visible in the task view"
    )

    # --- step 6: a start without a working directory is refused --------
    assert queued["confirm_button_visible"] is True, (
        "the start button did not open the execution panel"
    )
    assert queued["dialogs"], (
        "the refused start told the operator nothing -- no alert was raised"
    )
    assert queued["page_errors"] == [], (
        f"the page raised uncaught JavaScript exceptions: {queued['page_errors']!r}"
    )
    assert obs["phase_after_refused_start"] == "queued", (
        f"a refused start left the phase alone; got "
        f"{obs['phase_after_refused_start']!r}"
    )
    assert obs["facade_start_without_dir_status"] == 400, (
        f"the facade refused a start without a working directory with 400; "
        f"got {obs['facade_start_without_dir_status']}"
    )
    detail = obs["facade_start_without_dir_body"]
    detail_text = (
        detail.get("detail") if isinstance(detail, dict) else detail
    )
    assert detail_text, (
        f"the refusal carried no detail: {detail!r}"
    )

    # --- step 7: the stop ------------------------------------------------
    assert obs["in_flight_before_stop"] is True, (
        "the plan never started running -- a stop could not have had "
        "anything to stop"
    )
    assert obs["facade_stop_status"] == 200, (
        f"the facade refused the stop: {obs['facade_stop_body']!r}"
    )
    stop = obs["facade_stop_body"]
    assert stop["berth_phase"] == "stopped", f"berth_phase={stop['berth_phase']!r}"
    assert stop["final_phase"] == "stopped", f"final_phase={stop['final_phase']!r}"
    assert stop["dispatched"] == "execution", (
        f"the stop was not dispatched to the running execution: {stop!r}"
    )
    assert obs["phase_after_stop"] == "stopped", (
        f"the plan's phase is {obs['phase_after_stop']!r} after the stop"
    )

    # --- step 7: what the list shows now --------------------------------
    stopped = obs["browser_stopped"]
    assert obs["listed_after_stop"] is True, (
        "the stopped plan is still in the listing -- a terminal phase must "
        "not be listed"
    )
    assert stopped["stopped_card_count"] == 0, (
        "a stopped plan is a terminal plan -- it must not keep a card in "
        f"the list; the cards were {stopped['cards']!r}"
    )
    for card_id, label in stopped["cards"]:
        assert label != LEGACY_READY_LABEL, (
            f"the list shows the old vocabulary {LEGACY_READY_LABEL!r} for "
            f"plan {card_id!r}"
        )

    # --- step 8: the requeue --------------------------------------------
    assert obs["in_flight_before_requeue_stop"] is True, (
        "the plan was not running before the requeue -- an idle stop is "
        "ignored, requeue included"
    )
    assert obs["facade_requeue_stop_status"] == 200, (
        f"the facade refused the requeue stop: "
        f"{obs['facade_requeue_stop_body']!r}"
    )
    requeue = obs["facade_requeue_stop_body"]
    assert requeue["berth_phase"] == "stopped", (
        f"berth_phase={requeue['berth_phase']!r}"
    )
    assert requeue["final_phase"] == "queued", (
        f"the requeued plan's final phase is {requeue['final_phase']!r}"
    )
    assert obs["phase_after_requeue"] == "queued", (
        f"the plan's phase is {obs['phase_after_requeue']!r} after the requeue"
    )

    # --- step 8: the card is back, saying 排队中 ------------------------
    requeued = obs["browser_requeued"]
    assert obs["listed_after_requeue"] is True, (
        "the requeued plan did not come back to the listing"
    )
    assert requeued["requeued_card_count"] == 1, (
        f"the requeued plan has no card in the list; the cards were "
        f"{requeued['cards']!r}"
    )
    assert requeued["requeued_label"] == QUEUED_LABEL, (
        f"the requeued plan's card reads {QUEUED_LABEL!r}; got "
        f"{requeued['requeued_label']!r}"
    )
    assert LEGACY_READY_LABEL not in requeued["requeued_label"], (
        f"the requeued plan's card carries the old vocabulary "
        f"{LEGACY_READY_LABEL!r}: {requeued['requeued_label']!r}"
    )


def test_the_requeue_retires_the_stopped_execution_verdict(lifecycle_ui_flow):
    """A plan that waits again has no terminal verdict left over.

    The run's verdict and the workflow's phase are two different
    answers, and the listing filters on **both** (``routes/plans.py``,
    ``_plan_is_terminal``): a run that was **stopped** ends the plan,
    whatever the phase says.  The verdict it reads is the **persisted**
    one (``plan_execution.exec_status``) -- not the in-memory record
    ``/api/plan/{id}/summary`` reports, which keeps saying "stopped"
    after a requeue while the row no longer does.

    That is right for a plan that is over.  It is wrong for a plan that
    was stopped and then **queued again**: such a plan has no verdict
    left -- it is waiting for the next slot, not finished.  Leaving the
    stopped verdict on the row kept the requeued plan out of the
    listing, so its card never appeared and the page showed no
    "排队中" label for it at all.

    So the requeue retires the verdict, and this pins that fact: the
    verdict is terminal before the requeue, and is not afterwards.  The
    browser test above pins the consequence (the card is back and says
    "排队中"); this pins the cause.
    """
    obs = lifecycle_ui_flow

    assert obs["exec_verdict_after_stop"] in TERMINAL_EXEC_VERDICTS, (
        f"the stopped plan's verdict is {obs['exec_verdict_after_stop']!r}, "
        f"which is not one of the terminal verdicts "
        f"{sorted(TERMINAL_EXEC_VERDICTS)} -- the run was stopped, so its "
        f"verdict should be"
    )
    assert obs["exec_verdict_after_requeue"] not in TERMINAL_EXEC_VERDICTS, (
        f"the requeued plan still carries the terminal verdict "
        f"{obs['exec_verdict_after_requeue']!r} -- the listing filters it "
        f"out, so its card never appears and no '排队中' label is shown"
    )


def test_screenshots_are_written(lifecycle_ui_flow):
    """Both screenshots are on disk, and neither of them is empty."""
    artifacts_dir = Path(lifecycle_ui_flow["artifacts_dir"])
    for name in ("queued.png", "stopped.png"):
        path = artifacts_dir / name
        assert path.exists(), f"{path} was not written"
        assert path.stat().st_size > 0, f"{path} is empty"


# ---------------------------------------------------------------------------
# The isolation of one run from another
# ---------------------------------------------------------------------------
#
# The plan id of this run reaches outside the run: it is an argument of
# the browser driver, and it is the needle the teardown uses to find the
# executor that outlived the server.  Two things follow from that, and
# both of them are asserted below -- on a machine that runs two copies
# of this file at once (a CI shard next to a developer's own run), the
# old arrangement had one run kill the other run's driver.


def test_two_runs_do_not_share_a_plan_id():
    """The import title carries a token, so two runs get two plan ids.

    The plan id is derived from the import title, and the plans
    directory is per-run -- so a constant title gave every run on the
    machine the very same id, and the id is what the teardown (and the
    driver command line) is keyed on.
    """
    first = _import_title()
    second = _import_title()
    assert first != second, "two runs got the same import title"
    assert derive_plan_id(first) != derive_plan_id(second), (
        f"two runs got the same plan id: {derive_plan_id(first)!r}"
    )


def test_the_teardown_kills_the_executor_and_nothing_else(tmp_path):
    """The teardown signals the plan's executor -- and only the executor.

    The leftover executor used to be found with ``pgrep -f <plan id>``,
    which matches *every* command line that mentions the id: the browser
    driver of this run (it takes the id as an argument) and the driver
    of every other run on the machine.  A teardown that kills a
    stranger's driver fails a test it did not run -- and it did, with
    ``exit -15`` and an empty stdout.
    """
    plan_id = "20260101-teardown-probe"
    # A process that merely *mentions* the plan id -- what the browser
    # driver of another run looks like.
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", plan_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # The plan's own executor: ``cli.py --recover`` with the plan's
    # tasks file (the plan id is in that path).
    executor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            "cli.py",
            "--recover",
            "-w",
            str(tmp_path),
            "--tasks-file",
            str(tmp_path / "plans" / plan_id / "tasks.json"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _kill_leftover_executors(plan_id)
        deadline = time.time() + 15.0
        while time.time() < deadline and executor.poll() is None:
            time.sleep(0.2)
        assert executor.poll() is not None, (
            "the executor that outlived the server was not killed"
        )
        assert executor.returncode == -signal.SIGTERM, (
            f"the leftover executor was not stopped with SIGTERM; got "
            f"{executor.returncode}"
        )
        time.sleep(1.0)
        assert bystander.poll() is None, (
            "the teardown killed a process that only mentioned the plan id "
            f"(exit {bystander.returncode})"
        )
    finally:
        for proc in (bystander, executor):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# How a sibling module is imported
# ---------------------------------------------------------------------------
#
# ``backend/tests/e2e/`` **is** a package: ``backend/tests/__init__.py`` and
# ``backend/tests/e2e/__init__.py`` have been tracked since 720ceb5.  A
# sibling is therefore imported as ``tests.e2e.<name>``.
#
# A **bare** name -- ``from test_plan_lifecycle_ui_e2e import ...`` -- is
# the spelling that only resolves while pytest happens to put this
# directory on ``sys.path``.  The moment the directory is a package, that
# spelling stops resolving: the collection dies with an ``ImportError``
# before a single case runs, and the whole E2E lane goes red for a reason
# that has nothing to do with the code under test.
#
# The import *mode* is not the thing to change: pytest's ``pythonpath``
# and ``importmode`` are left exactly as they are, and the spelling of
# the import is what carries the fix.  This check reads the source, so it
# keeps the spelling honest no matter which pytest is doing the collecting.


#: The directory the E2E modules live in.
E2E_DIR = Path(__file__).resolve().parent

#: The prefix a sibling's **bare** name carries.  A package-qualified
#: import starts with ``tests.``, so only a name whose *first* component
#: starts with this prefix is a bare import of a sibling module.
_BARE_MODULE_PREFIX = "test_"


def _bare_sibling_imports(e2e_dir: Path) -> List[str]:
    """Every bare ``from test_... import ...`` in the E2E modules.

    The modules are read as **source** and parsed, rather than imported:
    the question is what the file says, and a file that cannot be
    imported is exactly the file whose spelling is in doubt.  Parsing
    also keeps the strings that *mention* an import -- the synthetic
    scripts some of these modules write out -- out of the answer: a
    script that is handed its own directory on ``sys.path`` imports by
    bare name on purpose.

    One line per offence, ``file:line`` and the import that is wrong, so
    the message of a failure names the line to change.
    """
    offenders: List[str] = []
    for module_path in sorted(e2e_dir.glob("test_*.py")):
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # A relative import (``from . import x``) names something
                # inside the package: it is spelled correctly already.
                if node.level:
                    continue
                module = node.module or ""
                if not module.split(".")[0].startswith(_BARE_MODULE_PREFIX):
                    continue
                offenders.append(
                    f"{module_path.name}:{node.lineno}: from {module} import ..."
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if not alias.name.split(".")[0].startswith(_BARE_MODULE_PREFIX):
                        continue
                    offenders.append(
                        f"{module_path.name}:{node.lineno}: import {alias.name}"
                    )
    return offenders


def test_the_sibling_modules_are_imported_by_package_name():
    """A sibling is imported as ``tests.e2e.<name>`` -- never by bare name.

    The E2E modules used to import their siblings by bare name, which
    only resolves while pytest happens to put this directory on
    ``sys.path``.  With ``backend/tests/e2e/`` being a package, that
    spelling dies at collection time with an ``ImportError`` -- and the
    lane that carries the browser replay goes red before a single case
    runs.

    So the spelling is pinned here: a bare sibling import in this
    directory is a failure, and it says which line to change.  The
    import mode is not touched -- the modules are read as source, so the
    check does not depend on which pytest is doing the collecting.
    """
    offenders = _bare_sibling_imports(E2E_DIR)
    assert not offenders, (
        "these E2E modules import a sibling by its bare name, which only "
        "resolves while pytest happens to put the directory on sys.path:\n"
        + "\n".join(offenders)
        + "\nA sibling is imported as ``tests.e2e.<name>`` -- "
        "``backend/tests/e2e`` is a package."
    )


# ---------------------------------------------------------------------------
# Which sibling actually gets imported
# ---------------------------------------------------------------------------
#
# ``tests.e2e.<name>`` is resolved through ``sys.path``, and ``sys.path``
# can carry more than one ``backend/``: a shared virtualenv, a
# ``PYTHONPATH`` left over from another checkout, an editable install.
# When it carries two, the sibling that gets imported may be the *other*
# tree's copy -- and then these modules run against a file that is not
# the one next to them.  The symptom is a missing helper, which reads
# like a broken module rather than like a wrong import.
#
# So the import is pinned to the directory the importing module lives in:
# the sibling that is loaded has to be the file next to it.


def _loaded_module_path(module_name: str) -> Path:
    """Where ``module_name`` was actually loaded from."""
    module = importlib.import_module(module_name)
    return Path(module.__file__).resolve()


def test_the_sibling_module_is_loaded_from_this_directory():
    """The imported sibling is the file that sits next to this one.

    The E2E modules import each other by package name, and that name is
    resolved through ``sys.path``.  A machine that has two checkouts of
    this repository on ``sys.path`` at once -- which a shared
    virtualenv plus a leftover ``PYTHONPATH`` produces -- resolves the
    name to whichever ``backend/`` comes first, not necessarily to this
    tree.

    So the loaded file is checked against this directory: the sibling
    that these modules run has to be the one beside them.
    """
    loaded = _loaded_module_path("tests.e2e.test_plan_lifecycle_ui_e2e")
    assert loaded.parent == E2E_DIR, (
        f"``tests.e2e.test_plan_lifecycle_ui_e2e`` was loaded from "
        f"{loaded}, which is not inside {E2E_DIR}. Another checkout of this "
        "repository is ahead of this one on sys.path, so these modules "
        "would run against a file that is not the one next to them."
    )


# ---------------------------------------------------------------------------
# The modules in this directory are Python
# ---------------------------------------------------------------------------
#
# A module that cannot be parsed takes the whole lane down at collection
# time, before a single case runs: the failure is reported as a
# collection error on four modules at once, and nothing in the output
# says that the file was overwritten.  So the modules in this directory
# are parsed here, and a file that is not valid Python is named.


def _modules_that_do_not_parse(e2e_dir: Path) -> List[str]:
    """Every module in ``e2e_dir`` that is not valid Python.

    :param e2e_dir: The directory to scan.
    :return: One line per broken module -- ``name:line: message``.
    """
    broken: List[str] = []
    for module_path in sorted(e2e_dir.glob("test_*.py")):
        try:
            ast.parse(
                module_path.read_text(encoding="utf-8"), filename=str(module_path)
            )
        except SyntaxError as exc:
            broken.append(f"{module_path.name}:{exc.lineno}: {exc.msg}")
    return broken


def test_the_e2e_modules_are_valid_python():
    """Every module in this directory is valid Python.

    A file that is not valid Python cannot be collected, so the failure
    lands on the lane rather than on the file that caused it.  Reading
    the sources here names the file instead.
    """
    broken = _modules_that_do_not_parse(E2E_DIR)
    assert not broken, (
        "these E2E modules are not valid Python:\n"
        + "\n".join(broken)
        + "\nA module that cannot be parsed takes the whole lane down at "
        "collection time, before a single case runs."
    )
