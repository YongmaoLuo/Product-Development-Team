"""跨轮 infra 重试账本的接线 —— 按轮记账，并给下一轮算 provider 排除集。

Arch 决策点 2（*infra 的跨轮有界重试与 provider 重选*）有三半，缺一不可：

  * Task 1 给了**账本**（``verification.infra_retry.InfraRetryLedger``）：
    按 VP 记下每一轮的 provider / reason_code，跨轮落盘，同轮幂等；
  * Task 5 给了**子代理的两端**：``run(excluded_providers=...)`` 把排除集
    送到底层的 provider 选择，``Verdict.provider`` 把这次真正用的
    provider 报回来；
  * 本文件钉的是**中间那一段 —— 接线**：账本和子代理各自就绪，但没人
    把它们接上。

接线的两端：

  * ``Orchestrator._record_infra_attempts`` —— 报告生成之后，把这一轮
    **非 PASSED 且分类为 ``infra``** 的条目记进账本。分类是 Task 2 回填
    进去的，所以记账必须跑在回填之后。
  * ``Orchestrator._excluded_providers_for`` —— 下一轮派活之前，把这个 VP
    已经用过的 provider 算成排除集交给子代理，让重试落在**另一家**身上。
  * 中间那一行 —— ``_delegate_to_sub_agent`` 记下本次真正用的 provider，
    记账时才有 provider 可记。

边界（每一条都是"记错了比不记更坏"）：

  * 只有 ``infra`` 的失败才记账 —— 交付物的缺陷**不**消耗 infra 的重试
    预算：缺陷要的是修复，不是重试；
  * PASSED 的条目不记账 —— 绿的点没有失败可分类；
  * 同一轮重复生成报告不重复记账（账本按 ``(vp, round)`` 幂等）；
  * 账本坏了**不能**让报告生成失败 —— 账本读不出来就当没有记录，
    宁可少记，不可崩掉一整轮验证。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_subagent import Verdict  # noqa: E402


#: The failure class of an attempt that malfunctioned — the environment
#: failed, the deliverable was never asked.  Spelled as a literal and
#: pinned against the module constant by the assertion below, so a
#: drift in either place is a test failure.
INFRA = "infra"

#: The failure class of a deliverable that is genuinely wrong.
DELIVERABLE_DEFECT = "deliverable_defect"


def test_the_infra_class_literal_matches_the_module_constant():
    """The literal used in these tests is the module's own constant."""
    from verification_subagent import INFRA_FAILURE_CLASS

    assert INFRA == INFRA_FAILURE_CLASS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubCodingTool:
    """A coding-tool stand-in that only carries the provider chain.

    ``provider_priority`` is the ordered list of providers the dispatch
    would walk; the orchestrator reads it to know whether an exclusion
    set leaves anything to pick.
    """

    def __init__(self, provider_priority: Optional[List[str]] = None) -> None:
        self.provider_priority = list(provider_priority or [])

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError(
            "this stand-in must not be asked for a judgment here"
        )


class _ReportingTool(_StubCodingTool):
    """A coding-tool stand-in whose judgment answers with a report."""

    def __init__(self, report: Dict[str, Any]) -> None:
        super().__init__(provider_priority=[])
        self.report = report

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return json.loads(json.dumps(self.report))


class _FakeSubAgent:
    """A ``VerificationSubAgent`` stand-in that records what it was handed.

    ``calls`` is a **class** attribute on purpose: the orchestrator
    constructs the sub-agent itself, so the test has no handle on the
    instance to read the recording from.
    """

    calls: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs

    async def run(self, **kwargs: Any) -> Verdict:
        type(self).calls.append(kwargs)
        return Verdict(
            verdict="FAILED",
            reasons=["the dispatch failed"],
            evidence=["a transport error"],
            provider="vendor-b",
        )


