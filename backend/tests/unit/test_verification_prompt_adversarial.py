"""2026-10-21: 对抗性要求并入第一遍判定提示词。

背景
----
删除判定之后的逐 VP 补审（per-VP supplement review）之后，对抗性
要求不能跟着消失 —— 它被并入第一遍
``VerificationSubAgent._build_prompt``：

  1. 计划里的 ``related_prd_criteria`` 现在会真正进入 sub-agent 提示词
     （此前该字段从未传给 LLM）；
  2. 提示词里新增一段 ``ADVERSARIAL SELF-CHECK``，要求 sub-agent 在
     下判定前写下至少两条反假设并逐条用证据证伪。

这些测试是**纯字符串**断言：不调 LLM、不碰文件系统，只有
``_build_prompt`` 一个纯函数（``vp_node`` 是 dict，``attempt`` 是 int）。
"""

import sys
from pathlib import Path

import pytest


# Ensure ``backend/`` is on ``sys.path`` so ``import verification_subagent``
# works regardless of the test runner entry point.  Mirrors the pattern
# used by ``test_verification_subagent_verdict.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_subagent import VerificationSubAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def code_review_subagent() -> VerificationSubAgent:
    """A sub-agent configured the same way the executor constructs one."""
    return VerificationSubAgent(method="code_review")


@pytest.fixture
def vp_node() -> dict:
    """Minimal verification-point node carrying a PRD criterion."""
    return {
        "id": "VP-001",
        "title": "t",
        "verification_method": "code_review",
        "priority": "high",
        "expected_result": "e",
        "related_prd_criteria": "PRD-XYZ",
        "evidence_artifact_dir": "/tmp/x",
    }


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


def test_prompt_carries_prd_criterion(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """The PRD criterion the plan recorded must reach the sub-agent LLM."""
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "PRD criterion: PRD-XYZ" in prompt


def test_prompt_demands_adversarial_self_check(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """The adversarial self-check replaced the deleted second pass.

    If this assertion ever fails, the adversarial requirement silently
    disappeared along with the per-VP supplement review it replaced.
    """
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "ADVERSARIAL SELF-CHECK" in prompt
    assert "counter-hypotheses" in prompt


def test_prompt_omits_prd_line_when_criterion_absent(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """A VP without ``related_prd_criteria`` must not raise or emit the line."""
    vp_node.pop("related_prd_criteria")
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "PRD criterion:" not in prompt
    # The adversarial section is unconditional — it does not depend on
    # whether the plan recorded a PRD criterion.
    assert "ADVERSARIAL SELF-CHECK" in prompt


def test_prompt_carries_process_safety_hard_rule(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """The prompt must forbid pattern-based process killing.

    2026-10-27: a verification sub-agent ran ``pkill -f "backend/cli.py"``
    and SIGTERMed the repair executors of three unrelated plans — every
    PDT executor on the host has that path in its argv. The prompt is one
    of the two defenses; the other is the ``pkill_killall_block``
    PreToolUse hook (see ``test_verification_process_safety_guard.py``).

    If this assertion ever fails, the sub-agent is being handed no
    instruction about how to stop a process — and the default it falls
    back on is the one that killed three plans.
    """
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "PROCESS SAFETY" in prompt
    assert "killall" in prompt
    assert "pkill" in prompt
