"""验证链闭环的端到端验收 —— 真 server、真钩子、真判定。

背景
----
缺陷 A（改过被验证树的 VP 拿着 PASSED 回轮次）与缺陷 B（成本闸、命令类
e2e 的判定路径）改的都是**验证链路自身**。链路自己的单元测试全绿，只能
证明每个零件没坏，证明不了**零件接起来**还能用 —— 坏掉的常常是接缝，
而接缝是单元测试看不见的那一段。

所以这份文件不测零件，测**接缝**：

  1. ``test_end_to_end_ui_and_gates`` —— 起一个真的 server 子进程，用真的
     Chromium 打开真的页面，点一次真的按钮，敲一次真的字，截一张真的图。
     断言落在**用户看得见的那个**控件上，不是 DOM 里躺着一个看不见的节点。
  2. ``test_tree_guard_rolls_back_in_real_repo`` —— 在 tmp git 仓库里跑
     **真的钩子脚本**（pre / post 两个独立进程），中间执行真的越权命令，
     然后断言四件事：patch 落了盘、树被回滚了、账本里有一行、判定层把这条
     PASSED 改判成 FAILED 并记了 ``verifier_mutated_tree`` 事件（四个字段
     齐全）。
  3. ``test_command_e2e_runs_without_llm`` —— 命令类 e2e 的判定由**退出码**
     给出（不开 LLM 会话），``cost=llm_quota`` 的验证点在默认开关下是
     DEFERRED（不执行），报告里单列「因成本跳过」小节。

三条硬约束
----------
* **不许 skip。** 浏览器起不来、server 起不来、钩子跑不动 —— 都是**失败**。
  一个被 skip 掉的验收等于没有验收：它既不证明链路是好的，也不证明它是坏的。
* **不许碰真数据。** server 的状态库与 plans 目录都指向 ``tmp_path``；被
  验证的仓库是 ``tmp_path`` 下的临时 git 仓库；截图落在 ``/tmp`` 下的固定
  目录，不进 home。
* **不许留进程。** server 子进程在用例结束时一定被 terminate 并 wait；浏览器
  一定被关掉。

跑在哪个 lane
-------------
本文件带 ``e2e`` 标记：只有 e2e 那条 lane 装了 Chromium，integration 那条
lane 没有 —— 标记错了，浏览器在 CI 上必然起不来。文件本身按任务要求放在
``tests/integration/`` 下（它是「跑真 server 的集成用例」，不是「mock 计划的
全流程 e2e」）。

唯一的替身
----------
第 2 条用例里，**子 agent**（要开 LLM 会话的那一半）被替身挡掉了：CI 上
没有模型。其余每一环 —— 钩子脚本、账本、判定层、持久化 —— 都是真的。
替身只挡在模型边界上，而那正是生产里本来就有的边界；它交回什么结论不
影响这条用例要证明的事：有 mutation 记录就改判，没有记录就原样通过。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx
import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
import verification_ci_runner  # noqa: E402
import verification_executor  # noqa: E402
import verification_report_md  # noqa: E402
import verification_subagent  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_persistence import VerificationPersistenceManager  # noqa: E402
from verification_subagent import Verdict  # noqa: E402

#: 整个文件跑在 e2e lane —— 只有那条 lane 装了 Chromium。
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: 截图落地的目录：固定在 ``/tmp`` 下，不进 home，也不进仓库。
SCREENSHOT_DIR = Path("/tmp/pdt-e2e-verify-gate")

#: server 起不来的上限（秒）。超时是失败，不是 skip。
READY_TIMEOUT_SECONDS = 60.0

#: 浏览器里输入框出现的上限（毫秒）。
INPUT_TIMEOUT_MS = 5000

#: 本轮轮次号（判定层按它读账本）。
ROUND_NO = 1

#: 树护栏那条用例的验证点号。
TREE_VP_ID = "VP-E2E-TREE"

#: 钩子的工具调用号 —— 快照文件按它命名。
TOOL_USE_ID = "tu_e2e_tree_1"

#: 越权命令原文：一条改被跟踪的文件，一条新建文件。
MUTATION_COMMAND = "echo x >> tracked.txt && echo n > new.txt"

#: pytest 的 hermetic 环境把 ``$HOME`` 指向临时目录，原 home 记在这里。
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

#: 成本闸的开关（操作者显式放行）。
ALLOW_LLM_QUOTA_ENV = "PDT_VERIFY_ALLOW_LLM_QUOTA"


# ---------------------------------------------------------------------------
# The live server
# ---------------------------------------------------------------------------


def _acquire_port() -> int:
    """向内核要一个空闲的回环端口。

    端口一律现取，**不写死常量**：写死的号会被别的服务占着，而对着别人
    的服务做断言等于什么都没验。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _child_env(tmp_path: Path, port: int) -> Dict[str, str]:
    """server 子进程的环境 —— 白名单，且状态库与 plans 目录都指向 tmp_path。

    两条最要紧的：

    * **状态库与 plans 目录都进 ``tmp_path``。** 真 plans 目录里是操作者
      的计划，真状态库里是操作者的运行记录 —— 这条用例绝不许往里写一个
      字节。
    * **不带 ``HOME``。** 没有它就没有钥匙串，通知器判定「未配置」，这条
      用例不会去动操作者的真实密钥链。

    ``PYTHONPATH`` 带上仓库根：``backend/prompts.py`` 要
    ``backend.framework.prompts``，只放 ``backend/`` 的话子进程在 import
    期就死了。
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(
            part
            for part in (str(_REPO_ROOT), os.environ.get("PYTHONPATH", ""))
            if part
        ),
        "PDT_PLANS_DIR": str(plans_root),
        "PDT_STATE_DB_PATH": str(tmp_path / "state.db"),
        "PDT_PORT": str(port),
        "PDT_HOST": "127.0.0.1",
        "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "PDT_PROVIDER_MODE": "inherit",
        "PDT_DISABLE_TEMP_SWEEP": os.environ.get("PDT_DISABLE_TEMP_SWEEP", "1"),
    }


def _stderr_tail(path: Path, lines: int = 50) -> str:
    """server 的 stderr 最后几行 —— 它死掉时唯一会解释自己的地方。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<server stderr unavailable>"
    tail = text.splitlines()[-lines:]
    return "\n".join(tail) if tail else "<server stderr is empty>"


