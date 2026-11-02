"""验证分支的端到端冒烟：CLI 全链路。

这一份文件把「创建验证计划 → 审核最小 PRD 与计划书 → 跳过执行直接验证 →
产出报告」这条链**从头到尾**跑一遍，中间没有替身：

* **真实 git** —— 被验证对象是一个真的仓库，``refs/pull/7/head`` 真的指向
  第二个提交；验证跑在真的 worktree 里（``git worktree add``）。
* **真实文件系统** —— 计划书、机器计划、审核状态、报告两份文件都真的落在
  盘上。
* **真实 SQLite** —— 阶段、基线、轮次、裁决都走状态库那几张表。
* **真实 FastAPI** —— 服务是一个真的 uvicorn 进程，CLI 是真的 HTTP 客户端
  （``python -m backend.cli``），不是 ``TestClient``。

唯一被替身化的是**模型**：脚本化 LLM（任务 28 的夹具）把三次提问的答案
钉死在盘上 —— 计划书、机器计划的执行细节、报告的判定。判定之所以能被替身
化而不失真，是因为执行阶段用的是框架自己执行的方法（``api_test``）：结论
是「声明的请求 + 声明的断言」的纯函数，没有自报成分。

冒烟要证明的东西
----------------

1. **链路通**：CLI 的四个动词（``generate`` / ``items`` / ``review`` /
   ``verify``）串起来，报告的两份文件（``verification_report.json`` 与
   ``verification-report.md``）都真的落盘。
2. **产物链对得上**：计划书十章节齐、基线行与冻结的基线逐字相同、审核条目
   全部已解决、机器计划与计划书的验证点集合逐条相等、worktree 的分支名是
   推导出来的那一个、PR head 没有被移动。
3. **不脏别人的仓库**：被验证对象的**主 checkout**（源仓库）跑完还是干净的
   —— 验证跑在 worktree 里，源仓库既不被切分支也不被提交。
4. **可复现**：连着跑三次，产物逐字相同。
5. **失败时说得清**：一次失败会带上计划目录、服务的日志、worktree 列表与
   主 checkout 的 ``git status`` —— 链路一断，要能一眼看出它断在哪一步，
   而不是只剩一句「断言不成立」。

预算与标记
----------

单次跑批 120 秒（:data:`RUN_BUDGET_SECONDS`）；整个文件带 ``e2e`` 标记 ——
CI 的 e2e lane 是**串行**的：这一份测试起真的 git worktree、真的 uvicorn
进程、真的 HTTP 服务，并排放会互相踩（端口、worktree 注册表、状态库）。

三次运行的一致性怎么算
----------------------

「产物哈希一致」指的是**同一份产物在三次运行里逐字相同**。三处内容天生
不该相同，它们被明确排除：

* **时间戳** —— 报告带 ``generated_at``，那是「什么时候验的」，不是「验了
  什么」。排除的键名写在 :data:`VOLATILE_REPORT_KEYS` 里；时间戳出现在
  字符串中间时（轮次日志的文件名）由两个正则替换掉。
* **plan id 路径** —— 三次运行各用一个 plan id，所以 worktree 的路径、
  报告里的 ``project_dir`` 与 ``plan_id`` 字段每次都不一样。任务书里那句
  「plan_id 路径除外」指的就是它：归一化把路径与字段里的 plan id 换成
  占位符。
* **并发度观测** —— ``vp_capacity_observed`` 是执行器在「有几条验证点同时
  在跑」的那一刻量出来的观测值。单条验证点时它可能是 0 也可能是 1，取决于
  调度时序。它是一次运行的**测量**，不是判定。

排除之后比的是**判定本身**：哪些验证点、什么状态、什么结论。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ``backend/tests/e2e/`` → ``backend/`` → 仓库根。
_E2E_DIR = Path(__file__).resolve().parent
_TESTS_DIR = _E2E_DIR.parent
_BACKEND_DIR = _TESTS_DIR.parent
_REPO_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from config_paths import resolve_plans_dir  # noqa: E402
from tests import _readiness  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    CHILD_BACKEND_DIR_ENV,
    PR_NUMBER,
    PR_REF,
    child_bootstrap_dir,
    register_worktree,
)
from verification_plan_doc import REQUIRED_SECTIONS  # noqa: E402
from verification_plan_review import REVIEW_FILENAME  # noqa: E402
from verification_workspace import branch_name  # noqa: E402

#: 本文件的所有测试都是端到端测试。
#:
#: ``e2e`` 这一个标记就是「串行」的契约：CI 的 e2e lane 不带 ``-n``，一个
#: 一个跑。这一份测试起真的 git worktree、真的 uvicorn、真的 HTTP 服务 ——
#: 并排放会互相踩。
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# 预算与常量
# ---------------------------------------------------------------------------

#: 单次跑批的预算（秒）。超时即失败：冒烟测试的价值在于它**当场**知道链路
#: 断了，而不是十分钟后有人想起来看一眼。
#:
#: 预算只有一个出处：``tests/_readiness.py``。
RUN_BUDGET_SECONDS = _readiness.RUN_BUDGET_SECONDS


@pytest.fixture(autouse=True)
def _server_readiness_budget(monkeypatch):
    """让这个文件起的真后端按共享预算等就绪。

    ``spawn_server`` 自带一个 30 秒的就绪上限，写在
    ``tests/fixtures/verification_mode.py`` 里 —— 那个文件这一轮不在能改的
    范围内，而这个 30 秒在机器忙的时候真的不够：后端的启动要读一遍机器，
    本机实测在 10 秒到 33 秒之间。

    所以这里在夹具调用时把上限换成共享预算里的那个数。换的只是「等多久」，
    断言一个字没动。

    夹具函数在自己的模块里按调用时的全局查找拿到 ``spawn_server``，所以
    替换模块上的这个名字就够，不必复制夹具本身。
    """
    from tests.fixtures import verification_mode

    original = verification_mode.spawn_server

    def spawn_with_budget(*args, **kwargs):
        kwargs.setdefault("timeout", _readiness.READY_TIMEOUT_SECONDS)
        return original(*args, **kwargs)

    monkeypatch.setattr(verification_mode, "spawn_server", spawn_with_budget)

#: 等报告落盘的轮询间隔（秒）。
POLL_INTERVAL_SECONDS = 0.5

#: 验证计划书的文件名（验证分支的产物）。
PLAN_DOC_FILENAME = "verification-plan.md"

#: 机器计划的文件名（验证回路读的那一份）。
MACHINE_PLAN_FILENAME = "verification_plan.json"

#: 报告的两份文件：JSON 是数据源，Markdown 是它的派生视图。
REPORT_JSON_FILENAME = "verification_report.json"
REPORT_MD_FILENAME = "verification-report.md"

#: 最小 PRD 的文件名与 PRD 审核状态的文件名。
PRD_FILENAME = "prd.md"
PRD_REVIEW_FILENAME = "review.json"

#: 跑批用的计划 id 模板。三次运行**各用一个 id** —— 这样每一次都是一次完整
#: 的从零开始（生成 → 翻译 → 验证），也让任务书里那句「plan_id 路径除外」
#: 真正有意义：worktree 的路径、报告里的 ``project_dir`` 与 ``plan_id``
#: 字段每次都不一样。
PLAN_ID_TEMPLATE = "e2e-verify-smoke-{n}"

#: 报告里天生每次都不同的字段。它们记的是「什么时候验的 / 这一轮量到了
#: 什么」，不是「验了什么」。
VOLATILE_REPORT_KEYS = frozenset(
    {
        "generated_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "timestamp",
        # 这一轮实测到的并发度：执行器在「有几条验证点同时在跑」的那一刻量
        # 出来的观测值。它是一次运行的测量，不是判定。
        "vp_capacity_observed",
    }
)

#: 时间戳的两种形状：ISO（``2026-10-25T12:44:59``）与紧凑
#: （``20261025_124459``，轮次日志文件名里用的那种）。
_ISO_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?"
)
_COMPACT_TIMESTAMP_RE = re.compile(r"\d{8}_\d{6}")


# ---------------------------------------------------------------------------
# 被验证对象：一个真的 HTTP 服务
# ---------------------------------------------------------------------------


class _ProbeHandler(BaseHTTPRequestHandler):
    """一个只会说「我活着」的真 HTTP 服务。

    ``api_test`` 的判定是「声明的请求 + 声明的断言」的纯函数：请求打到这
    个服务，断言是状态码 200，于是结论可复现 —— 不依赖模型，也不依赖任何
    外部依赖。
    """

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        """回一份 JSON；路径不被关心（只有一个探针）。"""
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """静音：默认实现往 stderr 写，冒烟的输出里不该有它。"""


class _ProbeService:
    """跑在后台线程里的 HTTP 探针。

    端口由内核分配（先绑再放），所以两个并行跑批不会撞端口。服务活在**测试
    进程**里，而验证跑在**服务进程**里 —— 两者都在回环上，请求走得到。
    """

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="pdt-e2e-probe",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        """探针的端口。"""
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """探针的 URL。"""
        return f"http://127.0.0.1:{self.port}/health"

    def stop(self) -> None:
        """停掉探针并等线程退出。"""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 计划书（脚本化 LLM 的答案）
# ---------------------------------------------------------------------------


def _plan_doc(repo_dir: Path, ref: str, pr_number: int, probe_url: str) -> str:
    """一份满足十章节契约的验证计划书。

    写在这里而不是交给模型：计划书是**被审核的对象**，它必须先是一份合法
    的文档，审核才有东西可审。脚本化 LLM 把这一份当作「生成计划书」那次提
    问的答案交出去。

    验证点只有一条，方法是 ``api_test`` —— 框架自己执行的方法（不请子
    agent、不问模型），判定是纯函数。
    """
    baseline_line = f"baseline: repo={repo_dir} ref={ref} pr={pr_number}"
    return f"""## 被验证对象

