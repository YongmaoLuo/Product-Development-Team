"""TDD spec — backend/verification/topic_relevance.py 相关性判定内核。

Background
----------
A1′ 要求「每次进入 verification 时判定活动 VP 是否仍在计划主题内」。逐个 VP
调 LLM 是不可接受的 —— 一次 verification 可能有几十条 VP，逐条调就是几十次
模型往返，成本与延迟都是数量级的浪费。所以判定必须分两级：

1. **确定性层（零 I/O）**：VP 声明的 ``related_prd_criteria`` 里直接写了判据
   编号（``AC-2``），或者写了判据正文（归一化后互相包含），即判「相关」，
   根本不碰模型。
2. **LLM 层（一次批量）**：剩下没命中的 VP 打包成**一次**调用交给模型。

判定内核是**纯函数**：唯一的外部依赖是调用方注入的 ``llm_batch`` 可调用对象。
这让本文件不需要任何 mock —— 每个用例塞一个普通函数进去，数它被调了几次。

TDD contract — 4 cases
-----------------------
1. ``test_exact_id_match`` — ID 命中 → ``deterministic_match``，且 ``llm_batch``
   调 **0** 次（确定性层已经判完了，不该再惊动模型）。
2. ``test_text_containment_match`` — 全角 / 中文标点 / 连续空白归一化后双向
   包含 → ``deterministic_match``。
3. ``test_batch_called_once`` — 3 个未命中的 VP → ``llm_batch`` **恰调 1 次**，
   且这一次里装着全部 3 个 VP。
4. ``test_fail_open_keeps_all`` — 返回不可解析 → 全部 ``fail_open`` 保留
   （一条 VP 都不能因为模型抽风而消失，更不能被判成无关）。

边界条件（同一文件覆盖）：

* ``llm_batch=None`` / 抛错 / 缺该 vp_id → 判 ``llm_relevant`` + ``tier="fail_open"``。
* 空 criteria、空 ``related_prd_criteria`` → 走兜底，**绝不**判 irrelevant。
* LLM 输出里 vps 集合外的 id 一律忽略。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification.topic_relevance import (  # noqa: E402
    RelevanceDecision,
    RelevanceResult,
    judge_topic_relevance,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _recording_llm_batch(response, calls=None):
    """Return an ``llm_batch`` fake that records every call it receives.

    No mocking framework is involved: the fake is a plain function, so the
    "batch is called exactly once" contract is asserted by counting the
    calls it received, not by patching something.
    """
    recorded = calls if calls is not None else []

    def llm_batch(vps, criteria):
        recorded.append({"vps": list(vps), "criteria": list(criteria)})
        if isinstance(response, Exception):
            raise response
        return response

    return llm_batch, recorded


# ---------------------------------------------------------------------------
# 1. exact id match — deterministic tier, zero LLM calls
# ---------------------------------------------------------------------------


def test_exact_id_match():
    llm_batch, calls = _recording_llm_batch(json.dumps({}))

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["AC-2"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    # 判定结果与接口契约里给的输出示例逐字段相同。
    assert result["VP-1"] == RelevanceResult(
        "VP-1", "deterministic_match", "deterministic", "id:AC-2", ("AC-2",)
    )
    assert result["VP-1"].decision == RelevanceDecision.DETERMINISTIC_MATCH
    assert result["VP-1"].tier == "deterministic"
    assert result["VP-1"].reason == "id:AC-2"
    assert result["VP-1"].matched_criterion_ids == ("AC-2",)

    # 确定性层已经判完，模型一次都不该被叫醒。
    assert calls == []


def test_exact_id_match_does_not_call_llm_even_when_others_are_unresolved():
    llm_batch, calls = _recording_llm_batch(json.dumps({"VP-2": {"relevant": True}}))

    result = judge_topic_relevance(
        vps=[
            {"id": "VP-1", "related_prd_criteria": ["AC-2"]},
            {"id": "VP-2", "related_prd_criteria": ["完全对不上的一句话"]},
        ],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].decision == RelevanceDecision.DETERMINISTIC_MATCH
    assert len(calls) == 1
    # 已经确定性命中的 VP 不进批次。
    assert [vp["id"] for vp in calls[0]["vps"]] == ["VP-2"]


# ---------------------------------------------------------------------------
# 2. text containment match
# ---------------------------------------------------------------------------


def test_text_containment_match():
    """全角标点 / 中文标点 / 连续空白归一化后，双向包含即判定命中。"""
    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["导出， ＣＳＶ"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=None,
    )

    assert result["VP-1"].decision == RelevanceDecision.DETERMINISTIC_MATCH
    assert result["VP-1"].tier == "deterministic"
    assert result["VP-1"].reason == "text:AC-2"
    assert result["VP-1"].matched_criterion_ids == ("AC-2",)


def test_text_containment_match_is_bidirectional():
    """判据正文更长、VP 声明更短时同样命中（包含是双向的）。"""
    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["导出 CSV"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV 报表并落盘"}],
        llm_batch=None,
    )

    assert result["VP-1"].decision == RelevanceDecision.DETERMINISTIC_MATCH
    assert result["VP-1"].matched_criterion_ids == ("AC-2",)


# ---------------------------------------------------------------------------
# 3. batch — one call for all unresolved VPs
# ---------------------------------------------------------------------------


def test_batch_called_once():
    llm_batch, calls = _recording_llm_batch(
        json.dumps(
            {
                "VP-1": {"relevant": True, "matched_criterion_ids": ["AC-2"]},
                "VP-2": {"relevant": False, "reason": "与计划主题无关"},
                "VP-3": {"relevant": True},
            }
        )
    )

    result = judge_topic_relevance(
        vps=[
            {"id": "VP-1", "related_prd_criteria": ["登录改造"]},
            {"id": "VP-2", "related_prd_criteria": ["压测脚本"]},
            {"id": "VP-3", "related_prd_criteria": ["部署回滚"]},
        ],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    # 三个未命中的 VP 打包成一次调用 —— 不是三次。
    assert len(calls) == 1
    assert [vp["id"] for vp in calls[0]["vps"]] == ["VP-1", "VP-2", "VP-3"]
    assert calls[0]["criteria"] == [{"id": "AC-2", "text": "导出 CSV"}]

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "llm"
    assert result["VP-1"].matched_criterion_ids == ("AC-2",)

    assert result["VP-2"].decision == RelevanceDecision.LLM_IRRELEVANT
    assert result["VP-2"].tier == "llm"
    assert result["VP-2"].reason == "与计划主题无关"

    assert result["VP-3"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-3"].tier == "llm"


def test_llm_relevant_and_irrelevant_are_distinct_decisions():
    llm_batch, _calls = _recording_llm_batch(
        json.dumps({"VP-1": True, "VP-2": False})
    )

    result = judge_topic_relevance(
        vps=[
            {"id": "VP-1", "related_prd_criteria": ["甲"]},
            {"id": "VP-2", "related_prd_criteria": ["乙"]},
        ],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-2"].decision == RelevanceDecision.LLM_IRRELEVANT


# ---------------------------------------------------------------------------
# 4. fail-open — a broken model must never delete or condemn a VP
# ---------------------------------------------------------------------------


def test_fail_open_keeps_all():
    llm_batch, calls = _recording_llm_batch("这不是 JSON")

    result = judge_topic_relevance(
        vps=[
            {"id": "VP-1", "related_prd_criteria": ["甲"]},
            {"id": "VP-2", "related_prd_criteria": ["乙"]},
            {"id": "VP-3", "related_prd_criteria": ["丙"]},
        ],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    # 一条 VP 都不能因为模型抽风而消失。
    assert set(result) == {"VP-1", "VP-2", "VP-3"}
    for vp_id, verdict in result.items():
        assert verdict.vp_id == vp_id
        assert verdict.decision == RelevanceDecision.LLM_RELEVANT
        assert verdict.tier == "fail_open"
        assert verdict.matched_criterion_ids == ()
    assert len(calls) == 1


def test_fail_open_when_llm_batch_is_none():
    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["甲"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=None,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "fail_open"


def test_fail_open_when_llm_batch_raises():
    llm_batch, calls = _recording_llm_batch(RuntimeError("provider 502"))

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["甲"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "fail_open"
    assert len(calls) == 1


def test_fail_open_when_vp_id_missing_from_llm_output():
    llm_batch, _calls = _recording_llm_batch(
        json.dumps({"VP-9": {"relevant": False}})
    )

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["甲"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "fail_open"


def test_llm_output_ids_outside_the_vp_set_are_ignored():
    llm_batch, _calls = _recording_llm_batch(
        json.dumps(
            {
                "VP-1": {"relevant": True},
                "VP-404": {"relevant": False},
            }
        )
    )

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["甲"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert set(result) == {"VP-1"}
    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "llm"


# ---------------------------------------------------------------------------
# 5. empty inputs fall back — never "irrelevant"
# ---------------------------------------------------------------------------


def test_undeclared_vps_still_enter_the_batch():
    """没有声明的 VP 也是「未命中」，必须一起进那一次批量调用。"""
    llm_batch, calls = _recording_llm_batch(json.dumps({}))

    judge_topic_relevance(
        vps=[
            {"id": "VP-1"},
            {"id": "VP-2", "related_prd_criteria": ["压测脚本"]},
        ],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert len(calls) == 1
    assert [vp["id"] for vp in calls[0]["vps"]] == ["VP-1", "VP-2"]


def test_empty_criteria_never_judges_irrelevant():
    llm_batch, calls = _recording_llm_batch(json.dumps({"VP-1": {"relevant": False}}))

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["甲"]}],
        criteria=[],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "fail_open"
    # 没有判据可比，问模型也是白问 —— 不发这次调用。
    assert calls == []


def test_empty_related_criteria_never_judges_irrelevant():
    llm_batch, calls = _recording_llm_batch(
        json.dumps({"VP-1": {"relevant": False, "reason": "无关"}})
    )

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": []}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    # 没有声明的 VP 也要进批次（模型照样回答），但模型凭措辞说它跑题，
    # 不足以判死一条没有主张的 VP —— 降级成兜底。
    assert len(calls) == 1
    assert [vp["id"] for vp in calls[0]["vps"]] == ["VP-1"]
    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "fail_open"


def test_declared_but_unmatched_vp_can_be_condemned_by_the_model():
    """声明了判据却对不上计划 —— 这是一条具体的矛盾，模型可以据此判无关。"""
    llm_batch, _calls = _recording_llm_batch(
        json.dumps({"VP-1": {"relevant": False, "reason": "与计划主题无关"}})
    )

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["压测脚本"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_IRRELEVANT
    assert result["VP-1"].tier == "llm"
    assert result["VP-1"].reason == "与计划主题无关"


def test_empty_related_criteria_falls_back_when_llm_is_unusable():
    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": []}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=None,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "fail_open"


def test_missing_related_criteria_key_is_treated_as_empty():
    result = judge_topic_relevance(
        vps=[{"id": "VP-1"}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=None,
    )

    assert result["VP-1"].tier == "fail_open"
    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT


# ---------------------------------------------------------------------------
# 6. shape of the returned mapping
# ---------------------------------------------------------------------------


def test_result_order_follows_input_order():
    llm_batch, _calls = _recording_llm_batch(json.dumps({}))

    result = judge_topic_relevance(
        vps=[
            {"id": "VP-3", "related_prd_criteria": ["甲"]},
            {"id": "VP-1", "related_prd_criteria": ["AC-2"]},
            {"id": "VP-2", "related_prd_criteria": ["乙"]},
        ],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert list(result) == ["VP-3", "VP-1", "VP-2"]


def test_result_is_a_frozen_dataclass():
    verdict = RelevanceResult(
        "VP-1", "deterministic_match", "deterministic", "id:AC-2", ("AC-2",)
    )

    try:
        verdict.reason = "mutated"  # type: ignore[misc]
    except Exception as exc:  # dataclasses.FrozenInstanceError
        assert type(exc).__name__ == "FrozenInstanceError"
    else:  # pragma: no cover - only reached when the dataclass is not frozen
        raise AssertionError("RelevanceResult must be frozen")


def test_matched_criterion_ids_is_a_tuple():
    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["AC-2"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=None,
    )

    assert isinstance(result["VP-1"].matched_criterion_ids, tuple)


# ---------------------------------------------------------------------------
# 7. llm_batch 的调用契约 —— 签名自适配
# ---------------------------------------------------------------------------
#
# 判定内核允许注入任意形状的 ``llm_batch``：真实模型客户端可能写成
# ``llm_batch(vps, criteria)``，也可能写成 ``llm_batch(payload)``，还可能把
# 参数声明成 keyword-only（``def llm_batch(*, vps, criteria)``）。
# 内核按签名自适配，把这些形状都接上。
#
# 这里的要害是**失败方式**：签名没接上会抛 TypeError，而 TypeError 被判定
# 内核吞掉后整批 VP 兜底 —— 结果是对的（按相关处理），但 tier 变成
# "fail_open"，原因是一条谁也看不懂的 "llm_error"。模型其实一次都没被
# 问过，却看上去像是问了、问崩了。所以自适配必须真的把这些形状接通：
# tier 必须是 "llm"。


def test_keyword_only_llm_batch_is_called_with_keyword_arguments():
    """``def llm_batch(*, vps, criteria)`` —— 关键字参数必须按关键字传。"""
    seen = {}

    def llm_batch(*, vps, criteria):
        seen["vps"] = list(vps)
        seen["criteria"] = list(criteria)
        return json.dumps({"VP-1": {"relevant": True}})

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["压测脚本"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    # 接上了，而不是被 TypeError 吞成兜底。
    assert result["VP-1"].tier == "llm", result["VP-1"].reason
    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert [vp["id"] for vp in seen["vps"]] == ["VP-1"]
    assert seen["criteria"] == [{"id": "AC-2", "text": "导出 CSV"}]


def test_mixed_positional_and_keyword_only_llm_batch_is_called_correctly():
    """``def llm_batch(vps, *, criteria)`` —— 位置参数位置传，关键字参数关键字传。"""
    seen = {}

    def llm_batch(vps, *, criteria):
        seen["vps"] = list(vps)
        seen["criteria"] = list(criteria)
        return json.dumps({"VP-1": {"relevant": True}})

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["压测脚本"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].tier == "llm", result["VP-1"].reason
    assert [vp["id"] for vp in seen["vps"]] == ["VP-1"]
    assert seen["criteria"] == [{"id": "AC-2", "text": "导出 CSV"}]


def test_keyword_only_single_argument_llm_batch_receives_the_payload():
    """``def llm_batch(*, payload)`` —— 单参数形状收到整个载荷 dict。"""
    seen = {}

    def llm_batch(*, payload):
        seen["payload"] = payload
        return json.dumps({"VP-1": {"relevant": True}})

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["压测脚本"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].tier == "llm", result["VP-1"].reason
    assert [vp["id"] for vp in seen["payload"]["vps"]] == ["VP-1"]
    assert seen["payload"]["criteria"] == [{"id": "AC-2", "text": "导出 CSV"}]


# ---------------------------------------------------------------------------
# 8. 模型回答的其它形状
# ---------------------------------------------------------------------------


def test_llm_result_list_form_is_parsed():
    """``{"results": [{"vp_id": ..., "relevant": ...}]}`` 这种列表形状也要认。"""
    llm_batch, _calls = _recording_llm_batch(
        json.dumps({"results": [{"vp_id": "VP-1", "relevant": True}]})
    )

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["压测脚本"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].decision == RelevanceDecision.LLM_RELEVANT
    assert result["VP-1"].tier == "llm"


def test_llm_reported_criterion_ids_outside_the_plan_are_filtered_out():
    """模型报的命中判据不在计划里 → 丢掉（不能报一个谁也引用不了的编号）。"""
    llm_batch, _calls = _recording_llm_batch(
        json.dumps(
            {
                "VP-1": {
                    "relevant": True,
                    "matched_criterion_ids": ["AC-2", "AC-99"],
                }
            }
        )
    )

    result = judge_topic_relevance(
        vps=[{"id": "VP-1", "related_prd_criteria": ["压测脚本"]}],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert result["VP-1"].matched_criterion_ids == ("AC-2",)


def test_duplicate_vp_id_keeps_the_first_entry():
    """同一个 vp_id 出现两次 → 保留第一条。"""
    llm_batch, _calls = _recording_llm_batch(json.dumps({}))

    result = judge_topic_relevance(
        vps=[
            {"id": "VP-1", "related_prd_criteria": ["AC-2"]},
            {"id": "VP-1", "related_prd_criteria": ["压测脚本"]},
        ],
        criteria=[{"id": "AC-2", "text": "导出 CSV"}],
        llm_batch=llm_batch,
    )

    assert list(result) == ["VP-1"]
    assert result["VP-1"].decision == RelevanceDecision.DETERMINISTIC_MATCH
    assert result["VP-1"].matched_criterion_ids == ("AC-2",)