class _LiveServer:
    """一个真的 server 子进程。"""

    def __init__(
        self,
        proc: subprocess.Popen,
        port: int,
        stderr_path: Path,
        stderr_handle: Any,
    ) -> None:
        self.proc = proc
        self.port = port
        self.stderr_path = stderr_path
        self.stderr_handle = stderr_handle

    @property
    def base_url(self) -> str:
        """The URL the browser and the readiness probe both talk to."""
        return "http://127.0.0.1:{0}".format(self.port)

    def stop(self) -> None:
        """收掉子进程 —— 不留一个孤儿在后台跑。"""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        try:
            self.stderr_handle.close()
        except OSError:
            pass


def _wait_until_ready(
    server: _LiveServer, timeout: float = READY_TIMEOUT_SECONDS,
) -> None:
    """等 server 真的能回答 ``/health``。

    起不来 → ``AssertionError``，附上 stderr 的最后 50 行。**不 skip**：
    「server 没起来」是一个要修的事实，不是「这次不算」。
    """
    deadline = time.monotonic() + timeout
    last_error = "no request was attempted"
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            raise AssertionError(
                "the spawned server exited before it became ready "
                "(rc={0})\n--- stderr (last 50 lines) ---\n{1}".format(
                    server.proc.returncode, _stderr_tail(server.stderr_path),
                )
            )
        try:
            with httpx.Client(trust_env=False, timeout=2.0) as client:
                response = client.get(server.base_url + "/health")
        except httpx.HTTPError as exc:
            last_error = repr(exc)
        else:
            if response.status_code == 200:
                return
            last_error = "status {0}".format(response.status_code)
        time.sleep(0.25)
    raise AssertionError(
        "the spawned server did not become ready within {0}s ({1})\n"
        "--- stderr (last 50 lines) ---\n{2}".format(
            timeout, last_error, _stderr_tail(server.stderr_path),
        )
    )