class _BrokenLedger:
    """A ledger stand-in whose every operation raises.

    The ledger lives on disk next to the plan; a broken file, a
    read-only directory or a corrupt JSON payload must never take the
    report down with it.
    """

    def record_attempt(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise OSError("the ledger file is gone")

    def excluded_providers(self, vp_id: str) -> List[str]:
        raise OSError("the ledger file is gone")


class _FakePersistence:
    """A persistence stand-in that swallows the audit events."""

    def write_verification_point_log(self, *args: Any, **kwargs: Any) -> None:
        return None


def _make_orchestrator(tmp_path: Path, coding_tool: Any = None) -> Orchestrator:
    """An orchestrator over a throwaway plan directory."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=coding_tool or _StubCodingTool(),
    )


def _infra_report(vp_id: str = "VP-006", **extra: Any) -> Dict[str, Any]:
    """A report whose one entry failed for infra reasons."""
    entry: Dict[str, Any] = {
        "id": vp_id,
        "status": "FAILED",
        "failure_class": INFRA,
        "reason_code": "coding_tool_call_failed",
    }
    entry.update(extra)
    return {"verification_results": [entry]}


# ---------------------------------------------------------------------------
# 1. 按轮记账 —— infra 的失败记进账本
# ---------------------------------------------------------------------------


def test_infra_attempt_recorded_with_provider(tmp_path):
    """infra 的失败带着这一轮的 provider 记进账本。

    任务书给的输入 / 输出示例，一字不差：``VP-006`` 在第 1 轮用
    ``vendor-a-pro`` 撞了一次 infra，账本里就该有这一条 —— 而且
    provider 在里面，因为 provider 是下一轮换家的依据。
    """
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._current_round = 1
    orchestrator._last_attempt_provider["VP-006"] = "vendor-a-pro"

    orchestrator._record_infra_attempts(_infra_report())

    assert orchestrator._infra_retry_ledger.attempts("VP-006") == [
        {
            "round": 1,
            "provider": "vendor-a-pro",
            "reason_code": "coding_tool_call_failed",
        }
    ]


def test_an_attempt_without_a_provider_is_recorded_with_a_blank_one(tmp_path):
    """provider 没查到就记空串 —— 记下"不知道"，不是"没有 provider"。

    空串不进排除集（它不是一家 provider），但这一轮**发生过**这件事
    必须记下来：重试预算按次数算，不按 provider 算。
    """
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._current_round = 1

    orchestrator._record_infra_attempts(_infra_report())

    assert orchestrator._infra_retry_ledger.attempts("VP-006") == [
        {
            "round": 1,
            "provider": "",
            "reason_code": "coding_tool_call_failed",
        }
    ]


# ---------------------------------------------------------------------------
# 2. 不该记的条目 —— 缺陷不消耗 infra 的重试预算
# ---------------------------------------------------------------------------


def test_deliverable_defect_not_recorded(tmp_path):
    """交付物的缺陷不记进 infra 账本。

    缺陷要的是**修复**，不是重试：把它记进 infra 的账，等于让这个 VP
    的重试预算被一次产品缺陷吃掉，第三次环境抖动之前它就被判定为
    "试完了"。
    """
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._current_round = 1
    orchestrator._last_attempt_provider["VP-006"] = "vendor-a-pro"

    orchestrator._record_infra_attempts({
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": DELIVERABLE_DEFECT,
                "reason_code": "assertion failed",
            }
        ]
    })

    assert orchestrator._infra_retry_ledger.attempts("VP-006") == []


def test_passed_entry_not_recorded(tmp_path):
    """PASSED 的条目不记账 —— 绿的点没有失败可分类。"""
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._current_round = 1
    orchestrator._last_attempt_provider["VP-006"] = "vendor-a-pro"

    orchestrator._record_infra_attempts({
        "verification_results": [
            {
                "id": "VP-006",
                "status": "PASSED",
                "failure_class": INFRA,
            }
        ]
    })

    assert orchestrator._infra_retry_ledger.attempts("VP-006") == []


# ---------------------------------------------------------------------------
# 3. 同轮幂等 —— 报告生成两次不重复记账
# ---------------------------------------------------------------------------


def test_same_round_recorded_once(tmp_path):
    """同一轮重复生成报告 → 账本里只有一条。

    报告这一轮可能被生成不止一次（LLM 判断重试、失败后的 fallback
    报告）。重复记账会把一次失败记成两次，白白吃掉重试预算。
    """
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._current_round = 1
    orchestrator._last_attempt_provider["VP-006"] = "vendor-a-pro"

    report = _infra_report()
    orchestrator._record_infra_attempts(report)
    orchestrator._record_infra_attempts(report)

    attempts = orchestrator._infra_retry_ledger.attempts("VP-006")
    assert len(attempts) == 1, attempts
    assert attempts[0]["round"] == 1, attempts


def test_a_later_round_is_a_separate_attempt(tmp_path):
    """换一轮就是一个新的尝试 —— 账本是按轮记的。"""
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._last_attempt_provider["VP-006"] = "vendor-a-pro"

    orchestrator._current_round = 1
    orchestrator._record_infra_attempts(_infra_report())
    orchestrator._current_round = 2
    orchestrator._record_infra_attempts(_infra_report())

    attempts = orchestrator._infra_retry_ledger.attempts("VP-006")
    assert [entry["round"] for entry in attempts] == [1, 2], attempts


# ---------------------------------------------------------------------------
# 4. 排除集 —— 下一轮换一家 provider
# ---------------------------------------------------------------------------


def test_excluded_providers_passed_to_sub_agent(tmp_path, monkeypatch):
    """子代理收到这个 VP 已经用过的 provider 排除集。

    账本记下第 1 轮是 ``vendor-a-pro`` 撞的，第 2 轮派活时排除集就得
    把它排除掉 —— 否则重试还是落在同一家身上，永远死在同一个地方。
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_StubCodingTool(["vendor-a-pro", "vendor-b"]),
    )
    orchestrator._current_round = 2
    orchestrator._infra_retry_ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro",
    )

    _FakeSubAgent.calls = []
    monkeypatch.setattr(verification_agent, "VerificationSubAgent", _FakeSubAgent)

    verdict = asyncio.run(
        orchestrator._delegate_to_sub_agent(
            {"id": "VP-006"}, "code_review", "VP-006",
        )
    )

    assert _FakeSubAgent.calls, "the sub-agent was never called"
    assert _FakeSubAgent.calls[0]["excluded_providers"] == ["vendor-a-pro"]
    # The provider the attempt actually used is remembered for the
    # ledger — the exclusion set is what the NEXT round will exclude.
    assert orchestrator._last_attempt_provider["VP-006"] == "vendor-b"
    assert verdict.provider == "vendor-b"


