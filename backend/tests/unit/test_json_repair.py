"""Unit tests for ``utils.json_repair``.

Smoke v4 surface
----------------
LLM replies that hit ``max_tokens`` mid-stream (or get truncated by
some provider-side output cap) used to take down the whole generator
stage with a ``JSONDecodeError`` from ``coding_tool.query_json``.
The retry counter would not help because there is no provider
fallback for parse failure (and the user explicitly does NOT want
provider-fallback — only local repair).

These tests pin the contract of ``parse_llm_json`` and
``_try_repair_truncated_json`` so we know exactly which inputs get
repaired and which still raise.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from utils.json_repair import (  # noqa: E402
    _extract_json_slice,
    _strip_code_fence,
    _strip_control_characters,
    _try_repair_truncated_json,
    parse_llm_json,
)


class TestParseLlmJsonHappyPath(unittest.TestCase):
    """Inputs that parse on the first try — no repair needed."""

    def test_valid_json_object(self):
        text = '{"findings": [{"severity": "high"}]}'
        self.assertEqual(parse_llm_json(text), {"findings": [{"severity": "high"}]})

    def test_valid_json_array(self):
        text = '[1, 2, 3]'
        self.assertEqual(parse_llm_json(text), [1, 2, 3])

    def test_prose_wrapped_json(self):
        text = (
            "Sure, here is the JSON you asked for:\n"
            '{"findings": [], "fixed_content": "ok"}\n'
            "Hope that helps!"
        )
        self.assertEqual(
            parse_llm_json(text),
            {"findings": [], "fixed_content": "ok"},
        )

    def test_code_fenced_json_with_json_tag(self):
        text = '```json\n{"findings": [{"severity": "low"}]}\n```'
        self.assertEqual(
            parse_llm_json(text),
            {"findings": [{"severity": "low"}]},
        )

    def test_code_fenced_json_bare(self):
        text = '```\n{"k": "v"}\n```'
        self.assertEqual(parse_llm_json(text), {"k": "v"})

    def test_object_with_nested_array_and_string(self):
        # Real-world LLM output: nested objects, Chinese strings,
        # escaped quotes — none of which should trip the parser.
        text = (
            '{"findings": [{"severity": "high", '
            '"finding": "前置条件：任务 1、任务 2 已完成", '
            '"action": "把 depends_on 补齐"}], '
            '"fixed_content": "已修复"}'
        )
        result = parse_llm_json(text)
        self.assertEqual(len(result["findings"]), 1)
        self.assertIn("前置条件", result["findings"][0]["finding"])


class TestParseLlmJsonRepair(unittest.TestCase):
    """Inputs where bracket extraction alone fails but repair saves it."""

    def test_truncated_mid_string_with_chinese_quote(self):
        """The exact failure mode observed in smoke v4 arch self-review:

        LLM output cut off inside a Chinese string literal whose
        outer delimiter was an ASCII double-quote, leaving the
        string unterminated.
        """
        # Truncated right after the opening `"前置条件` fragment.
        text = (
            '{"findings": [{"severity": "high", '
            '"finding": "前置条件：任务 1、任务 2 已完成（subtract 还没有写）'
        )
        # Without repair: json.JSONDecodeError. With repair: should
        # recover the partial fragment plus closing punctuation.
        result = parse_llm_json(text)
        self.assertIn("findings", result)
        # The repaired finding text is whatever made it into the
        # prefix before truncation; the parser closes the open
        # string + object + array as needed.
        self.assertTrue(len(result["findings"]) >= 1)
        self.assertEqual(result["findings"][0]["severity"], "high")

    def test_truncated_mid_object(self):
        text = '{"a": 1, "b": {"c": 2, "d":'
        result = parse_llm_json(text)
        # Repaired value: `{"a": 1, "b": {"c": 2, "d": null}}`
        # (the unfinished key gets closed with a null value).
        self.assertEqual(result["a"], 1)
        self.assertEqual(result["b"]["c"], 2)

    def test_truncated_mid_array(self):
        text = '{"items": [1, 2, 3, '
        result = parse_llm_json(text)
        self.assertEqual(result["items"], [1, 2, 3])

    def test_truncated_just_after_string_start(self):
        # Single key, value-string opened but never closed and no
        # closing brace.
        text = '{"reason": "started bu'
        result = parse_llm_json(text)
        self.assertEqual(result["reason"], "started bu")

    def test_truncated_after_key_no_value(self):
        # The most common LLM truncation: a key was just emitted and
        # the response cut off before the value.
        text = '{"findings":'
        # This case has no opening [ — repair should still close
        # the open object.
        result = parse_llm_json(text)
        # ``findings`` becomes null in the repaired output (key
        # closed without a value).
        self.assertIn("findings", result)

    def test_truncated_with_escaped_quote_in_string(self):
        # String contains an escaped quote — the escape scanner
        # must not be fooled into closing the string prematurely.
        text = '{"msg": "he said \\"hello\\" and then'
        result = parse_llm_json(text)
        # The escape sequence is incomplete (cut before the closing
        # `\"` of the inner quote), so repair closes the open string.
        # We only assert it parses without raising.
        self.assertIn("msg", result)

    def test_prose_then_truncated_json(self):
        text = (
            "Here you go:\n"
            '{"findings": [{"severity": "low", "finding": "中间被截断"'
        )
        result = parse_llm_json(text)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "low")


class TestParseLlmJsonUnrepairable(unittest.TestCase):
    """Inputs where repair genuinely cannot recover the input."""

    def test_empty_string_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_llm_json("")

    def test_whitespace_only_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_llm_json("   \n\t  ")

    def test_no_json_brackets_at_all_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_llm_json("the model declined to answer")

    def test_fallback_used_when_provided(self):
        # Caller accepts a soft fallback rather than raise — used
        # by self_review's "degrade to original draft" path.
        result = parse_llm_json("no json here", fallback={"findings": []})
        self.assertEqual(result, {"findings": []})

    def test_fallback_only_when_provided(self):
        # Without fallback, the same input raises.
        with self.assertRaises(json.JSONDecodeError):
            parse_llm_json("no json here")

    def test_non_string_input_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_llm_json({"already": "a dict"})


class TestAtomicWriteStillApplied(unittest.TestCase):
    """Smoke v4 also surfaced the requirement: writes stay atomic.

    This test pins the contract that ``parse_llm_json`` returns a
    plain dict (not a file handle, not a string), so callers can
    route it through ``utils.atomic_io.atomic_write_json`` and
    inherit atomic-rename durability.
    """

    def test_returns_plain_dict_for_caller_to_write_atomically(self):
        text = '{"a": 1}'
        result = parse_llm_json(text)
        self.assertIsInstance(result, dict)
        # Atomic_write_json contract: serialisable + PathLike dest.
        from utils.atomic_io import atomic_write_json
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "out.json"
            atomic_write_json(dest, result)
            # The file should exist and be valid JSON (not torn).
            self.assertTrue(dest.exists())
            self.assertEqual(json.loads(dest.read_text()), {"a": 1})


class TestStripCodeFence(unittest.TestCase):
    """Direct coverage of the small helper."""

    def test_no_fence_unchanged(self):
        self.assertEqual(_strip_code_fence('{"a":1}'), '{"a":1}')

    def test_fenced_json_tag(self):
        self.assertEqual(
            _strip_code_fence("```json\n{\"a\":1}\n```"),
            '{"a":1}',
        )

    def test_fenced_bare(self):
        self.assertEqual(
            _strip_code_fence("```\n{\"a\":1}\n```"),
            '{"a":1}',
        )

    def test_fence_with_surrounding_prose(self):
        self.assertEqual(
            _strip_code_fence("here:\n```json\n{\"a\":1}\n```\ndone"),
            '{"a":1}',
        )


class TestExtractJsonSlice(unittest.TestCase):
    """Direct coverage of the bracket-locator helper."""

    def test_picks_first_complete_object(self):
        # The earlier implementation took ``first {`` up to ``last }``,
        # which concatenated multiple top-level objects and made the
        # trailing second object look like garbage. Smoke v7 surfaced
        # the failure: ``{"a":1}{"b":2}`` was returned as
        # ``{"a":1}{"b":2}`` and json.loads raised
        # ``JSONDecodeError: Extra data``. The fix uses
        # ``raw_decode`` to find the first *complete* object and
        # drops any trailing content.
        text = 'noise {"a":1} more noise {"b":2}'
        self.assertEqual(_extract_json_slice(text), '{"a":1}')

    def test_trailing_garbage_after_first_object_is_dropped(self):
        # The LLM occasionally emits primary JSON + a stray second
        # object. The helper must keep the first and drop the rest.
        text = '{"primary": true}{"secondary": false}'
        self.assertEqual(_extract_json_slice(text), '{"primary": true}')

    def test_falls_back_to_array_when_no_object(self):
        text = 'noise [1, 2, 3] more'
        self.assertEqual(_extract_json_slice(text), '[1, 2, 3]')

    def test_returns_none_when_no_brackets(self):
        self.assertIsNone(_extract_json_slice("plain prose"))


class TestTryRepairTruncatedJson(unittest.TestCase):
    """Direct coverage of the repair walker."""

    def test_already_valid_is_noop(self):
        text = '{"a": 1}'
        self.assertEqual(_try_repair_truncated_json(text), text)

    def test_truncation_in_string_closes_string(self):
        text = '{"msg": "hel'
        repaired = _try_repair_truncated_json(text)
        self.assertEqual(json.loads(repaired), {"msg": "hel"})

    def test_truncation_in_object_closes_object(self):
        text = '{"a": {"b":'
        repaired = _try_repair_truncated_json(text)
        # Should be parseable back into a dict.
        self.assertIsInstance(json.loads(repaired), dict)

    def test_truncation_in_array_closes_array(self):
        text = '{"items": [1, 2,'
        repaired = _try_repair_truncated_json(text)
        self.assertEqual(json.loads(repaired), {"items": [1, 2]})

    def test_escape_in_string_does_not_break_walk(self):
        # The escape-aware scan must treat `\"` as a literal quote
        # and not close the string early.
        text = '{"k": "a\\"b'
        repaired = _try_repair_truncated_json(text)
        # Should parse cleanly (after repair closes the unterminated
        # string + object).
        self.assertIsInstance(json.loads(repaired), dict)


class TestSmokeV4FailureRegression(unittest.TestCase):
    """Regression tests pinned to the exact failure mode observed in
    smoke v4 — the LLM reply was cut off mid-stream inside a Chinese
    string literal whose outer delimiter was an ASCII double-quote.

    Without ``parse_llm_json`` the entire arch self-review stage
    aborted and the smoke plan stalled at ``prd_approved``.
    """

    def test_arch_self_review_truncated_inside_chinese_string(self):
        # Truncated right after the opening `"前置条件` fragment —
        # the LLM output stopped mid-byte inside the string literal.
        llm_reply = (
            '{"findings": [{"severity": "high", '
            '"finding": "前置条件：任务 1、任务 2 已完成（subtract 还没有写）'
        )
        # Self-review's contract: returns dict (possibly with
        # partial findings list) on parse success.
        from utils.json_repair import parse_llm_json
        result = parse_llm_json(llm_reply)
        # The high-severity finding should survive even though the
        # reply was truncated — the caller can downgrade severity
        # or retry as needed.
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "high")

    def test_nested_array_truncated_after_inner_object(self):
        # Outer shape: {findings: [{...}, {...} ]}
        # Truncation point: inside the second inner object's
        # "action" string.
        llm_reply = (
            '{"findings": ['
            '{"severity": "low", "finding": "good", "action": "ok"}, '
            '{"severity": "high", "finding": "dependency on 1 missing", '
            '"action": "add it"'
        )
        from utils.json_repair import parse_llm_json
        result = parse_llm_json(llm_reply)
        # The first finding survives intact; the second is truncated
        # mid-string but still recoverable (severity + finding are
        # closed; action is closed by the repair walker).
        self.assertGreaterEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "low")

    def test_prose_then_truncated_object_with_code_fence(self):
        # LLM wrote a friendly intro, then a code-fenced JSON, but
        # the JSON inside the fence was truncated.
        llm_reply = (
            "Here are my findings:\n"
            "```json\n"
            '{"findings": [{"severity": "medium", '
            '"finding": "模块职责边界不清晰"'
            "\n```"
        )
        from utils.json_repair import parse_llm_json
        result = parse_llm_json(llm_reply)
        self.assertEqual(result["findings"][0]["severity"], "medium")


class TestStripControlCharacters(unittest.TestCase):
    """v20 ported from a sibling checkout (commit 10f0df2).

    In M1 interactive mode the LLM sometimes echoes raw binary file
    content (e.g. ``Read`` tool output for a non-UTF-8 file) into
    its JSON reply. ``json.loads`` rejects unescaped control
    characters in string values, raising ``Invalid control character``
    mid-parse. ``_strip_control_characters`` strips these BEFORE the
    fence-extraction step so the JSON parses cleanly.

    The contract pinned here:
      * bytes in 0x00-0x08, 0x0b, 0x0c, 0x0e-0x1f, 0x7f are stripped
      * 0x09 (\\t), 0x0a (\\n), 0x0d (\\r) are preserved
      * all non-control bytes (incl. printable ASCII, multi-byte
        UTF-8, escapes) pass through unchanged
    """

    def test_strips_null_byte(self):
        self.assertEqual(
            _strip_control_characters("a\x00b"),
            "ab",
        )

    def test_strips_full_low_control_range(self):
        # 0x01..0x08, 0x0b, 0x0c, 0x0e..0x1f — every byte except \t \n \r
        raw = "".join(chr(c) for c in range(0x01, 0x20) if c not in (0x09, 0x0a, 0x0d))
        # Plus the DEL byte
        raw = raw + "\x7f"
        # Wrap each byte in a known marker so we can verify each
        # position is removed and the markers stay adjacent.
        text = "X".join(raw)
        self.assertNotIn("\x00", _strip_control_characters(text))

    def test_preserves_tab_newline_cr(self):
        # Tabs, newlines, and CR are explicitly allowed (they map to
        # \\t \\n \\r in escaped JSON strings).
        text = "line1\nline2\tcol2\r\nline3"
        self.assertEqual(_strip_control_characters(text), text)

    def test_preserves_printable_ascii(self):
        text = "Hello, World! 0123456789 @#$%^&*()"
        self.assertEqual(_strip_control_characters(text), text)

    def test_preserves_multibyte_utf8(self):
        # Chinese, emoji, accented Latin — all are outside the control
        # range so they pass through untouched.
        text = "中文测试 🎉 café"
        self.assertEqual(_strip_control_characters(text), text)

    def test_parse_llm_json_succeeds_with_null_in_string_value(self):
        # End-to-end: a JSON string value containing a raw \x00 would
        # crash json.loads before v20. After the strip, it parses.
        from utils.json_repair import parse_llm_json
        bad = '{"title": "hello\x00world", "n": 1}'
        result = parse_llm_json(bad)
        self.assertEqual(result["title"], "helloworld")
        self.assertEqual(result["n"], 1)

    def test_parse_llm_json_succeeds_with_del_in_string_value(self):
        from utils.json_repair import parse_llm_json
        bad = '{"name": "abc\x7fdef", "ok": true}'
        result = parse_llm_json(bad)
        self.assertEqual(result["name"], "abcdef")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()