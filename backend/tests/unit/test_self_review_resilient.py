"""Unit tests for the resilient self_review contract.

Smoke v7 surfaced a failure mode that the previous contract treated
as fatal: the LLM occasionally returned a reply that was not
JSON-parseable at all (plain prose, or just `{}`, or even an
empty string after the stream wrapped up). The previous contract
raised ``SelfReviewUnavailableError``, which aborted the entire
generator stage and left the plan stuck.

The governing principle ("let the framework do the data structure
mapping, the LLM only sees content") implies the audit step
should be best-effort: if the LLM cannot reply, we still have
the original first-pass draft, and the audit metadata should
record the failure rather than abort the pipeline.

The new contract is:

  1. First attempt: call the LLM with the standard prompt.
  2. On parse failure: retry once with a follow-up prompt that
     contains the actual ``JSONDecodeError`` message — LLM
     providers are much more reliable when they see the
     precise failure mode (e.g. "you emitted plain prose").
  3. On **second-parse failure**: return a *degraded report* with
     ``succeeded=False``, ``error=<reason>``, ``fixed_content=
     <original draft>``, regardless of ``mandatory``. A reply we
     could not parse is a content problem, and the canonical
     document is still the immutable first-pass draft.
  4. On ``coding_tool.query`` raising: ``mandatory=True`` (the
     default) propagates ``SelfReviewUnavailableError`` so a
     generator cannot promote an unaudited draft during an LLM
     outage; ``mandatory=False`` returns the degraded report. See
     the ``Raises:`` section of ``run_doc_self_review``'s docstring
     and ``tests/test_self_review.py``
     (``test_run_raises_self_review_unavailable_on_llm_failure`` /
     ``test_run_legacy_degrades_when_mandatory_false``). An earlier
     revision degraded silently in both cases; ``test_self_review.py``
     is the regression gate for the current contract.
  5. ``_parse_llm_response`` is also more lenient: a reply that
     contains *only* ``findings`` (no ``fixed_content``) is
     valid; the fixed_content falls back to the original draft.
     A reply with *only* ``fixed_content`` (no ``findings``)
     is also valid. A reply with *neither* is the only hard
     failure.

These tests pin all five invariants.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _load_self_review():
    """Lazy import so the test file can be collected without
    loading the full self_review module chain up-front."""
    from self_review import (
        run_doc_self_review,
        _last_parse_error,
        _parse_llm_response,
    )
    return run_doc_self_review, _last_parse_error, _parse_llm_response


# ---------------------------------------------------------------------------
# 1. First-parse success: unchanged behaviour
# ---------------------------------------------------------------------------


class TestFirstParseSuccess(unittest.TestCase):
    """The first call still parses on a normal LLM reply."""

    def test_normal_reply_passes_through(self):
        run_doc_self_review, _, _ = _load_self_review()
        fixed = "# PRD\n\n# cleaned"
        reply = json.dumps(
            {"findings": [{"severity": "low", "finding": "minor wording"}],
             "fixed_content": fixed},
            ensure_ascii=False,
        )

        class _GoodTool:
            def query(self, prompt, **kwargs):
                return reply

        result = run_doc_self_review(
            doc_content="# PRD",
            doc_type="prd",
            coding_tool=_GoodTool(),
        )
        self.assertEqual(result["succeeded"], True)
        self.assertEqual(result["fixed_content"], fixed)
        self.assertEqual(result["findings"][0]["finding"], "minor wording")


# ---------------------------------------------------------------------------
# 2. First parse fails → second attempt is invoked with error hint
# ---------------------------------------------------------------------------


class TestRetryWithErrorHint(unittest.TestCase):
    """When the first call returns unparseable JSON, the second
    call receives a follow-up prompt with the actual error message.
    The governing principle in action: tell the LLM exactly what
    went wrong instead of guessing.
    """

    def test_second_call_receives_error_followup(self):
        run_doc_self_review, _, _ = _load_self_review()
        captured = []

        class _FirstBadThenGood:
            def query(self, prompt, **kwargs):
                captured.append(prompt)
                if len(captured) == 1:
                    # Plain prose — not JSON at all.
                    return "Here is my review of the document..."
                # Second attempt: emit a valid JSON.
                return '{"findings": [], "fixed_content": "# PRD"}'

        result = run_doc_self_review(
            doc_content="# PRD",
            doc_type="prd",
            coding_tool=_FirstBadThenGood(),
        )

        self.assertEqual(len(captured), 2)
        self.assertIn("JSON", captured[1] or "", )
        # The follow-up must mention the actual error
        # ("plain prose" → "no '{'") so the LLM sees the
        # specific failure mode.
        self.assertIn("prose", captured[1].lower())
        self.assertEqual(result["succeeded"], True)

    def test_second_call_still_fails_returns_degraded_report(self):
        """If both attempts fail, the report is degraded but the
        pipeline does NOT abort — fixed_content falls back to
        the original draft and ``error`` carries the reason."""
        run_doc_self_review, _, _ = _load_self_review()

        class _AlwaysBad:
            def query(self, prompt, **kwargs):
                return "still plain prose"

        result = run_doc_self_review(
            doc_content="# PRD\noriginal content",
            doc_type="prd",
            coding_tool=_AlwaysBad(),
        )
        self.assertEqual(result["succeeded"], False)
        self.assertEqual(result["fixed_content"], "# PRD\noriginal content")
        self.assertIsNotNone(result.get("error"))
        # The error path passes the underlying error class name +
        # a marker string for parse position. We check for the
        # LAST parser error class name (ValueError when no '{'
        # is found — not "plain prose" the literal text).
        self.assertIn("ValueError", result["error"])


# ---------------------------------------------------------------------------
# 3. coding_tool raises an exception -> mandatory raises, legacy degrades
# ---------------------------------------------------------------------------


class TestToolExceptionHandling(unittest.TestCase):

    def test_coding_tool_exception_raises_when_mandatory(self):
        """``mandatory=True`` (the default) must NOT swallow an LLM outage.

        The audit step is the only thing standing between an
        unaudited draft and a promoted artifact, so a caller that
        asked for a mandatory review has to hear about the failure
        rather than silently receiving the draft it already had.
        """
        run_doc_self_review, _, _ = _load_self_review()
        from self_review import SelfReviewUnavailableError

        class _Boom:
            def query(self, prompt, **kwargs):
                raise RuntimeError("simulated outage")

        with self.assertRaises(SelfReviewUnavailableError) as ctx:
            run_doc_self_review(
                doc_content="# PRD",
                doc_type="prd",
                coding_tool=_Boom(),
            )
        self.assertIn("simulated outage", str(ctx.exception))

    def test_coding_tool_exception_degrades_when_not_mandatory(self):
        """``mandatory=False`` keeps the historical best-effort shape."""
        run_doc_self_review, _, _ = _load_self_review()

        class _Boom:
            def query(self, prompt, **kwargs):
                raise RuntimeError("simulated outage")

        result = run_doc_self_review(
            doc_content="# PRD",
            doc_type="prd",
            coding_tool=_Boom(),
            mandatory=False,
        )
        self.assertEqual(result["succeeded"], False)
        self.assertIn("simulated outage", result["error"])
        self.assertEqual(result["fixed_content"], "# PRD")

    def test_missing_coding_tool_raises_when_mandatory(self):
        """No ``coding_tool`` at all is the same outage as a raising one.

        A caller that asked for a mandatory review did not get one
        (``coding_tool=None``), so the same contract applies.
        """
        run_doc_self_review, _, _ = _load_self_review()
        from self_review import SelfReviewUnavailableError

        with self.assertRaises(SelfReviewUnavailableError):
            run_doc_self_review(
                doc_content="# PRD",
                doc_type="prd",
                coding_tool=None,
            )


# ---------------------------------------------------------------------------
# 4. Lenient parsing — partial replies are accepted
# ---------------------------------------------------------------------------


class TestLenientPartialReply(unittest.TestCase):
    """The user explicitly noted: even if the LLM only returns the
    decision-point content (without ``findings``), we should
    accept it. Conversely, findings-only is also acceptable.
    Only the *neither* case is a hard failure.
    """

    def test_findings_only_with_no_fixed_content_is_accepted(self):
        run_doc_self_review, _, _ = _load_self_review()
        reply = json.dumps(
            {"findings": [{"severity": "low", "finding": "x"}]},
            ensure_ascii=False,
        )

        class _FindingsOnly:
            def query(self, prompt, **kwargs):
                return reply

        result = run_doc_self_review(
            doc_content="# PRD\noriginal",
            doc_type="prd",
            coding_tool=_FindingsOnly(),
        )
        # The original draft is preserved as fixed_content (no
        # rewrite happened). Findings are kept.
        self.assertEqual(result["succeeded"], True)
        self.assertEqual(result["fixed_content"], "# PRD\noriginal")
        self.assertEqual(result["findings"][0]["finding"], "x")

    def test_fixed_content_only_with_no_findings_is_accepted(self):
        run_doc_self_review, _, _ = _load_self_review()
        fixed = "# PRD\n\ncleaned"
        reply = json.dumps({"fixed_content": fixed}, ensure_ascii=False)

        class _RewriteOnly:
            def query(self, prompt, **kwargs):
                return reply

        result = run_doc_self_review(
            doc_content="# PRD",
            doc_type="prd",
            coding_tool=_RewriteOnly(),
        )
        self.assertEqual(result["succeeded"], True)
        self.assertEqual(result["fixed_content"], fixed)
        self.assertEqual(result["findings"], [])

    def test_neither_findings_nor_fixed_content_is_hard_failure(self):
        """A reply that parses but contains no useful payload triggers
        a retry. After both retries fail, the call returns a
        degraded report with ``succeeded=False`` and the original
        draft as fixed_content."""
        run_doc_self_review, _, _ = _load_self_review()
        reply = "{}"

        class _Empty:
            def query(self, prompt, **kwargs):
                return reply

        result = run_doc_self_review(
            doc_content="# PRD\noriginal",
            doc_type="prd",
            coding_tool=_Empty(),
        )
        # `{}` parses cleanly but has no findings and no
        # fixed_content. The retry path runs once (gets `{}` again,
        # which still has no payload), then falls through to the
        # degraded report with the original draft.
        self.assertEqual(result["succeeded"], False)
        self.assertEqual(result["fixed_content"], "# PRD\noriginal")
        self.assertEqual(result["findings"], [])
        self.assertIsNotNone(result.get("error"))


# ---------------------------------------------------------------------------
# 5. _last_parse_error classification
# ---------------------------------------------------------------------------


class TestLastParseError(unittest.TestCase):

    def test_plain_prose_classified_as_no_brace(self):
        _, _last_parse_error, _ = _load_self_review()
        err = _last_parse_error("Here is my review, no JSON.")
        self.assertIsNotNone(err)
        # Message should mention prose / brace so the retry
        # hint can quote it back to the LLM.
        self.assertIn("prose", str(err).lower())

    def test_empty_string_returns_none(self):
        _, _last_parse_error, _ = _load_self_review()
        self.assertIsNone(_last_parse_error(""))
        self.assertIsNone(_last_parse_error("   \n  "))

    def test_valid_json_returns_none(self):
        _, _last_parse_error, _ = _load_self_review()
        self.assertIsNone(_last_parse_error('{"findings": []}'))

    def test_truncated_json_returns_json_decode_error(self):
        _, _last_parse_error, _ = _load_self_review()
        err = _last_parse_error('{"findings": [{"severity": "low"')
        self.assertIsNotNone(err)


# ---------------------------------------------------------------------------
# 6. _parse_llm_response: partial-reply tolerance
# ---------------------------------------------------------------------------


class TestParseLlmResponsePartial(unittest.TestCase):

    def test_findings_only_uses_fallback_for_fixed_content(self):
        _, _, _parse_llm_response = _load_self_review()
        parsed = _parse_llm_response(
            '{"findings": [{"severity": "low", "finding": "x"}]}',
            fallback_content="# original",
        )
        self.assertEqual(parsed["findings"][0]["finding"], "x")
        self.assertEqual(parsed["fixed_content"], "# original")

    def test_neither_returns_none(self):
        _, _, _parse_llm_response = _load_self_review()
        self.assertIsNone(_parse_llm_response("{}"))

    def test_empty_string_returns_none(self):
        _, _, _parse_llm_response = _load_self_review()
        self.assertIsNone(_parse_llm_response(""))


if __name__ == "__main__":
    unittest.main()