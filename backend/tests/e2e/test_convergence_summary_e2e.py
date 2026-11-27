"""端到端：收敛汇总端点 —— 在浏览器里问出「现在收敛率多少」。

背景
----
§1、§2、§3 的改动分散在 subagent、executor、orchestrator、loop 与只读端点，
各自的单测都绿，但那不等于整条链路可用：这个端点读的那张表，装的正是前面
几轮写进去的「为什么不收敛」；判定表散在好几个模块里，谁也没有把它们汇成
一张表。

这个文件是那条链路的**验收**：一个真的服务进程、一个真的浏览器、一次真的
``fetch``，然后把运维真正会看到的那几个数摆出来。

这条链
------
1. 用 ``state_machine.db.schema.migrate`` 往一个专属的状态库里写六行
   ``plan_verification``，``updated_at`` 统一为「服务启动前一分钟」——
   同一个时刻是刻意的：窗口过滤读的就是这一列；
2. 按运维的方式起一个真的服务进程（``uvicorn server:app``），等到
   ``/api/health`` 回答 200；
3. 用 Playwright 打开 ``http://127.0.0.1:<port>/``，在页面里执行
   ``fetch('/api/verification/convergence-summary?window_days=30')``，
   带着请求守卫的头 —— 那是前端自己的客户端会带的那一个；
4. 截图落盘 —— 证据。

六行与手算
----------
::

    e2e-conv-pass-1  passed                        —— 收敛了
    e2e-conv-pass-2  passed                        —— 收敛了
    e2e-conv-defect  FAILED / repair_gate_red      —— 代码不对
    e2e-conv-flake   FAILED / verdict_unavailable  —— 基础设施不稳
    e2e-conv-env     FAILED / service_preflight_failed —— 环境挡住
    e2e-conv-user    user_stopped                  —— 人叫停

分母 = 已出结论、且既不是人叫停也不是环境挡住 = 6 - 1 - 1 = **4**
分子 = 分母里 ``passed`` 的 = **2**
rate = 2 / 4 = **0.5**

``by_category`` 统计**全部**已出结论的行（人叫停与环境挡住都算 —— 它们只是
不进分母，因为它们说的不是「产品收不收敛」）：``code_defect`` 1 /
``infra_flake`` 1 / ``env_blocked`` 1 / ``user_stopped`` 1。

``unconverged`` 是「出了结论、但既不是通过也不是人叫停」的那些 —— 恰好三条：
``code_defect`` / ``infra_flake`` / ``env_blocked`` 各一。

为什么是浏览器
--------------
这条端点是给**运维在浏览器里**看的。curl 证明端点算得出来；浏览器证明它在
真实的同源页面里能被取到 —— 而且取到的是 JSON，不是被前端的 catch-all 接走
的 ``index.html``。

环境给不了的东西
----------------
**一个浏览器。** 没装 Playwright 的驱动、或者驱动在但浏览器二进制没下下来
的时候，这个用例 **skip**：一个没被驱动过的页面证明不了页面。浏览器二进制
在 Playwright 自己的缓存里，这里按仓库里其它端到端用例的同一把尺子去找
（找得到就用它，避免「驱动要的版本与装好的版本对不上」这件事）。

**一个模型。** 这条链路上没有任何一步需要模型：端点是只读的，一个字都不写。
所以服务用 ``PDT_LLM_MODE=scripted`` 起 —— 那是为了让它**不**去问真的模型，
不是为了让它问假的模型。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# 路径与端口
# ---------------------------------------------------------------------------

#: ``<repo>/backend`` —— 本文件住在 ``backend/tests/e2e/``。
BACKEND_DIR: Path = Path(__file__).resolve().parents[2]

#: ``<repo>`` —— 服务进程的 ``PYTHONPATH`` 需要它（``backend.framework.*``）。
REPO_ROOT: Path = BACKEND_DIR.parent

#: 这次验收的现场根目录。路径是任务书指定的那个，不是谁家目录里的路径，
#: 仓库里没有任何东西依赖它 —— 截图是证据，不是输入。
#:
#: 名字里带着 ``tmp``：这条根永远是 /tmp 底下的一次性现场，静态门禁
#: (``test_tests_do_not_write_real_plans``) 要的就是这种「根可证明」的写法
#: —— 它没法跟着环境变量去猜根落在哪儿，只能看出名字本身是不是临时根。
E2E_TMP_ROOT: Path = Path(
    os.environ.get("PDT_CONVERGENCE_E2E_ROOT", "/tmp/pdt-e2e")
)

#: 状态库：端点读的就是这张表里的 ``plan_verification``。
STATE_DB: Path = E2E_TMP_ROOT / "state.db"

#: 计划目录：服务需要它存在（它读计划的样子），但这条链路上没有计划。
#: 它落在上面那个临时根底下 —— 不是仓库里那棵被服务端读着的 plans 树。
PLANS_DIR: Path = E2E_TMP_ROOT / "plans"

#: 服务进程的日志。服务起不来时，这是唯一写着「为什么」的地方。
SERVER_LOG: Path = E2E_TMP_ROOT / "server.log"

#: 截图 —— 运维看见的那一屏。
SCREENSHOT_PATH: Path = E2E_TMP_ROOT / "convergence-summary.png"

#: 截图的**第二份**落点，在共享根之外。
#:
#: ``/tmp/pdt-e2e`` 是整轮 e2e 共用的根，而它会被清空：仓库里另一个 e2e
#: 用例在它自己的模块夹具里 ``rmtree(RUN_ROOT)`` —— 那一棵正是
#: ``/tmp/pdt-e2e``。它按文件名字母序排在本文**之后**（``test_convergence…``
#: < ``test_token_convergence…``），所以它清空的时候，本用例刚写下的截图
#: 还在盘上，却会在这一轮结束时被抹掉。任务书要的「端到端验收截图」得活得
#: 比这一轮久 —— 所以另存一份在共享根之外，两份都在。
#:
#: 不落在仓库里：那是产物，不是源码；``backend/tests/e2e/artifacts/`` 那类
#: 目录由各自的用例管，这条链路没有理由往仓库树里写东西。
DURABLE_SCREENSHOT_PATH: Path = Path(
    os.environ.get("PDT_CONVERGENCE_E2E_DURABLE_DIR", "/tmp")
) / "pdt-e2e-convergence-summary.png"

#: 端口。任务书指定的是 18973；环境变量是为了让并行跑的时候不撞车。
PORT: int = int(os.environ.get("PDT_CONVERGENCE_E2E_PORT", "18973"))

#: 浏览器的根地址。
BASE_URL: str = f"http://127.0.0.1:{PORT}"

#: 请求守卫的头：每个 ``/api/*`` 请求都必须带它，否则服务端回答 403。
#: 前端自己的客户端带的就是这一个 —— 这里带的是同一个。
REQUEST_HEADERS: Dict[str, str] = {"X-PDT-Request": "1"}


# ---------------------------------------------------------------------------
# 播种的那六行
# ---------------------------------------------------------------------------

#: ``(plan_id, verification_status, verification_stop_reason)``。
#:
#: 状态的大小写照任务书写：``FAILED`` 是大写 —— 判定表一律先归一化再比，
#: 所以大写的 ``FAILED`` 与小写的 ``failed`` 归到同一个分类。这不是炫技，
#: 是把「归一化」这条契约真的走一遍。
#:
#: 最后一行写的是 ``user_stopped``：**状态**那一列。生产里落库的是
#: ``loop_stopped`` + ``stop_reason='user_stopped'``（``mark_stopped`` 就是
#: 这么写的），而判定表两种写法都认 —— 这里用状态那一列的写法，好让这条
#: 分支也真的被走到一次。
SEED_ROWS: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("e2e-conv-pass-1", "passed", None),
    ("e2e-conv-pass-2", "passed", None),
    ("e2e-conv-defect", "FAILED", "repair_gate_red"),
    ("e2e-conv-flake", "FAILED", "verdict_unavailable"),
    ("e2e-conv-env", "FAILED", "service_preflight_failed"),
    ("e2e-conv-user", "user_stopped", "user_stopped"),
)


# ---------------------------------------------------------------------------
# 页面里发出去的那次请求
# ---------------------------------------------------------------------------

#: 在页面里执行的那段 JavaScript。它发的是前端自己的客户端会发的那种请求：
#: 同一个源、同一个守卫头。
#:
#: ``fetch`` 的返回值是一个 ``Response``，它不能穿过 Playwright 的序列化
#: 边界 —— 所以这段代码把状态码与解析出来的 JSON 一起装进一个普通对象带回来。
_FETCH_SUMMARY_JS = """
async () => {
    const response = await fetch(
        '/api/verification/convergence-summary?window_days=30',
        {headers: {'X-PDT-Request': '1'}}
    );
    return {status: response.status, body: await response.json()};
}
"""


# ---------------------------------------------------------------------------
# 播种
# ---------------------------------------------------------------------------


def _seed_state_db() -> str:
    """把 :data:`SEED_ROWS` 写进状态库，返回写入的 ``updated_at``。

    库是**重建**的，不是追加的：端点数的是整张 ``plan_verification`` 表，
    上一轮留下的行会混进分母，手算就不成立了。

    ``updated_at`` 统一为「服务启动前一分钟」—— 窗口过滤读的就是这一列，
    六行落在同一个时刻上，才谈得上「这批」。
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    E2E_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_DB.exists():
        STATE_DB.unlink()

    updated_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn = open_db(STATE_DB)
    try:
        migrate(conn)
        for plan_id, status, reason in SEED_ROWS:
            conn.execute(
                "INSERT INTO plan_verification ("
                "  plan_id, verification_status, verification_stop_reason, updated_at"
                ") VALUES (?, ?, ?, ?)",
                (plan_id, status, reason, updated_at),
            )
        conn.commit()
    finally:
        conn.close()
    return updated_at


