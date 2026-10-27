"""Token 收敛链路的端到端验收（任务 33）。

背景
----
单测与集成各自绿，不代表用户打开页面能跑通「建计划 → 启动执行 → 验证完成
→ 删除有审计 → 归因可查」。这条文件把整条链路**在真实浏览器里**走一遍，
并把六项 KPI 钉成断言。

六项 KPI 与它们的证据
----------------------
=========================  ==========================================
KPI                       证据
=========================  ==========================================
页面可操作                 真实 Chromium 打开真实页面：计划卡片与它的标签
                          可见，全程没有未捕获的 console 异常
删除有审计                 ``plans/<id>/vp_removal_audit.jsonl`` 恰好一
                          条主题外 VP 的移除记录，字段齐全
零 LLM 会话（确定性 VP）   ``plans/<id>/llm_sessions.jsonl`` 里确定性验证
                          点新增 0 行
可归因                     台账的归因行带着非空的 ``vp_id`` 与 ``round``
扫描报告落盘               ``plans/_analysis/vp-scannable-*.json`` 存在，
                          ``deterministic_total > 0``
证据可复核                 四张截图落在 ``/tmp/pdt-e2e/`` 且非空
=========================  ==========================================

真实的东西
----------
* **真的前后端** —— ``backend.server`` 作为一个**子进程**被起起来，前端由
  同一个进程提供；页面上的每一步都是真的点击。
* **真的浏览器** —— 真的 Chromium（Playwright），不是 DOM 模拟。
* **真的 CLI** —— 验证链路的 LLM 会话真的起一次 ``claude`` 子进程（命令与
  生产路径同一条：``--verbose --output-format stream-json
  --permission-mode bypassPermissions``，提示词走 stdin）。台账里那一行是
  ``usage_registry`` 真的写下的那一行。

替身化的部分
------------
只有**指针**，没有替代品：

* ``ANTHROPIC_BASE_URL`` 不给；provider 走 ``inherit``（端点由环境决定）。
* ``PDT_LLM_MODE=scripted`` 只给**服务进程**（访谈与报告阶段用它），
  **不给**测试进程 —— 测试进程里的 LLM 会话必须真的起一次 CLI，否则
  「零 LLM 会话」这条 KPI 就是自己证明自己。
* ``claude`` CLI 本身是一份**假实现**：它按提示词的内容作答，但走的是
  真的进程、真的 stream-json 协议、真的 session id。

为什么删除审计与归因要单独跑一轮
--------------------------------
HTTP 那条路（``verification_agent.Orchestrator._convert_plan_to_executor_schema``）
会把 ``related_prd_criteria`` 丢掉，所以**走 HTTP 的那条链路永远不可能移除
一条主题外验证点** —— 判定内核拿不到「这条验证点声明自己验的是哪条判据」，
于是它无从判死谁。要拿到移除审计，只能让执行器**直接**拿到原始计划字典。

所以这份文件里有两轮验证：

1. **服务进程里那一轮**（HTTP 链路）：建计划 → 执行 → 验证通过，证明页面
   与后端真的能跑通；
2. **测试进程里那一轮**（原始计划字典）：同一条链路、同一个计划目录、同一
   个计划 id，跑第 2 轮 —— 这一轮才带得动主题判定，于是才有删除审计、才有
   带 ``vp_id`` / ``round`` 的归因行。

两轮落在同一个 ``plans/<id>/`` 里，所以台账与审计文件里的坐标彼此对得上。

边界条件
--------
* 服务起不来（端口被占）→ 换一个 ``_free_port()`` 重试**一次**；仍失败就
  失败，并把 ``server.log`` 留在原地。
* 没有浏览器（这台机器装不了 Chromium）→ 整份文件**跳过**：跑一个「看起来
  绿」的空跑比不跑更坏。
* ``finally`` 里关掉服务与它起的子进程；浏览器上下文由驱动脚本自己关。
* 失败时把审计文件、台账与截图的路径写进 pytest 报告。

跑法::

    cd backend && .venv/bin/python3 -m pytest \\
        tests/e2e/test_token_convergence_ui_acceptance.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# 这份文件是端到端验收：它起服务、开浏览器、跑整条链路。
pytestmark = pytest.mark.e2e

#: ``backend/tests/e2e/`` → ``backend/``。
BACKEND_DIR = Path(__file__).resolve().parents[2]
#: 仓库根（服务子进程与扫描 CLI 的工作目录）。
PROJECT_ROOT = BACKEND_DIR.parent
#: 后端自己的虚拟环境 —— 服务子进程用它起，扫描报告也用它跑。
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"

for _entry in (BACKEND_DIR, PROJECT_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 整轮验收的根目录。brief 指名的路径；``PDT_E2E_RUN_ROOT`` 可以改它。
RUN_ROOT = Path(os.environ.get("PDT_E2E_RUN_ROOT", "/tmp/pdt-e2e"))

#: 服务进程的状态库 / 计划目录 / 脚本目录。
STATE_DB = RUN_ROOT / "state.db"
PLANS_DIR = RUN_ROOT / "plans"
SCRIPTS_DIR = RUN_ROOT / "scripts"

#: 截图目录 —— 就是验收根目录本身。
SHOTS_DIR = RUN_ROOT

#: 服务进程的日志。失败时它留在原地 —— 那是排查的第一手材料。
SERVER_LOG = RUN_ROOT / "server.log"

#: 用户在访谈框里输入的那句话。它的 slug 决定计划 id。
REQUIREMENT = (
    "Give the plan list an export button and keep every verification "
    "session attributable"
)

#: 四张截图。名字与 brief 一致。
SCREENSHOTS = (
    "01-plans.png",
    "02-interview.png",
    "03-tasks.png",
    "04-verification.png",
)

#: 确定性验证点：``api_test`` —— 判据可机械执行，**不开 LLM 会话**。
DETERMINISTIC_VP_ID = "VP-101"

#: 走 LLM 路径的验证点：``code_review`` —— 它的会话必须带着归因坐标。
LLM_VP_ID = "VP-202"

#: 主题外的验证点：它声明的判据编号在计划里一条都对不上。
OFF_TOPIC_VP_ID = "VP-900"

#: 计划里唯一的那条判据。
CRITERION_ID = "AC-1"

#: 测试进程里那一轮是**第 2 轮**（服务进程跑完了第 1 轮）。
ROUND_NO = 2
MAX_ROUNDS = 4

#: 请求守卫头 —— 每个 ``/api/*`` 调用都要带。
REQUEST_HEADERS = {"X-PDT-Request": "1", "Content-Type": "application/json"}

#: 驱动的预算。它得比整个套件的单例超时小：服务启动与收尾都排在它两边。
DRIVER_TIMEOUT_SECONDS = 300.0

#: 服务启动的预算。
SERVER_BOOT_TIMEOUT_SECONDS = 90.0


# ---------------------------------------------------------------------------
# 一个假的 ``claude`` CLI
# ---------------------------------------------------------------------------
#
# 替身只替**回答**，不替**进程**：命令、协议、session id、stdin 都是真的。
# 判定走提示词的内容 —— 主题判定批次与验证点裁决各有一个标记。

_FAKE_CLAUDE = r'''#!{interpreter}
"""A fake ``claude`` CLI: it answers, over the real stream-json protocol.

