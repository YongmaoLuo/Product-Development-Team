"""真实后端 + 真实浏览器：声明端口被陌生进程占着时的重定位验收。

任务 2-6 在**进程内**与**子进程**层面验收过：就绪判定按声明裁决、重定位
绕开陌生进程、副本就绪且陌生进程还活着。但没有一条测试走完「人在页面上
点验证」这条链。事实 A 的真实形态是这一整条：

    声明端口被陌生进程占着
        → 副本换到另一个端口并就绪
            → 页面给出可读的结果

这一轮走的就是这一整条，而且走的是**已经在跑的那个服务实例**：夹具计划
先被放进那个实例的 ``plans/`` 树、再被登记进那个实例读的状态库，然后由
**真浏览器**点真的那个按钮。

两条路线
--------

* :func:`test_browser_relocates_a_held_port_and_shows_a_result` — 主验收。
  对着**已经在跑的服务实例**走完 ①–⑦：夹具在它的树里、它的库里、它的
  页面上，它的徽标从「验证中」落到一个终态，页面给每个验证点画图标、给
  失败的那个写清原因，那条地址事件流说清副本换到了哪个端口，而那个陌生
  进程**还活着**。
* :func:`test_browser_relocates_a_held_port_on_a_private_server_and_shows_a_result`
  — 同一个夹具形态，对着**这一轮自己起的**后端走一遍。它不依赖任何已经
  跑着的实例，因此在任何机器上都能站住。

两条路线共用夹具：夹具服务、陌生进程、浏览器驱动脚本。

哪一条是真端到端
----------------

对着**被服务的实例**走的那一条是真端到端：
:func:`test_browser_relocates_a_held_port_and_shows_a_result` 与
:func:`test_the_served_route_starts_the_instance_when_none_is_running`。
那个实例是照操作员的样子起的 —— 没有 ``PDT_LLM_MODE=scripted`` 这样的模型
替身 —— 所以验证轮的裁决会真的去调模型、真烧 token。因此这两条带
``@pytest.mark.real_model``：CI（``CI=true``）自动跳过它们（见
``tests/conftest.py``），只有本机 nightly 用 ``-m real_model`` 才跑。

对着**这一轮自己起的私有后端**走的那一条
（:func:`test_browser_relocates_a_held_port_on_a_private_server_and_shows_a_result`）
不碰真模型：它用 ``_boot_server()`` 起那个后端，那里把模型调用换成了磁盘上
的固定回答（``PDT_LLM_MODE=scripted``）。所以它留在 CI 里跑。

被服务的实例从哪来
------------------

判据要的是「人对着**已经在跑的那个实例**点验证」这条链。那个实例此刻是不是
跑着，是**机器的状态**，不是产品的行为 —— 所以这条路线自己负责让它存在：

* 实例**已经在跑**（这一轮自己起的那个，或框架替这一轮起的那一个）→ 就用它；
* 实例**没有在跑** → 按这一轮自己的 'api' 服务声明的方式起一个：同一个解释
  器、同一个工程目录、同一棵 ``plans/`` 树、同一个状态库、同一个端口、
  同样的 ``PDT_ORPHAN_REAPER=0``。

起起来的那个实例**在这次跑完之后继续跑着** —— 它是这一轮的被服务实例，判据
的最后一步（以及人自己的那一次检查）读的就是它的 ``GET /api/plans``。它的
pid 和它的日志写在同一个目录里，找得到、也停得掉。

两条路径都**显式钉住** ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``：一次任务运
行的环境变量把 ``PDT_PLANS_DIR`` 指向**另一个** checkout 的 plans 树，一个照
它解析的实例会去服务一棵这条链从不碰的树。

唯一的替身是模型 —— 那是仓库自己留的缝：``PDT_LLM_MODE=scripted`` 把每一
次模型调用换成磁盘上的固定回答。不换的话，Phase 3 的裁决会真的去调模型：
不可复现，还烧钱。对着**被服务的实例**走的那条路线没有这个替身，于是 Phase 3
的裁决会失败，而框架会把这一轮的执行结果落成一份确定性的兜底报告 —— 轮次
照样结束，徽标照样落到终态。

provider 的配置分两种情形，区别在于**那个实例是谁起的**：

* **这一轮自己起的实例**（机器上没有实例在跑时）→ 由这条测试来定配置：
  ``PDT_PROVIDER_MODE=inherit``（不连 CC Switch，不需要 provider 链）、
  ``PDT_PROVIDER_CAPACITY_FILE`` 指向一个不存在的文件、
  ``PDT_DISABLE_FALSIFIABILITY_PROBE=1``。不这么定，子进程会继承测试进程
  的 ``PDT_PROVIDER_MODE=cc-switch``，启动期去连 CC Switch 的 :4000 ——
  本机有 CC Switch 所以看不出来，CI 上没有，于是启动直接失败。
* **已经在跑的实例** → 就用它，一个字都不改：那个实例的 provider 配置
  不是这条测试能改的（不改 ``PDT_PROVIDER_MODE``、也不改 provider 的
  排序）。

列表的缓存
----------

服务实例把 ``GET /api/plans`` 的结果缓存两秒（``server._PLANS_CACHE``）。
夹具是在**登记之后**才出现在树里的，所以紧接着的那一次查询有可能命中登记
之前那份快照 —— 页面就看不见这个计划。这不是把守卫放宽了：**拒绝（503）
照样当场失败**，只有「答了 200 但还没列出这个计划」会被重试 —— 就像前端
自己的侧栏轮询那样，重试的是缓存，不是拒绝。

与简报字面写法的几处偏差（都是**系统当前的真实行为**，不是这里放宽了断言；
改它们要改产品代码，不改测试）
------------------------------------------------------------------------------

1. **夹具服务必须服务后端自己的 state.db，并如实报告行数。** 简报写的是
   「自己的 0 行库」。照字面写会失败：本轮的 data-plane 门禁
   （``verification_agent._guard_api_test_data_plane``）拿实例报的
   ``plan_routing_rows`` 与**本轮自己的 fixture 行数**比，不相等就把这个
   验证点判成 BLOCKED —— artifact 根本不会产生。而实例门禁
   （``_compare_service_instance``）还要比 state.db 的 inode。所以夹具服务
   发布的是**后端那一份** state.db 与它的真实行数（两个数字都随库变化，
   天然一致）。
2. **VP 的 ✅/❌ 图标来自计划文件里的 ``status`` 字段。** 前端把
   ``verification_plan.json`` 的每个 VP 的 ``status`` 逐字渲染成图标
   （``frontend/app.js`` 的 ``renderVerificationPoints``），生产流程从**不**
   把裁决写回计划文件（那是一份只读的基线）。所以夹具计划里，通过的那个
   点带 ``"status": "passed"``、答错的那个带 ``"status": "failed"``；本轮
   自己的裁决另有独立证据：``/progress`` 的运行时分桶、artifact、地址事件
   流 —— 三者都要和页面上的图标对得上。
3. **``project_dir`` 是一个临时的夹具工程，不是仓库 worktree。** 声明的
   ``cwd`` / ``log_path`` 必须落在 ``project_dir`` 里面；worktree 里有
   ``__pycache__``，就绪前的 freshness 检查会**去重建**那个工程
   （``binary_freshness``），60 秒窗口里既慢又危险。临时夹具工程没有
   ``__pycache__``，freshness 直接 SKIPPED。
4. **失败的那个验证点必须是「答错了」，不是「没答上」。** 一次传输层失败
   （连接被拒、超时）会被框架归成 BLOCKED —— 「环境坏了，不是交付物坏了」
   —— 而 BLOCKED 不是 FAILED，页面也就没有「失败原因」可写。所以那个点
   问的是**已经答话的副本**，要的是一个副本不会给出的值。
5. **失败的那个验证点必须先被判掉。** 前端只在轮次**还在跑**的时候画
   失败明细面板（``renderVerificationPanel`` 在 ``status === "running"``
   之下才渲染）。没有依赖边的两个验证点是**同层并发**的，谁先跑完并不
   确定：失败点若最后跑完，它的裁决与轮次的终态写入几乎同时发生，页面
   根本来不及显示「为什么失败」。所以通过点声明 ``depends_on`` 指向失败
   点 —— 那是计划自己的排序机制（``base_executor._build_layers``）：失败
   被记下的那一刻，轮次还剩一个点要跑。

浏览器二进制从 ``~/Library/Caches/ms-playwright`` 里解析（**不下载**）：
Hermetic 环境把 ``$HOME`` 指向了临时目录，浏览器驱动子进程用
``PDT_TEST_REAL_HOME`` 把 ``$HOME`` 换回真正的家目录才找得到它。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: The interpreter the backend subprocess runs on.  A checkout with no
#: ``backend/.venv`` (fresh worktree, container) falls back to the
#: interpreter already running pytest, so the server really boots.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The environment variable that names the home this run was *launched*
#: with.  ``install_hermetic_env()`` redirects ``$HOME`` to a scratch
#: directory, so the browser driver has to put the real one back to find
#: the browser binaries.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

#: The private-server route's plan id.
PLAN_ID = "e2e-svc-relocation"

#: The service every fixture declares.
SERVICE_NAME = "env-probe"

#: The verification point that must pass: it asks the **relocated** copy
#: for its health probe.
VP_ID = "VP-001"

#: The verification point that must fail — and must fail with a reason
#: the page can show (see the module docstring, deviation 4).  It is
#: judged **first**: the frontend paints the failed/skipped detail panel
#: only while the round is running, so the failure has to be on record
#: while the round still has a point left to run.
VP_FAILING_ID = "VP-002"

#: The verification point that must pass.  In the served-instance route
#: it declares that it runs **after** the failing one, so the round is
#: still running when the failure is on record.
VP_PASSING_ID = "VP-003"

#: The labels the page shows once the round is over.
FINAL_BADGE_LABELS = {"已通过", "验证失败", "循环停止"}

#: The label the page shows while the round runs.
RUNNING_BADGE_LABEL = "验证中"

#: How long the served instance's plans tree has to stay free of the
#: fixture before the teardown believes it is gone.  The round's tail
#: writes the plan's usage report a second or two after the status went
#: terminal, so a shorter window would race it.
_CLEAN_TREE_WINDOW_SECONDS = 8.0

#: Where the **already-running** instance is.  This is the address of a
#: service instance, not a declared service port: the port a fixture
#: declares is always allocated at run time (see
#: :func:`_allocate_declared_port`) and never written down here.
SERVED_BASE_URL_ENV = "PDT_SERVED_BASE_URL"
DEFAULT_SERVED_BASE_URL = "http://127.0.0.1:18080"

#: The header the loopback request guard demands on every ``/api/*`` call.
GUARD_HEADER = {"X-PDT-Request": "1", "Accept": "application/json"}


# ---------------------------------------------------------------------------
# The fixture service
# ---------------------------------------------------------------------------

_FIXTURE_SERVICE = r'''
"""A tiny service that answers /health and /health/data.