def _plan_verification_fingerprint() -> str:
    """``plan_verification`` 全表的 sha256 —— 「端点一个字节都没写」的尺子。

    取的是**全表**（``SELECT *``）而不是那四列：端点要是在别处动了这张表，
    只看四列会漏掉。行按 ``plan_id`` 排 —— 那是主键，顺序是确定的，所以
    同一个内容永远得到同一个摘要；``repr`` 用的是元组（不是 ``sqlite3.Row``
    的地址），所以两次读到的同一张表给出同一个串。

    这把尺子量的是「只读」那半个承诺。端点的文档写着它不写这张表，而
    「不写」是一条只有比过前后才说得清的断言。
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db

    conn = open_db(STATE_DB)
    try:
        rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM plan_verification ORDER BY plan_id"
            ).fetchall()
        ]
    finally:
        conn.close()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _state_db_rows() -> List[Tuple[str, Optional[str]]]:
    """库里 ``plan_verification`` 的 ``(plan_id, updated_at)``，按 id 排。

    读的是**服务读的那张表** —— 端点的数就是从这张表算出来的，所以这张表
    本身就是尺子。
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db

    conn = open_db(STATE_DB)
    try:
        return [
            (str(row[0]), row[1])
            for row in conn.execute(
                "SELECT plan_id, updated_at FROM plan_verification ORDER BY plan_id"
            ).fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 证据的落盘
# ---------------------------------------------------------------------------


def _publish_durable_copy(primary: Path, destination: Path) -> Path:
    """把主图另存一份到共享根之外，返回另存的那份的路径。

    拷贝而不是移动：任务书指定的那份留在原地（``/tmp/pdt-e2e/
    convergence-summary.png``），另存的一份是**加**上去的保险 —— 共享根
    被清空时，至少还有一份证据在。

    目录不存在就建出来：另存的位置可能还没有。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(primary, destination)
    return destination


# ---------------------------------------------------------------------------
# 服务进程
# ---------------------------------------------------------------------------


def _server_python() -> str:
    """起服务用的解释器。

    仓库自己的虚拟环境优先（CI 里 ``uv sync`` 建的就是它），没有它就用跑
    pytest 的这个解释器 —— 一个没有虚拟环境的检出也得能把服务起起来，而不是
    在起之前就失败。
    """
    venv_python = BACKEND_DIR / ".venv" / "bin" / "python3"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _port_is_free(port: int) -> bool:
    """这个端口现在能不能被绑上。

    这个检查是承重的：端口被人占着的时候，我们起的那个服务会带着
    「address already in use」退出去，而健康探针会问到**占着端口的那个
    服务** —— 探针拿到 200，用例却以为自己问的是自己起的那个。先问一句，
    比事后猜要便宜。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _server_env() -> Dict[str, str]:
    """服务进程的环境。

    继承当前进程的环境（里面已经有这套件自己的隔离：状态库、计划目录、
    仓库根都在 ``PYTHONPATH`` 上），再把它自己要的那几样写死 —— 服务看的
    必须是**这个**状态库，不是隔离环境里那一个。
    """
    env = dict(os.environ)
    pythonpath = [str(REPO_ROOT)]
    inherited = env.get("PYTHONPATH", "")
    if inherited:
        pythonpath.append(inherited)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(STATE_DB),
            "PDT_PLANS_DIR": str(PLANS_DIR),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": str(PORT),
            "PDT_DEBUG_ROUTES": "1",
            "PDT_LLM_MODE": "scripted",
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1",
            "PYTHONPATH": os.pathsep.join(pythonpath),
        }
    )
    return env


