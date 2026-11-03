"""端到端验收：判死之后那一轮真的停了（2026-11-02）。

前面的那些任务改的都是**单元层面**的东西：死进程的清理、租约的强制释放、
僵尸轮次的作废标记。这一份把验收推到**端到端** —— 真后端、真进程、真浏览器。

要证明的是一句话：**判死之后那一轮真的停了**。

    * 判死的那一刻还在跑的那个进程，**真的死了**（不是「应该死」）；
    * 那个计划的工作流租约，**真的被放开了**（不是「应该放开」）；
    * 放开之后，**新的一轮真的能起来**（不是「应该能起来」）；
    * 仪表盘上，**真的显示的是失败**（不是「应该显示失败」）。

现场是真的
----------

起一个真的 ``backend.server`` 子进程，喂它一个**会挂住**的验证轮。

「挂住」是真的挂住：``PATH`` 上放一个假的 ``claude``，它记下自己的 pid，
然后永远睡下去。真的验证轮会把真的 coding tool 拉起来，真的 coding tool
会真的把这个假 ``claude`` 拉成子进程 —— 于是这一轮是真的在跑一个真的
进程，而不是一个 mock。

判死也是真的判死：这一轮挂着不动，它写的日志仍然是新鲜的（心跳被调快到
5 秒，所以日志这一路永远是「新」的，判死走的是**结果文件**那条信号），
于是 ``_lazy_check_verification`` 的判定阶梯会落到
``verification_results_stale``，然后走完整的死亡序列：

    cleanup → force-release → invalidate → 终态写盘

三件事因此是可以被看见的，而不是被相信的：

* 计划的 ``process_ledger.jsonl`` 记着这一轮那个进程**起**过、
  **过**了 —— 带着 pid 的那条记录说的那个 pid **真的不在了**
  （``os.kill(pid, 0)`` 抛 ``ProcessLookupError``）；
* 计划目录里的 ``.verification_round.lock`` **真的没了**；
* 计划的状态 **真的** 变成了 ``failed``，而且**只**写了一次终态 ——
  写盘次数就是 ``KIND_PLAN_CLOSED`` 卡片事件的次数。

台账里那条记录长什么样：这一轮挂住的那个进程是**验证子代理**拉起来的，
所以它落在清理路径的「子代理」那一支里 —— 台账记的是它的 ``spawn``
和它的 ``exit``（不是「轮次作用域」那一支的 ``killed``；那一支管的是
编排器自己拉起的 ``coding_query_*`` 子进程）。两份记录都说的是同一件事：
**这个 pid 的进程真的结束了**。

三个用例
--------

* ``test_e2e_death_actually_kills_the_round`` —— 判死之后进程真的死了、
  租约真的放开了、终态只写了一次。
* ``test_e2e_second_start_is_accepted_after_death`` —— 判死之后再来一次
  ``/start`` **被接受**，轮次真的往前走了一位，而且新的一轮拿到的是
  **另一个** token 的租约（租约真的被交出去了）。
* ``test_e2e_dashboard_shows_failed_after_death`` —— 浏览器点开计划、
  点「启动验证」，等这一轮被判死，然后页面上的徽标**真的**是「验证失败」。

复用
----

``_free_port`` / ``_seed`` / ``_boot`` / ``_wait_ready`` / ``_teardown``
按**文件路径**从 ``test_execution_status_restart_e2e.py`` 借来
（``backend/tests/e2e/`` 没有 ``__init__.py``，跨模块 import 依赖 pytest
的 rootdir gymnastics，太脆）。那份文件**一个字都不改**。

边界
----

* 端口被占 → 换一个空闲端口**重试一次**（``_free_port`` 挑的是内核给的
  端口，但挑到和绑上之间仍有可能被别人抢走）；
* 没有 Chromium → 浏览器那一段跳过，并在报告里说明；
* 失败路径也要收摊：``_teardown`` 停后端，``os.killpg`` 兜底；
* 截图落在 pytest 的临时目录里，**不进仓库**。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
#: 仓库根 —— 「截图没有落进工作区」这条断言的基准面。
REPO_ROOT = PROJECT_ROOT

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: 判死那一场用的计划。计划目录、``plan_routing`` 行、台账都以它为键。
PLAN_DEATH = "e2e-verification-death"

#: 浏览器那一场用的计划。**两个计划**：浏览器那一场要点「启动验证」，
#: 点了之后徽标就变成「验证中」，于是它没法再当「徽标是失败」的证据。
PLAN_UI = "e2e-verification-death-ui"

#: 等待判死落地的预算（秒）。判死要等两样东西：60 秒的启动宽限期
#: （``_VERIFICATION_STARTUP_GRACE_SECONDS``，生产里写死的），以及守护
#: 进程的心跳跳（30 秒一次）。所以它落在 60~90 秒之间。
DEATH_TIMEOUT_SECONDS = 240.0

#: 等判死之后的第二次 ``/start`` 把新的一轮拉起来（抢调度租约要等
#: 30 秒）—— 预算给得比死亡本身宽。
SECOND_START_TIMEOUT_SECONDS = 120.0

#: 浏览器在等各个页面元素时的预算（毫秒）。没有固定 sleep：一个等不到的
#: 等待就是一次带原因的失败。
BROWSER_TIMEOUT_MS = 30000

#: 等「验证失败」出现在徽标上的预算（毫秒）。判死落在点击之后
#: 60~90 秒，所以这个预算必须比它宽。
BROWSER_DEATH_TIMEOUT_MS = 150000

#: 整段浏览器过程的预算（秒）。
DRIVER_TIMEOUT_SECONDS = 300

#: 截图的文件名。它落在 pytest 的临时目录里 —— **不在**仓库工作区里。
SCREENSHOT_NAME = "after_death.png"

#: ``install_hermetic_env()`` 把 ``$HOME`` 指向了一个临时目录；Playwright
#: 的浏览器二进制装在**跑起来时那个 home** 底下。这个变量记着那个 home。
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

#: 判死那一轮用的验证点：一个 ``code_review``。真的验证轮会真的去跑它，
#: 于是真的 coding tool 会真的把假 ``claude`` 拉成子进程。
SEEDED_VERIFICATION_POINTS = (
    {
        "id": "VP-001",
        "title": "审查代码",
        "related_prd_criteria": ["AC-001"],
        "verification_method": "code_review",
        "priority": "high",
        "expected_result": "ok",
    },
)

#: 判死要靠的那条信号：**结果文件**多久没动过了。种子里写一份两小时前的
#: 结果文件，守护进程的判定阶梯就有一条现成的「陈旧」证据。
STALE_ARTIFACT_AGE_SECONDS = 7200.0

#: 守护进程的两条阈值（环境变量）。判定阶梯读到的是**结果文件**的年龄，
#: 两条阈值都调到「已经越过硬上限」的位置，判死才会在宽限期一过就落地，
#: 而不是再等一个钟头。
WATCHDOG_ENV = {
    "VERIFICATION_WATCHDOG_STALENESS_SECONDS": "20",
    "VERIFICATION_HARD_STALE_SECONDS": "60",
    # 心跳调快：coding tool 每 5 秒往 ``execution.log`` 写一次进度，
    # 于是「日志陈旧」那一路永远是新鲜的，判死只可能走结果文件那一路 ——
    # 也就是下面种出来的那一条。
    "PDT_CODING_QUERY_PROGRESS_SECONDS": "5",
}

#: 后端日志里那几行判死的证据。
DEATH_SEQUENCE_RE = re.compile(
    r"\[verification_watchdog\] death sequence plan=(?P<plan>\S+) "
    r"round=(?P<round>\S+) stop_reason=(?P<reason>\S+) "
    r"cleaned=(?P<cleaned>\S+) lease_released=(?P<lease>\S+) "
    r"invalidated=(?P<invalidated>\S+)"
)
TERMINAL_ENTRY_RE = re.compile(
    r"\[verification_terminal\] entry plan=(?P<plan>\S+) "
    r"status=(?P<status>\S+) stop_reason=(?P<reason>\S+)"
)


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E files
# ---------------------------------------------------------------------------


def _load_sibling(module_name: str, filename: str):
    """Load a sibling E2E module **by file path**, without modifying it.

    ``backend/tests/e2e/`` 的包结构会变，跨模块 import 依赖 pytest 的
    rootdir gymnastics；按文件路径加载避开那一份脆弱性，也不复制一行被
    借走的代码。
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