It publishes the **backend's own** state database as the data plane it
serves, and reports the number of routing rows that database holds.
Why not a private database of its own: the round's data-plane gate
compares the instance's row count with the round's own fixture count,
and the instance gate compares the database's inode with the one the
round has open.  A service that published a database of its own would
be refused as "not the round's instance" before the verification point
ever ran -- and the acceptance this file exists for is that the
verification point *does* run.
"""
import json
import os
import sqlite3
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DB = os.environ.get("PDT_STATE_DB_PATH", "")
PID_DIR = os.environ.get("PDT_FIXTURE_PID_DIR", "")
BOOT_ID = uuid.uuid4().hex


def routing_rows():
    """How many plan_routing rows the state database holds."""
    if not STATE_DB or not os.path.exists(STATE_DB):
        return 0
    for _ in range(5):
        try:
            conn = sqlite3.connect(
                "file:" + STATE_DB + "?mode=ro", uri=True, timeout=5.0,
            )
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM plan_routing"
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            pass
    return 0


def instance():
    """The fact sheet about this process."""
    block = {
        "boot_id": BOOT_ID,
        "pid": os.getpid(),
        "state_db_path": STATE_DB or None,
    }
    try:
        block["state_db_inode"] = int(os.stat(STATE_DB).st_ino)
    except OSError:
        block["state_db_inode"] = None
    return block


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send({"status": "ok"})
        elif path == "/health/data":
            self._send({
                "status": "ok",
                "plan_routing_rows": routing_rows(),
                "instance": instance(),
            })
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1])
    if PID_DIR:
        try:
            os.makedirs(PID_DIR, exist_ok=True)
            with open(
                os.path.join(PID_DIR, "service-%d.pid" % port), "w",
                encoding="utf-8",
            ) as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port the kernel says is free right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _allocate_declared_port(*texts: str) -> int:
    """The port the fixture's declaration names — allocated, never written down.

    Two rules:

    * it comes from ``service_manager.allocate_free_port()`` — the same
      allocator the harness itself uses, so the port is one the kernel
      says is free right now;
    * its digits must not occur in any path the start command carries:
      the relocation rewrites **every** standalone occurrence of the
      declared port in that command, so a port that also occurs in the
      state database path would be rewritten there too — and the service
      would be pointed at a database that does not exist.
    """
    import service_manager

    for _ in range(200):
        port = service_manager.allocate_free_port()
        if port is None:
            continue
        digits = str(port)
        if all(digits not in text for text in texts):
            return port
    raise RuntimeError(
        "could not allocate a declared port that avoids the given paths"
    )


def _wait_for_server(base_url: str, timeout: float = 60.0) -> None:
    """Block until ``GET /health`` on *base_url* returns 2xx."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"server at {base_url} did not start in {timeout}s; last_exc={last_exc}"
    )


def _http_json(
    url: str, method: str = "GET", body: Optional[dict] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any]:
    """One request to the served instance, with the guard header.

    The loopback request guard refuses every ``/api/*`` call that does
    not carry ``X-PDT-Request``; the bundled UI sends it, and so does
    this helper.
    """
    data = None
    headers = dict(GUARD_HEADER)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _start_stranger(port: int, lifetime: float = 600.0) -> subprocess.Popen:
    """A real stranger holding *port* — and holding it silently.

    A bare listener: it accepts connections and never answers them, so
    nothing about it can be mistaken for a service.  It is a process
    this module started and nobody else knows about, which is exactly
    the situation the ownership rules have to survive.

    Its lifetime is bounded by its own sleep — **the test never kills
    it**.  A port held by a process we cannot positively identify is not
    ours to kill, and the acceptance asserts exactly that (step ⑥).
    """
    return subprocess.Popen(
        [
            sys.executable, "-c",
            "import socket,time;s=socket.socket();"
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('127.0.0.1',{port}));s.listen(5);"
            f"time.sleep({lifetime})",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def _wait_for_a_listener(port: int, proc: subprocess.Popen) -> None:
    """Block until *port* is held and the holder is *proc*."""
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"the stranger died before it could hold port {port} "
                f"(exit {proc.returncode})"
            )
        with socket.socket() as sock:
            sock.settimeout(0.5)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    raise RuntimeError(f"the stranger never took port {port}")