{baseline_line}

被验证对象是上游仓库 {repo_dir} 的 {ref}。

## 验证范围

- {probe_url}
- 被验证对象自己的检出

## 排除范围

- 与本次改动无关的历史提交与其它分支。

## 验证方法

api_test：向被验证对象声明的服务发一条请求，按声明的断言判定。

## 验证点清单

### VP-001 探针服务返回 200

- 范围：{probe_url}
- 验证方法：api_test
- 通过判据：
  - C1: 探针服务回一个 200 状态码
- 证据要求：
  - E1: 该请求的响应状态码原文
- 预期结果：请求得到 200。

## 预期结果

验证点清单里的每一条都通过。

## 证据要求

每个验证点的响应原文。

## 通过判据

全部验证点通过即本轮通过。

## 环境与依赖

git；一个回环上的 HTTP 服务；不需要网络。

## 风险面

工作区写错仓库；由工作区根目录守卫拦下。
"""


def _translation_answer(probe_url: str) -> str:
    """翻译那次提问的答案：机器计划缺的执行细节。

    身份字段（id / 标题 / 方法 / 判据 / 预期结果）由计划书说了算，模型只补
    执行细节：这里是一个请求与一条断言。
    """
    payload = {
        "verification_points": [
            {
                "id": "VP-001",
                "request": {
                    "method": "GET",
                    "url": probe_url,
                    "headers": {},
                    "body": None,
                    # 这是一次 HTTP 请求的超时，不是「进程多久就绪」的预算：
                    # 探针是测试进程里的桩服务，答得比一秒快。30 秒是产品
                    # 自己的单请求默认值（``verification_api_runner``），照抄
                    # 产品默认值，才不会让这条用例比产品本身更宽松。
                    "timeout_seconds": 30,
                },
                "assertions": [
                    {"name": "C1: 探针服务回一个 200 状态码", "status": 200}
                ],
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def _judgment_answer() -> str:
    """判定那次提问的答案：报告的结论。

    判定与执行结果一致：那条验证点通过，所以整体通过。
    """
    payload = {
        "overall_status": "PASSED",
        "verification_results": [
            {
                "id": "VP-001",
                "status": "PASSED",
                "actual_result": "请求得到 200",
                "evidence": "HTTP 200",
            }
        ],
        "requirement_deviations": [],
        "summary": "验证分支端到端冒烟：一条 api_test 验证点通过。",
    }
    return json.dumps(payload, ensure_ascii=False)


#: 脚本化 LLM 的三份答案与它们的提示词片段。
#:
#: 片段是最长匹配优先：三条互不包含，谁也不会认错别人的答案。
SCRIPTED_PROMPTS = {
    "verification_plan": "你要写一份「验证计划书」",
    "verification_translation": "仓库事实（JSON）：",
    "verification_judgment": "## 验证计划",
}


def _install_scripted_answers(script_dir: Path, answers: Dict[str, str]) -> None:
    """把三份答案写进脚本化 LLM 的目录。

    夹具已经写好了「生成计划书」那一份；这里把三份一起登记进 registry ——
    登记表的形状是「提示词片段 → 答案文件」，三个键各答一次提问。

    答案文件按 key 命名，与夹具那份（``verification_plan.txt``）分开：这一份
    的计划书用 ``api_test``，与夹具默认的 ``automated_test`` 不同。
    """
    registry: Dict[str, Any] = {"prompts": {}}
    for key, answer in answers.items():
        matcher = SCRIPTED_PROMPTS[key]
        filename = f"smoke_{key}.txt"
        (script_dir / filename).write_text(answer, encoding="utf-8")
        registry["prompts"][key] = {"match": matcher, "file": filename}
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _cli_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """跑 CLI 子进程用的环境。

    继承测试进程的环境（hermetic 的 plans 树、状态库、脚本化 LLM、工作区
    根目录都在里面），再加三样：CLI 指向服务的那台机器，仓库根在
    ``sys.path`` 上（``-m backend.cli`` 需要它），以及 **child bootstrap**
    （provider 链的桩是本进程里的一次 monkeypatch，子进程不继承 monkeypatch
    —— 没有它，子进程问 CC Switch 要 provider 链，而 CC Switch 并不在跑）。

    仓库根在 ``sys.path`` 上**不够**：provider 链的桩是按**顶层**模块名
    装的（``import provider_order``），所以后端包目录也得在 ``sys.path``
    上 —— ``PDT_TEST_BACKEND_DIR`` 告诉桩去哪里找它。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(child_bootstrap_dir()), str(_REPO_ROOT)]
        + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    )
    env[CHILD_BACKEND_DIR_ENV] = str(_BACKEND_DIR)
    if extra:
        env.update(extra)
    return env