def _start_server(tmp_path: Path) -> _LiveServer:
    """起一个真的 server（独立子进程），并等它就绪。"""
    port = _acquire_port()
    stdout_path = tmp_path / "server.stdout.log"
    stderr_path = tmp_path / "server.stderr.log"
    stdout_handle = open(stdout_path, "wb")  # noqa: SIM115 — closed by stop()
    stderr_handle = open(stderr_path, "wb")  # noqa: SIM115 — closed by stop()
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "uvicorn", "server:app",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=str(_BACKEND_DIR),
        env=_child_env(tmp_path, port),
        stdout=stdout_handle,
        stderr=stderr_handle,
    )
    server = _LiveServer(proc, port, stderr_path, stderr_handle)
    try:
        _wait_until_ready(server)
    except AssertionError:
        server.stop()
        raise
    return server


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------


def _real_home() -> Path:
    """跑起来那个 home —— Chromium 就装在它底下。

    pytest 的 hermetic 环境把 ``$HOME`` 指向一个临时目录；真 home 由
    ``_pytest_hermetic_env`` 记在 ``PDT_TEST_REAL_HOME`` 里。
    """
    value = os.environ.get(REAL_HOME_ENV, "").strip()
    if value:
        return Path(value)
    return Path.home()


@contextlib.contextmanager
def _real_home_env() -> Iterator[Path]:
    """调用期间把 ``$HOME`` 指回跑起来那个 home。"""
    home = _real_home()
    previous = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        yield home
    finally:
        if previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous


def _chromium_executable(home: Path) -> Optional[str]:
    """已装好的 Chromium —— 在 playwright 的缓存里找，不下载、不写死路径。

    路径是**运行时**在这个 home 底下找出来的：写死一份绝对路径会在别的
    机器上（CI runner、别人的笔记本）静悄悄地指到一个不存在的地方。
    """
    roots = (
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
    )
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