def _start_server() -> subprocess.Popen:
    """按运维的方式起一个真的服务进程。

    命令就是任务书写的那一条：``python -m uvicorn server:app --host
    127.0.0.1 --port <port>``。没有替身，没有 mock —— 这条链路要验的就是
    「真的跑起来时它对不对」。
    """
    E2E_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    log_handle = SERVER_LOG.open("wb")
    try:
        return subprocess.Popen(
            [
                _server_python(),
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
            ],
            cwd=str(BACKEND_DIR),
            env=_server_env(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        # 子进程有自己的那份描述符；这一份留着只是白占一个句柄。
        log_handle.close()


def _stop_server(proc: subprocess.Popen) -> None:
    """把服务停下，并且确认端口能再被绑上。

    停不掉的服务会活过它自己那个用例，去回答下一轮的健康探针 —— 所以这里
    不只是「发个信号」，还要确认它真的走了。
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover — 防御性
            pass
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _port_is_free(PORT):
            return
        time.sleep(0.1)
    raise AssertionError(
        f"the server on :{PORT} was stopped but the port is still bound; a "
        f"listener this case started would outlive it and answer a later "
        f"run's health probe"
    )


def _server_log_tail(limit: int = 2000) -> str:
    """服务日志的尾巴 —— 服务起不来时，唯一写着「为什么」的地方。"""
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<server log unavailable>"


def _wait_for_health(proc: subprocess.Popen, timeout: float = 90.0) -> None:
    """等到 ``/api/health`` 回答 200。

    ``/api/health`` 在守卫的范围内，所以探针也得带守卫的头 —— 服务回答 403
    是「我还活着但你不该这么问」，不是「我好了」。
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the server on {BASE_URL} exited with {proc.returncode} before "
                f"it became healthy. Its output:\n{_server_log_tail()}"
            )
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/api/health", headers=dict(REQUEST_HEADERS)
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                if 200 <= int(response.status) < 300:
                    return
                last = f"status {response.status}"
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = repr(exc)
        time.sleep(0.25)
    raise AssertionError(
        f"the server on {BASE_URL} did not answer /api/health with 200 within "
        f"{timeout}s; the last answer was {last}. Its output:\n{_server_log_tail()}"
    )


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------


def _playwright_cache_roots() -> List[Path]:
    """Playwright 缓存可能所在的地方。

    缓存住在**发起这次运行的那个家目录**下面 —— 这套件把 ``$HOME`` 重定向到
    一个临时目录（为了不碰操作者的家），所以真实家目录要从
    ``PDT_TEST_REAL_HOME`` 读回来。
    """
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    home = Path(real_home) if real_home else Path.home()
    return [
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
    ]


def _browser_executable() -> Optional[str]:
    """已经装好的 Chromium，找得到就返回它的路径。

    Playwright 从自己的缓存里解析它要启动的浏览器；驱动要的版本与装好的
    版本对不上时，它启动不了任何东西。这里找的是**已经装好的**那些 ——
    什么都不下载，也什么都不写死。
    """
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
    )
    for root in _playwright_cache_roots():
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern)):
                if candidate.exists():
                    return str(candidate)
    return None


