"""Framework-shared inline prompt constants.

Why this module exists
----------------------
A handful of LLM prompt strings are consumed by **more than one**
framework component (e.g. ``agent.Agent`` and
``verification_agent.VerificationAgent`` both run an "inline spec /
code review" LLM call against the same envelope). Until recently each
component defined its own copy of the template, or one component
reached sideways into the other component's module namespace to
import it. Both shapes create coupling problems:

* **Duplicate copies** drift. The pre-2026-09-04 status quo had two
  copies of the spec/code-review prompt — one in ``agent.py`` (used
  by ``agent._inline_spec_code_review``) and one in
  ``verification_agent.py`` (used by
  ``verification_agent._supplement_spec_code_review``). They were
  supposed to be identical but were prone to drift.

* **Sideways imports** form hidden cycles. The previous mitigation
  was a lazy ``from agent import INLINE_SPEC_CODE_REVIEW_PROMPT``
  inside ``verification_agent``. The docstring admitted it was
  "to avoid a hard coupling", but the cycle was still real: any
  future refactor that imported ``verification_agent`` before
  ``agent`` would hit an ``ImportError``. Tests had to remember to
  load ``agent`` first to register the symbol.

The fix is the canonical pattern this ``framework/`` package is
designed for: hoist the shared text into a leaf module with **zero
dependencies on the rest of the codebase**. Both ``agent`` and
``verification_agent`` now import from here. The cycle is broken
because ``framework.prompts`` depends on neither.

Contents
--------
* :data:`INLINE_SPEC_CODE_REVIEW_PROMPT` — the spec/code-review
  prompt used by both ``agent._inline_spec_code_review`` (pre-commit
  hook) and ``verification_agent._supplement_spec_code_review``
  (DP3 action item 2). Format placeholders are
  ``{task_desc}`` and ``{git_diff_stat}``.

Adding new shared prompts
-------------------------
If you find yourself reaching sideways into another module's
namespace for a prompt string, add the string here instead and have
both callers import it. This module MUST stay free of any
non-stdlib import so the import-cost stays negligible (the entire
point of breaking the cycle) — the contract is enforced by the
``test_prompts_module_only_uses_stdlib`` test.
"""

from __future__ import annotations

__all__ = ["INLINE_SPEC_CODE_REVIEW_PROMPT"]


#: Inline spec / code review prompt (DP3).
#:
#: Used by both ``agent._inline_spec_code_review`` (pre-commit
#: adversarial self-check) and
#: ``verification_agent._supplement_spec_code_review`` (DP3 action
#: item 2: judgment-phase per-VP supplement review). The two call
#: sites format it with the same two kwargs::
#:
#:     INLINE_SPEC_CODE_REVIEW_PROMPT.format(
#:         task_desc=...,
#:         git_diff_stat=...,
#:     )
#:
#: The LLM is asked to emit a single JSON object with envelope
#: ``{spec_compliance, code_quality, should_block, reason}`` — both
#: call sites parse that exact shape.
#:
#: Kept at module scope (rather than as an f-string buried in a
#: method body) so the template is:
#:
#:   1. Greppable as a single string — easier to audit and version
#:      than a multi-line f-string.
#:   2. Re-usable across components without re-importing each other.
#:   3. Introspectable by tests / tooling without rerunning the
#:      agent or the verifier.
INLINE_SPEC_CODE_REVIEW_PROMPT = (
    "你是一名严格的代码审校员。下面是一个任务的实现，"
    "请对照任务说明做一次对抗性自检（adversarial review），"
    "检查实现是否真的满足了任务说明的全部要求。\n\n"
    "任务说明（spec）：\n{task_desc}\n\n"
    "代码变更摘要（git diff --stat）：\n{git_diff_stat}\n\n"
    "请按以下 JSON 格式输出审查结论（不要输出任何 JSON 以外的内容）：\n"
    "{{\n"
    '  "spec_compliance": "high" | "medium" | "low",\n'
    '  "code_quality":    "high" | "medium" | "low",\n'
    '  "should_block":    true | false,\n'
    '  "reason":          "<简短中文说明，<=200 字>"\n'
    "}}\n\n"
    "判定规则：\n"
    "1. spec_compliance 衡量实现是否完成了任务说明的全部要求；\n"
    "2. code_quality 衡量实现是否有明显质量问题（硬编码 / 死代码 / "
    "明显可改进 / 错误处理缺失等）；\n"
    "3. 仅当 spec_compliance == 'high' 或 code_quality == 'high' "
    "（即存在严重偏离）时才允许 should_block = true；\n"
    "4. 当 spec_compliance != 'high' 且 code_quality != 'high' "
    "时 should_block 必须为 false；\n"
    "5. reason 字段请用中文简要说明判定依据。"
)