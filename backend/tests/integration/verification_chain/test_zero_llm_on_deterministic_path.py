"""TDD spec — 确定性路径上的 LLM 会话数（台账行数增量）。

Background
----------
A4 的核心收益是「确定性路径零 LLM 会话」。这是否定性断言 —— **默认写法
根本不检查**：没有人在写「这条验证点走确定性路径」的时候，顺手去数一下会
话台账多了一行还是零行。于是「确定性路径」慢慢变成了一个**约定**，而约定
在没人看的时候会自己漂移。

所以这里把收益变成**可观测的断言**：

* 观测手段 = ``plans/<plan_id>/llm_sessions.jsonl`` 的**行数**。一次 LLM
  会话写一行 JSON，所以行数就是会话数。
* 确定性路径上跑一遍 → 行数增量必须是 **0**。
* 对照组（一条 ``code_review`` 验证点，走 LLM 路径）→ 行数增量必须**恰好
  是 1**。

对照组是这套断言的**一半**：一个恒为零的计数器和一个真的没开会话的计数器，
在断言里长得一模一样。只有当同一个计数器在对照组上数出 1 时，「确定性路径
是 0」才是一句有内容的断言。

TDD contract — 2 cases
-----------------------
1. ``test_deterministic_path_over_the_fixture_writes_no_session`` —— 夹具
   计划（任务 7 的 ``vp-fixture-001``）逐条走确定性路径，台账行数增量为 0。
2. ``test_control_group_session_increment`` —— 对照组：一条 ``code_review``
   验证点走 LLM 路径，台账行数增量**恰好**是 1。

边界条件
--------
* 对照组必须走**生产路径**：真的 ``VerificationSubAgent``、真的
  ``ClaudeCodingTool``、真的记录函数；只有**出网的那一层**被换掉。换成假
  的工具，那一行就是测试自己写的，「对照组 +1」就只证明了「测试会写
  文件」。
* 两次运行用**同一个** plans 目录（同一个 ``PDT_PLANS_DIR``）、**同一个**
  计数器 —— 否则「0」和「1」不是同一把尺子量的。
* 夹具的 ``ci_entry`` 是整仓门禁（``python scripts/ci_local.py``），**不能**
  在测试里跑 —— 换掉的只有执行目标（真服务 / 真短命令），判据与方法原样
  保留。

测试命令::

    pytest backend/tests/integration/verification_chain/test_zero_llm_on_deterministic_path.py -v
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlsplit

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
    OUTCOME_PASS,
    resolve,
    run_deterministic,
)

# 真 uvicorn、真短命令、台账计数器 —— 与三态那一套共用同一份工具：观测的
# 尺子必须只有一把。
from .test_runner_outcomes import (  # noqa: E402
    SESSION_LEDGER,
    count_session_lines,
    live_server,
    load_fixture_point,
    load_fixture_points,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 全确定性计划夹具（任务 7）—— ``api_test`` × 2 + ``full_ci`` × 1 + ``e2e`` × 1。
VP_FIXTURE_001 = _BACKEND_DIR / "tests" / "fixtures" / "verification" / "vp-fixture-001"

#: 混合计划夹具 —— 里面有一条 ``code_review`` 验证点（VP-901），当对照组用。
VP_FIXTURE_002 = _BACKEND_DIR / "tests" / "fixtures" / "verification" / "vp-fixture-002"

#: 确定性路径那个计划的 id。
PLAN_ID = "vp-fixture-001"

#: 对照组那个计划的 id（就用混合夹具自己的 id）。
CONTROL_PLAN_ID = "vp-fixture-002"

#: 对照组用的验证点 —— 混合夹具里唯一一条 ``code_review``。
CONTROL_VP_ID = "VP-901"

#: 一条真的短命令：退出码 0。它替掉夹具里的整仓门禁入口。
_PASSING_GATE = "print('gate ok')"


# ---------------------------------------------------------------------------
# 把夹具的验证点换成「在这个测试里真能跑」的形状
# ---------------------------------------------------------------------------


def _executable_point(point: Dict, base_url: str) -> Dict:
    """把夹具验证点的**执行目标**换成本测试自己的真服务 / 真短命令。

    夹具给的是**形状**（哪条判据、用什么方法），不是可执行的地址：它的
    api 指向 ``127.0.0.1:8080``（没人在听），它的 ``ci_entry`` 是整仓门禁
    （``python scripts/ci_local.py`` —— 不能跑在测试里）。换掉的只有地址，
    **判据与方法原样保留**。
    """
    method = point.get("verification_method")
    if method == "api_test":
        request = dict(point.get("request") or {})
        path = urlsplit(str(request.get("url") or "")).path or "/"
        request["url"] = f"{base_url}{path}"
        return {**point, "request": request}
    if method == "full_ci":
        return {
            **point,
            "ci_entry": f"{shlex.quote(sys.executable)} -c {shlex.quote(_PASSING_GATE)}",
            "ci_timeout_seconds": 60,
        }
    # 其它方法（e2e）按夹具的**声明**原样跑 —— 声明本身就是断言的一部分。
    return dict(point)


def _load_control_point() -> Dict:
    """混合夹具里的对照组验证点（那条 ``code_review``）。"""
    plan = json.loads(
        (VP_FIXTURE_002 / PLAN_FILENAME).read_text(encoding="utf-8")
    )
    for point in plan["verification_points"]:
        if point["id"] == CONTROL_VP_ID:
            return json.loads(json.dumps(point))
    raise AssertionError(f"the mixed fixture declares no {CONTROL_VP_ID}")


# ---------------------------------------------------------------------------
# 1. 确定性路径 —— 台账行数增量为 0
# ---------------------------------------------------------------------------


def test_deterministic_path_over_the_fixture_writes_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """夹具计划逐条走确定性路径：一次 LLM 会话都不开。

    断言的是**行数增量**，不是「没看到调用 LLM 的迹象」：台账多一行，就是
    多开了一次会话。
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    plan_dir = tmp_path / "plans" / PLAN_ID

    outcomes: List = []
    with live_server(tmp_path) as base_url:
        points = load_fixture_points()
        before = count_session_lines(plan_dir)
        with usage_registry.plan_usage_context(plan_id=PLAN_ID):
            for point in points:
                outcomes.append(
                    run_deterministic(
                        _executable_point(point, base_url),
                        project_dir=tmp_path,
                    )
                )
        after = count_session_lines(plan_dir)

    assert after - before == 0, (
        "the deterministic path opened an LLM session — the ledger grew by "
        f"{after - before} line(s)"
    )

    # 不是空转：至少有一条真的判过（真 HTTP 往返 / 真子进程）。
    statuses = [outcome.status for outcome in outcomes]
    assert OUTCOME_PASS in statuses, (
        f"no fixture point was graded — the zero above would be vacuous: "
        f"{statuses}"
    )

    # 逐条的状态钉在这里（含夹具里那条 e2e）。e2e 按**声明**判不了：
    # 分派表把 e2e 指向 CI runner，而这条验证点声明的是 ``target_url``、
    # 没有 ``ci_entry`` —— 于是它升级，而不是被「判过」。
    assert statuses == [
        OUTCOME_PASS,     # VP-001 api_test  GET  → 真服务
        OUTCOME_PASS,     # VP-002 api_test  POST → 真服务
        OUTCOME_PASS,     # VP-003 full_ci        → 真短命令
        OUTCOME_ESCALATE,  # VP-004 e2e          → 按声明判不了
    ], f"the fixture's points changed verdict: {statuses}"