#: 真后端进程、种子、空闲端口、收摊。**一个字都不改**。
_RESTART = _load_sibling(
    "pdt_execution_status_restart_helpers",
    "test_execution_status_restart_e2e.py",
)
#: 浏览器助手：Chromium 的发现逻辑。
_TOKEN = _load_sibling(
    "pdt_token_convergence_helpers",
    "test_token_convergence_ui_acceptance.py",
)

_free_port = _RESTART._free_port
_seed = _RESTART._seed
_boot = _RESTART._boot
_wait_ready = _RESTART._wait_ready
_teardown = _RESTART._teardown


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

#: 每个 ``/api/*`` 请求都要带这个头（``backend/request_guard.py``），
#: 不带就是 403，请求根本到不了路由。
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}


def _http_json(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    """One HTTP request against the backend; ``(status, decoded body)``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in REQUEST_HEADERS.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8", "replace")


def _backend_log(proc: subprocess.Popen) -> str:
    """The backend's own output, so far."""
    path = getattr(proc, "_pdt_log_path", None)
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - the file is ours
        return ""


def _pid_is_alive(pid: int) -> bool:
    """True when the kernel still knows this pid."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, just not ours
        return True
    except OSError:  # pragma: no cover - defensive
        return False
    return True


def _wait_for_pid_gone(pid: int, timeout: float = 15.0) -> bool:
    """Wait until the kernel no longer knows this pid.

    The kill is a signal, not an instant: the process has to be reaped
    before its pid leaves the process table.  A pid that is still there
    a second later is a pid that outlived the judgement.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_is_alive(pid)


def _event_pid(event: Dict[str, Any]) -> int:
    """The pid a ledger event names, or ``0`` when it names none."""
    try:
        return int(event.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _plan_dir(work: Path, plan_id: str) -> Path:
    """Where the plan's own directory lives inside the test's tree."""
    return work / "plans" / plan_id


def _workflow_lease_path(work: Path, plan_id: str) -> Path:
    """The workflow-level lease file — the one the death sequence releases."""
    return _plan_dir(work, plan_id) / ".verification_round.lock"


def _lease_holder_token(path: Path) -> Optional[str]:
    """The round token the lease file carries, if it carries one."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = record.get("round_token")
    return str(token) if token else None


def _ledger_events(work: Path, plan_id: str) -> List[Dict[str, Any]]:
    """The plan's own durable record of the processes it ran.

    ``process_ledger.jsonl`` is append-only: one JSON object per line,
    each one an event about a process the plan started or ended.
    """
    path = _plan_dir(work, plan_id) / "process_ledger.jsonl"
    events: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _routing_phase(work: Path, plan_id: str) -> Optional[str]:
    """The plan's phase, as the state machine sees it."""
    import sqlite3

    try:
        conn = sqlite3.connect(str(work / "state.db"))
    except sqlite3.Error:  # pragma: no cover - defensive
        return None
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - defensive
        return None
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _verification_status(base_url: str, plan_id: str) -> Dict[str, Any]:
    """What the verification status endpoint says about the plan."""
    status, body = _http_json("GET", f"{base_url}/api/verification/{plan_id}/status")
    if status != 200 or not isinstance(body, dict):
        return {}
    return body


def _wait_for_death(base_url: str, plan_id: str, timeout: float) -> Dict[str, Any]:
    """Wait until the plan's verification is written off as failed."""
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _verification_status(base_url, plan_id)
        if str(last.get("verification_status") or "") == "failed":
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"the verification of {plan_id} was not judged dead within "
        f"{timeout}s; last status={last!r}"
    )


def _wait_for_round(
    base_url: str, plan_id: str, round_number: int, timeout: float,
) -> Dict[str, Any]:
    """Wait until the status endpoint reports the given round."""
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _verification_status(base_url, plan_id)
        if int(last.get("verification_round") or 0) >= round_number:
            return last
        time.sleep(0.5)
    raise AssertionError(
        f"the verification of {plan_id} did not reach round {round_number} "
        f"within {timeout}s; last status={last!r}"
    )


# ---------------------------------------------------------------------------
# The fake ``claude``: the round that hangs
# ---------------------------------------------------------------------------


def _install_fake_claude(work: Path) -> Path:
    """Put a fake ``claude`` on the PATH — one that hangs forever.

    The coding tool spawns ``claude`` **from the PATH**; this script is
    what it finds first.  It records its own pid (so the test can check
    that the pid the ledger names is a pid this thing really had) and
    then sleeps forever, so the round that spawned it hangs exactly the
    way a wedged round hangs: with a live child process.

    It is a real process, in its own session (``exec`` keeps the pid),
    so a group signal reaches it and only it.
    """
    bindir = work / "fakebin"
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$$" >> "{pids}"\n'
        "exec sleep 100000\n".format(pids=work / "claude.pids"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bindir


def _fake_claude_pids(work: Path) -> List[int]:
    """Every pid the fake ``claude`` has ever had."""
    try:
        text = (work / "claude.pids").read_text(encoding="utf-8")
    except OSError:
        return []
    pids: List[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _kill_fake_claudes(work: Path) -> None:
    """Every fake ``claude`` this test started is gone.

    The fixture's teardown does this whether the tests passed or not:
    a ``sleep 100000`` left running would outlive the test session.
    """
    for pid in _fake_claude_pids(work):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def _seed_death_plan(work: Path, plan_id: str) -> Path:
    """Write a plan whose verification round will hang, and be judged dead.

    Four pieces:

    * ``plans/<id>/tasks.json`` + ``plan_routing`` 行 —— 借来的 ``_seed``
      写的那些：没有任务列表，计划在列表里就没有这张卡；
    * ``plan_execution`` 行（``project_dir``）—— 启动接口从**行**里读
      工作目录，不从请求体里读；
    * ``plans/<id>/verification_plan.json`` —— 一个真的验证点，真的验证轮
      会真的去跑它，于是真的 coding tool 会真的把假 ``claude`` 拉起来；
    * ``plans/<id>/verification_execution_results.json`` —— **两小时前**
      的结果文件。守护进程的判定阶梯读的就是它：日志那一路被调快的心跳
      顶住了（新），只有这一路是陈旧的。
    """
    _seed(work, plan_id)
    plan_dir = _plan_dir(work, plan_id)

    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {"verification_points": list(SEEDED_VERIFICATION_POINTS)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    results = plan_dir / "verification_execution_results.json"
    results.write_text(
        json.dumps({"seeded": "a stale artefact, two hours old"}),
        encoding="utf-8",
    )
    old = time.time() - STALE_ARTIFACT_AGE_SECONDS
    os.utime(results, (old, old))

    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(str(work / "state.db"))
    try:
        migrate(conn)
        # ``executing`` 是启动验证的一个合法来源相位；改相位用 CAS，
        # 跟生产里写相位的是同一条路。
        RoutingRepository(conn).try_mark_phase(
            plan_id, ("queued",), "executing",
        )
        ExecutionRepository(conn).insert(
            plan_id, project_dir=str(work / "target"),
        )
    finally:
        conn.close()
    return plan_dir


# ---------------------------------------------------------------------------
# The backend process
# ---------------------------------------------------------------------------


def _boot_backend(work: Path) -> Tuple[subprocess.Popen, int, str]:
    """Boot the backend; if the port was taken, take another and retry once.

    ``_free_port`` asks the kernel for a port, so the port is free at the
    moment it is handed back — but the gap between "asked for" and "bound"
    is real, and a squatter in that gap makes the boot die with
    ``[Errno 48] Address already in use``.  That is the one failure worth
    retrying; anything else is a real failure and is raised at once.
    """
    last: Optional[BaseException] = None
    for attempt in (1, 2):
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc = _boot(work, port)
        try:
            _wait_ready(base_url, proc)
        except AssertionError as exc:
            last = exc
            _teardown(proc)
            output = str(exc).lower()
            taken = (
                "address already in use" in output
                or "errno 48" in output
                or "errno 98" in output
            )
            if attempt == 2 or not taken:
                raise
            continue
        return proc, port, base_url
    raise AssertionError(f"the backend never came up: {last!r}")


# ---------------------------------------------------------------------------
# The Chromium discovery (borrowed from the upstream acceptance file)
# ---------------------------------------------------------------------------


def _with_real_home(func):
    """Call ``func()`` with ``$HOME`` pointing at the launching home."""
    real_home = os.environ.get(REAL_HOME_ENV)
    if not real_home:
        return func()
    previous = os.environ.get("HOME")
    os.environ["HOME"] = real_home
    try:
        return func()
    finally:
        if previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous


def _upstream_browser_executable() -> Optional[str]:
    """``browser_executable()`` from the upstream acceptance file.

    它住在 ``test_token_convergence_ui_acceptance.py`` 的**驱动源码字符串**
    里面（那里它不是模块级的名字），所以这里用 ``ast`` 把它从那段源码里
    取出来执行 —— 借的是上游那一份实现，本文件不另写一份。

    ``$HOME`` 在 pytest 树里被重定向到了临时目录，而 Chromium 装在**跑起来
    那个 home** 底下；所以调用期间把 ``$HOME`` 临时换回去。
    """
    tree = ast.parse(_TOKEN._DRIVER)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "browser_executable":
            namespace: Dict[str, Any] = {"Path": Path}
            exec(  # noqa: S102  # nosec B102 - the upstream helper, verbatim
                compile(
                    ast.Module(body=[node], type_ignores=[]),
                    "<upstream-browser-driver>",
                    "exec",
                ),
                namespace,
            )
            return _with_real_home(namespace["browser_executable"])
    raise AssertionError(
        "the upstream acceptance file no longer carries a "
        "browser_executable() helper -- the browser discovery has to be "
        "re-pointed at whoever owns it now"
    )


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the dashboard the way an operator does, and report what it shows.

argv: <base_url> <plan_id> <screenshot_path> <chromium_executable>

Every step is a click on the page, so the frontend's own handlers run.
Nothing here reaches into the application's internals to make a panel
show up or a badge change.

The report is one JSON object on stdout.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


base_url, plan_id, screenshot_path, chromium = sys.argv[1:5]

#: How long the browser waits for each of the things it needs.
timeout_ms = 30000

#: How long the browser waits for the round to be judged dead.
death_timeout_ms = 150000

report = {"page_errors": [], "console_errors": [], "dialogs": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=chromium)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

    def _on_dialog(dialog):
        report["dialogs"].append(dialog.message or "")
        try:
            dialog.dismiss()
        except Exception:
            pass

    page.on("dialog", _on_dialog)

    def _on_console(message):
        if message.type != "error":
            return
        text = message.text or ""
        # A resource that 404s is reported by the browser as a console
        # error; the dashboard asks for a handful of optional assets.
        if "Failed to load resource" in text:
            return
        report["console_errors"].append(text)

    page.on("console", _on_console)

    page.goto(base_url, wait_until="load")
    page.wait_for_selector("#app", timeout=timeout_ms)

    # --- 1: the plan card ---------------------------------------------
    card = '.plan-card[data-plan="%s"]' % plan_id
    page.wait_for_selector(card, timeout=timeout_ms)
    report["plan_card_visible"] = page.is_visible(card)

    # --- 2: the user clicks it -----------------------------------------
    page.click(card)
    page.wait_for_selector(".task-card", timeout=timeout_ms)
    page.wait_for_selector(
        "#verification-panel", state="visible", timeout=timeout_ms
    )
    report["verification_panel_visible"] = page.is_visible("#verification-panel")

    # The round has not started yet, so the badge still carries its
    # initial value and the start button is on offer.
    report["badge_before_start"] = (
        page.inner_text("#verification-status-badge") or ""
    ).strip()
    report["round_before_start"] = (
        page.inner_text("#verification-round") or ""
    ).strip()
    report["start_button_visible_before_start"] = page.is_visible(
        "#btn-start-verification"
    )

    # --- 3: the user starts the verification ---------------------------
    page.click("#btn-start-verification")

    # The round started: the badge leaves its initial value.
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            if (!badge) return false;
            const text = (badge.textContent || '').trim();
            return text !== '' && text !== '未开始';
        }""",
        timeout=timeout_ms,
    )
    report["badge_after_start"] = (
        page.inner_text("#verification-status-badge") or ""
    ).strip()
    report["round_after_start"] = (
        page.inner_text("#verification-round") or ""
    ).strip()

    # --- 4: the round is judged dead ----------------------------------
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            if (!badge) return false;
            const text = (badge.textContent || '').trim();
            return text === '验证失败' || text === '失败';
        }""",
        timeout=death_timeout_ms,
    )
    report["badge_after_death"] = (
        page.inner_text("#verification-status-badge") or ""
    ).strip()
    report["round_after_death"] = (
        page.inner_text("#verification-round") or ""
    ).strip()

    # A finished round is offered no start control: the frontend hides it
    # on purpose (there is no way to start a round again from the UI, a
    # failed round is recovered through /reset + /start).
    report["start_button_visible_after_death"] = page.is_visible(
        "#btn-start-verification"
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


def _current_interpreter_has_playwright() -> bool:
    """Can the interpreter running pytest import ``playwright.sync_api``?"""
    probe = subprocess.run(
        [sys.executable, "-c", "import playwright.sync_api"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` 指向回跑起来时的那个 home（见 :data:`REAL_HOME_ENV`），这样
    Playwright 找得到它装在那儿的浏览器二进制。
    """
    env = dict(os.environ)
    real_home = os.environ.get(REAL_HOME_ENV)
    if real_home:
        env["HOME"] = real_home
    return env


def _parse_report(stdout: str) -> Dict[str, Any]:
    """The report the driver printed — the last non-empty line, as JSON."""
    lines = [line for line in (stdout or "").strip().splitlines() if line.strip()]
    if not lines:
        raise AssertionError("the browser driver printed nothing")
    return json.loads(lines[-1])


def _drive_in_process(argv: List[str]) -> Dict[str, Any]:
    """Run the driver inside this interpreter (it has playwright)."""
    buffer = io.StringIO()
    previous_argv = list(sys.argv)
    # The driver reads its arguments the way a script does -- ``argv[1:]``
    # -- so the program name has to be there too.
    sys.argv = ["<verification-death-browser-driver>", *argv]
    try:
        with redirect_stdout(buffer):
            exec(  # noqa: S102  # nosec B102 - the driver, run verbatim
                compile(_BROWSER_DRIVER, "<verification-death-browser-driver>", "exec"),
                {"__name__": "__main__"},
            )
    finally:
        sys.argv = previous_argv
    return _parse_report(buffer.getvalue())


def _drive_in_subprocess(
    interpreter: str, argv: List[str], work: Path,
) -> Dict[str, Any]:
    """Run the driver as its own process, with an interpreter that has playwright."""
    script = work / "drive_verification_death.py"
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
    base_url: str, plan_id: str, screenshot_path: Path, work: Path,
) -> Dict[str, Any]:
    """Drive the page and return what it showed."""
    pytest.importorskip(
        "playwright.sync_api",
        reason="playwright is not installed — the browser half is skipped",
    )
    chromium = _upstream_browser_executable()
    if not chromium:
        pytest.skip(
            "chromium is not installed — the browser half is skipped"
        )

    argv = [base_url, plan_id, str(screenshot_path), chromium]
    if _current_interpreter_has_playwright():
        return _drive_in_process(argv)
    raise AssertionError(
        "the interpreter running this test cannot import playwright.sync_api"
    )


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def death_env(tmp_path_factory) -> Dict[str, Any]:
    """A real backend, a round that hangs, and the death that follows.

    The sequence is the point: a plan whose verification round hangs on a
    real process, a watchdog that judges it dead, and the evidence that
    the judgement really stopped the round.
    """
    work = tmp_path_factory.mktemp("verification-death-e2e")
    bindir = _install_fake_claude(work)
    _seed_death_plan(work, PLAN_DEATH)
    _seed_death_plan(work, PLAN_UI)

    # The fake ``claude`` has to be the first ``claude`` the backend finds,
    # and the watchdog's thresholds have to be the ones that make the death
    # land inside this test's budget.  ``_boot`` copies the environment of
    # this process, so the variables are set here and put back afterwards.
    saved_env = {
        key: os.environ.get(key)
        for key in list(WATCHDOG_ENV) + ["PATH"]
    }
    os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ.update(WATCHDOG_ENV)

    proc: Optional[subprocess.Popen] = None
    try:
        proc, port, base_url = _boot_backend(work)

        # The round of PLAN_DEATH is started the way an operator starts it.
        status, body = _http_json(
            "POST",
            f"{base_url}/api/verification/{PLAN_DEATH}/start",
            {"project_dir": str(work / "target")},
        )
        assert status == 200, (
            f"the verification of {PLAN_DEATH} refused to start: "
            f"{status} {body!r}"
        )

        death_status = _wait_for_death(
            base_url, PLAN_DEATH, DEATH_TIMEOUT_SECONDS,
        )

        yield {
            "base_url": base_url,
            "port": port,
            "work": work,
            "proc": proc,
            "death_status": death_status,
        }
    finally:
        # The failure path stops the backend too.
        if proc is not None:
            _teardown(proc)
            if proc.poll() is None:  # pragma: no cover - defensive
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (OSError, AttributeError):
                    pass
        _kill_fake_claudes(work)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# 1. The judgement stopped the round
# ---------------------------------------------------------------------------


def test_e2e_death_actually_kills_the_round(death_env):
    """判死之后，那一轮真的停了。

    三件事，一件都不能只是「应该」：

    * 判死的那一刻还在跑的那个进程，**真的死了** —— 台账里记着它的 pid，
      而那个 pid 已经不在内核的进程表里；
    * 计划的工作流租约，**真的被放开了** —— 文件不在了；
    * 终态**只写了一次** —— 写盘一次就是 ``KIND_PLAN_CLOSED`` 卡片事件
      一次，多写一次就是多推一张卡片。
    """
    work = death_env["work"]
    plan_dir = _plan_dir(work, PLAN_DEATH)

    status = death_env["death_status"]
    assert status.get("verification_status") == "failed", (
        f"the verification of {PLAN_DEATH} was not written off as failed: "
        f"{status!r}"
    )

    # --- the lease is really gone ---------------------------------------
    lease = _workflow_lease_path(work, PLAN_DEATH)
    assert not lease.exists(), (
        f"the dead round's workflow lease is still on disk: {lease}"
    )

    # --- the process is really dead -------------------------------------
    #
    # The round's process is the one the plan's own durable ledger says
    # it started — the first ``claude`` the round ran.  The pid the
    # ledger names is a pid that process really had: the fake ``claude``
    # wrote its own pid down when it started.
    ledger = _ledger_events(work, PLAN_DEATH)
    child_pids = _fake_claude_pids(work)
    spawns = [
        event for event in ledger
        if event.get("event") == "spawn" and _event_pid(event) in child_pids
    ]
    assert spawns, (
        f"the plan's process ledger recorded no spawn of the round's own "
        f"child: ledger={ledger!r} child_pids={child_pids!r}"
    )
    round_pid = _event_pid(spawns[0])
    assert round_pid, f"the spawn record carries no pid: {spawns[0]!r}"

    # The kernel no longer knows this pid: the round really stopped.
    assert _wait_for_pid_gone(round_pid), (
        f"the round's process {round_pid} is still running after the death"
    )
    with pytest.raises(ProcessLookupError):
        os.kill(round_pid, 0)

    # And the plan's ledger knows it ended.
    ended = [
        event for event in ledger
        if event.get("event") in ("exit", "killed")
        and _event_pid(event) == round_pid
    ]
    assert ended, (
        f"the plan's ledger never recorded the end of pid {round_pid}: "
        f"{ledger!r}"
    )

    # Every process the plan's ledger says ended is really gone.
    for event in ledger:
        if event.get("event") not in ("exit", "killed"):
            continue
        pid = _event_pid(event)
        if not pid:
            continue
        assert _wait_for_pid_gone(pid), (
            f"the plan's ledger says pid {pid} ended, but the process is "
            f"still running: {event!r}"
        )

    # A diagnostic, not a verdict: the killed child gave the coding tool
    # an empty reply, and the coding tool's empty-response retry starts a
    # fresh child two seconds later — after the death sequence's cleanup
    # had already run.  That one is a leak of the retry, not of the
    # death, so it is reported here rather than asserted on.
    survivors = [pid for pid in child_pids if _pid_is_alive(pid)]
    print(
        f"[e2e] the round's children still alive after the death: "
        f"{survivors!r} (of {child_pids!r})"
    )

    # --- the verdict was written exactly once ---------------------------
    log = _backend_log(death_env["proc"])
    entries = [
        match for match in TERMINAL_ENTRY_RE.finditer(log)
        if match.group("plan") == PLAN_DEATH
    ]
    assert len(entries) == 1, (
        f"the terminal state of {PLAN_DEATH} was written {len(entries)} times; "
        f"every write fires a KIND_PLAN_CLOSED card event"
    )

    deaths = [
        match for match in DEATH_SEQUENCE_RE.finditer(log)
        if match.group("plan") == PLAN_DEATH
    ]
    assert len(deaths) == 1, (
        f"the death sequence ran {len(deaths)} times for {PLAN_DEATH}: "
        f"{[match.groupdict() for match in deaths]!r}"
    )
    death = deaths[0]
    assert death.group("cleaned") == "True", (
        f"the round's processes were not cleaned up: {death.groupdict()!r}"
    )
    assert death.group("lease") == "True", (
        f"the workflow lease was not released: {death.groupdict()!r}"
    )
    assert death.group("invalidated") == "True", (
        f"the dead round was not marked invalid: {death.groupdict()!r}"
    )

    # --- the plan really is failed --------------------------------------
    assert _routing_phase(work, PLAN_DEATH) == "failed", (
        f"the plan's phase is {_routing_phase(work, PLAN_DEATH)!r}, not 'failed'"
    )


# ---------------------------------------------------------------------------
# 2. The plan is free again
# ---------------------------------------------------------------------------


def test_e2e_second_start_is_accepted_after_death(death_env):
    """判死之后，再来一次 ``/start`` 是**被接受**的，而且轮次往前走了一位。

    判死不只是写一个状态：它把计划**交回来**了。交回来的证据有三个 ——
    启动不被 409 挡掉、轮次从 1 变成 2、而新的一轮拿到的是**另一个**
    token 的租约（租约文件里的 token 换了人，才叫真的交出去了）。
    """
    base_url = death_env["base_url"]
    work = death_env["work"]

    log = _backend_log(death_env["proc"])
    deaths = [
        match for match in DEATH_SEQUENCE_RE.finditer(log)
        if match.group("plan") == PLAN_DEATH
    ]
    assert deaths, "the death sequence never ran — there is no dead round"
    dead_token = deaths[0].group("round")

    status, body = _http_json(
        "POST",
        f"{base_url}/api/verification/{PLAN_DEATH}/start",
        {"project_dir": str(work / "target")},
    )
    assert status == 200, (
        f"the second start was refused with {status}: {body!r}"
    )

    reached = _wait_for_round(base_url, PLAN_DEATH, 2, SECOND_START_TIMEOUT_SECONDS)
    assert int(reached.get("verification_round") or 0) >= 2, (
        f"the second start did not advance the round: {reached!r}"
    )

    # The new round takes the plan's workflow lease — with a token of its
    # own.  The dead round's token is still on record in the log; the file
    # on disk carries somebody else's.
    lease = _workflow_lease_path(work, PLAN_DEATH)
    holder: Optional[str] = None
    deadline = time.monotonic() + SECOND_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        token = _lease_holder_token(lease)
        if token:
            holder = token
            break
        time.sleep(0.1)
    assert holder, (
        "the new round never took the workflow lease — the dead round's "
        "lease was not handed back"
    )
    assert holder != dead_token, (
        f"the new round took the dead round's lease: {holder!r} == {dead_token!r}"
    )


# ---------------------------------------------------------------------------
# 3. The dashboard says so
# ---------------------------------------------------------------------------


def test_e2e_dashboard_shows_failed_after_death(death_env, tmp_path):
    """页面上的徽标**真的**是「验证失败」。

    用户走的是同一条路：真后端、真页面、真点击。点开计划，点「启动验证」，
    然后等这一轮被判死 —— 徽标自己翻成「验证失败」。

    判死之后页面**不再**提供「启动验证」：一轮已经结束的验证，界面不给
    再来一轮的入口（失败之后的恢复是 ``/reset`` + ``/start``，不是这个
    按钮）。这是界面自己的拒绝，不是这一份测试加的规则。
    """
    base_url = death_env["base_url"]
    work = death_env["work"]
    screenshot_path = tmp_path / SCREENSHOT_NAME

    report = _drive(base_url, PLAN_UI, screenshot_path, work)

    assert report.get("plan_card_visible") is True, (
        f"the plan card never appeared on the page: {report!r}"
    )
    assert report.get("verification_panel_visible") is True, (
        f"the verification panel did not open: {report!r}"
    )

    assert report.get("start_button_visible_before_start") is True, (
        f"the page offered no way to start the verification: {report!r}"
    )
    assert report.get("badge_before_start") == "未开始", (
        f"the badge read {report.get('badge_before_start')!r} before the "
        f"round was started: {report!r}"
    )

    assert report.get("badge_after_start") == "验证中", (
        f"the round did not start: the badge read "
        f"{report.get('badge_after_start')!r}: {report!r}"
    )
    assert report.get("dialogs") == [], (
        f"the page refused the start with a dialog: {report.get('dialogs')!r}"
    )

    assert report.get("badge_after_death") in ("验证失败", "失败"), (
        f"the badge reads {report.get('badge_after_death')!r} after the "
        f"death, not 「验证失败」: {report!r}"
    )

    # A finished round is offered no start control any more.
    assert report.get("start_button_visible_after_death") is False, (
        f"the page still offers 「启动验证」 for a finished round: {report!r}"
    )

    # The screenshot is the archive: it exists, it is not empty, and it is
    # in pytest's temporary directory — never in the repository.
    assert screenshot_path.is_file(), (
        f"the screenshot was not written: {screenshot_path}"
    )
    assert screenshot_path.stat().st_size > 0, (
        f"the screenshot is empty: {screenshot_path}"
    )
    print(
        f"[e2e] the screenshot of the failed round: {screenshot_path} "
        f"({screenshot_path.stat().st_size} bytes); what the page showed: "
        f"badge={report.get('badge_after_death')!r} "
        f"round={report.get('round_after_death')!r}"
    )
    resolved = screenshot_path.resolve()
    assert not resolved.is_relative_to(REPO_ROOT.resolve()), (
        f"the screenshot landed inside the repository workspace: {resolved}"
    )
    assert resolved.is_relative_to(Path(str(work)).resolve().parent), (
        f"the screenshot is outside pytest's temporary directory: {resolved}"
    )

    assert report.get("page_errors") == [], (
        f"the page raised uncaught JavaScript exceptions: "
        f"{report.get('page_errors')!r}"
    )