# ---------------------------------------------------------------------------
# The tree guard (a real hook script, run as a real process)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑一条 git 命令，返回 stdout。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def _init_repo(repo: Path) -> Path:
    """建一个一次性的 git 仓库，里面有一个被跟踪的文件。"""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _run_guard(
    phase: str, repo: Path, plan_dir: Path, payload: Dict[str, Any],
) -> subprocess.CompletedProcess:
    """跑一次真的钩子脚本（独立进程），payload 走 stdin。

    钩子在生产里就是一个独立进程（``settings.json`` 里的一条命令），所以
    这里也用独立进程跑 —— 进程内的调用证明不了「钩子真的能被拉起来」。
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PDT_TREE_GUARD_PHASE": phase,
        "PDT_VP_WRITE_SCOPE": str(plan_dir),
        "PDT_PROJECT_DIR": str(repo),
        "PDT_VP_ROUND": str(ROUND_NO),
        "PDT_VP_ID": TREE_VP_ID,
    }
    return subprocess.run(
        [sys.executable, str(_BACKEND_DIR / "verification_tree_guard.py")],
        cwd=str(_BACKEND_DIR),
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


class _StubSubAgent:
    """子 agent 的替身 —— 它要开 LLM 会话，而这条 lane 里没有模型。

    替身只挡在模型边界上：它交回一条 PASSED，像真的一样。判定层要证明的
    是「有 mutation 记录时这条 PASSED 会被改判」，所以替身交回什么并不影响
    结论 —— 有记录就改判，没有记录就原样通过。
    """

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs

    async def run(self, **kwargs: Any) -> Verdict:
        return Verdict(
            verdict="PASSED",
            reasons=["stub sub-agent: this lane has no model"],
            evidence=["stub evidence"],
            provider="stub-provider",
            chosen_model="stub-model",
            model_complexity="simple",
        )


def _log_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """本轮的验证点日志（JSON lines）里的全部条目。"""
    entries: List[Dict[str, Any]] = []
    logs_dir = plan_dir / "logs"
    if not logs_dir.is_dir():
        return entries
    for path in sorted(logs_dir.glob("verification_*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
    return entries


# ---------------------------------------------------------------------------
# 1) 真 server + 真浏览器
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def acceptance_summary() -> Dict[str, Any]:
    """这一轮验收的结论摘要 —— 任务书「输出示例」里那个形状。

    断言散在三条用例里，操作者要的是一眼能看完的那一行：server 起没起来、
    界面上几条断言过了、回滚有没有发生、报告里有没有「因成本跳过」小节。
    用例各写各的字段，模块结束时合成一行打出来。
    """
    summary: Dict[str, Any] = {
        "server": "down",
        "ui_assertions": 0,
        "mutation_rollback": False,
        "deferred_section": False,
    }
    yield summary
    print(
        "\nVERIFY_GATE_E2E_SUMMARY " + json.dumps(summary, ensure_ascii=False),
    )


def test_end_to_end_ui_and_gates(tmp_path, acceptance_summary):
    """真页面上的控件是用户看得见的那个 —— 点得动、敲得进去、看得见。

    走的是生产里那条路：server 子进程 → uvicorn → 静态页面 → 前端脚本 →
    真的 Chromium。断言落在「用户看见的东西」上：布局可见、点开新建计划后
    出现一个能敲字的输入框、敲进去的字确实在里面、截图落盘且非空。
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    server = _start_server(tmp_path)
    try:
        with _real_home_env() as home:
            executable = _chromium_executable(home)
            with sync_playwright() as driver:
                # 找不到已装的 Chromium 时让 playwright 自己解析 —— 它起不来
                # 就是失败，不许 skip。
                if executable:
                    browser = driver.chromium.launch(executable_path=executable)
                else:
                    browser = driver.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.goto(server.base_url + "/", wait_until="load")

                    page.wait_for_selector("#app-layout", timeout=30000)
                    assert page.is_visible("#app-layout"), "首页没有渲染出主布局"

                    page.click("#btn-new-plan")

                    try:
                        handle = page.wait_for_selector(
                            "textarea, input[type=text]",
                            timeout=INPUT_TIMEOUT_MS,
                        )
                    except PlaywrightTimeoutError:
                        pytest.fail(
                            "no input control appeared within {0}ms after "
                            "clicking #btn-new-plan\n"
                            "--- page content (first 2000 chars) ---\n{1}".format(
                                INPUT_TIMEOUT_MS, page.content()[:2000],
                            )
                        )

                    handle.fill("verification gate e2e")
                    assert handle.input_value() == "verification gate e2e", (
                        "输入框里的字不是敲进去的那句"
                    )

                    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                    shot = SCREENSHOT_DIR / "home.png"
                    page.screenshot(path=str(shot))
                    assert shot.exists(), "截图没落盘：{0}".format(shot)
                    assert shot.stat().st_size > 0, "截图是空文件：{0}".format(shot)

                    # 界面上过掉的三条断言：布局可见、字敲进去了、截图非空。
                    acceptance_summary["server"] = "up"
                    acceptance_summary["ui_assertions"] = 3
                finally:
                    browser.close()
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 2) 真钩子：改过被验证树的 VP 拿不到 PASSED
# ---------------------------------------------------------------------------