def _driver_available() -> bool:
    """这个环境里能不能真的驱动一个浏览器。"""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# 验收
# ---------------------------------------------------------------------------


def test_convergence_summary_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """六行种进库里 → 浏览器里问一次 → 运维会看到的那几个数成立。

    四条断言，条条是手算出来的：

    * ``denominator == 4`` —— 环境挡住与人叫停都出了结论，但**都不进分母**；
    * ``numerator == 2`` 且 ``rate`` 就是 0.5；
    * ``by_category`` 四类各一；
    * ``unconverged`` 恰好三条（``code_defect`` / ``infra_flake`` /
      ``env_blocked`` 各一），截图落在盘上。
    """
    if not _driver_available():
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "endpoint cannot be asked from a browser, and an endpoint that "
            "was not asked proves nothing about the endpoint"
        )
    executable = _browser_executable()
    if executable is None:
        pytest.skip(
            "the Playwright driver is installed but its browser binaries are "
            "not; the endpoint cannot be asked from a browser"
        )

    # 浏览器要能找到自己的缓存 —— 缓存住在真实家目录下面，而这套件把
    # ``$HOME`` 重定向走了。服务进程已经用隔离环境的 ``$HOME`` 起好了，
    # 这里换回去只影响浏览器。
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        monkeypatch.setenv("HOME", real_home)

    updated_at = _seed_state_db()
    assert _port_is_free(PORT), (
        f"port {PORT} is already taken on this machine; the health probe "
        f"would answer from somebody else's server and this case would "
        f"certify a chain it never started"
    )
    proc = _start_server()
    try:
        _wait_for_health(proc)
        assert proc.poll() is None, (
            f"the server this case started is already gone "
            f"(exit code {proc.returncode}); the health answer came from "
            f"somebody else. Its output:\n{_server_log_tail()}"
        )

        from playwright.sync_api import sync_playwright

        with sync_playwright() as driver:
            browser = driver.chromium.launch(
                headless=True, executable_path=executable
            )
            try:
                page = browser.new_page()
                page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
                # 「只读」是这条端点的半个承诺（arch 决策点 8）：它不写
                # ``plan_verification``。摘要取在 fetch 的两侧 —— 而且取
                # 紧贴着**那一次** fetch：页面加载带来的别的写入不该算在
                # 这条端点的头上。
                fingerprint_before = _plan_verification_fingerprint()
                answer = page.evaluate(_FETCH_SUMMARY_JS)
                fingerprint_after = _plan_verification_fingerprint()
                page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
                # 另存一份在共享根之外 —— 共享根会被另一个 e2e 用例清空。
                _publish_durable_copy(SCREENSHOT_PATH, DURABLE_SCREENSHOT_PATH)
            finally:
                browser.close()
    finally:
        _stop_server(proc)

    # --- 服务确实回答了，且回答的是 JSON ---------------------------------
    assert isinstance(answer, dict), f"the page answered with {answer!r}"
    assert answer["status"] == 200, (
        f"the endpoint answered with {answer['status']}: {answer['body']!r}"
    )
    body = answer["body"]
    assert isinstance(body, dict), f"the answer is not a document: {body!r}"

    # --- 手算：分母 4、分子 2、rate 0.5 ----------------------------------
    # 环境挡住与人叫停都出了结论，但它们说的不是「产品收不收敛」，所以
    # 不进分母：6 - 1 - 1 = 4。
    assert body["denominator"] == 4, (
        f"the denominator is {body['denominator']}, hand-count says 4: {body!r}"
    )
    assert body["numerator"] == 2, (
        f"the numerator is {body['numerator']}, hand-count says 2: {body!r}"
    )
    assert abs(float(body["rate"]) - 0.5) < 0.01, (
        f"the rate is {body['rate']}, hand-count says 0.5: {body!r}"
    )

    # --- 分类直方图：四类各一 --------------------------------------------
    # by_category 覆盖每一个出结论的行（人叫停 / 环境挡住都算），
    # 但 passed 不是一种失败分类，不进这张表。
    assert body["by_category"] == {
        "code_defect": 1,
        "infra_flake": 1,
        "env_blocked": 1,
        "user_stopped": 1,
    }, f"the categories are {body['by_category']!r}, hand-count says one each"

    # --- 未收敛清单：三条，各一类 ----------------------------------------
    unconverged = body["unconverged"]
    assert len(unconverged) == 3, (
        f"the unconverged list holds {len(unconverged)} entries, hand-count "
        f"says three: {unconverged!r}"
    )
    assert sorted(entry["category"] for entry in unconverged) == [
        "code_defect",
        "env_blocked",
        "infra_flake",
    ], f"the unconverged list is {unconverged!r}, hand-count says one of each"
    assert all(entry.get("plan_id") for entry in unconverged), (
        f"an unconverged entry does not name its plan: {unconverged!r}"
    )

    # --- 只读：那张表一个字节都没动 --------------------------------------
    # 「只读」是这条端点的半个承诺（arch 决策点 8：只读入口可被真实调用）。
    # 上面那几条只证明了「算得对」，没证明「没写」—— 而端点一旦顺手刷新了
    # ``updated_at``，下一次再量的时候窗口的起点就被自己挪走了，数字会
    # 随「看」的次数而变。
    assert fingerprint_after == fingerprint_before, (
        "the endpoint wrote to plan_verification; it is a read-only entry "
        f"point: before={fingerprint_before} after={fingerprint_after}"
    )

    # --- 证据 --------------------------------------------------------------
    assert SCREENSHOT_PATH.exists(), (
        f"the screenshot is not on disk: {SCREENSHOT_PATH}"
    )
    assert SCREENSHOT_PATH.stat().st_size > 0, (
        f"the screenshot on disk is empty: {SCREENSHOT_PATH}"
    )

    # 播种的六行确实落在服务读的那张表里 —— 上面的数是从这张表算出来的，
    # 表里没有它们，那些数就无从谈起。夹具没种上，后面什么也证明不了。
    rows = _state_db_rows()
    seeded_ids = {plan_id for plan_id, _status, _reason in SEED_ROWS}
    assert {plan_id for plan_id, _ts in rows} == seeded_ids, (
        f"the state database holds {sorted(plan_id for plan_id, _ts in rows)}, "
        f"the seeded rows are {sorted(seeded_ids)}"
    )
    # 六行的 ``updated_at`` 是同一个时刻 —— 窗口过滤读的就是这一列。
    assert {timestamp for _plan_id, timestamp in rows} == {updated_at}, (
        "the seeded rows do not carry one common updated_at"
    )

    # --- 证据要活得比这一轮久 --------------------------------------------
    # 共享根 ``/tmp/pdt-e2e`` 是整轮 e2e 共用的：仓库里另一个 e2e 用例在
    # 它自己的模块夹具里 ``rmtree`` 整个根目录，而它按文件名字母序排在
    # 本文之后 —— 于是本用例产出的截图会被它抹掉，任务书要的「端到端验收
    # 截图」就留不下来了。所以另存一份在共享根之外。
    assert DURABLE_SCREENSHOT_PATH.exists(), (
        f"the durable copy of the screenshot is not on disk: "
        f"{DURABLE_SCREENSHOT_PATH}"
    )
    assert DURABLE_SCREENSHOT_PATH.stat().st_size > 0, (
        f"the durable copy on disk is empty: {DURABLE_SCREENSHOT_PATH}"
    )
    assert (
        DURABLE_SCREENSHOT_PATH.read_bytes() == SCREENSHOT_PATH.read_bytes()
    ), (
        f"the durable copy {DURABLE_SCREENSHOT_PATH} does not carry the same "
        f"bytes as the screenshot {SCREENSHOT_PATH}"
    )


