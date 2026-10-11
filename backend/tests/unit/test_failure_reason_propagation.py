"""2026-10-11 失败原因在到达下游前被截断 —— 缺陷钉桩。

两个断点，一根病根：**信息在传给下游消费者的路上被静默丢弃**。

1. ``Orchestrator._verdict_to_result`` 取 ``reasons[0]`` 当
   ``actual_result``。子 agent 按判据顺序输出 reasons，**满足的在前、
   不满足的在最后**，所以 ``reasons[0]`` 系统性地是一条"已满足"的判据。
   实测 VP-016 第 4 轮：``reasons[0]`` 是判据①成立（246 字），真正的
   失败原因（判据④后半，623 字）在 ``reasons[4]``。修复 agent 拿到的
   输入因此是一份"每条判据都过了、verdict 却是 FAILED"的说明。

2. ``_build_judgment_prompt`` 把 evidence 硬砍到 500 字符。VP-016 的
   evidence 是 7141 字符，第 500 字符正好切在结论的前半句；而且是
   **静默** 截断，报告员无从知道它被砍过，只能把残缺当完整。

这两个断言在修复前必红。
"""

from __future__ import annotations

from verification_agent import (
    _MAX_PROMPT_EVIDENCE_CHARS,
    _render_evidence_for_prompt,
    Orchestrator,
)
from verification_subagent import Verdict


# ---------------------------------------------------------------------------
# 1. _verdict_to_result —— 全部 reasons 必须活到 actual_result
# ---------------------------------------------------------------------------

#: 照抄 VP-016 第 4 轮的结构：5 条 reasons，前 4 条讲"判据成立"
#: （最后一条是"判据④的前半段成立"），第 5 条讲"判据④的后半段
#: **不成立**" —— 那才是真正的失败原因。
_SATISFIED_REASONS = [
    "判据①：CLI 接受 --remote 形式的 endpoint 参数 —— 成立（246 字）",
    "判据②：endpoint 可写入 ~/.claude 下的远端配置 —— 成立（305 字）",
    "判据③：重启后配置被正确加载 —— 成立（279 字）",
    "判据④前半：启动时读取远端配置不报错 —— 成立（251 字）",
]
_FAILING_REASON = (
    "判据④后半【不成立】：进程未把远端 endpoint 注入到子进程环境，"
    "实测子进程读到的仍是默认 localhost（623 字）—— 这才是本 VP 失败的原因"
)


def _vp016_like_verdict() -> Verdict:
    """构造一个 reasons[0] 满足、reasons[-1] 不满足的假 Verdict。"""
    return Verdict(
        verdict="FAILED",
        reasons=[*_SATISFIED_REASONS, _FAILING_REASON],
        evidence=["$ claude --version\nclaude 2.1.245"],
    )


def test_actual_result_carries_the_failing_reason_not_just_the_first():
    """actual_result 必须同时含"已满足"和"未满足"，且以未满足的那条结尾。"""
    result = Orchestrator._verdict_to_result("VP-016", _vp016_like_verdict())

    actual_result = result["actual_result"]

    # 满足的判据仍然在（不能因为修 bug 把"已满足"也丢了）。
    for satisfied in _SATISFIED_REASONS:
        assert satisfied in actual_result, (
            f"已满足的判据不应丢失：{satisfied[:20]}…"
        )

    # 真正的失败原因 —— 旧行为下这一条根本不在 actual_result 里。
    assert _FAILING_REASON in actual_result, (
        "失败判据必须出现在 actual_result 中；旧实现只取 reasons[0]，"
        "系统性丢掉排在最后的不满足判据。"
    )

    # 顺序语义：失败原因在最后（子 agent 满足在前、不满足在最后的约定）。
    assert actual_result.rstrip().endswith(_FAILING_REASON.rstrip()), (
        "actual_result 必须以不满足的那条判据结尾。"
    )