def _process_command_line(pid: int) -> str:
    """The command line of *pid*, as the machine reports it."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip()


def _process_is_alive(pid: int) -> bool:
    """Is *pid* still a process the machine knows about?

    ``ps -p`` is the check the acceptance names: the stranger has to be
    findable by the ordinary system tools, not merely by a handle this
    test happens to hold.
    """
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid="],
        capture_output=True, text=True, check=False,
    )
    return str(pid) in result.stdout


def _write_scripted_llm(script_dir: Path) -> None:
    """Write the scripted answers the server will read instead of a model.

    The registry maps a **matcher** (a substring of the prompt) to a
    response file.  The rules the double itself enforces: an exact match
    wins, otherwise the most specific fragment wins, and a prompt nothing
    matches is an error (there is no silent fallback -- that is the point
    of the double).

    * ``## 验证计划`` -- the header the Phase 3 judgment prompt starts
      with, so the round gets a well-formed report.
    * ``e`` -- the catch-all.  It is one character long, so every other
      (longer, more specific) matcher beats it; it exists so that a
      prompt nobody scripted is answered with *something* instead of
      raising.
    """
    script_dir.mkdir(parents=True, exist_ok=True)
    registry = {
        "prompts": {
            "judgment": "## 验证计划",
            "e": "e",
        }
    }
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (script_dir / "judgment.txt").write_text(
        json.dumps(
            {
                "overall_status": "PASSED",
                "summary": "夹具服务的健康探针在重定位后的端口上返回 200 且 status=ok。",
                "verification_results": [
                    {
                        "id": VP_ID,
                        "status": "PASSED",
                        "actual_result": "GET /health 返回 200，JSON 中 status 为 ok。",
                        "evidence": "见 vp_artifacts/VP-001/api_response.json。",
                    }
                ],
                "requirement_deviations": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (script_dir / "e.txt").write_text(
        json.dumps({"ok": True}, ensure_ascii=False), encoding="utf-8"
    )


def _api_test_point(
    vp_id: str, *, expected_value: str, plan_status: str, title: str,
    expected_result: str, criteria: str,
    depends_on: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """One ``api_test`` verification point against the fixture service.

    Every point asks the **relocated** copy for its health probe.  The
    difference between a passing and a failing point is the value the
    assertion expects: ``ok`` for the one that must pass, ``not-ok`` for
    the one that must fail.

    The failing point fails on an **assertion**, never on a transport
    error: the framework classifies a request that never reached the
    service as BLOCKED ("the environment failed, not the deliverable"),
    and a BLOCKED point is not a failed one — so the page would have no
    failure to explain.

    ``plan_status`` is what the page draws the point's icon from: the
    frontend renders the plan file's per-point ``status`` field
    verbatim (production never writes verdicts back into the plan
    file), so the fixture states the outcome it expects.

    ``depends_on`` is the plan's own ordering edge.  The executor
    partitions the points into layers with it
    (``base_executor._build_layers``): a point that depends on another
    lands in the layer **after** it, so it is dispatched only once that
    one is through.
    """
    point: Dict[str, Any] = {
        "id": vp_id,
        "title": title,
        "related_prd_criteria": criteria,
        "verification_method": "api_test",
        "verification_phase": 1,
        "priority": "high",
        "expected_result": expected_result,
        "status": plan_status,
        "uses_services": [SERVICE_NAME],
        "request": {
            "method": "GET",
            "url": "{{svc.env-probe.url}}/health",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码", "status": 200},
            {
                "name": "副本的存活标记",
                "json_path": "$.status",
                "equals": expected_value,
            },
        ],
        "timeout_seconds": 120,
        "execution_group": 0,
    }
    if depends_on:
        point["depends_on"] = list(depends_on)
    return point


def _write_verification_plan(
    plan_dir: Path, declared_port: int, state_db: Path, pid_dir: Path,
    *, failing_point: bool = False,
) -> None:
    """Write the machine plan: one declared service, its ``api_test`` points.

    The declaration's start command pins the state database the service
    serves -- that pin is what the readiness gate judges the instance
    against, so it names the **backend's** database (see the module
    docstring).

    With ``failing_point`` the plan carries **two** points: the failing
    one and the passing one, and the passing one declares that it runs
    **after** the failing one.  The edge is not cosmetic:

    * the frontend paints the failed/skipped detail panel **only while
      the round is running** (``renderVerificationPanel`` in
      ``frontend/app.js``), so the failing point has to be judged while
      the round still has the second point left to run;
    * two points without an edge share one layer and race — the
      executor dispatches them together, and whichever finishes last
      has its verdict written in the same instant as the round's
      terminal write.  When that is the failing point, the page never
      gets to show why it failed.

    The dependency edge is the plan's own ordering mechanism
    (``base_executor._build_layers``): the passing point lands in the
    layer after the failing one, so the failure is on record for the
    whole of the passing point's run.
    """
    start_cmd = (
        f"PDT_STATE_DB_PATH={shlex.quote(str(state_db))} "
        f"PDT_FIXTURE_PID_DIR={shlex.quote(str(pid_dir))} "
        f"{shlex.quote(str(VENV_PYTHON))} fixture_service.py "
        f"--port {declared_port}"
    )
    if failing_point:
        points: List[Dict[str, Any]] = [
            _api_test_point(
                VP_FAILING_ID,
                expected_value="not-ok",
                plan_status="failed",
                title="副本不会自报一个它没有给出的标记",
                expected_result=(
                    "GET {{svc.env-probe.url}}/health 返回 200，"
                    "但 JSON 中 status 不是 not-ok，这条断言必须失败。"
                ),
                criteria=(
                    "一个答错了的验证点，页面要画出失败图标并写清失败原因"
                ),
            ),
            _api_test_point(
                VP_PASSING_ID,
                expected_value="ok",
                plan_status="passed",
                title="重定位后的副本在它自己的端口上回答健康探针",
                expected_result=(
                    "GET {{svc.env-probe.url}}/health 返回 200，"
                    "JSON 中 status 为 ok；该端口不是声明的端口。"
                ),
                criteria=(
                    "声明端口被陌生进程占着时，副本换端口就绪，"
                    "页面给出可读的结果，陌生进程仍然活着"
                ),
                depends_on=[VP_FAILING_ID],
            ),
        ]
    else:
        points = [
            _api_test_point(
                VP_ID,
                expected_value="ok",
                plan_status="passed",
                title="重定位后的副本在它自己的端口上回答健康探针",
                expected_result=(
                    "GET {{svc.env-probe.url}}/health 返回 200，"
                    "JSON 中 status 为 ok；该端口不是声明的端口。"
                ),
                criteria=(
                    "声明端口被陌生进程占着时，副本换端口就绪，"
                    "页面给出可读的结果，陌生进程仍然活着"
                ),
            ),
        ]
    plan = {
        "services": [
            {
                "name": SERVICE_NAME,
                "port": declared_port,
                "start_cmd": start_cmd,
                "cwd": ".",
                "health_url": f"http://127.0.0.1:{declared_port}/health",
                "ready_timeout_seconds": 60,
                "log_path": "tmp/env-probe.log",
                "reap_on_exit": True,
            }
        ],
        "verification_points": points,
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _boot_server(
    work: Path, script_dir: Path, plans_dir: Path, state_db: Path,
) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` subprocess; return ``(base_url, proc)``."""
    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(state_db),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": str(port),
            "PDT_DEBUG_ROUTES": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            # "inherit" and not a provider: this server exists to prove
            # the relocation, not to exercise provider routing.
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(work / "absent.yaml"),
            # The model is the one thing that is replaced: a real model
            # call would be neither reproducible nor free.
            "PDT_LLM_MODE": "scripted",
            "PDT_LLM_SCRIPT_DIR": str(script_dir),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it (see the sibling E2E file for the full account).
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
        _wait_for_server(base_url, timeout=60.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise
    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc


def _teardown_server(proc: Optional[subprocess.Popen]) -> None:
    """Terminate *proc* and close its log handle."""
    if proc is None:
        return
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


def _kill_pid_files(pid_dir: Path) -> None:
    """Stop every process that wrote a pid file into *pid_dir*.

    The relocated copies record their pid, so a copy that the round did
    not manage to register in its ledger (a second relocation, whose
    runtime is thrown away) is still stopped here.

    These are the **copies** — processes this fixture started and that
    the framework's ledger knows about.  The stranger never writes a pid
    file, so it is not reachable from here: nothing in this module kills
    a process it cannot identify.
    """
    if not pid_dir.is_dir():
        return
    for pid_file in sorted(pid_dir.glob("*.pid")):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break


# ---------------------------------------------------------------------------
# The served instance (the already-running one)
# ---------------------------------------------------------------------------


def _served_base_url() -> str:
    """The address of the already-running instance.

    It is an *instance* address, not a declared service port: the port a
    fixture declares is allocated at run time and never written down.
    """
    return os.environ.get(SERVED_BASE_URL_ENV, DEFAULT_SERVED_BASE_URL).rstrip("/")


def _served_instance(base_url: str) -> Dict[str, Any]:
    """The fact sheet the served instance publishes about itself."""
    instance = _served_instance_probe(base_url)
    if instance is None:
        pytest.fail(
            f"the instance at {base_url} did not answer /health/data; "
            "this acceptance needs an instance that is up"
        )
    _require_instance_fields(instance, base_url)
    return instance


def _served_instance_probe(
    base_url: str, timeout: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """The instance's fact sheet, or ``None`` when nothing answers there.

    ``None`` covers both "the port is closed" and "something that is not
    an instance is on it".  It is deliberately not a failure: the caller
    decides whether an absent instance is something to report (the
    check that reads the instance *after* the round) or something to
    fix (the check that walks the page).
    """
    try:
        status, payload = _http_json(
            f"{base_url}/health/data", timeout=timeout,
        )
    except (urllib.error.URLError, OSError):
        return None
    if status != 200 or not isinstance(payload, dict):
        return None
    instance = payload.get("instance")
    if not isinstance(instance, dict) or not instance:
        return None
    return instance


def _require_instance_fields(
    instance: Dict[str, Any], base_url: str,
) -> None:
    """The fields this acceptance cannot do without."""
    for field in ("plans_dir", "state_db_path", "boot_id"):
        if not instance.get(field):
            pytest.fail(
                f"the instance at {base_url} has no {field}; this "
                f"acceptance needs to know which tree and which database "
                f"the served instance reads: {instance}"
            )


def _served_instance_runtime_dir(port: int) -> Path:
    """Where the log and the pid of a served instance are kept.

    It lives outside the checkout on purpose: the instance outlives the
    test run, and a file inside the repository would be a change nobody
    asked for.
    """
    root = Path(tempfile.gettempdir()) / "pdt-e2e-served-instance"
    path = root / str(port)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _start_served_instance(
    base_url: str, *, plans_dir: Optional[Path] = None,
    state_db: Optional[Path] = None,
) -> subprocess.Popen:
    """Start the instance the acceptance walks.

    This is the round's own 'api' service, run by hand: the same
    interpreter, the same project directory, the same plans tree and
    state database, the same port -- and the same
    ``PDT_ORPHAN_REAPER=0``, so the reaper does not touch the fixture
    while the round runs.

    The two paths are pinned explicitly rather than left to the
    resolver: a task run's environment points ``PDT_PLANS_DIR`` at
    **another** checkout's plans tree, and an instance that resolved it
    would serve a tree this acceptance never touches.

    The process outlives the test on purpose -- it is the environment's
    served instance, and the acceptance's last step reads
    ``GET /api/plans`` on it.  Its pid and its log are written next to
    each other, so it can be found and stopped.

    Because **this** call is the one that starts the instance, the
    instance's provider configuration is this test's to set: it is
    pinned to ``inherit`` (no CC Switch, no provider chain -- the
    walk is about the relocation, not about provider routing), with a
    capacity file that does not exist and the falsifiability probe
    disabled.  An instance that was **already running** is used as it
    is; its configuration is not touched.
    """
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        pytest.fail(
            f"the served base url {base_url} names no port; the instance "
            "this acceptance walks is addressed by port"
        )

    runtime = _served_instance_runtime_dir(port)
    env = dict(os.environ)
    env.update(
        {
            "PDT_PLANS_DIR": str(plans_dir or (PROJECT_ROOT / "plans")),
            "PDT_STATE_DB_PATH": str(
                state_db or (PROJECT_ROOT / ".pdt" / "state.db")
            ),
            "PDT_HOST": host,
            "PDT_PORT": str(port),
            "PDT_ORPHAN_REAPER": "0",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            # The instance serves the operator's **live** state database,
            # and the framework refuses to open that database from a
            # test run unless the operator opted in
            # (``state_machine.db.connection``).  This walk is that
            # deliberate one-off: it registers a fixture plan in the
            # database the instance reads and takes it out again.
            "PDT_ALLOW_REAL_STATE_DB": "1",
            # "inherit" and not a provider: the instance this walk starts
            # exists to serve the relocation, not to exercise provider
            # routing.  Without this the child inherits the test run's
            # ``PDT_PROVIDER_MODE=cc-switch`` and the boot asks CC Switch
            # on :4000 for a fleet -- which no CI machine has, so the
            # start aborts with ``api_unreachable`` and the walk times
            # out.  An instance that was **already running** is used as
            # it is: its provider configuration is not this test's to
            # change.
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(runtime / "absent.yaml"),
            # A falsifiability probe would reach for a real provider; the
            # instance this walk starts only has to answer its routes.
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway instance has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it (same account as ``_boot_server``).
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_path = runtime / "server.log"
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._pdt_log_handle = handle  # type: ignore[attr-defined]
    (runtime / "server.pid").write_text(str(proc.pid), encoding="utf-8")
    try:
        _wait_for_server(base_url, timeout=90.0)
    except Exception as exc:
        proc.kill()
        proc.wait(timeout=5)
        handle.close()
        pytest.fail(
            f"the served instance did not start at {base_url} ({exc}); "
            f"its log says: "
            f"{log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}"
        )
    return proc


def _ensure_served_instance(
    base_url: str, *, plans_dir: Optional[Path] = None,
    state_db: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Optional[subprocess.Popen]]:
    """The instance the acceptance walks, up and answering.

    An instance that is **already running** is used as it is -- that is
    the case the acceptance is about: the round's own service, serving
    the plans tree this walk puts a fixture into.

    An instance that is **not running** is started here, the way the
    round's service starts it.  Whether somebody else started it first
    is a fact about the machine, not about the product; a walk that
    only works when the machine happens to be ready reports the
    machine's state instead of the behaviour.

    Returns the instance's fact sheet and the process this call
    started (``None`` when the instance was already up -- that process
    is not ours to stop).
    """
    instance = _served_instance_probe(base_url)
    if instance is None:
        started = _start_served_instance(
            base_url, plans_dir=plans_dir, state_db=state_db,
        )
        instance = _wait_for_the_instance(base_url)
        if instance is None:
            pytest.fail(
                f"the instance this test started at {base_url} does not "
                "answer /health/data (something else may be answering "
                "there); its log is under "
                f"{Path(tempfile.gettempdir()) / 'pdt-e2e-served-instance'}"
            )
        _require_instance_fields(instance, base_url)
        return instance, started
    _require_instance_fields(instance, base_url)
    return instance, None


def _wait_for_the_instance(
    base_url: str, timeout: float = 30.0,
) -> Optional[Dict[str, Any]]:
    """Wait until the instance answers with its fact sheet.

    ``/health`` answers before the instance has finished coming up --
    the state machine recovers the plans it left behind on the way in
    -- so the first answer can be a moment too early.
    """
    deadline = time.time() + timeout
    while True:
        instance = _served_instance_probe(base_url)
        if instance is not None:
            return instance
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _wait_for_the_fixture_to_be_listed(
    base_url: str, fixture_id: str, *, timeout: float = 30.0,
) -> Tuple[int, Any]:
    """Wait until the served instance's listing names the fixture.

    The instance answers ``GET /api/plans`` from a short-lived cache
    (``server._PLANS_CACHE``, two seconds), so a listing rendered
    before the fixture was registered can still be in flight when the
    fixture appears.  The bundled UI copes with that the obvious way:
    the sidebar polls until the plan shows up.  This is the same wait,
    done once, up front.

    A **refusal** is not waited out: a 503 means the instance says its
    data plane does not describe its tree, and that has to fail here
    and now — retrying it would be exactly the "swallow the refusal"
    this acceptance must not do.  Only a 200 that does not name the
    fixture yet is retried.

    Returns the last ``(status, payload)`` it saw.
    """
    deadline = time.time() + timeout
    status, payload = 0, {}
    while True:
        status, payload = _http_json(f"{base_url}/api/plans")
        if status != 200:
            return status, payload
        listed = payload if isinstance(payload, list) else []
        if any(row.get("id") == fixture_id for row in listed):
            return status, payload
        if time.time() >= deadline:
            return status, payload
        time.sleep(0.5)


def _fixture_visibility_facts(
    state_db: Path, plans_dir: Path, plan_id: str,
) -> Dict[str, Any]:
    """What the served instance's tree and database actually hold.

    A listing that does not show the fixture is a fact about one of
    three things: the **database** (is the plan registered?), the
    **tree** (is its directory there?) or the **answer** (what did the
    instance render?).  All three are read here, so a failure says
    which one is wrong instead of only "it was not listed".
    """
    import sqlite3

    db_ids: List[str] = []
    db_error = ""
    try:
        conn = sqlite3.connect(
            f"file:{state_db}?mode=ro", uri=True, timeout=5.0,
        )
        try:
            db_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT plan_id FROM plan_routing ORDER BY plan_id"
                )
            ]
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - diagnostics only
        db_error = f"{type(exc).__name__}: {exc}"

    tree_dirs = (
        sorted(d.name for d in plans_dir.iterdir() if d.is_dir())
        if plans_dir.is_dir()
        else []
    )

    return {
        "plan_id": plan_id,
        "db_plan_ids": db_ids,
        "registered_in_db": plan_id in db_ids,
        "tree_dirs": tree_dirs,
        "dir_in_tree": plan_id in tree_dirs,
        "db_error": db_error,
    }


def _api_test_judged_at(plan_dir: Path) -> Dict[str, str]:
    """When each ``api_test`` point was judged, per the round's own log.

    The round writes one ``verification_<round>_<stamp>.log`` per round
    and appends an ``api_test_executed`` event for every point it
    graded.  Those timestamps are the order the points were judged in —
    and that order decides whether the failure was on record while the
    round was still running (the only window in which the page shows
    the reason).

    The first stamp wins for a point that was judged more than once:
    the first judgement is the one the page could have seen.
    """
    judged: Dict[str, str] = {}
    logs_dir = plan_dir / "logs"
    if not logs_dir.is_dir():
        return judged
    for log_file in sorted(logs_dir.glob("verification_*.log")):
        try:
            lines = log_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event_type") != "api_test_executed":
                continue
            vp_id = str(event.get("verification_point_id") or "")
            stamp = str(event.get("timestamp") or "")
            if vp_id and stamp:
                judged.setdefault(vp_id, stamp)
    return judged


def _remove_fixture_plan(
    plan_dir: Path, state_db: Path, plan_id: str, base_url: str,
    *, timeout: float = 45.0,
) -> bool:
    """Take the fixture out of the served instance's world again.

    The rows that described the plan go first, then the plan directory.

    The directory has to be removed **repeatedly**: a verification round
    re-aggregates the plan's LLM usage into
    ``plans/<id>/usage_report.json`` on a background thread, and it does
    that *after* the status already went terminal — the badge says the
    round is over while the tail is still writing.  A removal that
    races that write leaves a stray directory behind, and a stray
    directory in the plans tree is exactly what makes the plans listing
    refuse to answer at all (503).  So the removal is repeated until the
    tree stays clean, and the served instance's own listing is the
    proof: it has to answer 200 again, with this fixture gone from it.

    Returns whether the served instance was handed back intact.
    """
    from state_machine.db.connection import open as open_db

    conn = open_db(state_db)
    try:
        for table in ("plan_routing", "plan_execution", "plan_verification"):
            conn.execute(f"DELETE FROM {table} WHERE plan_id = ?", (plan_id,))
        conn.commit()
    finally:
        conn.close()

    deadline = time.time() + timeout
    clean_since: Optional[float] = None
    while time.time() < deadline:
        shutil.rmtree(plan_dir, ignore_errors=True)
        if plan_dir.exists():
            # Something wrote into it again — the round's tail.
            clean_since = None
        else:
            if clean_since is None:
                clean_since = time.time()
            elif time.time() - clean_since >= _CLEAN_TREE_WINDOW_SECONDS:
                break
        time.sleep(0.5)

    status, listing = _http_json(f"{base_url}/api/plans")
    if status != 200:
        return False
    if plan_id not in json.dumps(listing, ensure_ascii=False):
        return True

    # The listing is cached for a couple of seconds, so an answer that
    # still names the fixture may be a snapshot taken before the rows
    # went away.  Ask again until the answer reflects the removal.
    while time.time() < deadline:
        time.sleep(0.5)
        status, listing = _http_json(f"{base_url}/api/plans")
        if status != 200:
            return False
        if plan_id not in json.dumps(listing, ensure_ascii=False):
            return True
    return False


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page and report what the badge shows.

The page is driven through its own events (``click``), so the
frontend's own code paths run -- including the ``alert`` it raises when
a start is refused.  A dialog is therefore evidence, not noise.

Two things the frontend draws **only while a round is running** are
collected here: the per-verification-point icons and the failed/skipped
detail panel with its reasons (``renderVerificationPanel`` paints the
panel only under ``status === "running"``).  The driver asks the page
to re-read its own state through the page's own ``fetchVerificationStatus()``
-- the function its five-second timer calls -- so the panel is looked
for as often as the driver looks, not only once per timer tick.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot
    launch anything.  The binaries that *are* installed are found here
    instead -- nothing is downloaded, and nothing is hard-coded: the
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


def badge_text(page):
    badge = page.locator("#verification-status-badge")
    return badge.text_content().strip() if badge.count() else ""


def vp_icon(page, vp_id):
    icon = page.locator(
        '.verification-point[data-vp-id="%s"] .verification-point-icon' % vp_id
    )
    return icon.text_content().strip() if icon.count() else ""


def sidebar_error(page):
    """The banner the page paints when the plans listing failed."""
    node = page.locator("#sidebar-plans .sidebar-empty")
    return node.text_content().strip() if node.count() else ""


def detail_items(page):
    """The detail panel's items: the id and the reason text of each."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll(
                '#verification-counts-detail .task-status-detail-item'
           )).map((el) => ({
                id: (el.querySelector('.task-status-detail-id') || {}).textContent || '',
                reason: (el.querySelector('.task-status-detail-reason') || {}).textContent || '',
           }))"""
    )


def refresh(page):
    """Ask the page to re-read its own state (with its own function)."""
    page.evaluate(
        """async () => {
            if (typeof window.fetchVerificationStatus === 'function') {
                await window.fetchVerificationStatus();
            }
        }"""
    )


url, plan_id, screenshot_path, vp_ids_json = sys.argv[1:5]
vp_ids = json.loads(vp_ids_json)

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {
    "page_errors": [],
    "console_errors": [],
    "dialogs": [],
    "browser_executable": executable or "",
    "icons": {},
    "reasons": {},
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

    item = '.sidebar-plan-item[data-plan="%s"]' % plan_id

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)

    # The sidebar is filled from /api/plans; a plan that appeared a
    # moment ago is not in the first answer.  Reloading is what the
    # sidebar poll does, and it is the only way the cache gets out of
    # the way.
    for _ in range(30):
        if page.locator(item).count():
            break
        page.wait_for_timeout(500)
        page.reload(wait_until="load")
        page.wait_for_timeout(500)
    if not page.locator(item).count():
        raise AssertionError("the plan never appeared in the sidebar")
    report["sidebar_item_visible"] = True
    report["sidebar_error"] = sidebar_error(page)

    # The user clicks the plan, then the real "start verification"
    # button.
    page.click(item)
    page.wait_for_selector("#btn-start-verification", state="visible", timeout=30000)
    report["start_button_visible"] = True
    report["badge_before_start"] = badge_text(page)
    page.click("#btn-start-verification")

    # The badge goes to 验证中 while the round runs ...
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            return !!badge && badge.textContent.trim() === '验证中';
        }""",
        timeout=60000,
    )
    report["badge_while_running"] = badge_text(page)

    # ... and leaves it when the round is over.  Everything the
    # frontend paints only while the round runs is collected on the way.
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            refresh(page)
        except Exception:
            pass
        for vp_id in vp_ids:
            icon = vp_icon(page, vp_id)
            if icon and vp_id not in report["icons"]:
                report["icons"][vp_id] = icon
        for entry in detail_items(page):
            reason = (entry.get("reason") or "").strip()
            if reason:
                report["reasons"].setdefault(entry.get("id") or "", reason)
        if badge_text(page) != "验证中":
            break
        page.wait_for_timeout(400)

    report["badge_after"] = badge_text(page)
    report["icons_after"] = {vp_id: vp_icon(page, vp_id) for vp_id in vp_ids}
    report["reasons_after"] = {
        (entry.get("id") or ""): (entry.get("reason") or "")
        for entry in detail_items(page)
    }
    report["body_text"] = page.inner_text("body")[:4000]
    report["sidebar_error_after"] = sidebar_error(page)

    page.screenshot(path=screenshot_path, full_page=True)
    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    Two candidates, in order: the interpreter running pytest, and the
    interpreter behind a ``playwright`` command on ``PATH`` -- the
    command is a Python entry point, so its shebang names an
    interpreter that has the package installed.  Nothing is hard-coded:
    both are read out of the environment.
    """
    candidates = [sys.executable]

    command = shutil.which("playwright")
    if command:
        try:
            first_line = Path(command).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.startswith("#!"):
            parts = shlex.split(first_line[2:].strip())
            interpreter = ""
            if parts:
                if os.path.basename(parts[0]) == "env" and len(parts) > 1:
                    interpreter = parts[1]
                else:
                    interpreter = parts[0]
            if "python" in interpreter:
                candidates.append(interpreter)

    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright.sync_api"],
                capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


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