What it does, in order:

1. reads stdin to EOF (the parent writes the prompt and closes stdin);
2. picks an answer by looking at the prompt (topic batch vs verdict);
3. emits the three events the reader loop needs — ``system`` / ``init``
   (carrying the session id it was handed), ``assistant`` (the text) and
   ``result`` (the usage metadata);
4. appends one JSON line to the invocation record, so the test can see
   that a CLI really ran;
5. exits 0.

It answers; it does not stay alive.  The point of this file is the
*session*, not the wait.
"""

import json
import os
import sys
from pathlib import Path

#: The marker the topic-relevance batch carries.
TOPIC_MARKER = "PDT-TOPIC-RELEVANCE-BATCH"

#: The marker a verification-point prompt carries.
VERDICT_MARKER = "Verification point:"

#: The answer the topic batch gets: this verification point is not
#: about the plan's topic any more.
TOPIC_ANSWER = {topic_answer}

#: The answer a verification point gets: it holds.
VERDICT_ANSWER = {verdict_answer}

#: Where the invocation record goes.
RECORD = Path(__file__).resolve().parent.parent / "claude_invocations.jsonl"


def session_id_from(argv):
    """The session id the dispatch asked for, or ``""``."""
    for index, arg in enumerate(argv):
        if arg in ("--session-id", "--resume") and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def main():
    argv = sys.argv[1:]
    session_id = session_id_from(argv)

    # The parent writes the prompt and closes stdin; reading to EOF is
    # as far along as the dispatch gets before the answer comes back.
    try:
        prompt = sys.stdin.read()
    except Exception:
        prompt = ""

    if TOPIC_MARKER in prompt:
        answer = json.dumps(TOPIC_ANSWER, ensure_ascii=False)
        kind = "topic_batch"
    elif VERDICT_MARKER in prompt:
        answer = json.dumps(VERDICT_ANSWER, ensure_ascii=False)
        kind = "verification_point"
    else:
        answer = json.dumps(
            {"verdict": "FAILED", "reasons": ["the fake CLI got a prompt it does not know"],
             "evidence": []},
            ensure_ascii=False,
        )
        kind = "unknown"

    record = {
        "argv": argv,
        "session_id": session_id,
        "kind": kind,
        "pid": os.getpid(),
    }
    try:
        with RECORD.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass

    events = [
        {"type": "system", "subtype": "init", "session_id": session_id},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": answer}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "usage": {"input_tokens": 128, "output_tokens": 32},
            "total_cost_usd": 0.001,
            "num_turns": 1,
            "duration_ms": 42,
            "is_error": False,
            "result": answer,
        },
    ]
    for event in events:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _install_fake_claude(root: Path) -> Path:
    """Write the fake ``claude`` into ``<root>/fake_bin``; return that dir.

    The directory is what goes on ``PATH`` for the test process — the
    verification round that runs **in this process** spawns the CLI
    itself, so the fake has to be on *this* process's ``PATH``.
    """
    bin_dir = root / "fake_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = sys.executable or "/usr/bin/env python3"
    topic_answer = {
        OFF_TOPIC_VP_ID: {
            "relevant": False,
            "reason": (
                "the verification point declares a criterion the plan "
                "never mentions, and its subject is outside the plan's topic"
            ),
        }
    }
    verdict_answer = {
        "verdict": "PASSED",
        "reasons": ["the implementation carries the contract the plan asked for"],
        "evidence": ["the reviewed module names the verification point and its round"],
    }
    script = bin_dir / "claude"
    # A plain replacement, not ``str.format``: the fake's own body is
    # full of JSON braces, and ``format`` would read every one of them
    # as a field name.
    #
    # The answers go in as **Python literals** (``repr``), not as JSON
    # text: ``json.dumps`` spells a boolean ``false``, which is not a
    # Python literal and would blow up the fake on import.
    script.write_text(
        _FAKE_CLAUDE.replace("{interpreter}", interpreter)
        .replace("{topic_answer}", repr(topic_answer))
        .replace("{verdict_answer}", repr(verdict_answer)),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


# ---------------------------------------------------------------------------
# 浏览器驱动
# ---------------------------------------------------------------------------

_DRIVER = r'''
"""Drive the served page through the four acceptance steps.