def test_actual_result_preserves_reason_order():
    """拼接顺序必须与 reasons 原顺序一致（下游靠它读"哪条是失败的"）。"""
    verdict = _vp016_like_verdict()
    result = Orchestrator._verdict_to_result("VP-016", verdict)

    positions = [result["actual_result"].index(r) for r in verdict.reasons]
    assert positions == sorted(positions), f"reasons 的相对顺序被打乱：{positions}"
    # 与 executor 路径 (_build_execution_results_from_executor) 同形。
    assert result["actual_result"] == "\n".join(verdict.reasons)


def test_actual_result_uses_every_reason_not_just_the_first():
    """显式钉住"取了不止第一条" —— 旧实现只带 reasons[0] 的 246 字。"""
    result = Orchestrator._verdict_to_result("VP-016", _vp016_like_verdict())

    assert len(result["actual_result"]) > len(_SATISFIED_REASONS[0])
    assert result["actual_result"] == "\n".join(
        [*_SATISFIED_REASONS, _FAILING_REASON]
    )


def test_actual_result_falls_back_when_no_reasons():
    """没有 reasons 时仍要给出点东西，不能是空串。"""
    result = Orchestrator._verdict_to_result(
        "VP-099", Verdict(verdict="FAILED")
    )

    assert result["actual_result"] == "verdict=FAILED"


# ---------------------------------------------------------------------------
# 2. evidence 渲染 —— 截断就声明，不许静默
# ---------------------------------------------------------------------------

def test_evidence_under_limit_is_untouched_and_unannotated():
    """未超限：原样保留，且**不含**任何截断声明。"""
    text = "判据④后半未达成：子进程读到的仍是 localhost。"

    out = _render_evidence_for_prompt(text)

    assert out == text
    assert "已截断" not in out


def test_evidence_over_limit_is_truncated_and_declares_itself():
    """超限：正文被砍到上限，且文本里必须明说被砍过、并报原长。"""
    original = "x" * (_MAX_PROMPT_EVIDENCE_CHARS + 4321)

    out = _render_evidence_for_prompt(original)

    # 声明必须在。
    assert "已截断" in out
    assert str(len(original)) in out, "截断声明必须带上原始长度"
    assert str(_MAX_PROMPT_EVIDENCE_CHARS) in out, "截断声明必须带上保留长度"

    # 长度 = 上限 + 声明文字的长度（不多不少）。
    notice = out[_MAX_PROMPT_EVIDENCE_CHARS:]
    assert len(out) == _MAX_PROMPT_EVIDENCE_CHARS + len(notice)
    assert out.startswith("x" * _MAX_PROMPT_EVIDENCE_CHARS)


def test_evidence_dict_renders_readably_not_as_python_repr():
    """子 agent 路径的 dict 形状不能走 str(dict)（那是一坨 Python repr）。"""
    evidence = {
        "details": "命令输出见下",
        "raw": ["$ pytest -q", "12 failed, 3 passed"],
    }

    out = _render_evidence_for_prompt(evidence)

    assert out == "$ pytest -q\n12 failed, 3 passed"
    assert "{" not in out and "'" not in out


def test_evidence_dict_without_raw_falls_back_to_details():
    """没有 raw 时退回 details，而不是空串或 repr。"""
    out = _render_evidence_for_prompt({"details": "只有 details"})

    assert out == "只有 details"


def test_evidence_dict_truncation_is_announced_after_render():
    """dict 形状超限时同样声明截断，且按渲染后的长度计算。"""
    raw = ["y" * 5000, "z" * 5000]  # 渲染后 10001 字 > 8000
    out = _render_evidence_for_prompt({"details": "d", "raw": raw})

    assert "已截断" in out
    assert "10001" in out
    assert len(out) <= _MAX_PROMPT_EVIDENCE_CHARS + 200


def test_vp016_7141_char_evidence_survives_the_new_limit():
    """实测 7141 字 evidence 在新上限下**不再**被砍（原 500 会砍）。"""
    evidence = "e" * 7141

    out = _render_evidence_for_prompt(evidence)

    assert out == evidence
    assert "已截断" not in out