def test_published_screenshot_outlives_the_shared_root_wipe(tmp_path) -> None:
    """另存的那一份，扛得住共享根被整个抹掉。

    这条用例不碰真的 ``/tmp/pdt-e2e``：它在 ``tmp_path`` 里造一个假的共享
    根，写一张假截图进去，把它整棵抹掉，然后问另存的那一份还在不在。

    为什么值得单独立一条：端到端那条用例只证明了「截图写下来了」，没证明
    「它活得过共享根被清空」。而共享根确实会被清空 —— ``/tmp/pdt-e2e`` 是
    整轮 e2e 共用的，另一个用例在它的模块夹具里就 ``rmtree`` 这一棵。
    """
    shared_root = tmp_path / "pdt-e2e"
    shared_root.mkdir()
    primary = shared_root / "convergence-summary.png"
    payload = b"\x89PNG\r\n\x1a\nnot-really-a-png-but-bytes-are-bytes"
    primary.write_bytes(payload)

    durable = _publish_durable_copy(primary, tmp_path / "durable" / "shot.png")

    # 另一个用例的模块夹具做的事，一模一样。
    shutil.rmtree(shared_root, ignore_errors=True)

    assert not primary.exists(), (
        "the shared root was not wiped, so this case proves nothing about "
        "durability"
    )
    assert durable.exists(), (
        f"the durable copy did not outlive the shared-root wipe: {durable}"
    )
    assert durable.read_bytes() == payload, (
        f"the durable copy carries different bytes: {durable}"
    )
