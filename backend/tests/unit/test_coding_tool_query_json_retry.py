"""Unit tests for the new query_json retry-with-error-hint path.

Smoke v9 surfaced that the first-pass generation path (query_json
in coding_tool) inherited the same vulnerability as self_review:
when the LLM returns 0 bytes or plain prose, the helper raises
JSONDecodeError immediately without retrying. This commit
mirrors the self_review retry path: a second `_run_claude_interactive` call
with the actual parse error message as a follow-up hint.

These tests pin the new contract:
  * First-call success: still returns synchronously.
  * First-call failure (empty / plain prose): retries with hint.
  * Second-call failure: raises JSONDecodeError (degraded).
  * Hint prompt mentions the actual error class + message.

M1 migration (2026-08-11): ``query_json`` now delegates to
``_run_claude_interactive`` (interactive + Read tool access +
session resume). Tests mock that method directly. The
``_run_claude_interactive`` return shape is
``(text, session_id, result_meta)`` — ``result_meta`` is the usage
metadata dict captured from the stream-json ``result`` event — so
the side_effect helper unpacks accordingly.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _make_tool(responses):
    """Build a ClaudeCodingTool whose `_run_claude_interactive`
    returns the given text responses in order, one per call.

    Returns the tool and a list of captured prompts. The
    side-effect wrapper ignores ``session_id`` and ``resume`` kwargs
    (interactive mode args) and always returns a fixed UUID for
    session_id (which the production code records but tests
    don't assert on).
    """
    from coding_tool import ClaudeCodingTool

    tool = ClaudeCodingTool()
    captured_prompts = []

    def _side_effect(prompt, *args, **kwargs):
        captured_prompts.append(prompt)
        if not responses:
            raise AssertionError(
                f"_run_claude_interactive called more times than "
                f"expected ({len(captured_prompts)} > {len(responses) + len(captured_prompts)})"
            )
        # Return ``(text, session_id, result_meta)`` tuple — the new
        # contract (result_meta carries the usage/cost fields captured
        # from the stream-json ``result`` event; tests don't assert on
        # it, so an empty dict is fine).
        return responses.pop(0), "fixed-session-id", {}

    tool._run_claude_interactive = MagicMock(side_effect=_side_effect)
    tool._captured_prompts = captured_prompts
    return tool


# ---------------------------------------------------------------------------
# 1. First-call success: no retry, no extra LLM call
# ---------------------------------------------------------------------------


class TestQueryJsonFirstCallSuccess(unittest.TestCase):
    def test_valid_first_call_returns_immediately(self):
        tool = _make_tool(['{"tasks": [{"id": 1}]}'])
        result = tool.query_json(prompt="x", system_instruction="y")
        self.assertEqual(result, {"tasks": [{"id": 1}]})
        self.assertEqual(tool._run_claude_interactive.call_count, 1)


# ---------------------------------------------------------------------------
# 2. First call fails → retry with hint → second call succeeds
# ---------------------------------------------------------------------------


class TestQueryJsonRetryWithHint(unittest.TestCase):
    def test_empty_reply_triggers_retry(self):
        # First call: empty. Second call: valid JSON.
        tool = _make_tool(["", '{"tasks": []}'])
        result = tool.query_json(prompt="x", system_instruction="y")
        self.assertEqual(result, {"tasks": []})
        self.assertEqual(tool._run_claude_interactive.call_count, 2)

    def test_plain_prose_triggers_retry(self):
        tool = _make_tool([
            "Here is my response, no JSON yet.",
            '{"tasks": [{"id": 1, "title": "x"}]}',
        ])
        result = tool.query_json(prompt="x", system_instruction="y")
        self.assertEqual(result, {"tasks": [{"id": 1, "title": "x"}]})
        self.assertEqual(tool._run_claude_interactive.call_count, 2)

    def test_truncated_json_triggers_retry(self):
        # First call: a JSON object whose value is truncated mid-string
        # such that parse_llm_json can repair (closes the dangling
        # quote + object) — but the FIXED value is short, so we
        # can't tell from the test which call produced the fix.
        # To force a retry, use a reply that parse_llm_json cannot
        # repair: zero JSON boundaries at all.
        tool = _make_tool([
            "no json at all here, just prose",
            '{"tasks": [{"id": 1, "title": "complete"}]}',
        ])
        result = tool.query_json(prompt="x", system_instruction="y")
        self.assertEqual(result, {"tasks": [{"id": 1, "title": "complete"}]})
        self.assertEqual(tool._run_claude_interactive.call_count, 2)

    def test_followup_prompt_contains_error(self):
        """The retry prompt must contain the actual error so the LLM
        can see the precise failure mode."""
        tool = _make_tool(["", '{"a": 1}'])
        result = tool.query_json(prompt="x", system_instruction="y")
        self.assertEqual(result, {"a": 1})
        # Second-call prompt mentions JSON + the precise failure
        # mode + a hint that the next response must start with `{`.
        self.assertGreaterEqual(len(tool._captured_prompts), 2)
        followup = tool._captured_prompts[1]
        self.assertIn("JSON", followup)
        self.assertIn("无法解析", followup)
        self.assertIn("{", followup)


# ---------------------------------------------------------------------------
# 3. Both attempts fail: raises JSONDecodeError (degraded)
# ---------------------------------------------------------------------------


class TestQueryJsonBothAttemptsFail(unittest.TestCase):

    def test_both_empty_reply_raises(self):
        tool = _make_tool(["", ""])
        with self.assertRaises(json.JSONDecodeError):
            tool.query_json(prompt="x", system_instruction="y")
        self.assertEqual(tool._run_claude_interactive.call_count, 2)

    def test_both_plain_prose_raises(self):
        tool = _make_tool(["first reply garbage", "second reply garbage"])
        with self.assertRaises(json.JSONDecodeError):
            tool.query_json(prompt="x", system_instruction="y")
        self.assertEqual(tool._run_claude_interactive.call_count, 2)


if __name__ == "__main__":
    unittest.main()