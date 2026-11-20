"""2026-11-18：成本门按**计划**的 mode 分流 —— production 不生效，test 维持 DEFERRED。

问题
----
成本闸原本只有一个开关：``cost=llm_quota`` 的验证点一律不执行（记一条
``DEFERRED``）。但「这次验证要花钱」是不是问题，取决于**这个计划是来干什么
的**：生产计划里真花钱是预期（不验就等于没验），测试计划里真花钱是事故（测试
自己把额度烧掉了）。同一个进程里可以同时跑着一个生产计划和一个测试计划 ——
所以判据不能是进程级环境变量，得是**计划级**的 mode。

契约
----
``_cost_gate_verdict(vp, plan_mode)``：

  * ``production`` → 成本门**不生效**：任何成本声明都不拦，验证点照常执行；
  * ``test`` → 成本门生效：``cost=llm_quota`` 的验证点不执行，记一条
    ``DEFERRED``，并留下一条日志锚点 ``cost_gate_skipped_test_mode=[...]``；
  * ``test`` + ``PDT_VERIFY_ALLOW_LLM_QUOTA=1`` → 照常执行（操作者明确说
    「这次可以花」）；
  * 免费的成本声明（``none`` / ``low`` / 缺省）在**两个模式**下都照常执行 ——
    mode 的分流不许漏到与额度无关的验证点上。

执行器拿不到 mode 时去**计划的状态行**里读（``read_plan_mode``）：mode 是
计划级属性，就得从计划读，不能从进程环境读。

七条 TDD 规格：

  1. ``test_production_mode_does_not_gate``；
  2. ``test_test_mode_defers_with_plan_mode_marker``；
  3. ``test_anchor_only_in_test_mode``；
  4. ``test_executor_reads_plan_state_when_mode_omitted``；
  5. ``test_allow_env_still_forces_through``；
  6. ``test_non_llm_quota_vps_identical_in_both_modes``；
  7. ``test_only_llm_quota_differs_between_modes``。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import (  # noqa: E402
    VerificationExecutor,
    _cost_gate_verdict,
)

#: 成本闸的开关（操作者显式放行）。
_ALLOW_ENV = "PDT_VERIFY_ALLOW_LLM_QUOTA"

#: 写成本门那条日志锚点的 logger。
_EXECUTOR_LOGGER = "verification_executor"

#: 成本门被真正触发时写下的锚点前缀。
_ANCHOR = "cost_gate_skipped_test_mode"


@pytest.fixture(autouse=True)
def _clean_switch(monkeypatch):
    """每个用例都从「开关关着」开始。

    开关是进程级的环境变量：漏到下一个用例里，那个用例会看到一个自己
    没打开的放行（表现为「成本闸没拦住」），这类污染最难查。
    """
    monkeypatch.delenv(_ALLOW_ENV, raising=False)


def _quota_vp() -> Dict[str, Any]:
    """一条声明了 ``cost=llm_quota`` 的验证点。"""
    return {"id": "VP-1", "cost": "llm_quota"}


# ---------------------------------------------------------------------------
# 1) production：成本门不生效
# ---------------------------------------------------------------------------


def test_production_mode_does_not_gate():
    """``plan_mode="production"`` → 成本门不拦，验证点照常执行。

    真花钱是生产验证的预期 —— 生产计划里把这条验证点延后，等于「没验就
    过」。
    """
    assert _cost_gate_verdict(_quota_vp(), plan_mode="production") is None


# ---------------------------------------------------------------------------
# 2) test：维持 DEFERRED，且证据里带上 plan_mode
# ---------------------------------------------------------------------------


def test_test_mode_defers_with_plan_mode_marker():
    """``plan_mode="test"`` → 仍然是 ``DEFERRED``，证据里写着是哪个模式。

    证据里的 ``plan_mode`` 是给报告用的：一条被成本门挡下的验证点必须说
    得出「它是在哪个模式下被挡下的」，否则报告读者分不清「测试模式挡的」
    和「生产模式挡的」。
    """
    verdict = _cost_gate_verdict(_quota_vp(), plan_mode="test")

    assert verdict is not None
    assert verdict["status"] == "DEFERRED", verdict
    assert verdict["evidence"]["cost"] == "llm_quota", verdict
    assert verdict["evidence"]["gate"] == "llm_quota", verdict
    assert verdict["evidence"]["plan_mode"] == "test", verdict


# ---------------------------------------------------------------------------
# 3) 日志锚点只写在 test 模式
# ---------------------------------------------------------------------------


def test_anchor_only_in_test_mode(caplog):
    """锚点 ``cost_gate_skipped_test_mode=[...]`` 只在 test 模式出现。

    production 模式永远不写这条锚点 —— 报告与仪表盘按它统计「test 模式下因
    成本门跳过」的验证点，生产计划里一条都没有，写了就是假账。
    """
    with caplog.at_level(logging.INFO, logger=_EXECUTOR_LOGGER):
        _cost_gate_verdict({"id": "VP-ANCHOR", "cost": "llm_quota"}, plan_mode="test")

    assert "%s=['VP-ANCHOR']" % _ANCHOR in caplog.text, caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_EXECUTOR_LOGGER):
        _cost_gate_verdict(
            {"id": "VP-ANCHOR", "cost": "llm_quota"}, plan_mode="production",
        )

    assert _ANCHOR not in caplog.text, caplog.text


# ---------------------------------------------------------------------------
# 4) 执行器没拿到 mode 时去计划的状态行里读
# ---------------------------------------------------------------------------


def test_executor_reads_plan_state_when_mode_omitted(tmp_path, monkeypatch):
    """``plan_mode`` 省略 → 执行器读这个计划的 mode（``read_plan_mode``）。

    mode 是**计划级**属性：同一个进程里可以同时跑着一个生产计划和一个测试
    计划，所以执行器不能靠进程级环境变量猜，只能问这个计划。
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    from plan_state import PlanState

    plan_dir = tmp_path / "plan-cost-gate-mode"
    plan_dir.mkdir(parents=True, exist_ok=True)
    state = PlanState(plan_dir)
    state.ensure_row()
    assert state.set_mode("test") == "test"

    executor = VerificationExecutor(
        verification_plan={"verification_points": []},
        plan_id=plan_dir.name,
        plan_dir=plan_dir,
        sub_agent_runner=Mock(
            return_value={"status": "PASSED", "reasons": ["ok"], "evidence": {}}
        ),
    )

    assert executor.plan_mode == "test", executor.plan_mode
    # 读到的 mode 真的作用在成本门上（不是只挂了个属性）。
    assert _cost_gate_verdict(
        _quota_vp(), plan_mode=executor.plan_mode,
    ) is not None