Run as::

    python drive_acceptance.py <url> <plan_id> <checkout> <shots> <requirement>

Every step is a click on the page, so the frontend's own handlers run.
The driver reports what the page showed; the assertions live in the test
that runs it.
"""

import json
import sys
import time
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


url, plan_id, checkout, shots_dir, requirement = sys.argv[1:6]
shots = Path(shots_dir)
shots.mkdir(parents=True, exist_ok=True)

#: The verification statuses that mean "the round is over".
TERMINAL = ("passed", "failed", "loop_stopped")

report = {
    "page_errors": [],
    "console_messages": [],
    "dialogs": [],
}

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

card = '.plan-card[data-plan="%s"]' % plan_id

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()

    def on_page_error(exc):
        report["page_errors"].append(str(exc))

    def on_console(message):
        report["console_messages"].append(
            {"type": message.type, "text": message.text}
        )

    def on_dialog(dialog):
        report["dialogs"].append(dialog.message)
        dialog.dismiss()

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("dialog", on_dialog)

    # --- 1: the plan list ---------------------------------------------
    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_selector(card, timeout=30000)
    report["plan_card_visible"] = page.is_visible(card)
    report["plan_card_text"] = page.inner_text(card)
    report["plan_id"] = page.get_attribute(card, "data-plan")
    # The card carries a status tag and the phase dots -- the tags are
    # what tells the operator which state the plan is in.
    report["plan_status_visible"] = page.is_visible(card + " .plan-status")
    report["plan_status_text"] = page.inner_text(card + " .plan-status")
    report["plan_step_count"] = page.locator(card + " .plan-step-dot").count()
    page.screenshot(path=str(shots / "01-plans.png"), full_page=True)

    # --- 2: the interview ---------------------------------------------
    page.click("#btn-new-plan")
    page.wait_for_selector("#interview-view:not(.hidden)", timeout=15000)
    page.fill("#interview-input", requirement)
    page.click("#btn-send-interview")
    # The interviewer answers with a system message; the view starts with
    # one of its own, so the second one is the answer.
    page.wait_for_function(
        "() => document.querySelectorAll('#interview-chat .chat-msg.system').length >= 2",
        timeout=60000,
    )
    report["interview_chat"] = page.inner_text("#interview-chat")
    report["interview_labels"] = page.locator(
        "#interview-chat .chat-msg.system .msg-label"
    ).all_inner_texts()
    page.screenshot(path=str(shots / "02-interview.png"), full_page=True)

    # --- 3: into the task view ----------------------------------------
    # The plan list is only re-read when the view comes back to it, so
    # the walk returns home first -- the same way an operator does.
    page.click("#btn-sidebar-home")
    page.wait_for_selector("#plans-view:not(.hidden)", timeout=15000)
    page.wait_for_selector(card, state="visible", timeout=30000)
    report["plan_card_visible_after_interview"] = page.is_visible(card)
    page.click(card)
    page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
    page.wait_for_selector("#btn-start-execution", state="visible", timeout=30000)
    report["tasks_view_visible"] = page.is_visible("#tasks-view")
    report["start_button_visible"] = page.is_visible("#btn-start-execution")
    report["task_panel_text"] = page.inner_text("#tasks-view")
    page.screenshot(path=str(shots / "03-tasks.png"), full_page=True)

    # --- 4: start the execution, wait for the verification ------------
    page.click("#btn-start-execution")
    page.wait_for_selector("#btn-confirm-execute", state="visible", timeout=15000)
    page.fill("#project-dir-input", checkout)
    page.click("#btn-confirm-execute")

    status = None
    deadline = time.time() + 300
    while time.time() < deadline:
        status = page.evaluate(
            """async (planId) => {
                const res = await fetch(`/api/verification/${planId}/status`,
                    {headers: {"X-PDT-Request": "1"}});
                if (!res.ok) { return null; }
                return await res.json();
            }""",
            plan_id,
        )
        if status and status.get("verification_status") in TERMINAL:
            break
        time.sleep(2)
    else:
        raise AssertionError(
            "the verification never reached a terminal status; last payload: %r" % (status,)
        )
    report["verification_status"] = status.get("verification_status")
    report["verification_round"] = status.get("verification_round")
    report["plan_phase"] = status.get("current_phase")

    # The page does not start verification polling on its own after an
    # execution, so the panel is refreshed through the page's own
    # fetcher before the screenshot is taken.
    page.evaluate("() => fetchVerificationStatus()")
    page.wait_for_timeout(1500)
    report["verification_badge"] = page.inner_text("#verification-status-badge")
    page.screenshot(path=str(shots / "04-verification.png"), full_page=True)

    context.close()
    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


class BrowserUnavailable(RuntimeError):
    """The browser pass could not run here: no driver, or no browser.

    Raised only for the two cases where *this machine* cannot drive a
    page -- the driver is not installed, or its browser binaries are
    not.  It is not a failure of the chain under test.
    """


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with (see
    ``PDT_TEST_REAL_HOME``) so that Playwright can find the browser
    binaries it installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        env["HOME"] = real_home
    return env


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    Two candidates, in order: the interpreter running pytest, and the
    interpreter behind a ``playwright`` command on ``PATH`` -- the
    command is a Python entry point, so its shebang names an
    interpreter that has the package installed.  Nothing is hard-coded:
    both are read out of the environment.
    """
    import shlex
    import shutil

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
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def _http(url: str, method: str = "GET", body: Any = None, timeout: float = 30.0) -> int:
    """Call the server; return the status code (0 when it did not answer)."""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=dict(REQUEST_HEADERS), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001 - the server may not be up yet
        return 0


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a JSON-lines file; a missing file is an empty list."""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


# ---------------------------------------------------------------------------
# 这一轮验收的证据
# ---------------------------------------------------------------------------


@dataclass
class Acceptance:
    """一次端到端验收留下的全部证据。"""

    #: 验收根目录。
    root: Path
    #: 计划 id。
    plan_id: str
    #: 服务进程的根地址。
    base_url: str
    #: 驱动脚本的报告。
    driver: Dict[str, Any] = field(default_factory=dict)
    #: 移除审计的行。
    audit_rows: List[Dict[str, Any]] = field(default_factory=list)
    #: 台账的行。
    ledger_rows: List[Dict[str, Any]] = field(default_factory=list)
    #: 扫描报告的路径与内容。
    scan_report: Optional[Path] = None
    scan_payload: Dict[str, Any] = field(default_factory=dict)
    #: 这一轮的执行器给出的结论（验证点 id → status）。
    verdicts: Dict[str, str] = field(default_factory=dict)

    @property
    def plan_dir(self) -> Path:
        return PLANS_DIR / self.plan_id

    @property
    def audit_path(self) -> Path:
        return self.plan_dir / "vp_removal_audit.jsonl"

    @property
    def ledger_path(self) -> Path:
        return self.plan_dir / "llm_sessions.jsonl"

    def screenshots(self) -> Dict[str, Path]:
        return {name: SHOTS_DIR / name for name in SCREENSHOTS}

    def paths_report(self) -> str:
        """The paths a reader needs when the acceptance fails."""
        shots = ", ".join(str(p) for p in self.screenshots().values())
        return (
            f"audit: {self.audit_path}\n"
            f"ledger: {self.ledger_path}\n"
            f"scan: {self.scan_report}\n"
            f"screenshots: {shots}\n"
            f"server log: {SERVER_LOG}"
        )


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


def _write_scripted_scripts() -> None:
    """Write the scripted LLM answers the **server** process reads.

    Two prompts are answered from disk: the interview (matched on the
    requirement text) and the report judgement (matched on the
    verification-plan heading).
    """
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    (SCRIPTS_DIR / "interview.txt").write_text(
        json.dumps(
            {
                "product_form": "software",
                "in_scope": ["an export button on the plan list"],
                "out_of_scope": [],
                "open_questions": [],
                "complete": False,
                "questions": ["Which acceptance criteria does the export button have?"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (SCRIPTS_DIR / "judgment.txt").write_text(
        json.dumps(
            {
                "overall_status": "PASSED",
                "verification_results": [],
                "requirement_deviations": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (SCRIPTS_DIR / "registry.json").write_text(
        json.dumps(
            {
                "prompts": {
                    "interview": {"match": REQUIREMENT, "file": "interview.txt"},
                    "judgment": {"match": "## 验证计划", "file": "judgment.txt"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_checkout(root: Path) -> Path:
    """A real git checkout with a passing test for the task's pre-flight."""
    checkout = root / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "test_acceptance.py").write_text(
        "def test_acceptance():\n    assert True\n", encoding="utf-8"
    )
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
    # The service-freshness pre-flight compares a service process' start
    # time (second resolution) with the newest source mtime in the
    # project dir.  Backdate the sources so the service under test
    # counts as fresh (it was started after the last source change).
    old = time.time() - 3600
    for source in checkout.rglob("*.py"):
        os.utime(source, (old, old))
    return checkout


