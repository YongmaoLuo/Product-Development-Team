"""TDD spec — 确定性 runner 的三态判定（真 HTTP、真子进程）。

Background
----------
PASS / FAIL / ESCALATE 三态是**否定性断言**的载体：

* **PASS** —— 判过了，裁决在 :attr:`DeterministicOutcome.verdict` 里；
* **FAIL** —— 没判过，但证据在（stdout / stderr / 退出码）；
* **ESCALATE** —— **判不了**，带上触发原因与证据，落回 LLM 路径。

这三句话只有在**真的跑过**之后才成立。mock 出来的 runner 返回值只能证明
「代码读了 mock」，证明不了「runner 真的跑得起来」—— 而「跑得起来」正是
这条路径存在的理由：它要替代的正是子 agent 的自报。

所以这个文件里没有 mock：

* ``api_test`` 打的是**真的 uvicorn**（``--host 127.0.0.1 --port 0``，端口
  由内核分配，从 uvicorn 的启动日志里读回来）；
* ``full_ci`` 跑的是**真的短命令**（真的子进程、真的退出码、真的 stderr）；
* ``ESCALATE`` 用的是**真的校验器**（runner 自己的 ``validate_vp`` 拒收）。

TDD contract — 3 cases
-----------------------
1. ``test_pass_produces_verdict_without_session`` —— 真 HTTP 判定通过，产出
   裁决，且 LLM 会话台账的行数增量是 **0**。
2. ``test_fail_carries_process_evidence`` —— 真命令失败，裁决带上退出码与
   stderr（以及 stdout）。
3. ``test_escalate_carries_reason`` —— runner 判不了，升级并带上触发原因与
   证据。

边界条件
--------
* 真进程与真端口必须在 ``finally`` 里关闭并 join：活过测试的 uvicorn 会回答
  下一次运行的健康探针，还占着端口不放。
* 端口必须是内核给的（``--port 0``）：写死的端口在并行分片里会撞车。
* 每次判定都在 :func:`usage_registry.plan_usage_context` 里跑 —— 否则台账的
  零增量可能只是「没有落点」的假零（记录函数在没有 plan 上下文时静默
  不写）。
* 夹具的 ``ci_entry`` 是整仓门禁（``python scripts/ci_local.py``），**不能**
  在测试里跑 —— 所以真命令由测试自己给；夹具提供的是**验证点的形状**。

测试命令::

    pytest backend/tests/integration/verification_chain/test_runner_outcomes.py -v
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import usage_registry  # noqa: E402
from verification.scan import PLAN_FILENAME  # noqa: E402
from verification_subagent import (  # noqa: E402
    ESCALATE_PATH_MODE,
    LLM_ROUTE,
    OUTCOME_ESCALATE,
    OUTCOME_FAIL,
    OUTCOME_PASS,
    resolve,
    run_deterministic,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 夹具与常量
# ---------------------------------------------------------------------------

#: 所有夹具的根目录 —— ``backend/tests/fixtures``。
FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures"

#: 全确定性计划夹具（任务 7）：``api_test`` × 2 + ``full_ci`` × 1 + ``e2e`` × 1。
VP_FIXTURE_001 = FIXTURES_ROOT / "verification" / "vp-fixture-001"

#: 本文件里两个计划各自的 id —— 台账按 plan id 分目录。
PLAN_ID = "vp-fixture-001"

#: LLM 会话台账的文件名（``plans/<plan_id>/llm_sessions.jsonl``）。
SESSION_LEDGER = "llm_sessions.jsonl"

#: 真 uvicorn 起来之后读它的启动日志用的正则 —— 端口是内核给的，uvicorn 自己
#: 把它打出来：``Uvicorn running on http://127.0.0.1:50193``。
_PORT_PATTERN = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")

#: 被服务的小应用。两个端点对应夹具 VP-001（GET）与 VP-002（POST）的形状。
_APP_SOURCE = '''\
"""A tiny app for the real-uvicorn half of this test file."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/api/items")
def list_items():
    return {"id": "item-001", "items": [{"id": "item-001"}]}


@app.post("/api/items", status_code=201)
def create_item():
    return {"items": [{"id": "item-001"}]}