def _run_cli(
    args: List[str], env: Optional[Dict[str, str]] = None
) -> Tuple[int, str, str]:
    """跑一次 ``python -m backend.cli``；返回 ``(退出码, stdout, stderr)``。

    真的子进程：CLI 那一层（参数解析、URL 拼装、错误码、退出码）都在里面，
    替身化它就等于没测它。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "backend.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=_cli_env(env),
        check=False,
        timeout=RUN_BUDGET_SECONDS,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _cli_json(stdout: str) -> Dict[str, Any]:
    """CLI 的 stdout 是一行 JSON；取出它。"""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"CLI 的 stdout 里没有一行 JSON：{stdout!r}")


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


def _plan_dir(plan_id: str) -> Path:
    """计划在盘上的目录。"""
    return resolve_plans_dir() / plan_id


def _write_minimal_prd(plan_dir: Path, text: str) -> None:
    """写下最小 PRD，并把它唯一的决策点标成「已审过」。

    PRD 审核状态住在 ``review.json`` 里：条目序号 → 状态。验证分支的最小
    PRD 允许整条 skip，所以「已解决」是 accepted 或 skipped 都行 —— 这里用
    accepted。
    """
    (plan_dir / PRD_FILENAME).write_bytes(text.encode("utf-8"))
    review = {
        "items": [
            {"index": 0, "status": "accepted", "note": "冒烟：最小 PRD 已审"}
        ],
    }
    (plan_dir / PRD_REVIEW_FILENAME).write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _record_baseline(plan_id: str, repo_dir: Path, head_sha: str) -> None:
    """把基线与起始阶段记进状态库（阶段 ``prd_review``）。

    走 repository 层而不是手搓文件：读侧找的是冻结那一步放进去的那一份，
    手搓一份就是第二个来源。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_id,
            phase="prd_review",
            completed_phases=[],
            review_rounds={},
            flags={},
            verification={
                "status": "pending",
                "round": 0,
                "max_rounds": 1,
                "stop_reason": None,
                "baseline": {
                    "repo_dir": str(repo_dir),
                    "ref": PR_REF,
                    "commit_sha": head_sha,
                    "pr_number": PR_NUMBER,
                },
            },
            last_updated=None,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑一条 git 命令，返回 stdout。"""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} 在 {repo} 里失败："
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return (completed.stdout or "").strip()


def _git_status(repo: Path) -> str:
    """``git status --porcelain`` 的原文。"""
    return _git(repo, "status", "--porcelain")


# ---------------------------------------------------------------------------
# 失败时留下的证据
# ---------------------------------------------------------------------------


def _plan_dirs_on_disk() -> List[Path]:
    """这一份测试用到的计划目录（已经建出来的那些）。

    三次运行各有自己的 plan id，所以按模板把它们都找回来：失败之后要看
    的是**全部**跑批留下的东西，而不是最后一次。
    """
    root = resolve_plans_dir()
    found: List[Path] = []
    for number in (1, 2, 3):
        candidate = root / PLAN_ID_TEMPLATE.format(n=number)
        if candidate.exists():
            found.append(candidate)
    return found


def _failure_evidence(
    upstream_repo: Any = None,
    running_server: Any = None,
    plan_dirs: Optional[List[Path]] = None,
) -> str:
    """失败时该带上的四样证据。

    任务书的「接口契约」要求这一份测试失败时留下：计划目录、日志、
    worktree 列表、git status。少了它们，一次失败只剩一句「断言不成立」，
    而下一棒该去看哪个目录、哪台服务、哪个仓库，无从得知 —— 这正是端到端
    冒烟最不该浪费的地方：链路一断，最先丢的就是「它到底走到哪一步了」。

    这里读的都是**已经存在**的东西（计划目录、服务的日志、仓库的状态），
    不新增任何副作用：采证据这件事本身不该再动一次被验证对象。
    """
    blocks: List[str] = []

    dirs = plan_dirs if plan_dirs is not None else _plan_dirs_on_disk()
    if dirs:
        described: List[str] = []
        for plan_dir in dirs:
            try:
                names = sorted(p.name for p in plan_dir.iterdir())
            except OSError as exc:
                names = [f"<列不出来: {exc!r}>"]
            described.append(f"  {plan_dir}\n    {names}")
        blocks.append("计划目录:\n" + "\n".join(described))
    else:
        blocks.append("计划目录: <一个都没建出来>")

    if running_server is not None:
        blocks.append(
            f"服务日志（{running_server.log_path}）:\n"
            f"{running_server.log_tail(limit=4000)}"
        )
    else:
        blocks.append("服务日志: <服务没起来>")

    if upstream_repo is not None:
        repo = upstream_repo.repo
        try:
            worktrees = _git(repo, "worktree", "list")
        except AssertionError:
            worktrees = "<git worktree list 失败>"
        blocks.append(f"worktree 列表（{repo}）:\n{worktrees}")

        try:
            status = _git_status(repo)
        except AssertionError:
            status = "<git status 失败>"
        blocks.append(
            f"主 checkout 的 git status --porcelain:\n{status or '<空>'}"
        )
    else:
        blocks.append("worktree 列表: <没有上游仓库>")
        blocks.append("主 checkout 的 git status --porcelain: <没有上游仓库>")

    return "\n\n".join(blocks)


@contextlib.contextmanager
def _evidence_on_failure(
    upstream_repo: Any = None,
    running_server: Any = None,
    plan_dirs: Optional[List[Path]] = None,
):
    """失败时把证据接到错误消息后面。

    **为什么不用钩子。** pytest 9 起，测试模块不再被注册成插件
    （``PluginManager.consider_module`` 只导入 ``pytest_plugins``，不注册
    模块本身），模块里写的 ``pytest_runtest_makereport`` 根本不会被调用。
    而把钩子放进 ``conftest.py`` 会让这份证据溢出它该待的地方：它讲的是
    **这一份测试**的计划目录与被验证对象。

    所以证据在**会失败的那段代码**旁边取，而不是在报告阶段补：链路在哪
    一段断，证据就在哪一段取 —— 断在跑批里就有跑批的日志，断在断言上就
    有那一趟的全部产物。

    异常本身原样抛出去（只把消息加长）：类型与回溯都不变，读到的人拿到的
    仍是原来那个失败，而不是一个被包装过的新失败。
    """
    try:
        yield
    except Exception as exc:
        try:
            evidence = _failure_evidence(
                upstream_repo=upstream_repo,
                running_server=running_server,
                plan_dirs=plan_dirs,
            )
        except Exception as inner:  # pragma: no cover - 证据不该盖掉真正的失败
            evidence = f"<采集失败证据时出错：{inner!r}>"
        original = exc.args[0] if exc.args else ""
        exc.args = (
            f"{original}\n\n=== 失败证据 ===\n{evidence}",
        ) + exc.args[1:]
        raise


# ---------------------------------------------------------------------------
# 产物哈希
# ---------------------------------------------------------------------------


def _normalize_text(text: str, plan_id: str) -> str:
    """把字符串里的 plan id 与时间戳换成占位符。"""
    text = text.replace(plan_id, "<plan_id>")
    text = _ISO_TIMESTAMP_RE.sub("<timestamp>", text)
    text = _COMPACT_TIMESTAMP_RE.sub("<timestamp>", text)
    return text


def _normalize(value: Any, plan_id: str) -> Any:
    """把产物里的路径与时间戳归一化。

    三处归一化，各有各的理由（见模块文档）：

    * **时间戳** —— ``generated_at`` 记的是「什么时候验的」；
    * **plan id 路径** —— 报告里的 ``project_dir`` 是 worktree 的绝对路径，
      路径里有 plan id。任务书里那句「plan_id 路径除外」指的就是它；
    * **并发度观测** —— ``vp_capacity_observed`` 是一次运行的测量。
    """
    if isinstance(value, dict):
        return {
            key: _normalize(item, plan_id)
            for key, item in value.items()
            if key not in VOLATILE_REPORT_KEYS
        }
    if isinstance(value, list):
        return [_normalize(item, plan_id) for item in value]
    if isinstance(value, str):
        return _normalize_text(value, plan_id)
    return value


def _artifact_hashes(plan_dir: Path, plan_id: str) -> Dict[str, str]:
    """五份产物的哈希。

    三份是逐字哈希（计划书、机器计划、审核状态 —— 它们天生确定）；两份是
    归一化之后的哈希（报告 JSON 与报告 Markdown —— 它们带时间戳、路径与
    一次运行的测量）。
    """
    hashes: Dict[str, str] = {}

    for name in (PLAN_DOC_FILENAME, MACHINE_PLAN_FILENAME, REVIEW_FILENAME):
        raw = (plan_dir / name).read_bytes()
        hashes[name] = hashlib.sha256(raw).hexdigest()

    report = json.loads(
        (plan_dir / REPORT_JSON_FILENAME).read_text(encoding="utf-8")
    )
    normalized = _normalize(report, plan_id)
    hashes[REPORT_JSON_FILENAME] = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    markdown = _normalize_text(
        (plan_dir / REPORT_MD_FILENAME).read_text(encoding="utf-8"), plan_id
    )
    hashes[REPORT_MD_FILENAME] = hashlib.sha256(
        markdown.encode("utf-8")
    ).hexdigest()

    return hashes


# ---------------------------------------------------------------------------
# 跑批
# ---------------------------------------------------------------------------


class _Run:
    """一次跑批的结果：产物路径与内容。"""

    def __init__(
        self,
        plan_id: str,
        plan_dir: Path,
        workspace: Path,
        main_checkout_status_before: str,
        upstream_repo: Any = None,
        running_server: Any = None,
    ) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.workspace = workspace
        #: 被验证对象的**主 checkout**在跑这一趟之前的 ``git status
        #: --porcelain``。它必须是空的 —— 跑完之后也必须还是空的。
        self.main_checkout_status_before = main_checkout_status_before
        #: 这一趟的被验证对象与服务。留着它们是为了失败时能交出证据
        #: （仓库的 worktree 列表、服务的日志）—— 只有对象才带着那些。
        self.upstream_repo = upstream_repo
        self.running_server = running_server

    @property
    def evidence(self) -> Dict[str, Any]:
        """失败时交证据用的来源。"""
        return {
            "upstream_repo": self.upstream_repo,
            "running_server": self.running_server,
            "plan_dirs": [self.plan_dir],
        }

    @property
    def report_json(self) -> Path:
        """报告的 JSON。"""
        return self.plan_dir / REPORT_JSON_FILENAME

    @property
    def report_md(self) -> Path:
        """报告的 Markdown。"""
        return self.plan_dir / REPORT_MD_FILENAME

    @property
    def plan_doc(self) -> Path:
        """验证计划书。"""
        return self.plan_dir / PLAN_DOC_FILENAME

    @property
    def machine_plan(self) -> Path:
        """机器计划。"""
        return self.plan_dir / MACHINE_PLAN_FILENAME


def _wait_for_reports(plan_dir: Path, deadline: float) -> None:
    """等两份报告都落盘。

    验证跑在服务进程的线程里，CLI 早就返回了 —— 所以这里等的是**产物**，
    不是某个接口的答复。等不到就是链路断了，超时即失败。
    """
    while time.monotonic() < deadline:
        if (plan_dir / REPORT_JSON_FILENAME).exists() and (
            plan_dir / REPORT_MD_FILENAME
        ).exists():
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    missing = [
        name
        for name in (REPORT_JSON_FILENAME, REPORT_MD_FILENAME)
        if not (plan_dir / name).exists()
    ]
    raise AssertionError(
        f"验证跑批在预算内没有产出报告，缺：{missing}；计划目录：{plan_dir}"
    )


def _run_verification(
    plan_id: str,
    plan_dir: Path,
    upstream: Any,
    probe: _ProbeService,
    scripted_dir: Path,
    server: Any,
    deadline: float,
) -> _Run:
    """跑一遍完整链路，返回产物。

    四步：生成计划书 → 读条目 → 逐条审 → 直接验证。

    ``upstream`` 与 ``server`` 是**对象**而不是路径与端口：失败时要交出的
    证据里有它们（仓库的 worktree 列表、服务的日志），只有对象才带着那些。
    """
    upstream_repo = upstream.repo

    with _evidence_on_failure(
        upstream_repo=upstream,
        running_server=server,
        plan_dirs=[plan_dir],
    ):
        # 主 checkout 在这一趟之前的状态：验证跑在 worktree 里，源仓库既不被
        # 切分支也不被提交，所以这里必须是空的，跑完也必须还是空的。
        main_checkout_status_before = _git_status(upstream_repo)

        _install_scripted_answers(
            scripted_dir,
            {
                "verification_plan": _plan_doc(
                    upstream_repo, PR_REF, PR_NUMBER, probe.url
                ),
                "verification_translation": _translation_answer(probe.url),
                "verification_judgment": _judgment_answer(),
            },
        )

        head_sha = _git(upstream_repo, "rev-parse", PR_REF)
        _record_baseline(plan_id, upstream_repo, head_sha)

        env = {"PDT_API_BASE_URL": f"http://127.0.0.1:{server.port}"}

        # 1. 生成计划书。
        code, out, err = _run_cli(
            ["verification-plan", "generate", plan_id], env
        )
        assert code == 0, f"generate 退出码 {code}；stderr={err!r}；stdout={out!r}"
        assert (plan_dir / PLAN_DOC_FILENAME).exists(), "计划书没有落盘"

        # 2. 读审核条目。
        code, out, err = _run_cli(["verification-plan", "items", plan_id], env)
        assert code == 0, f"items 退出码 {code}；stderr={err!r}"
        items_payload = _cli_json(out)
        items = items_payload.get("items") or []
        assert items, "计划书没有审核条目"

        # 3. 逐条审（全部 accept）。
        doc_sha = items_payload.get("doc_sha256")
        for item in items:
            code, out, err = _run_cli(
                [
                    "verification-plan",
                    "review",
                    plan_id,
                    "--item",
                    str(item["index"]),
                    "--status",
                    "accept",
                    "--note",
                    "冒烟：条目已审",
                    "--doc-sha256",
                    str(doc_sha),
                ],
                env,
            )
            assert code == 0, (
                f"review item {item['index']} 退出码 {code}；stderr={err!r}"
            )

        # 4. 跳过执行，直接验证。
        code, out, err = _run_cli(["verification", "verify", plan_id], env)
        assert code == 0, f"verify 退出码 {code}；stderr={err!r}；stdout={out!r}"

        _wait_for_reports(plan_dir, deadline)

    workspace = Path(os.environ["PDT_VERIFICATION_WORKSPACE_ROOT"]) / plan_id
    return _Run(
        plan_id,
        plan_dir,
        workspace,
        main_checkout_status_before,
        upstream_repo=upstream,
        running_server=server,
    )


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def probe():
    """真 HTTP 探针。"""
    service = _ProbeService()
    try:
        yield service
    finally:
        service.stop()


@pytest.fixture
def smoke_run(
    upstream_repo,
    minimal_prd,
    scripted_llm,
    workspace_root,
    running_server,
    probe,
) -> _Run:
    """跑一遍完整链路。

    计划目录里先放最小 PRD 与它的审核状态 —— 「审核最小 PRD」是链路的第一
    步：计划书生成的那道门就是「PRD 的每一条决策点都已被处置」。
    """
    plan_id = PLAN_ID_TEMPLATE.format(n=1)
    plan_dir = _plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)
    _write_minimal_prd(plan_dir, minimal_prd.text)

    deadline = time.monotonic() + RUN_BUDGET_SECONDS
    run = _run_verification(
        plan_id,
        plan_dir,
        upstream_repo,
        probe,
        Path(scripted_llm.script_dir),
        running_server,
        deadline,
    )

    # worktree 是这一趟跑批起的东西：交给夹具的 teardown 收走。
    register_worktree(upstream_repo.repo, run.workspace)
    return run


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_full_verification_mode_smoke(smoke_run: _Run) -> None:
    """CLI 全链路跑通，两份报告都落盘。

    链路：生成计划书 → 读条目 → 逐条审 → 直接验证 → 出报告。
    「跳过执行」是验证分支本身的语义：这条计划没有开发任务清单，验证直接
    发生在被验证对象的 worktree 里。

    这一条断言的是**产物清单**：计划书、机器计划、worktree、报告两份 ——
    链路的每一环都在盘上留下了它该留下的东西。
    """
    with _evidence_on_failure(**smoke_run.evidence):
        assert smoke_run.plan_doc.exists(), "计划书没有落盘"
        assert smoke_run.machine_plan.exists(), "机器计划没有落盘"
        assert smoke_run.report_json.exists(), "报告的 JSON 没有落盘"
        assert smoke_run.report_md.exists(), "报告的 Markdown 没有落盘"

        report = json.loads(smoke_run.report_json.read_text(encoding="utf-8"))
        assert report.get("overall_status") == "PASSED", (
            f"报告结论不是 PASSED：{report.get('overall_status')!r}"
        )
        # 报告记的工作目录是**被验证对象的 worktree**，不是 plan 的旧目录 ——
        # 验证跑在被验对象身上，这是验证分支与开发分支最要紧的一条差别。
        # 两边都取解析后的路径再比：worktree 落在系统的临时目录里，而临时目录
        # 在 macOS 上是 ``/var`` → ``/private/var`` 的符号链接。
        assert Path(str(report.get("project_dir"))).resolve() == (
            smoke_run.workspace.resolve()
        ), (
            f"报告里的 project_dir 不是 worktree：{report.get('project_dir')!r} "
            f"（worktree：{str(smoke_run.workspace)!r}）"
        )
        markdown = smoke_run.report_md.read_text(encoding="utf-8")
        assert markdown.strip(), "报告的 Markdown 是空的"


def test_artifacts_and_git_invariants(smoke_run: _Run, upstream_repo) -> None:
    """六项产物链断言。

    1. 计划书十章节齐（且顺序对）；
    2. 计划书的基线行与冻结的基线逐字相同；
    3. 审核条目全部已解决；
    4. 机器计划与计划书的验证点集合逐条相等；
    5. worktree 的分支名是推导出来的那一个；
    6. PR head 没有被移动。

    外加一条 git 不变式：被验证对象的**主 checkout**跑这一趟之前是干净的，
    跑完之后还是干净的 —— 验证发生在 worktree 里，源仓库既不被切分支也不
    被提交。
    """
    plan_dir = smoke_run.plan_dir

    with _evidence_on_failure(**smoke_run.evidence):
        # 1. 十章节。
        doc = smoke_run.plan_doc.read_text(encoding="utf-8")
        headings = [
            line[3:].strip()
            for line in doc.splitlines()
            if line.startswith("## ") and not line.startswith("### ")
        ]
        assert headings == list(REQUIRED_SECTIONS), (
            f"计划书的章节不是十章节契约：{headings!r}"
        )

        # 2. 基线行。
        baseline_lines = [
            line.strip()
            for line in doc.splitlines()
            if line.strip().startswith("baseline:")
        ]
        assert len(baseline_lines) == 1, f"基线行不是恰好一行：{baseline_lines!r}"
        assert str(upstream_repo.repo) in baseline_lines[0], (
            f"基线行里的仓库不是被验证对象：{baseline_lines[0]!r}"
        )
        assert PR_REF in baseline_lines[0], (
            f"基线行里的 ref 不对：{baseline_lines[0]!r}"
        )

        # 3. 审核条目全部已解决。
        review = json.loads(
            (plan_dir / REVIEW_FILENAME).read_text(encoding="utf-8")
        )
        statuses = {item["index"]: item["status"] for item in review["items"]}
        assert statuses, "审核状态里没有条目"
        assert all(
            status in ("accepted", "skipped") for status in statuses.values()
        ), f"审核条目没有全部解决：{statuses!r}"

        # 4. 机器计划与计划书的验证点集合逐条相等。
        machine = json.loads(smoke_run.machine_plan.read_text(encoding="utf-8"))
        machine_ids = [vp["id"] for vp in machine["verification_points"]]
        doc_ids = re.findall(r"^###\s+(VP-\d+)", doc, flags=re.MULTILINE)
        assert machine_ids == doc_ids, (
            f"机器计划的验证点集合与计划书对不上：{machine_ids!r} vs {doc_ids!r}"
        )

        # 5. worktree 的分支名。
        expected_branch = branch_name(smoke_run.plan_id)
        actual_branch = _git(
            smoke_run.workspace, "rev-parse", "--abbrev-ref", "HEAD"
        )
        assert actual_branch == expected_branch, (
            f"worktree 的分支名不对：{actual_branch!r} != {expected_branch!r}"
        )

        # 6. PR head 没有被移动。
        assert (
            _git(upstream_repo.repo, "rev-parse", PR_REF)
            == upstream_repo.head_sha()
        ), "PR head 被移动了"

        # 主 checkout：跑这一趟之前干净，跑完之后也干净。
        assert smoke_run.main_checkout_status_before == "", (
            "被验证对象的主 checkout 在跑这一趟之前就不是干净的："
            f"{smoke_run.main_checkout_status_before!r}"
        )
        after = _git_status(upstream_repo.repo)
        assert after == "", f"被验证对象的主 checkout 被这一趟跑脏了：{after!r}"


def test_three_runs_are_deterministic(
    upstream_repo, minimal_prd, scripted_llm, workspace_root, running_server, probe
) -> None:
    """连着跑三次，产物逐字相同。

    三次各用一个 plan id：每一次都是一次**从零开始**的完整链路（生成 → 审
    核 → 翻译 → 验证 → 出报告），不是把同一份产物读三遍。

    「相同」排除三样天生每次都不同的东西：时间戳、含 plan id 的路径、这一
    轮实测到的并发度 —— 排除规则写在 :func:`_normalize` 与
    :data:`VOLATILE_REPORT_KEYS` 里，排除之后比的是**判定本身**。
    """
    with _evidence_on_failure(
        upstream_repo=upstream_repo,
        running_server=running_server,
        plan_dirs=_plan_dirs_on_disk(),
    ):
        hashes: List[Dict[str, str]] = []
        for attempt in range(3):
            plan_id = PLAN_ID_TEMPLATE.format(n=attempt + 1)
            plan_dir = _plan_dir(plan_id)
            plan_dir.mkdir(parents=True, exist_ok=True)
            _write_minimal_prd(plan_dir, minimal_prd.text)

            deadline = time.monotonic() + RUN_BUDGET_SECONDS
            run = _run_verification(
                plan_id,
                plan_dir,
                upstream_repo,
                probe,
                Path(scripted_llm.script_dir),
                running_server,
                deadline,
            )
            register_worktree(upstream_repo.repo, run.workspace)
            hashes.append(_artifact_hashes(plan_dir, plan_id))

        assert hashes[0] == hashes[1] == hashes[2], (
            "三次运行的产物不一致："
            f"{json.dumps(hashes, ensure_ascii=False, indent=2)}"
        )