def test_a_minimal_orchestrator_can_still_delegate(tmp_path, monkeypatch):
    """跳过 ``__init__`` 的精简 orchestrator 也能走这条生产入口。

    写护栏那一侧的测试用 ``Orchestrator.__new__`` 造了一个"只带必要
    字段"的 orchestrator —— ``__init__`` 被跳过，账本和 provider 表
    都不存在。这条路径是**生产入口**，不能因为接线加了两个字段就炸掉。
    """
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plan_dir = tmp_path / "plan"
    orchestrator.plan_dir.mkdir(parents=True, exist_ok=True)
    orchestrator.project_dir = tmp_path / "project"
    orchestrator.coding_tool = None
    orchestrator.persistence = _FakePersistence()
    orchestrator._current_round = 2

    _FakeSubAgent.calls = []
    monkeypatch.setattr(verification_agent, "VerificationSubAgent", _FakeSubAgent)

    verdict = asyncio.run(
        orchestrator._delegate_to_sub_agent(
            {"id": "VP-006"}, "code_review", "VP-006",
        )
    )

    assert verdict.provider == "vendor-b"
    # No ledger → no exclusions to hand over, but the attempt still runs.
    assert _FakeSubAgent.calls[0]["excluded_providers"] == []
    # And the provider is remembered all the same.
    assert orchestrator._last_attempt_provider["VP-006"] == "vendor-b"