def _seed_plan_state(plan_id: str) -> Path:
    """Create the plan directory and the rows the start route needs.

    Two things have to be on disk before ``POST /api/execution/<id>/start``
    can do anything:

    * ``plans/<plan_id>/tasks.json`` -- the route answers 404 without it.
      The one task carries a ``test_command`` that passes, so the
      pre-flight completes the task without an LLM call.
    * the state-machine rows -- ``plan_routing`` (the start CASes the
      phase from ``queued``) and ``plan_execution`` (the row the run
      state is written into).

    This runs **before** the server boots: the plan listing answers from
    a two-second cache, so a plan seeded after the first read would miss
    the page load that is supposed to show it.
    """
    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": REQUIREMENT,
                "tasks": [
                    {
                        "id": "T1",
                        "title": "export button",
                        "description": "write the export button",
                        "test_command": "python3 -m pytest -q test_acceptance.py",
                        "files_to_modify": [],
                        "depends_on": [],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(STATE_DB)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "queued")
        ExecutionRepository(conn).insert(plan_id, exec_status="not_started")
        conn.commit()
    finally:
        conn.close()
    return plan_dir


def _write_verification_plan(plan_id: str, base_url: str) -> None:
    """Write the plan the verification phase reads.

    The one verification point is a deterministic ``api_test`` against
    the server's own health endpoint -- the server is up by now, so the
    request can name its real address.
    """
    plan_dir = PLANS_DIR / plan_id
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "verification_points": [
                    {
                        "id": "VP-001",
                        "title": "the plan list api answers",
                        "verification_method": "api_test",
                        "request": {
                            "method": "GET",
                            "url": f"{base_url}/api/health",
                            "headers": {"X-PDT-Request": "1"},
                        },
                        "assertions": [{"status": 200}],
                        "expected_result": "200",
                        "priority": "medium",
                        "related_prd_criteria": [CRITERION_ID],
                    }
                ],
                "criteria": [
                    {"id": CRITERION_ID, "text": "the plan list answers with every plan"}
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _server_env() -> Dict[str, str]:
    """The environment the server subprocess runs under."""
    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(STATE_DB),
            "PDT_PLANS_DIR": str(PLANS_DIR),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": "",  # filled in by the caller
            "PDT_DEBUG_ROUTES": "1",
            "PDT_LLM_MODE": "scripted",
            "PDT_LLM_SCRIPT_DIR": str(SCRIPTS_DIR),
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost",
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
            # The task's pre-flight runs ``python3 -m pytest`` — that has
            # to be the backend's own virtualenv, not whatever python3
            # happens to be first on the PATH.
            "PATH": f"{VENV_PYTHON.parent}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)
    return env


def _boot_server_once(port: int) -> Tuple[str, subprocess.Popen]:
    """Boot one server subprocess; return ``(base_url, proc)``."""
    env = _server_env()
    env["PDT_PORT"] = str(port)
    # Appended, not truncated: a retry after a failed boot keeps the
    # first attempt's log next to the second one's.
    log_handle = SERVER_LOG.open("ab")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + SERVER_BOOT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:
            log_handle.close()
            raise AssertionError(
                f"the server process exited with {proc.returncode}; "
                f"see {SERVER_LOG}"
            )
        if _http(f"{base_url}/api/plans", timeout=5) != 0:
            proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
            return base_url, proc
        time.sleep(0.3)
    proc.kill()
    proc.wait(timeout=10)
    log_handle.close()
    raise AssertionError(
        f"the server did not answer within {SERVER_BOOT_TIMEOUT_SECONDS}s; "
        f"see {SERVER_LOG}"
    )


def _boot_server() -> Tuple[str, subprocess.Popen]:
    """Boot the server; retry once with a fresh port when it does not come up."""
    last_error: Optional[Exception] = None
    for _attempt in range(2):
        try:
            return _boot_server_once(_free_port())
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            time.sleep(1.0)
    raise AssertionError(
        f"the server did not boot after two attempts: {last_error}; "
        f"see {SERVER_LOG}"
    )


def _stop_server(proc: subprocess.Popen) -> None:
    """Stop the server, its subprocesses, and close its log handle.

    The server is started in its own session (``start_new_session=True``),
    so it is its own process-group leader: signalling the group reaches
    the task subprocesses it spawned too.  Terminating only the server
    would leave those running — a background test runner must not leave
    strays behind.
    """
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            _signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                proc.wait(timeout=10)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the server's whole process group; fall back to the process."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (OSError, ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass


# ---------------------------------------------------------------------------
# 测试进程里那一轮验证
# ---------------------------------------------------------------------------


def _raw_plan(base_url: str) -> Dict[str, Any]:
    """The **raw** plan the executor judges — with the criteria intact.

    The HTTP path converts the plan before the executor sees it, and the
    conversion drops ``related_prd_criteria``; a plan without that field
    cannot be judged off-topic, so it can never be removed.  This dict is
    the plan as the plan file records it.
    """
    return {
        "criteria": [
            {
                "id": CRITERION_ID,
                "text": "the plan list answers with every plan and its tags",
            }
        ],
        "vps": [
            {
                "id": DETERMINISTIC_VP_ID,
                "title": "the health endpoint answers",
                "verification_method": "api_test",
                "priority": "medium",
                "related_prd_criteria": [CRITERION_ID],
                "request": {
                    "method": "GET",
                    "url": f"{base_url}/api/health",
                    "headers": {"X-PDT-Request": "1"},
                },
                "assertions": [{"status": 200}],
                "expected_result": "200",
            },
            {
                "id": LLM_VP_ID,
                "title": "the plan list keeps every verification session attributable",
                "verification_method": "code_review",
                "priority": "medium",
                "related_prd_criteria": [CRITERION_ID],
                "expected_result": "every session names its verification point and its round",
            },
            {
                "id": OFF_TOPIC_VP_ID,
                "title": "the release pipeline publishes a release digest",
                "verification_method": "code_review",
                "priority": "medium",
                "related_prd_criteria": ["AC-NOT-IN-PRD"],
                "expected_result": "a release digest is published for every release",
            },
        ],
    }


def _run_in_process_round(plan_id: str, base_url: str, plan_dir: Path) -> Dict[str, str]:
    """Run the second verification round **in this process**.

    The round is the production shape: executor -> dispatch -> sub-agent
    -> ``claude`` CLI -> usage registry.  What is different from the
    HTTP path is only the plan: this one keeps the criteria, so the
    topic judgement can run and the off-topic point can be removed.

    Returns the verdict of every verification point that ran.
    """
    from coding_tool import ClaudeCodingTool

    log_dir = plan_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    project_dir = plan_dir.parent.parent / "checkout"

    # The ledger is written under ``PDT_PLANS_DIR`` -- the same root the
    # service uses, so both rounds land in one file.
    os.environ["PDT_PLANS_DIR"] = str(PLANS_DIR)
    # This process must NOT be in scripted mode: the whole point of the
    # round is that the CLI really runs.
    os.environ.pop("PDT_LLM_MODE", None)
    os.environ.pop("PDT_LLM_SCRIPT_DIR", None)
    # Inherit the providers: the CLI is the real one, and the ambient
    # environment is where its endpoint comes from.
    os.environ["PDT_PROVIDER_MODE"] = "inherit"
    # The tool resolves its provider **when it is constructed**, so the
    # environment above has to be in place first.
    tool = ClaudeCodingTool(cwd=str(project_dir), scene="verification")
    # The CLI is spawned by name, so the fake has to be the ``claude``
    # this process resolves.  Only for the duration of the round: a fake
    # left on the ``PATH`` would answer for every later test in the
    # session.
    fake_bin = RUN_ROOT / "fake_bin"
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{saved_path}"
    try:
        return _run_round(plan_id, base_url, plan_dir, tool, log_dir, project_dir)
    finally:
        os.environ["PATH"] = saved_path


def _run_round(
    plan_id: str,
    base_url: str,
    plan_dir: Path,
    tool: Any,
    log_dir: Path,
    project_dir: Path,
) -> Dict[str, str]:
    """The round itself: executor -> dispatch -> sub-agent -> CLI -> ledger."""
    import asyncio

    import usage_registry
    from verification_executor import VerificationExecutor
    from verification_subagent import VerificationSubAgent

    async def _dispatch(vp: Dict[str, Any]) -> Dict[str, Any]:
        agent = VerificationSubAgent(
            method=vp.get("verification_method", "code_review"),
            max_retries=0,
            model_complexity="simple",
            scene="verification",
            plan_id=plan_id,
            registry=None,
        )
        verdict = await agent.run(
            vp_node=vp,
            coding_tool=tool,
            log_dir=log_dir,
            project_dir=project_dir,
            plan_id=plan_id,
        )
        return {
            "status": verdict.verdict,
            "reasons": list(verdict.reasons),
            "evidence": {"vp_id": vp.get("id")},
        }

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(STATE_DB)
    try:
        repo = VerificationRepository(conn)
        # This run IS the plan's second round: the state store says so
        # before the round starts, exactly as the production loop does.
        repo.init_round(plan_id, ROUND_NO, MAX_ROUNDS)
        conn.commit()

        executor = VerificationExecutor(
            verification_plan=_raw_plan(base_url),
            plan_id=plan_id,
            plan_dir=plan_dir,
            sub_agent_runner=_dispatch,
            verif_repo=repo,
            topic_relevance_llm_batch=_topic_batch(tool),
            round_no=ROUND_NO,
        )
        with usage_registry.plan_usage_context(plan_id=plan_id):
            asyncio.run(executor.run(max_parallel=0))

        verdicts = {
            str(v.get("vp_id")): str(v.get("status"))
            for v in executor.collect_verdicts()
            if isinstance(v, dict)
        }
        repo.complete_round(
            plan_id,
            {"status": "passed", "verdicts": verdicts},
            status="passed",
            publish_closed=False,
        )
        conn.commit()
    finally:
        conn.close()
    return verdicts


def _topic_batch(tool: Any) -> Any:
    """Build the batched topic judgement — one model call for the batch."""

    def _batch(vps, criteria):
        prompt = (
            "PDT-TOPIC-RELEVANCE-BATCH\n"
            "Decide, for each verification point, whether it is still inside "
            "the plan's topic.\n"
            + json.dumps(
                {"vps": list(vps), "criteria": list(criteria)},
                ensure_ascii=False,
            )
        )
        return tool.query_json(
            prompt=prompt,
            system_instruction=(
                "You are a scope judge. Answer with one JSON object keyed by "
                "verification point id and nothing else."
            ),
        )

    return _batch


# ---------------------------------------------------------------------------
# 扫描报告
# ---------------------------------------------------------------------------


def _write_scan_report() -> Tuple[Path, Dict[str, Any]]:
    """Run the real scan CLI; return ``(path, payload)``.

    The report is written where the analysis artefacts live: under the
    plans root's ``_analysis/`` directory.
    """
    analysis_dir = PLANS_DIR / "_analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    out_path = analysis_dir / f"vp-scannable-{stamp}.json"
    result = subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "backend.verification.scan",
            "--plans-dir",
            str(PLANS_DIR),
            "--out",
            str(out_path),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"the scan CLI failed (exit {result.returncode}): {result.stderr[-2000:]}"
        )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    return out_path, payload


# ---------------------------------------------------------------------------
# 这一轮验收（模块作用域：整条链路只走一次）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def acceptance() -> Acceptance:
    """Walk the whole chain once and hand the evidence to the assertions.

    The walk is expensive (a server, a browser, a verification round), so
    it runs once per module and the three tests read the same evidence.
    """
    if not VENV_PYTHON.exists():
        pytest.skip(f"the backend virtualenv is missing: {VENV_PYTHON}")
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "playwright is not importable here (install it, or put a "
            "'playwright' command on PATH); the acceptance walk needs a "
            "real browser"
        )

    # A fresh root every run: the screenshots and the plan directory are
    # the evidence, and stale evidence is not evidence.
    shutil.rmtree(RUN_ROOT, ignore_errors=True)
    for directory in (RUN_ROOT, PLANS_DIR, SCRIPTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    from framework.ids import derive_plan_id

    plan_id = derive_plan_id(REQUIREMENT)
    _write_scripted_scripts()
    fake_bin = _install_fake_claude(RUN_ROOT)
    checkout = _make_checkout(RUN_ROOT)

    # The verification round runs **in this process**, and the CLI is
    # spawned by name — so the fake has to be the ``claude`` this process
    # resolves.  That means touching this process's ``PATH`` (and the
    # plans root, and the LLM mode).  Those are restored in the
    # ``finally``: a module-scoped fixture that leaves a fake ``claude``
    # on the ``PATH`` would silently answer for every later test in the
    # same session.
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "PATH",
            "PDT_PLANS_DIR",
            "PDT_LLM_MODE",
            "PDT_LLM_SCRIPT_DIR",
            "PDT_PROVIDER_MODE",
        )
    }

    base_url = ""
    proc: Optional[subprocess.Popen] = None
    driver_report: Dict[str, Any] = {}
    verdicts: Dict[str, str] = {}
    scan_report: Optional[Path] = None
    scan_payload: Dict[str, Any] = {}
    try:
        # The plan is seeded before the server boots: the listing answers
        # from a two-second cache, so a plan seeded after the first read
        # would miss the page load that is supposed to show it.
        _seed_plan_state(plan_id)

        base_url, proc = _boot_server()
        _write_verification_plan(plan_id, base_url)

        driver_script = RUN_ROOT / "drive_acceptance.py"
        driver_script.write_text(_DRIVER, encoding="utf-8")
        result = subprocess.run(
            [
                interpreter,
                str(driver_script),
                f"{base_url}/",
                plan_id,
                str(checkout),
                str(SHOTS_DIR),
                REQUIREMENT,
            ],
            capture_output=True,
            text=True,
            timeout=DRIVER_TIMEOUT_SECONDS,
            check=False,
            env=_driver_env(),
        )
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}"
            if "Executable doesn't exist" in combined or (
                "playwright install" in combined
            ):
                raise BrowserUnavailable(combined)
            raise AssertionError(
                f"the browser driver failed (exit {result.returncode}); "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        driver_report = json.loads(result.stdout.strip().splitlines()[-1])

        # The second round runs **in this process** — the HTTP path
        # cannot produce a removal, because it drops the criteria.
        verdicts = _run_in_process_round(plan_id, base_url, PLANS_DIR / plan_id)

        scan_report, scan_payload = _write_scan_report()
    except BrowserUnavailable as exc:
        pytest.skip(f"no browser on this machine: {exc}")
    except Exception as exc:  # noqa: BLE001 - the report must carry the paths
        raise AssertionError(
            f"the acceptance walk failed: {type(exc).__name__}: {exc}\n"
            f"server log: {SERVER_LOG}\n"
            f"screenshots: {SHOTS_DIR}\n"
            f"audit: {PLANS_DIR / plan_id / 'vp_removal_audit.jsonl'}\n"
            f"ledger: {PLANS_DIR / plan_id / 'llm_sessions.jsonl'}"
        ) from exc
    finally:
        if proc is not None:
            _stop_server(proc)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return Acceptance(
        root=RUN_ROOT,
        plan_id=plan_id,
        base_url=base_url,
        driver=driver_report,
        audit_rows=_read_jsonl(PLANS_DIR / plan_id / "vp_removal_audit.jsonl"),
        ledger_rows=_read_jsonl(PLANS_DIR / plan_id / "llm_sessions.jsonl"),
        scan_report=scan_report,
        scan_payload=scan_payload,
        verdicts=verdicts,
    )


# ---------------------------------------------------------------------------
# 1. 四步走完，六项 KPI 全绿
# ---------------------------------------------------------------------------


def test_ui_walkthrough_asserts_all_kpis(acceptance: Acceptance) -> None:
    """建计划 → 启动执行 → 验证完成 → 删除有审计 → 归因可查，六项全绿。

    每一条断言对应一项 KPI；失败时把证据的路径一起打出来。
    """
    report = acceptance.paths_report()

    # --- KPI 1: 页面可操作 ------------------------------------------
    # 计划卡片与它的标签在真实页面上看得见。
    assert acceptance.driver.get("plan_card_visible") is True, (
        f"the plan card was not visible on the page\n{report}"
    )
    card_text = acceptance.driver.get("plan_card_text") or ""
    assert acceptance.plan_id in card_text, (
        f"the plan card does not carry the plan id {acceptance.plan_id!r}: "
        f"{card_text!r}\n{report}"
    )
    # 卡片上的标签：状态标签 + 各阶段的圆点。
    assert acceptance.driver.get("plan_status_visible") is True, (
        f"the plan card carries no visible status tag\n{report}"
    )
    assert (acceptance.driver.get("plan_status_text") or "").strip(), (
        f"the plan card's status tag is empty\n{report}"
    )
    assert int(acceptance.driver.get("plan_step_count") or 0) > 0, (
        f"the plan card carries no phase tags\n{report}"
    )
    assert acceptance.driver.get("plan_card_visible_after_interview") is True, (
        f"the plan card was not visible after the interview\n{report}"
    )
    # 任务视图与「开始执行」按钮都在。
    assert acceptance.driver.get("tasks_view_visible") is True, (
        f"the task view did not open\n{report}"
    )
    assert acceptance.driver.get("start_button_visible") is True, (
        f"the start-execution button was not visible\n{report}"
    )
    # 访谈真的问答了一轮。页面上那行标签是 CSS 大写的，所以比小写。
    assert "interviewer" in (acceptance.driver.get("interview_chat") or "").lower(), (
        f"the interview view carried no interviewer message\n{report}"
    )
    assert len(acceptance.driver.get("interview_labels") or []) >= 2, (
        f"the interview view carried no interviewer turn\n{report}"
    )
    # 验证走到了终态。
    assert acceptance.driver.get("verification_status") in (
        "passed",
        "failed",
        "loop_stopped",
    ), (
        f"the verification never reached a terminal status: "
        f"{acceptance.driver.get('verification_status')!r}\n{report}"
    )
    # KPI 1 还包括「没有未捕获的 console 异常」—— 这一段也属于「页面可
    # 操作」：页面自己抛了异常，就谈不上可操作。
    page_errors = acceptance.driver.get("page_errors") or []
    assert page_errors == [], (
        f"the page raised uncaught exception(s): {page_errors}\n{report}"
    )
    console_errors = [
        message
        for message in (acceptance.driver.get("console_messages") or [])
        if message.get("type") == "error"
        and "Failed to load resource" not in str(message.get("text") or "")
    ]
    assert console_errors == [], (
        f"the page logged console error(s): {console_errors}\n{report}"
    )

    # --- KPI 2: 删除有审计 ------------------------------------------
    # 恰好一条 —— 主题外的那条验证点。
    assert len(acceptance.audit_rows) == 1, (
        f"the removal audit must carry exactly one row (the off-topic "
        f"verification point), got {len(acceptance.audit_rows)}\n{report}"
    )
    row = acceptance.audit_rows[0]
    assert row.get("vp_id") == OFF_TOPIC_VP_ID, (
        f"the audited removal is not the off-topic verification point: "
        f"{row}\n{report}"
    )
    for field in (
        "schema_version",
        "ts",
        "plan_id",
        "round",
        "component",
        "vp_id",
        "decision",
        "tier",
        "reason",
        "evidence",
        "vp",
        "verdict",
    ):
        assert field in row, f"the audit row is missing {field!r}: {row}\n{report}"
    assert row.get("plan_id") == acceptance.plan_id, (
        f"the audit row belongs to another plan: {row}\n{report}"
    )
    assert str(row.get("decision")) == "llm_irrelevant", (
        f"the removal was not decided by the model: {row}\n{report}"
    )
    assert str(row.get("tier")) == "llm", (
        f"the removal did not come from the model tier: {row}\n{report}"
    )
    assert row.get("round") == ROUND_NO, (
        f"the audit row carries the wrong round: {row}\n{report}"
    )
    # 被移除的那条验证点没有跑过 —— 它连结论都没有。
    assert OFF_TOPIC_VP_ID not in acceptance.verdicts, (
        f"the off-topic verification point was executed anyway: "
        f"{acceptance.verdicts}\n{report}"
    )

    # --- KPI 3: 确定性验证点零 LLM 会话 -----------------------------
    deterministic_rows = [
        entry
        for entry in acceptance.ledger_rows
        if entry.get("vp_id") == DETERMINISTIC_VP_ID
    ]
    assert deterministic_rows == [], (
        f"the deterministic verification point opened "
        f"{len(deterministic_rows)} model session(s); it must open none\n{report}"
    )
    # 而且它确实跑了 —— 零会话不是「没跑」。
    assert acceptance.verdicts.get(DETERMINISTIC_VP_ID) == "PASSED", (
        f"the deterministic verification point did not pass: "
        f"{acceptance.verdicts}\n{report}"
    )

    # --- KPI 4: 可归因 ----------------------------------------------
    attributed = [entry for entry in acceptance.ledger_rows if entry.get("vp_id")]
    assert attributed, f"the ledger carries no attributed row at all\n{report}"
    llm_rows = [entry for entry in attributed if entry.get("vp_id") == LLM_VP_ID]
    assert llm_rows, (
        f"the ledger carries no row for {LLM_VP_ID}: "
        f"{acceptance.ledger_rows}\n{report}"
    )
    for entry in llm_rows:
        assert entry.get("vp_id") == LLM_VP_ID, entry
        assert entry.get("round") == ROUND_NO, (
            f"the attributed row does not name the round it belongs to: "
            f"{entry}\n{report}"
        )
        assert entry.get("session_id"), (
            f"the attributed row carries no session id: {entry}\n{report}"
        )
        assert entry.get("plan_id") == acceptance.plan_id, (
            f"the attributed row belongs to another plan: {entry}\n{report}"
        )
    # 走了 LLM 的那条验证点也得有结论 —— 台账有行，结论也得有。
    assert acceptance.verdicts.get(LLM_VP_ID) == "PASSED", (
        f"the model-backed verification point did not pass: "
        f"{acceptance.verdicts}\n{report}"
    )

    # --- KPI 5: 扫描报告落盘 ----------------------------------------
    assert acceptance.scan_report is not None and acceptance.scan_report.exists(), (
        f"the scan report was not written\n{report}"
    )
    totals = acceptance.scan_payload.get("totals") or {}
    assert int(totals.get("deterministic_total") or 0) > 0, (
        f"the scan report counts no deterministic verification point: "
        f"{acceptance.scan_payload}\n{report}"
    )

    # --- KPI 6: 证据可复核 ------------------------------------------
    for name, path in acceptance.screenshots().items():
        assert path.exists(), f"the screenshot {name} is missing\n{report}"
        assert path.stat().st_size > 0, (
            f"the screenshot {name} is empty\n{report}"
        )


# ---------------------------------------------------------------------------
# 2. 四张截图都在
# ---------------------------------------------------------------------------


def test_screenshots_archived(acceptance: Acceptance) -> None:
    """四张截图都落盘了，而且不是空文件。

    截图是这条验收**唯一**的像素级证据：没有它们，前一条断言里
    「页面上看得见」就只是一句话。
    """
    report = acceptance.paths_report()
    shots = acceptance.screenshots()
    assert set(shots) == set(SCREENSHOTS), shots
    for name, path in shots.items():
        assert path.exists(), f"the screenshot {name} is missing: {path}\n{report}"
        size = path.stat().st_size
        assert size > 0, f"the screenshot {name} is empty: {path}\n{report}"
        # PNG 的魔数 —— 一个 0 字节的占位文件不是截图。
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", (
            f"the screenshot {name} is not a PNG: {path}\n{report}"
        )