'''


# ---------------------------------------------------------------------------
# 观测：LLM 会话台账的行数
# ---------------------------------------------------------------------------


def count_session_lines(plan_dir: Path) -> int:
    """这个计划的 LLM 会话台账有多少行（台账不存在 = 0 行）。

    数**行**不数字节：一次会话写一行 JSON，行数就是会话数。文件不存在时
    返回 0 —— 「一条都没写」和「写了一条」的区别，正是这个文件要钉住的。
    """
    path = plan_dir / SESSION_LEDGER
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


# ---------------------------------------------------------------------------
# 夹具的验证点
# ---------------------------------------------------------------------------


def load_fixture_points() -> List[Dict]:
    """夹具计划里的全部验证点（深拷贝，改动不污染夹具文件）。"""
    plan = json.loads(
        (VP_FIXTURE_001 / PLAN_FILENAME).read_text(encoding="utf-8")
    )
    return json.loads(json.dumps(plan["verification_points"]))


def load_fixture_point(vp_id: str) -> Dict:
    """夹具计划里的某一条验证点（深拷贝）。"""
    for point in load_fixture_points():
        if point["id"] == vp_id:
            return point
    raise AssertionError(f"the fixture declares no {vp_id}")


# ---------------------------------------------------------------------------
# 真 uvicorn
# ---------------------------------------------------------------------------


def _uvicorn_executable() -> str:
    """真 uvicorn 的可执行文件。

    先找**当前解释器旁边**的那个：venv 里的 uvicorn 与这个 venv 的 fastapi
    是一套的，而 PATH 上的 uvicorn 可能来自另一套环境（那套未必装了
    fastapi，起来就会死在 import 上）。
    """
    beside = Path(sys.executable).parent / "uvicorn"
    if beside.exists():
        return str(beside)
    found = shutil.which("uvicorn")
    if found:
        return found
    raise RuntimeError(
        "uvicorn is not available — the runner-outcomes test needs a real "
        "server to grade an api_test against"
    )


def _await_port(proc: subprocess.Popen, log_path: Path, timeout: float = 30.0) -> int:
    """等 uvicorn 把它自己绑到的端口打出来（``--port 0`` 时只有它知道）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"uvicorn exited with {proc.returncode} before it reported a "
                f"port:\n{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        match = _PORT_PATTERN.search(
            log_path.read_text(encoding="utf-8", errors="replace")
        )
        if match:
            return int(match.group(1))
        time.sleep(0.05)
    raise RuntimeError(
        "uvicorn did not report its port within "
        f"{timeout}s:\n{log_path.read_text(encoding='utf-8', errors='replace')}"
    )


def _await_answering(base_url: str, timeout: float = 30.0) -> None:
    """等服务真的开始回答 —— 「进程活着」不等于「能判定」。"""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{base_url}/api/items", timeout=2
            ) as response:
                if 200 <= int(response.status) < 300:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.05)
    raise RuntimeError(f"{base_url} never answered within {timeout}s ({last_error})")


def _stop_process(proc: subprocess.Popen) -> None:
    """关掉真进程并 join（``wait`` 就是 join）。

    顺序是刻意的：``terminate`` 只是**请求**，``wait`` 才回收；不 join 的话
    这个 uvicorn 会活过测试，继续占着端口回答下一次运行的健康探针。
    """
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@contextmanager
def live_server(work_dir: Path) -> Iterator[str]:
    """起一个真的 uvicorn（``127.0.0.1:0``），产出它的 base URL。

    端口由内核分配 —— 写死的端口在并行分片里会撞车，而撞车的后果是「上一
    个测试的服务器回答了这一次的请求」，判定结果指向另一棵树。

    进程与端口都在 ``finally`` 里关闭并 join。
    """
    app_dir = work_dir / "served_app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "served_items_app.py").write_text(_APP_SOURCE, encoding="utf-8")

    env = dict(os.environ)
    pythonpath = [str(app_dir)]
    inherited = env.get("PYTHONPATH")
    if inherited:
        pythonpath.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    log_path = work_dir / "uvicorn.log"
    log_file = open(log_path, "wb")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        [
            _uvicorn_executable(),
            "served_items_app:app",
            "--host", "127.0.0.1",
            "--port", "0",
            "--log-level", "info",
        ],
        cwd=str(app_dir),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        port = _await_port(proc, log_path)
        base_url = f"http://127.0.0.1:{port}"
        _await_answering(base_url)
        yield base_url
    finally:
        _stop_process(proc)
        log_file.close()


# ---------------------------------------------------------------------------
# 1. PASS —— 真 HTTP 判过，产出裁决，且一次 LLM 会话都不开
# ---------------------------------------------------------------------------


def test_pass_produces_verdict_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_test 打真的服务：判过 → 有裁决；台账一行都不涨。

    「没有会话」不是靠「没有调用 LLM 的迹象」推断的，是靠台账的**行数增量**
    数的：行数没涨，就是没开会话。
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    plan_dir = tmp_path / "plans" / PLAN_ID

    with live_server(tmp_path) as base_url:
        vp = load_fixture_point("VP-001")
        vp["request"] = {
            **vp["request"],
            "url": f"{base_url}/api/items?limit=10",
        }

        before = count_session_lines(plan_dir)
        with usage_registry.plan_usage_context(plan_id=PLAN_ID):
            outcome = run_deterministic(vp)
        after = count_session_lines(plan_dir)

    assert outcome.status == OUTCOME_PASS, outcome.reasons
    assert outcome.is_deterministic
    assert outcome.verdict is not None
    assert outcome.verdict["status"] == "PASSED"
    assert outcome.reasons, "a PASS must say why it passed"

    # 证据是**真的响应**：状态码来自真的 HTTP 往返。
    assert outcome.evidence["status_code"] == 200
    assert outcome.evidence["assertions_failed"] == []

    assert after - before == 0, (
        "the deterministic path opened an LLM session — the ledger grew by "
        f"{after - before} line(s)"
    )


# ---------------------------------------------------------------------------
# 2. FAIL —— 真命令失败，裁决带着进程的证据
# ---------------------------------------------------------------------------

#: 一条真的短命令：往 stdout 写一行、往 stderr 写一行、退出码 3。
_FAILING_GATE = (
    "import sys; "
    "sys.stdout.write('gate stdout line\\n'); "
    "sys.stderr.write('gate failed on purpose\\n'); "
    "sys.exit(3)"
)


def test_fail_carries_process_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真子进程失败：裁决必须带退出码 / stderr / stdout。

    「没判过」只有一个 FAILED 是不够的 —— 没人能复现它。证据必须是**进程
    自己的**：退出码是 ``sys.exit(3)`` 那个 3，stderr 是子进程写的那行字。
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))

    vp = {
        "id": "VP-FAIL-001",
        "verification_method": "full_ci",
        "ci_entry": (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(_FAILING_GATE)}"
        ),
        "ci_timeout_seconds": 60,
    }

    with usage_registry.plan_usage_context(plan_id=PLAN_ID):
        outcome = run_deterministic(vp, project_dir=tmp_path)

    assert outcome.status == OUTCOME_FAIL, outcome.reasons
    assert outcome.verdict is not None
    assert outcome.verdict["status"] == "FAILED"

    assert outcome.exit_code == 3, (
        "the verdict lost the process exit code — a FAILED nobody can "
        "reproduce is a self-report"
    )
    assert "gate failed on purpose" in outcome.stderr
    assert "gate stdout line" in outcome.stdout
    assert outcome.evidence["exit_code"] == 3
    assert outcome.evidence["timed_out"] is False


# ---------------------------------------------------------------------------
# 3. ESCALATE —— 判不了，带触发原因与证据
# ---------------------------------------------------------------------------


def test_escalate_carries_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runner 判不了：升级，带原因与证据，且**不带裁决**。

    服务就在那儿（真的 uvicorn），判不了的原因不是环境 —— 是这条验证点本身
    的断言没有判据：``json_path`` 点名了要看什么，却没说期望是什么。没有
    判据的断言恒真，所以它必须被拒收，而不是被「判过」。
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))

    with live_server(tmp_path) as base_url:
        vp = load_fixture_point("VP-001")
        vp["request"] = {
            **vp["request"],
            "url": f"{base_url}/api/items?limit=10",
        }
        vp["assertions"] = [
            {"name": "没有比较器的断言", "json_path": "$.id"},
        ]

        # 分派层先判：这条验证点判不了，落回 LLM 路径。
        assert resolve(vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)

        outcome = run_deterministic(vp)

    assert outcome.status == OUTCOME_ESCALATE
    assert outcome.needs_llm
    assert outcome.route == LLM_ROUTE
    assert outcome.reasons, "an escalation must name what triggered it"
    assert outcome.evidence.get("issues"), (
        "an escalation must carry the evidence that triggered it"
    )
    assert outcome.verdict is None, (
        "an escalation is not a verdict — it asks for the LLM path"
    )
    with pytest.raises(ValueError):
        outcome.to_verdict_dict()