def test_tree_guard_rolls_back_in_real_repo(tmp_path, monkeypatch, acceptance_summary):
    """真钩子跑一遍：存证 → 回滚 → 记账 → 改判，四个字段齐全。

    钩子是两个独立进程（pre / post），中间那条命令真的改了仓库；post 阶段
    量到改动，存下 patch、把树复原、把这一轮记进账本；判定层读这份账本，
    把子 agent 交回的 PASSED 改判成 FAILED，并记一条
    ``verifier_mutated_tree`` 事件。
    """
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": MUTATION_COMMAND},
        "tool_use_id": TOOL_USE_ID,
    }

    # --- pre：拍快照 ----------------------------------------------------
    pre = _run_guard("pre", repo, plan_dir, payload)
    assert pre.returncode == 0, pre.stderr
    snapshot = (
        repo / ".pdt" / "verify" / f"round-{ROUND_NO}"
        / f"snapshot-{TOOL_USE_ID}.json"
    )
    assert snapshot.exists(), "pre 阶段没有拍下快照：{0}".format(snapshot)

    # --- 工具调用真的改了树（非空洞：先证明树确实变了）------------------
    mutated = subprocess.run(
        MUTATION_COMMAND,
        cwd=str(repo),
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mutated.returncode == 0, mutated.stderr
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\nx\n"
    assert (repo / "new.txt").exists(), "越权命令没有新建文件"

    # --- post：比对 → 存证 → 回滚 → 记账 --------------------------------
    post = _run_guard("post", repo, plan_dir, payload)
    assert post.returncode == 0, post.stderr

    # 回滚：被跟踪的文件复原，新建的消失。
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\n", (
        "被跟踪的文件没有被回滚"
    )
    assert not (repo / "new.txt").exists(), "新建的文件没有被删掉"

    # 存证：patch 落了盘，而且非空（回滚之后就重建不出来了）。
    evidence_dir = (
        repo / ".pdt" / "evidence" / "mutations" / f"round-{ROUND_NO}"
    )
    patches = sorted(evidence_dir.glob("*.patch"))
    assert len(patches) == 1, patches
    patch = patches[0]
    assert patch.stat().st_size > 0, "patch 是空文件：{0}".format(patch)

    # 记账：账本里有一行，四个字段齐全。
    ledger = (
        plan_dir / ".pdt" / "vp_write_scope" / "mutations"
        / f"round-{ROUND_NO}" / f"{TREE_VP_ID}.jsonl"
    )
    assert ledger.exists(), "账本没有落盘：{0}".format(ledger)
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["created"] == ["new.txt"], row
    assert row["modified"] == ["tracked.txt"], row
    assert row["deleted"] == [], row
    assert row["patch"], row
    assert Path(str(row["patch"])).exists(), row

    # --- 判定层：拿着这份账本改判 ---------------------------------------
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plan_dir = plan_dir
    orchestrator.project_dir = repo
    orchestrator.coding_tool = None
    orchestrator.persistence = VerificationPersistenceManager(plan_dir)
    orchestrator.persistence.start_round(ROUND_NO)
    orchestrator._current_round = ROUND_NO

    monkeypatch.setattr(verification_agent, "VerificationSubAgent", _StubSubAgent)

    verdict = asyncio.run(
        orchestrator._delegate_to_sub_agent(
            {"id": TREE_VP_ID}, "code_review", TREE_VP_ID,
        )
    )

    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.failure_class_source == "deterministic", verdict

    # 事件：``verifier_mutated_tree``，四个字段齐全。
    events = [
        entry
        for entry in _log_events(plan_dir)
        if entry.get("event_type") == "verifier_mutated_tree"
    ]
    assert len(events) == 1, events
    data = events[0].get("data") or {}
    assert data.get("created") == ["new.txt"], data
    assert data.get("modified") == ["tracked.txt"], data
    assert data.get("deleted") == [], data
    assert data.get("patch"), data
    assert Path(str(data.get("patch"))).exists(), data

    acceptance_summary["mutation_rollback"] = True


# ---------------------------------------------------------------------------
# 3) 命令类 e2e 不开 LLM 会话；成本闸把要花钱的验证点延后
# ---------------------------------------------------------------------------