# ---------------------------------------------------------------------------
# 3. 全程没有未捕获的异常
# ---------------------------------------------------------------------------


def test_no_console_errors(acceptance: Acceptance) -> None:
    """整条走下来，页面上没有一个未捕获的异常。

    判据是**未捕获**的异常（``pageerror``）与真正的 console error。
    资源加载失败（favicon 之类）不算：那是网络噪音，不是页面自己的错。
    """
    report = acceptance.paths_report()
    page_errors = acceptance.driver.get("page_errors") or []
    assert page_errors == [], (
        f"the page raised uncaught exception(s): {page_errors}\n{report}"
    )

    console_errors = [
        message
        for message in (acceptance.driver.get("console_messages") or [])
        if message.get("type") == "error"
        and "Failed to load resource" not in str(message.get("text") or "")
    ]
    assert console_errors == [], (
        f"the page logged console error(s): {console_errors}\n{report}"
    )

    # 页面用 alert 报告自己的失败（「启动执行失败：…」）。一次正常的走
    # 不会弹窗；弹了就是页面自己承认失败。
    failure_dialogs = [
        text
        for text in (acceptance.driver.get("dialogs") or [])
        if "失败" in str(text)
    ]
    assert failure_dialogs == [], (
        f"the page reported a failure in a dialog: {failure_dialogs}\n{report}"
    )