def _screenshot_path() -> Path:
    """Where the final screenshot goes.

    The task names ``/tmp/svc-ready-2/e2e/final.png``; a machine
    without ``/tmp`` gets the platform's temporary directory instead.
    """
    root = Path("/tmp") if os.path.isdir("/tmp") else Path(tempfile.gettempdir())
    return root / "svc-ready-2" / "e2e" / "final.png"


def _run_driver(
    interpreter: str, script: Path, base_url: str, plan_id: str, screenshot: Path,
    vp_ids: Sequence[str],
) -> Dict[str, Any]:
    """Run one browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{base_url}/",
            plan_id,
            str(screenshot),
            json.dumps(list(vp_ids)),
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
            pytest.fail(
                "the Playwright driver is installed but its browser "
                "binaries are not; this acceptance needs a real browser"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _require_playwright(strict: bool = False) -> str:
    """The interpreter that can drive a browser.

    With ``strict`` a missing browser **fails** the test; without it the
    test is skipped.  The difference is what the caller claims: the
    acceptance against the already-running instance has to really walk
    the page, and a skipped walk would report a green that was never
    walked — so that one fails.  The private-server route is a
    convenience walk that may run anywhere, and there a machine without
    a browser is a missing capability, not a broken product (the PR
    gate never installs one: ``ci.yml``'s ``e2e-on-demand`` runs ``uv
    sync --locked`` and nothing else).
    """
    interpreter = _playwright_interpreter()
    if interpreter is not None:
        return interpreter
    message = (
        "playwright is not importable here (install it, or put a "
        "'playwright' command on PATH); this acceptance walk needs a "
        "real browser"
    )
    if strict:
        pytest.fail(message)
    pytest.skip(message)
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# The test: against the already-running instance
# ---------------------------------------------------------------------------


@pytest.mark.real_model
@pytest.mark.timeout(900)
def test_browser_relocates_a_held_port_and_shows_a_result(
    tmp_path: Path, plan_sqlite_seeder, monkeypatch,
):
    """The whole chain, in a real browser, against the running instance.

    ① a fixture plan under the served instance's ``plans/`` tree, its
    service declared on a port, and a **stranger** holding that port;
    ② the sidebar lists the plan and the page opens it;
    ③ the badge shows 「验证中」 and then lands on a final verdict;
    ④ every verification point carries an icon, and the failed one
    carries a readable reason;
    ⑤ the request went to the copy's own port, and the address event
    stream says which port that was;
    ⑥ the stranger is still running;
    ⑦ no uncaught exception, no alert, no white screen.

    The fixture has to be **registered** in the state database the
    served instance reads: the plans listing refuses to answer (503)
    when the tree holds plans the database knows nothing about, and
    that refusal is the guard this acceptance must not weaken.  So the
    fixture is registered the way a real plan is registered, and taken
    out again when the round is over.
    """
    base_url = _served_base_url()
    # The instance this walk uses: the one that is already running, or
    # the one this call starts (and leaves running -- it is the
    # environment's served instance, and the acceptance's last step
    # reads ``GET /api/plans`` on it).
    instance, _started_instance = _ensure_served_instance(base_url)
    plans_dir = Path(instance["plans_dir"])
    state_db = Path(instance["state_db_path"])
    boot_id = instance["boot_id"]

    # The fixture's identity: a plan id of its own, so the served
    # instance's tree and database can be handed back exactly as they
    # were.
    fixture_id = f"pdt-e2e-vp015-{uuid.uuid4().hex[:8]}"
    plan_dir = plans_dir / fixture_id
    project_dir = tmp_path / "project"
    pid_dir = project_dir / "tmp" / "pids"

    # The served instance reads the operator's live database; the
    # fixture has to be registered **there**, and the framework refuses
    # to open that database from a test run unless the operator opted
    # in.  This is that opt-in: a deliberate one-off against the live
    # instance, and the fixture's rows are removed again below.
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(state_db))
    monkeypatch.setenv("PDT_ALLOW_REAL_STATE_DB", "1")

    # The port the declaration names.  Its digits must not occur in any
    # path the start command carries: the relocation rewrites every
    # standalone occurrence of the port there.
    declared_port = _allocate_declared_port(
        str(state_db), str(project_dir), str(plans_dir), str(pid_dir),
    )

    stranger = _start_stranger(declared_port)
    try:
        for directory in (plan_dir, project_dir, pid_dir):
            directory.mkdir(parents=True, exist_ok=True)
        (project_dir / "fixture_service.py").write_text(
            _FIXTURE_SERVICE, encoding="utf-8"
        )
        _wait_for_a_listener(declared_port, stranger)
        _write_verification_plan(
            plan_dir, declared_port, state_db, pid_dir, failing_point=True,
        )

        # --- ① the fixture is registered in the served instance -----
        plan_sqlite_seeder(
            plan_dir,
            fixture_id,
            phase="executing",
            project_dir=project_dir,
            verification={"status": "not_started", "round": 0, "max_rounds": 1},
            verification_status="not_started",
            verification_round=0,
            max_rounds=1,
        )

        status, listing = _wait_for_the_fixture_to_be_listed(
            base_url, fixture_id,
        )
        assert status == 200, (
            f"the served instance refused to list its own plans ({status}): "
            f"{listing}"
        )
        assert fixture_id in [row.get("id") for row in listing], (
            f"the fixture plan is not in the served instance's listing: "
            f"{listing} / "
            f"{_fixture_visibility_facts(state_db, plans_dir, fixture_id)}"
        )

        # --- the browser really opens the page and drives it ---------
        screenshot = _screenshot_path()
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        driver_script = tmp_path / "driver.py"
        driver_script.write_text(_BROWSER_DRIVER, encoding="utf-8")

        interpreter = _require_playwright(strict=True)
        report = _run_driver(
            interpreter, driver_script, base_url, fixture_id, screenshot,
            [VP_FAILING_ID, VP_PASSING_ID],
        )

        # --- ② the sidebar lists the plan and opens it --------------
        assert report["sidebar_item_visible"] is True, report
        assert "计划列表加载失败" not in report["sidebar_error"], report
        assert "计划列表加载失败" not in report["sidebar_error_after"], report
        assert report["start_button_visible"] is True, report

        # --- ③ the badge ran and landed on a final verdict ----------
        assert report["badge_while_running"] == RUNNING_BADGE_LABEL, report
        assert report["badge_after"] in FINAL_BADGE_LABELS, report

        # --- ④ every point has an icon, the failed one a reason -----
        icons = report["icons"]
        assert set(icons) == {VP_FAILING_ID, VP_PASSING_ID}, report
        assert icons[VP_FAILING_ID] == "❌", report
        assert icons[VP_PASSING_ID] == "✅", report
        reason = (report["reasons"].get(VP_FAILING_ID) or "").strip()
        assert reason, (
            "the page drew no readable reason for the failed verification "
            f"point: {report}"
        )

        # --- the runtime agrees with what the page drew -------------
        status, progress = _http_json(
            f"{base_url}/api/verification/{fixture_id}/progress"
        )
        assert status == 200, progress
        assert VP_PASSING_ID in (progress.get("completed_vps") or []), progress
        assert VP_FAILING_ID in (progress.get("failed_vps") or []), progress
        failed_entry = next(
            (
                entry for entry in (progress.get("vps") or [])
                if entry.get("id") == VP_FAILING_ID
            ),
            None,
        )
        assert failed_entry is not None, progress
        assert (failed_entry.get("actual_result") or "").strip(), (
            f"the runtime recorded the failure without a reason: {progress}"
        )

        # --- ⑤ the request went to the copy, not to the stranger ----
        # The port the plan declares is read back from the plan file:
        # that file is what the round resolved, so it is the port the
        # request must **not** have gone to.
        plan = json.loads(
            (plan_dir / "verification_plan.json").read_text(encoding="utf-8")
        )
        plan_port = int(plan["services"][0]["port"])
        assert plan_port == declared_port, (
            f"the fixture plan declares port {plan_port} but the stranger "
            f"holds {declared_port}: they must be the same port for this "
            "acceptance to mean anything"
        )

        # --- the fixture declared the order the page depends on ------
        # The failed point has to be judged **while the round is still
        # running** — the frontend paints the failed-detail panel only
        # under ``status === "running"``.  Two points without a
        # dependency edge share a layer and race; the failure can then
        # land in the same instant as the round's terminal write, and
        # the page never gets to show why it failed.  The fixture
        # therefore declares that the passing point runs **after** the
        # failing one.
        plan_points = {
            str(point.get("id")): point
            for point in (plan.get("verification_points") or [])
        }
        assert VP_PASSING_ID in plan_points, plan
        assert plan_points[VP_PASSING_ID].get("depends_on") == [VP_FAILING_ID], (
            "the fixture does not declare that the passing point runs after "
            "the failing one, so the failure can be judged in the same "
            f"instant as the round's terminal write: {plan_points}"
        )

        # --- the runtime judged the failure while the round ran ------
        # The round's own log says when each point was judged.  The
        # failing one has to be judged first: that is what puts its
        # verdict on record while the round still has the passing point
        # left to run — the window in which the page shows the reason.
        judged_at = _api_test_judged_at(plan_dir)
        assert VP_FAILING_ID in judged_at and VP_PASSING_ID in judged_at, (
            "the round's log does not say when each point was judged: "
            f"{judged_at}"
        )
        assert judged_at[VP_FAILING_ID] < judged_at[VP_PASSING_ID], (
            "the failing point was judged after the passing one, so the "
            "round was already over when the failure landed: "
            f"{judged_at}"
        )

        artifact_path = plan_dir / "vp_artifacts" / VP_PASSING_ID / "api_response.json"
        assert artifact_path.exists(), (
            f"the verification point produced no artifact: {artifact_path}"
        )
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        request_url = str((artifact.get("request") or {}).get("url") or "")
        assert request_url, artifact
        artifact_port = urllib.parse.urlsplit(request_url).port
        assert artifact_port is not None, request_url
        assert artifact_port != plan_port, (
            f"the request went to the stranger's port {plan_port}: "
            f"{request_url}"
        )

        # --- the address event stream says where the port came from --
        events_path = plan_dir / "logs" / "service_address_events.jsonl"
        assert events_path.exists(), (
            f"the round wrote no address events: {events_path}"
        )
        events = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        relocated = [
            event for event in events
            if event.get("event") == "relocated"
            and event.get("old_port") == plan_port
        ]
        assert relocated, (
            "no 'relocated' address event for the declared port "
            f"{plan_port}: {events}"
        )
        assert relocated[0]["new_port"] == artifact_port, (
            f"the relocated port {relocated[0]['new_port']} is not the port "
            f"the request went to ({artifact_port})"
        )

        # --- ① the stranger is a stranger, and ⑥ it is still alive --
        stranger_command = _process_command_line(stranger.pid)
        declaration = str(plan["services"][0]["start_cmd"])
        assert declaration not in stranger_command, (
            "the stranger is running the declaration's own command line; "
            f"it has to be somebody else: {stranger_command!r}"
        )
        assert "fixture_service" not in stranger_command, (
            "the stranger is running the fixture's own service; it has to "
            f"be somebody else: {stranger_command!r}"
        )
        ledger = plan_dir / "process_ledger.jsonl"
        if ledger.exists():
            owned = []
            for line in ledger.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    owned.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            assert all(
                entry.get("pid") != stranger.pid for entry in owned
            ), (
                "the framework's ledger claims the stranger as its own "
                "process; a port held by a process we cannot identify is "
                "not ours to kill"
            )
        assert stranger.poll() is None, (
            "the stranger must still be running: a port held by a process "
            "we cannot positively identify is not ours to kill"
        )
        assert _process_is_alive(stranger.pid), (
            f"ps cannot find the stranger (pid {stranger.pid}) any more"
        )

        # --- ⑦ no exception, no alert, no white screen -------------
        assert report["page_errors"] == [], report
        assert report["dialogs"] == [], report
        assert fixture_id in report["body_text"], (
            "the page does not show the plan it opened — that is a white "
            f"screen: {report['body_text']!r}"
        )
        assert screenshot.exists(), (
            f"the acceptance screenshot is missing: {screenshot}"
        )

        # --- the instance that answered is the one that ran the round -
        after = _served_instance(base_url)
        assert after["boot_id"] == boot_id, (
            "the instance that answered changed while the round ran; "
            f"{boot_id} -> {after['boot_id']}"
        )
    finally:
        # The fixture goes away — the plan directory and the rows that
        # described it.  The stranger does NOT: it is a process this
        # test cannot identify as its own, and the acceptance says it
        # has to be alive when the round is over.
        _kill_pid_files(pid_dir)
        cleaned = _remove_fixture_plan(
            plan_dir, state_db, fixture_id, base_url,
        )

    # The served instance has to be exactly as it was found: its plans
    # tree free of the fixture and its listing answering again.  A
    # leftover directory there is what makes the listing refuse to
    # answer at all, so this is asserted, not assumed.
    assert cleaned, (
        "the fixture did not leave the served instance's world: its plans "
        "listing does not answer 200 again (or still lists the fixture)"
    )


@pytest.mark.real_model
@pytest.mark.timeout(300)
def test_the_served_route_starts_the_instance_when_none_is_running(
    tmp_path: Path, monkeypatch,
):
    """The walk must not depend on somebody else having started the instance.

    This is the case the acceptance would otherwise never be exercised
    in: a machine where the round's service is **not** up.  Whether
    somebody else started it first is a fact about the machine, not
    about the product -- so the walk brings one up itself, the way the
    round's 'api' service is brought up, and reads the tree and the
    database that instance actually serves.

    The instance this starts is stopped again at the end: the
    acceptance's own instance is the environment's, this one is the
    test's.
    """
    base_url = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setenv(SERVED_BASE_URL_ENV, base_url)
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    state_db = tmp_path / "state.db"

    instance, started = _ensure_served_instance(
        base_url, plans_dir=plans_dir, state_db=state_db,
    )
    try:
        assert Path(instance["plans_dir"]) == plans_dir, instance
        assert Path(instance["state_db_path"]) == state_db, instance
        status, listing = _http_json(f"{base_url}/api/plans")
        assert status == 200, listing
        assert isinstance(listing, list), listing
    finally:
        if started is not None:
            _teardown_server(started)


# ---------------------------------------------------------------------------
# The test: against a server of this test's own
# ---------------------------------------------------------------------------


@pytest.mark.timeout(600)
def test_browser_relocates_a_held_port_on_a_private_server_and_shows_a_result(
    tmp_path: Path, plan_sqlite_seeder, monkeypatch,
):
    """The same chain, against a server this test starts itself.

    The declared port is held by a stranger this test started.  The
    round must get out of the way (start a copy on a port of its own),
    the copy must become ready, the page must show a readable result,
    the request must have gone to the copy's port -- and the stranger
    must still be running when the page says so (R4).
    """
    plans_dir = tmp_path / "plans"
    state_db = tmp_path / "state.db"
    project_dir = tmp_path / "project"
    plan_dir = plans_dir / PLAN_ID
    script_dir = tmp_path / "llm"
    pid_dir = project_dir / "tmp" / "pids"

    for directory in (plans_dir, plan_dir, project_dir, pid_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (project_dir / "fixture_service.py").write_text(
        _FIXTURE_SERVICE, encoding="utf-8"
    )

    # The seeder and the server have to read **the same** database: the
    # server only lists the plans its own database knows about, so a
    # seed that went somewhere else would leave the fixture invisible
    # (and the plans listing would refuse to answer at all).
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(state_db))

    # The port the declaration names.  Its digits must not occur in any
    # path the start command carries: the relocation rewrites every
    # standalone occurrence of the port there.
    declared_port = _allocate_declared_port(
        str(state_db), str(project_dir), str(plans_dir), str(pid_dir),
    )

    stranger = _start_stranger(declared_port)
    server: Optional[subprocess.Popen] = None
    try:
        _wait_for_a_listener(declared_port, stranger)

        _write_scripted_llm(script_dir)
        _write_verification_plan(plan_dir, declared_port, state_db, pid_dir)
        plan_sqlite_seeder(
            plan_dir,
            PLAN_ID,
            phase="executing",
            project_dir=project_dir,
            verification={"status": "not_started", "round": 0, "max_rounds": 3},
        )

        base_url, server = _boot_server(tmp_path, script_dir, plans_dir, state_db)

        screenshot = _screenshot_path()
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        driver_script = tmp_path / "driver.py"
        driver_script.write_text(_BROWSER_DRIVER, encoding="utf-8")

        interpreter = _require_playwright()
        report = _run_driver(
            interpreter, driver_script, base_url, PLAN_ID, screenshot, [VP_ID],
        )

        # --- the page showed a readable result ------------------------
        assert report["badge_while_running"] == RUNNING_BADGE_LABEL, report
        assert report["badge_after"] in FINAL_BADGE_LABELS, report
        assert report["start_button_visible"] is True, report
        assert report["icons"].get(VP_ID) == "✅", report
        assert report["page_errors"] == [], report
        assert report["dialogs"] == [], report
        assert screenshot.exists(), (
            f"the acceptance screenshot is missing: {screenshot}"
        )

        # --- the request went to the copy, not to the stranger -------
        # The port the plan declares is read back from the plan file:
        # that file is what the round resolved, so it is the port the
        # request must **not** have gone to.
        plan = json.loads(
            (plan_dir / "verification_plan.json").read_text(encoding="utf-8")
        )
        plan_port = int(plan["services"][0]["port"])
        assert plan_port == declared_port, (
            f"the fixture plan declares port {plan_port} but the stranger "
            f"holds {declared_port}: they must be the same port for this "
            "acceptance to mean anything"
        )
        artifact_path = plan_dir / "vp_artifacts" / VP_ID / "api_response.json"
        assert artifact_path.exists(), (
            f"the verification point produced no artifact: {artifact_path}"
        )
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        request_url = str((artifact.get("request") or {}).get("url") or "")
        assert request_url, artifact
        artifact_port = urllib.parse.urlsplit(request_url).port
        assert artifact_port is not None, request_url
        assert artifact_port != plan_port, (
            f"the request went to the stranger's port {plan_port}: "
            f"{request_url}"
        )

        # --- the address event stream says where the port came from --
        events_path = plan_dir / "logs" / "service_address_events.jsonl"
        assert events_path.exists(), (
            f"the round wrote no address events: {events_path}"
        )
        events = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        relocated = [
            event for event in events
            if event.get("event") == "relocated"
            and event.get("old_port") == plan_port
        ]
        assert relocated, (
            "no 'relocated' address event for the declared port "
            f"{plan_port}: {events}"
        )
        assert relocated[0]["new_port"] == artifact_port, (
            f"the relocated port {relocated[0]['new_port']} is not the port "
            f"the request went to ({artifact_port})"
        )

        # --- the stranger is still alive (R4) ------------------------
        assert stranger.poll() is None, (
            "the stranger must still be running: a port held by a process "
            "we cannot positively identify is not ours to kill"
        )
    finally:
        _kill_pid_files(pid_dir)
        _teardown_server(server)
        # The stranger is left alone on purpose: it is a process this
        # test cannot identify as its own, and its own bounded lifetime
        # is what ends it.
