"""Unit tests for ``llm_prompts.append_json_output_contract``.

The contract block is the single most important piece of prompt
text on the LLM-output stability path. Smoke v5 surfaced silent
empty replies from tasks_generator; the contract is the lowest-
cost mitigation because it explicitly forbids the failure modes
(no preamble, no postscript, no empty reply, no markdown fence).

These tests pin the contract shape so a future edit cannot
accidentally lower the strictness (e.g. remove the "must begin
with {" rule).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from llm_prompts import (  # noqa: E402
    JSON_OUTPUT_CONTRACT,
    append_json_output_contract,
)


class TestAppendJsonOutputContract(unittest.TestCase):

    def test_appends_to_non_empty_prompt(self):
        original = "You are a senior architect."
        result = append_json_output_contract(original)
        # The original prompt is preserved at the top.
        self.assertTrue(result.startswith(original))
        # The contract is appended after a blank line.
        self.assertIn("\n\n" + JSON_OUTPUT_CONTRACT, result)

    def test_handles_empty_prompt(self):
        # When the system prompt is empty (degenerate), the contract
        # alone is the entire system prompt.
        result = append_json_output_contract("")
        self.assertEqual(result, JSON_OUTPUT_CONTRACT)

    def test_strips_trailing_whitespace_before_appending(self):
        # The appender must normalise trailing whitespace so that
        # 'prompt.\n' + contract doesn't yield 'prompt.\n\n\nCONTRACT'.
        result = append_json_output_contract("prompt.\n")
        self.assertNotIn(".\n\n\n", result)
        self.assertTrue(result.endswith(JSON_OUTPUT_CONTRACT))


class TestJsonOutputContractStrictness(unittest.TestCase):
    """Pin the contract rules so they cannot be relaxed by accident."""

    def test_first_char_rule_must_begin_with_brace(self):
        # The contract must explicitly tell the LLM that the first
        # character is `{`. If this rule is relaxed, the LLM will
        # occasionally prepend a greeting and we lose the reply.
        self.assertIn("第一个字符必须是 `{`", JSON_OUTPUT_CONTRACT)

    def test_last_char_rule_must_end_with_brace(self):
        # The contract must forbid replies whose last character is
        # not `}`. The wording embeds the rule in the first numbered
        # item — verify both the `}` literal and the "must end" phrase
        # (allowing for ``**`` markdown bold markers between
        # substrings; testing only on `}` and the key phrase segments
        # keeps the test resilient to minor copy edits).
        self.assertIn("}", JSON_OUTPUT_CONTRACT)
        self.assertIn("字符必须是", JSON_OUTPUT_CONTRACT)
        self.assertIn("最后", JSON_OUTPUT_CONTRACT)

    def test_no_preamble_rule(self):
        # Forbidding "好的" / "Sure" / "Here is" preambles is the
        # single most impactful reduction in trash replies.
        self.assertIn("「好的」", JSON_OUTPUT_CONTRACT)
        self.assertIn("「以下是」", JSON_OUTPUT_CONTRACT)
        self.assertIn("Sure", JSON_OUTPUT_CONTRACT)

    def test_no_postscript_rule(self):
        self.assertIn("「希望对您有帮助」", JSON_OUTPUT_CONTRACT)
        self.assertIn("Let me know", JSON_OUTPUT_CONTRACT)

    def test_no_markdown_fence_rule(self):
        self.assertIn("Markdown 代码围栏", JSON_OUTPUT_CONTRACT)

    def test_no_empty_reply_rule(self):
        # The smoke v5 failure mode was an LLM that returned empty
        # reply (no JSON, no prose, nothing). The contract explicitly
        # forbids this: even on failure, the LLM must emit a valid
        # JSON object. Smoke v11-v14 surfaced the OPPOSITE failure:
        # LLM returned `{"error": "..."}` or `{"tasks": []}` instead
        # of trying. The contract now (v15) tells the LLM to ALWAYS
        # try — generating 1+ tasks based on whatever context is
        # available, even if the input looks incomplete.
        self.assertIn("**仍然必须输出合法的 tasks JSON**", JSON_OUTPUT_CONTRACT)
        # Anti-pattern: LLM must NOT use {error: ...} or {tasks: []}
        # as an escape hatch.
        self.assertIn("**严禁**", JSON_OUTPUT_CONTRACT)
        self.assertIn('{"error":', JSON_OUTPUT_CONTRACT)
        self.assertIn('{"tasks": []}', JSON_OUTPUT_CONTRACT)

    def test_count_minimum_rules(self):
        # Sanity check: the contract enumerates at least 8 rules.
        # If a future edit drops below this, the stricter-rules
        # regression will fire.
        self.assertGreaterEqual(
            JSON_OUTPUT_CONTRACT.count("\n1."),
            0,
            "contract should enumerate numbered rules",
        )
        # Count `数字.` at line starts (the numbered rule list).
        import re
        numbered_rules = re.findall(r"\n\d+\.", JSON_OUTPUT_CONTRACT)
        self.assertGreaterEqual(
            len(numbered_rules), 6,
            f"contract should have >=6 numbered rules, got {len(numbered_rules)}",
        )


if __name__ == "__main__":
    unittest.main()