# ---------------------------------------------------------------------------
# 2. 对照组 —— 同一个计数器在 LLM 路径上数出 1
# ---------------------------------------------------------------------------


def test_control_group_session_increment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照组：一条 ``code_review`` 验证点 → 台账**恰好** +1 行。

    没有这一条，「确定性路径是 0」就是一句无法证伪的话：恒零的计数器也
    是零。对照组用**同一把尺子**（同一个 plans 目录、同一个行数计数器）
    在 LLM 路径上数出 1，于是「0」才有内容。

    对照组走的是生产路径：真的 ``VerificationSubAgent``、真的
    ``ClaudeCodingTool``、真的 ``usage_registry.record_llm_call``。只有
    **出网的那一层**被换成了定好的裁决 —— 记录会话的那一行是生产代码写
    的，不是测试写的。
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    plan_dir = tmp_path / "plans" / CONTROL_PLAN_ID

    control_vp = _load_control_point()
    assert control_vp["verification_method"] == "code_review"

    # 分派：code_review 不在确定性分派表里 → 落回 LLM 路径。
    assert resolve(control_vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)

    from coding_tool import ClaudeCodingTool
    from verification_subagent import VerificationSubAgent

    def _fake_interactive(self, *args, **kwargs):
        """出网那一层的替身：返回一句定好的裁决，别的一概不做。"""
        return (
            json.dumps(
                {
                    "verdict": "PASSED",
                    "reasons": ["control group"],
                    "evidence": [],
                }
            ),
            "control-group-session",
            {"usage": {"input_tokens": 1}, "total_cost_usd": 0.0},
        )

    monkeypatch.setattr(
        ClaudeCodingTool, "_run_claude_interactive", _fake_interactive
    )
    tool = ClaudeCodingTool(scene="verification")

    before = count_session_lines(plan_dir)
    with usage_registry.plan_usage_context(plan_id=CONTROL_PLAN_ID):
        verdict = asyncio.run(
            VerificationSubAgent(method="code_review", max_retries=0).run(
                vp_node=control_vp,
                coding_tool=tool,
                log_dir=tmp_path / "logs",
                plan_id=CONTROL_PLAN_ID,
            )
        )
    after = count_session_lines(plan_dir)

    assert verdict.verdict == "PASSED", verdict.reasons

    assert after - before == 1, (
        "the control group must move the very same counter the "
        f"deterministic path claims to leave at zero — it moved by "
        f"{after - before} line(s) instead of exactly 1"
    )

    # 观测手段确实是活的：那一行真的在磁盘上，而且是一次会话。
    ledger = plan_dir / SESSION_LEDGER
    assert ledger.exists(), "the control group wrote no ledger at all"
    entries = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["plan_id"] == CONTROL_PLAN_ID
    assert entries[0]["session_id"] == "control-group-session"
    assert entries[0]["ok"] is True