def test_command_e2e_runs_without_llm(tmp_path, monkeypatch, acceptance_summary):
    """命令类 e2e 由退出码判定；要花钱的验证点默认延后并单列成节。

    三段：

    * ``validate_vp`` 对一条声明齐整的命令类 e2e 没有意见；
    * 分派结论是「确定性 runner」而不是 LLM 路径 —— 这条判定不开任何模型
      会话，退出码就是结论；
    * ``cost=llm_quota`` 的验证点在默认开关下是 DEFERRED（**没有**被执行），
      报告里单列「因成本跳过（DEFERRED）」小节，且它不进失败清单。
    """
    repo = _init_repo(tmp_path / "repo")
    (repo / "e2e_entry.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "echo 'verification gate e2e entry'\n"
        "exit 0\n",
        encoding="utf-8",
    )

    vp: Dict[str, Any] = {
        "id": "VP-E2E-001",
        "verification_method": "e2e",
        "e2e_kind": "command",
        "entry_command": "bash e2e_entry.sh",
        "title": "端到端命令冒烟",
        "expected_result": "exit 0",
    }

    # --- schema：这条验证点是可执行的 ------------------------------------
    issues = verification_ci_runner.validate_vp(vp, project_dir=repo)
    assert issues == [], [issue.to_dict() for issue in issues]

    # --- 分派：判定归确定性 runner，不开 LLM 会话 ------------------------
    route, mode = verification_subagent.resolve(vp)
    assert route == "verification_ci_runner", route
    assert mode == verification_subagent.DETERMINISTIC_MODE, mode

    outcome = verification_subagent.run_deterministic(vp, project_dir=repo)
    assert outcome.status == verification_subagent.OUTCOME_PASS, outcome
    assert outcome.route == "verification_ci_runner", outcome
    verdict = outcome.verdict or {}
    assert verdict.get("status") == "PASSED", verdict
    assert verdict.get("evidence", {}).get("exit_code") == 0, verdict

    # 反证：浏览器类的 e2e 框架判不了 —— 它必须落回 LLM 路径。分派表不是
    # 「什么都往确定性上派」。
    browser_route, browser_mode = verification_subagent.resolve(
        {"id": "VP-E2E-BROWSER", "verification_method": "e2e"},
    )
    assert browser_route == verification_subagent.LLM_ROUTE, browser_route
    assert browser_mode == verification_subagent.ESCALATE_PATH_MODE, browser_mode

    # --- 成本闸：默认延后，且**不执行** ---------------------------------
    monkeypatch.delenv(ALLOW_LLM_QUOTA_ENV, raising=False)
    calls: List[Dict[str, Any]] = []

    def _runner(vp_arg: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(vp_arg)
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    cost_vp: Dict[str, Any] = dict(
        vp, id="VP-E2E-002", cost="llm_quota",
    )
    executor = verification_executor.VerificationExecutor(
        verification_plan={"verification_points": [dict(cost_vp)]},
        plan_id="plan-e2e-cost-gate",
        plan_dir=tmp_path / "plan",
        sub_agent_runner=_runner,
    )

    cost_verdict = asyncio.run(executor._run_single_vp(dict(cost_vp)))

    assert cost_verdict["status"] == "DEFERRED", cost_verdict
    assert calls == [], "成本闸拦下的验证点绝不许真去执行"
    assert cost_verdict["evidence"]["cost"] == "llm_quota", cost_verdict

    # --- 报告：「因成本跳过」单列成节，且不进失败清单 --------------------
    report = {
        "final_verdict": "passed",
        "verification_results": [
            {
                "id": "VP-E2E-001",
                "status": "PASSED",
                "reasons": ["exit 0"],
            },
            {
                "id": "VP-E2E-002",
                "status": cost_verdict["status"],
                "cost": "llm_quota",
                "reasons": list(cost_verdict["reasons"]),
                "evidence": dict(cost_verdict["evidence"]),
            },
        ],
        "verification_points": [
            {"id": "VP-E2E-001", "status": "PASSED"},
            {"id": "VP-E2E-002", "status": cost_verdict["status"]},
        ],
    }

    markdown = verification_report_md.render(report)

    section_title = "## " + verification_report_md.COST_DEFERRED_SECTION_TITLE
    assert section_title in markdown, markdown
    section = markdown.split(section_title, 1)[1].split("\n## ", 1)[0]
    deferred_rows = [
        line for line in section.splitlines() if line.startswith("| VP-E2E-002")
    ]
    assert len(deferred_rows) == 1, section
    assert "llm_quota" in deferred_rows[0], deferred_rows

    # DEFERRED 不是失败：它不得出现在失败清单里。
    failure_rows = [
        line
        for line in markdown.splitlines()
        if line.startswith("|") and "失败" in line
    ]
    assert not any("VP-E2E-002" in line for line in failure_rows), failure_rows

    acceptance_summary["deferred_section"] = True