# ---------------------------------------------------------------------------
# 5) 操作者的开关仍然能强制放行
# ---------------------------------------------------------------------------


def test_allow_env_still_forces_through(monkeypatch):
    """``PDT_VERIFY_ALLOW_LLM_QUOTA=1`` → test 模式也照常执行。

    mode 的分流不是「谁都不许花」：操作者明确说「这次可以花」时，两个模式
    都放行。
    """
    monkeypatch.setenv(_ALLOW_ENV, "1")

    assert _cost_gate_verdict(_quota_vp(), plan_mode="test") is None


# ---------------------------------------------------------------------------
# 6) 与额度无关的验证点：两个模式结论一致
# ---------------------------------------------------------------------------


def test_non_llm_quota_vps_identical_in_both_modes():
    """``cost`` 为 ``none`` / ``low`` / 缺省 → 两个模式都返回 ``None``。

    mode 的分流只许作用在「真花钱」那一条上。一条免费验证点被某个模式延后
    了，说明分流漏到了不该漏的地方。
    """
    vps: List[Dict[str, Any]] = [
        {"id": "VP-NONE", "cost": "none"},
        {"id": "VP-LOW", "cost": "low"},
        {"id": "VP-MISSING"},
    ]

    for vp in vps:
        assert _cost_gate_verdict(vp, plan_mode="test") is None, vp
        assert _cost_gate_verdict(vp, plan_mode="production") is None, vp


# ---------------------------------------------------------------------------
# 7) 两个模式之间只有 llm_quota 这一条结论不同
# ---------------------------------------------------------------------------


def test_only_llm_quota_differs_between_modes():
    """同一组验证点，两个模式之间**只有** ``llm_quota`` 那条结论不同。

    这是「分流」这个词的精确含义：其余验证点在两个模式下的结论必须逐字
    相同 —— 否则生产计划与测试计划跑出来的东西就不是同一个东西了。
    """
    vps: List[Dict[str, Any]] = [
        {"id": "VP-NONE", "cost": "none"},
        {"id": "VP-LOW", "cost": "low"},
        {"id": "VP-MISSING"},
        {"id": "VP-QUOTA", "cost": "llm_quota"},
    ]

    differing = [
        vp["id"]
        for vp in vps
        if _cost_gate_verdict(vp, plan_mode="test")
        != _cost_gate_verdict(vp, plan_mode="production")
    ]

    assert differing == ["VP-QUOTA"], differing
