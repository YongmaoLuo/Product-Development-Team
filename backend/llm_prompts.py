"""Shared LLM prompt tails for structured-JSON outputs.

Why this module exists
----------------------
The four generator system prompts (prd_generator, arch_generator,
test_design_generator, tasks_generator) all ask the LLM to emit a
single JSON object as the entire reply. Two prompt-shape details
have outsized impact on whether the LLM actually does that:

  1. **Where the JSON instruction lives.** Empirically, the LLM
     weights the LAST instruction it sees more heavily than the
     first. Putting the JSON instruction inside the system prompt
     (which the LLM reads first) is much weaker than putting it as
     the final line of the user prompt, OR — better — appending it
     to the system prompt tail so it is the very last thing the
     LLM sees.

  2. **How explicit the JSON contract is.** LLM providers
     (Anthropic / Vendor C / Vendor A / OpenAI) have non-trivial
     baseline rates of preamble ("Sure, here is the JSON..."),
     postscript ("Let me know if you need anything else"), and
     prose-only-empty replies (rare, but observed in smoke v5).
     A direct prohibition on these patterns, phrased as the
     contract the LLM is *expected* to honour, materially reduces
     the empty-reply rate and the prose-wrapping rate.

This module is therefore the SINGLE source of truth for the
"output contract" tail appended to every generator's system
prompt. Updating copy here propagates to all four generators in
one commit.

Note: the trailing line
    ``IMPORTANT: Return ONLY the JSON object requested, no markdown fencing.``
    already exists on the *user-prompt* side in
    ``coding_tool._query_json_internal``. We keep it there as
    belt-and-braces and append this richer block to the system
    prompt tail. The two layers reinforce the same contract.

Anti-patterns explicitly forbidden
----------------------------------
The contract below enumerates the failure modes we have observed
in smoke runs (v4 truncated mid-string, v5 empty reply) and
preempts them with explicit rules. New failure modes found in
future smoke runs should be added here.
"""

from __future__ import annotations


# Append this block to the system prompt of any generator that
# expects a single JSON object as the response. Keep it tight —
# the LLM is more likely to follow a short, dense contract than
# a long one.
JSON_OUTPUT_CONTRACT = """
## 输出契约（CRITICAL — 违反契约会导致整段响应被丢弃）
你的整个响应必须是且仅是一个 JSON 对象，从 `{` 字符开始到 `}` 字符结束。必须遵守以下规则：

1. **第一个字符必须是 `{`**，最后**一个字符必须是 `}`**（允许前后各 0 个空白字符）。
2. **不要**在 JSON 之前加任何开场白，例如「好的」「以下是」「我理解了」「Sure」「Here is the JSON」。
3. **不要**在 JSON 之后追加任何结束语，例如「希望对您有帮助」「Let me know if you need anything else」「如果需要可以告诉我」。
4. **不要**用 Markdown 代码围栏（如 ```json 或 ```）包裹 JSON。
5. **不要**输出任何 Markdown 标题（## 或 ###）或解释性段落。
6. **不要**写「分析」「建议」「总结」等元描述 — 直接输出 JSON 即可。
7. 如果 prompt 中要求返回 JSON 数组，同样适用以上规则：第一个字符是 `[`，最后一个是 `]`，中间元素之间用逗号分隔。
8. 如果你觉得输入信息不足（例如 prompt 里没看到 PRD、需求描述、用户故事等），**仍然必须输出合法的 tasks JSON**。基于 prompt 里你能找到的任何上下文（背景/目标/约束/已有任务模式），生成 1 个或多个最合理的 task。**严禁**输出 `{"error": "..."}` 或 `{"tasks": []}` 作为逃避——这两种形式都会被 tasks_generator 当作失败重试，浪费 LLM 调用。
""".strip()


def append_json_output_contract(system_prompt: str) -> str:
    """Return ``system_prompt`` with the JSON output contract tacked on.

    Used by all four generators as the final closing line of their
    system prompt so the LLM sees the contract as the literal
    last instruction before any user-supplied content.
    """
    if not system_prompt:
        return JSON_OUTPUT_CONTRACT
    return system_prompt.rstrip() + "\n\n" + JSON_OUTPUT_CONTRACT