def test_no_alternative_provider_is_logged(tmp_path, caplog):
    """整条 provider 链都被排除了 → 说一句"没有备选"。

    排除集把所有候选都排掉时，下一次尝试必然还是落在同一家身上。这句
    提示是这件事唯一可见的地方 —— 不说，读者只看到"重试了，还是同一
    家，还是同一个错"。
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_StubCodingTool(["vendor-a-pro"]),
    )
    orchestrator._infra_retry_ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro",
    )

    with caplog.at_level(logging.WARNING):
        excluded = orchestrator._excluded_providers_for("VP-006")

    assert excluded == ["vendor-a-pro"]
    assert "no alternative provider" in caplog.text, caplog.text


def test_an_excluded_set_with_a_provider_left_is_not_logged(tmp_path, caplog):
    """还剩一家没被排除 → 没什么好说的。"""
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_StubCodingTool(["vendor-a-pro", "vendor-b"]),
    )
    orchestrator._infra_retry_ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro",
    )

    with caplog.at_level(logging.WARNING):
        excluded = orchestrator._excluded_providers_for("VP-006")

    assert excluded == ["vendor-a-pro"]
    assert "no alternative provider" not in caplog.text, caplog.text


def test_an_empty_provider_chain_is_not_logged(tmp_path, caplog):
    """provider 链是空的 → 没有"备选"可言，也就没有那句话。"""
    orchestrator = _make_orchestrator(
        tmp_path, coding_tool=_StubCodingTool([]),
    )
    orchestrator._infra_retry_ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro",
    )

    with caplog.at_level(logging.WARNING):
        excluded = orchestrator._excluded_providers_for("VP-006")

    assert excluded == ["vendor-a-pro"]
    assert "no alternative provider" not in caplog.text, caplog.text


def test_an_unknown_vp_has_no_exclusions(tmp_path):
    """没记过账的 VP → 空排除集（排除 nobody）。"""
    orchestrator = _make_orchestrator(tmp_path)

    assert orchestrator._excluded_providers_for("VP-999") == []


# ---------------------------------------------------------------------------
# 5. 账本坏了不能让报告生成失败
# ---------------------------------------------------------------------------


def test_ledger_error_does_not_break_report(tmp_path):
    """账本读写炸了 → 报告照常生成。

    账本是一次记账的账，不是这一轮验证的成败。账本坏了而报告跟着崩，
    等于用一本辅助账把整轮验证带走。
    """
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._current_round = 1
    orchestrator._last_attempt_provider["VP-006"] = "vendor-a-pro"
    orchestrator._infra_retry_ledger = _BrokenLedger()

    # Neither call may raise.
    orchestrator._record_infra_attempts(_infra_report())
    assert orchestrator._excluded_providers_for("VP-006") == []


# ---------------------------------------------------------------------------
# 6. 接线 —— 报告生成之后记账
# ---------------------------------------------------------------------------


def test_report_generation_records_the_infra_attempt(tmp_path):
    """生成报告时，infra 的失败被记进了账本。

    这是接线的证据：分类是 Task 2 回填的，记账必须跑在回填**之后**，
    否则回填前的条目还没有 ``failure_class``，一条也记不上。
    """
    tool = _ReportingTool({
        "overall_status": "FAILED",
        "summary": "the judgment LLM's own words",
        "verification_results": [
            {"id": "VP-006", "status": "FAILED", "actual_result": "timeout"},
        ],
        "requirement_deviations": [],
    })
    orchestrator = _make_orchestrator(tmp_path, coding_tool=tool)
    orchestrator._current_round = 1
    orchestrator._last_attempt_provider["VP-006"] = "vendor-a-pro"

    orchestrator.generate_verification_report({
        "verification_points": [],
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "actual_result": "the dispatch timed out",
                "evidence": json.dumps({
                    "vp_id": "VP-006",
                    "reason_code": "coding_tool_call_failed",
                }),
                "failure_class": INFRA,
            }
        ],
    })

    attempts = orchestrator._infra_retry_ledger.attempts("VP-006")
    assert len(attempts) == 1, attempts
    assert attempts[0]["round"] == 1, attempts
    assert attempts[0]["provider"] == "vendor-a-pro", attempts
