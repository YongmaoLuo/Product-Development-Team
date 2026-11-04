"""2026-11-04（缺陷 B 第 4 条）：成本闸 —— ``cost=llm_quota`` 的验证点默认不执行。

问题
----
有些验证点跑起来会让**被验证的产品**真去调 provider（真的花钱）。计划
以前没有表达这件事的地方，于是「跑一次要花钱的验证」和「跑一次不花钱的
验证」在计划里长得一模一样 —— 门禁只能事后从日志里看出钱花掉了。

契约
----
验证点自己声明成本（VP 字段 ``cost``）：

  * ``cost`` 缺省或为 ``"none"`` → 照常执行；
  * ``cost == "llm_quota"`` → **默认不执行**，产出一条 ``DEFERRED`` 裁决；
  * 未知取值 → **按 ``llm_quota`` 处理**（保守：看不懂的成本声明不许被
    当成免费）；
  * 环境变量 ``PDT_VERIFY_ALLOW_LLM_QUOTA=1``（或 ``true`` / ``yes``）→
    照常执行（操作者明确说「这次可以花」）。

为什么是 ``DEFERRED`` 而不是 ``SKIPPED`` / ``FAILED``
------------------------------------------------------
  * ``FAILED`` = 「跑了，没过」→ 会被当成缺陷、生成修复任务。成本闸拦下
    的验证点**没有跑过**，报成失败就是凭空制造一个修复任务；
  * ``SKIPPED`` = 终态成功（进 ``completed_set``），下一轮不会再跑它 ——
    而成本闸的意思正是「以后还要跑」；
  * ``DEFERRED`` = 「这一轮没跑」：不进任何终态桶，不写 FAILED，
    ``failure_class`` 的取值域一个字节都不动。

四条 TDD 规格：

  1. ``test_llm_quota_vp_deferred_by_default`` —— 默认 → DEFERRED；
  2. ``test_switch_allows_execution`` —— 开关打开 → runner 被调用；
  3. ``test_cost_none_runs_normally`` —— ``cost=none`` → 照常执行；
  4. ``test_unknown_cost_is_conservative`` —— 未知取值 → DEFERRED。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock

import pytest

import verification_cancel
from verification_executor import VerificationExecutor


#: 成本闸的开关（操作者显式放行）。
_ALLOW_ENV = "PDT_VERIFY_ALLOW_LLM_QUOTA"

#: ``cost=llm_quota`` 被拦下时写进 reasons 的那句话（逐字节固定 —— 报告
#: 与仪表盘按这句话识别「因成本跳过」）。
_DEFERRED_REASON = (
    "cost: llm_quota —— 默认跳过（PDT_VERIFY_ALLOW_LLM_QUOTA=1 可执行）"
)


@pytest.fixture(autouse=True)
def _clean_switch_and_cancel_flag(monkeypatch):
    """每个用例都从「开关关着、没人喊停」开始。

    开关是进程级的环境变量，取消标记是进程级的全局集合 —— 两者都必须
    复位，否则一个用例的设置会漏到下一个用例里（这类污染在并行跑的时候
    表现为「偶发失败」，最难查）。
    """
    monkeypatch.delenv(_ALLOW_ENV, raising=False)
    verification_cancel._reset_for_tests()
    yield
    verification_cancel._reset_for_tests()


def _executor(plan_dir: Path, runner: Any = None) -> VerificationExecutor:
    """一个只关心「runner 有没有被调用」的执行器。"""
    return VerificationExecutor(
        verification_plan={"verification_points": [dict(_VP)]},
        plan_id="plan-cost-gate",
        plan_dir=plan_dir,
        sub_agent_runner=runner
        or Mock(
            return_value={"status": "PASSED", "reasons": ["ok"], "evidence": {}}
        ),
    )


_VP: Dict[str, Any] = {
    "id": "VP-003",
    "verification_method": "e2e",
    "e2e_kind": "command",
    "title": "端到端冒烟",
    "expected_result": "exit 0",
}


# ---------------------------------------------------------------------------
# 1) 默认：cost=llm_quota → DEFERRED，runner 一次都不许被调用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_quota_vp_deferred_by_default(tmp_path):
    """默认（开关关着）时，一条 ``cost=llm_quota`` 的验证点不执行。

    裁决是 ``DEFERRED``，不是 ``FAILED``：这条验证点**没跑过**，报成失败
    会凭空制造一个修复任务。理由与证据把「为什么没跑」写清楚。
    """
    runner = Mock(
        return_value={"status": "PASSED", "reasons": ["ok"], "evidence": {}}
    )
    ex = _executor(tmp_path, runner)

    verdict = await ex._run_single_vp(dict(_VP, cost="llm_quota"))

    assert verdict["status"] == "DEFERRED", verdict
    assert verdict["reasons"] == [_DEFERRED_REASON], verdict
    assert verdict["evidence"] == {"cost": "llm_quota", "gate": "llm_quota"}, verdict
    runner.assert_not_called(), (
        "成本闸拦下的验证点绝不许真去调 provider"
    )


# ---------------------------------------------------------------------------
# 2) 开关打开 → 照常执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_allows_execution(tmp_path, monkeypatch):
    """``PDT_VERIFY_ALLOW_LLM_QUOTA=1`` → 验证点照常执行。

    操作者明确说「这次可以花」时，成本闸让路 —— runner 必须被调用。
    """
    monkeypatch.setenv(_ALLOW_ENV, "1")
    runner = Mock(
        return_value={"status": "PASSED", "reasons": ["ok"], "evidence": {}}
    )
    ex = _executor(tmp_path, runner)

    verdict = await ex._run_single_vp(dict(_VP, cost="llm_quota"))

    assert verdict["status"] == "PASSED", verdict
    runner.assert_called_once(), "开关打开后 runner 必须被调用"


@pytest.mark.asyncio
async def test_switch_accepts_true_and_yes(tmp_path, monkeypatch):
    """开关的取值是 ``1`` / ``true`` / ``yes``（大小写不敏感）。"""
    for raw in ("1", "true", "TRUE", "yes", "Yes"):
        monkeypatch.setenv(_ALLOW_ENV, raw)
        runner = Mock(
            return_value={"status": "PASSED", "reasons": [], "evidence": {}}
        )
        ex = _executor(tmp_path, runner)

        verdict = await ex._run_single_vp(dict(_VP, cost="llm_quota"))

        assert verdict["status"] == "PASSED", (raw, verdict)
        runner.assert_called_once(), raw


# ---------------------------------------------------------------------------
# 3) cost=none / 缺省 → 照常执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_none_runs_normally(tmp_path):
    """``cost="none"`` → 照常执行（这条验证点不花钱）。"""
    runner = Mock(
        return_value={"status": "PASSED", "reasons": ["ok"], "evidence": {}}
    )
    ex = _executor(tmp_path, runner)

    verdict = await ex._run_single_vp(dict(_VP, cost="none"))

    assert verdict["status"] == "PASSED", verdict
    runner.assert_called_once()


@pytest.mark.asyncio
async def test_missing_cost_runs_normally(tmp_path):
    """``cost`` 缺省 → 照常执行。

    成本声明是**新增**字段：老计划里没有它，不能因为多了个字段就让
    全世界都跑不动。
    """
    runner = Mock(
        return_value={"status": "PASSED", "reasons": ["ok"], "evidence": {}}
    )
    ex = _executor(tmp_path, runner)

    verdict = await ex._run_single_vp(dict(_VP))

    assert verdict["status"] == "PASSED", verdict
    runner.assert_called_once()


# ---------------------------------------------------------------------------
# 4) 未知取值 → 保守（按 llm_quota 处理）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_cost_is_conservative(tmp_path):
    """看不懂的成本取值 → 按 ``llm_quota`` 处理（延后，不执行）。

    保守的方向只有一个：宁可少跑（延后到下一轮），不可多跑（真去调了
    provider 才回来发现这个成本声明没看懂）。
    """
    runner = Mock(
        return_value={"status": "PASSED", "reasons": ["ok"], "evidence": {}}
    )
    ex = _executor(tmp_path, runner)

    verdict = await ex._run_single_vp(dict(_VP, cost="golden_ticket"))

    assert verdict["status"] == "DEFERRED", verdict
    assert verdict["evidence"]["gate"] == "llm_quota", verdict
    runner.assert_not_called()


# ---------------------------------------------------------------------------
# 5) DEFERRED 不进终态桶
# ---------------------------------------------------------------------------


def test_deferred_does_not_enter_a_terminal_bucket(tmp_path):
    """一条 ``DEFERRED`` 裁决不进任何终态桶。

    进了 ``completed`` 桶它就永远不会再跑；进了 ``failed`` 桶它就变成
    一个待修复的缺陷。两个都不是 —— 它只是「这一轮没跑」。
    """
    ex = _executor(tmp_path)

    ex.record_verdict(
        "VP-003",
        {
            "status": "DEFERRED",
            "reasons": [_DEFERRED_REASON],
            "evidence": {"cost": "llm_quota", "gate": "llm_quota"},
        },
    )

    assert "VP-003" in ex._deferred_vps
    assert "VP-003" not in ex.completed_vps
    assert "VP-003" not in ex.failed_vps
    assert "VP-003" not in ex.skipped_vps
